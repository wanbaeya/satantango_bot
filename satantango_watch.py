#!/usr/bin/env python3
"""
사탄탱고 예매 오픈 감시 봇
--------------------------------
targets.txt에 적어둔 예매/시간표 페이지들을 주기적으로 긁어서, '사탄탱고' 회차가
새로 뜨면 텔레그램(+ 맥 알림 센터)으로 즉시 푸시한다.

실행:
    pip install requests
    export TG_TOKEN="123456:AA..."      # BotFather에서 발급
    export TG_CHAT="123456789"          # 아래 '설정' 참고
    python3 satantango_watch.py                 # 계속 감시
    python3 satantango_watch.py --once          # 1회 체크 (cron/GH Actions용)
    python3 satantango_watch.py --interval 45   # 폴링 주기(초), 기본 60

주의: 이 스크립트는 '새 회차가 시간표에 등록/예매 오픈되는 것'을 잡는다.
      이미 매진된 회차의 취소표(빈자리)는 좌석 API를 따로 파싱해야 하므로
      기본 동작에 포함되지 않는다.
"""

import argparse
import hashlib
import json
import os
import platform
import random
import re
import subprocess
import sys
import time
from datetime import datetime

import requests

# ─────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────

KEYWORDS = ["사탄탱고", "사탄 탱고", "Satantango", "Sátántangó"]

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# ── 감시 대상 ───────────────────────────────────────────────
# 극장 주소는 옆에 있는 targets.txt 파일에서 읽어온다.
# targets.txt 한 줄 형식:   극장이름 | 주소
# 예:                      라이카시네마 | https://laika.co.kr/schedule
TARGETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "targets.txt")


def load_targets():
    out = []
    try:
        lines = open(TARGETS_FILE, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        log("targets.txt 파일이 없음. 극장 주소를 넣은 targets.txt를 만들어야 함.")
        return out
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if "|" not in ln:
            log(f"형식이 이상한 줄은 건너뜀 → {ln}")
            continue
        name, url = [x.strip() for x in ln.split("|", 1)]
        if not url.startswith("http"):
            log(f"주소가 http로 시작하지 않아 건너뜀 → {name}")
            continue
        out.append({"name": name, "url": url, "method": "GET"})
    return out


# ─────────────────────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────────────────────

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
TIME_RE = re.compile(r"\b([01]?\d|2[0-9]|3[01]):([0-5]\d)\b")
DATE_RE = re.compile(r"(20\d{2})[-./]?(\d{2})[-./]?(\d{2})")


def log(msg):
    print(f"[{datetime.now():%m-%d %H:%M:%S}] {msg}", flush=True)


def to_text(raw):
    """HTML/JSON 응답을 검색 가능한 평문으로."""
    txt = TAG_RE.sub(" ", raw)
    txt = txt.replace("&nbsp;", " ")
    if re.search(r"\\u[0-9a-fA-F]{4}", txt):  # JSON 안의 \uXXXX 한글 복원
        txt = re.sub(r"\\u([0-9a-fA-F]{4})",
                     lambda m: chr(int(m.group(1), 16)), txt)
    return WS_RE.sub(" ", txt)


def extract_hits(text):
    """'사탄탱고' 주변 문맥을 잘라내고, 회차 지문(fingerprint)을 만든다."""
    hits = []
    for kw in KEYWORDS:
        for m in re.finditer(re.escape(kw), text, re.IGNORECASE):
            lo = max(0, m.start() - 250)
            hi = min(len(text), m.end() + 250)
            ctx = text[lo:hi].strip()
            times = sorted(set(f"{h.zfill(2)}:{mnt}" for h, mnt in TIME_RE.findall(ctx)))
            dates = sorted(set("".join(d) for d in DATE_RE.findall(ctx)))
            if times or dates:
                for t in (times or ["--:--"]):
                    hits.append({"key": f"{dates[0] if dates else '?'}|{t}", "ctx": ctx})
            else:
                # 시간 정보가 없어도 '영화가 페이지에 등장한 것' 자체를 신호로
                hits.append({"key": hashlib.md5(ctx.encode()).hexdigest()[:10], "ctx": ctx})
    return hits


def fetch(target, session):
    method = target.get("method", "GET").upper()
    headers = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"}
    headers.update(target.get("headers", {}))
    kwargs = {"headers": headers, "timeout": 15}
    if method == "POST":
        if "json" in target:
            kwargs["json"] = target["json"]
        elif "data" in target:
            kwargs["data"] = target["data"]
    r = session.request(method, target["url"], **kwargs)
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"
    return r.text


# ─────────────────────────────────────────────────────────────
# 알림
# ─────────────────────────────────────────────────────────────

def notify(title, body, url=None):
    line = f"🎟️ {title}\n{body}"
    if url:
        line += f"\n{url}"
    log("ALERT → " + line.replace("\n", " | "))

    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": line, "disable_web_page_preview": False},
                timeout=10,
            )
        except Exception as e:
            log(f"  텔레그램 전송 실패: {e}")

    if platform.system() == "Darwin":
        try:
            safe = body.replace('"', "'")[:200]
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{safe}" with title "사탄탱고 예매 오픈" sound name "Glass"'],
                check=False,
            )
        except Exception:
            pass

    print("\a", end="", flush=True)  # 터미널 비프


# ─────────────────────────────────────────────────────────────
# 상태
# ─────────────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# ─────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────

def check_all(targets, state, session, warm):
    """warm=True면 첫 실행 → 기존 회차는 기록만 하고 알림 안 보냄."""
    for t in targets:
        name = t["name"]
        try:
            raw = fetch(t, session)
        except Exception as e:
            kind = type(e).__name__
            if "Resolution" in str(e) or "NameResolution" in kind:
                why = "주소를 찾을 수 없음 — targets.txt의 주소에 오타가 있는지 확인"
            elif "403" in str(e) or "401" in str(e):
                why = "극장 사이트가 접근을 막음 — 다른 시간표 페이지 주소로 바꿔볼 것"
            elif "404" in str(e):
                why = "그런 페이지가 없음 — 주소를 다시 복사해올 것"
            elif "Timeout" in kind or "timeout" in str(e).lower():
                why = "응답이 너무 느림 — 다음 번에 다시 시도됨"
            else:
                why = f"연결 실패 ({kind})"
            log(f"{name}: ❌ {why}")
            continue

        text = to_text(raw)
        hits = extract_hits(text)
        seen = set(state.get(name, []))

        if not hits:
            hint = " (페이지는 열렸지만 시간표가 안 보임 — 주소가 맞는지 확인)" if len(text) < 800 else ""
            log(f"{name}: 아직 사탄탱고 회차 없음{hint}")
            state[name] = sorted(seen)
            continue

        new = [h for h in hits if h["key"] not in seen]
        for h in new:
            seen.add(h["key"])
            if not warm:
                notify(name, h["ctx"][:350], t["url"])

        state[name] = sorted(seen)
        log(f"{name}: 회차 {len(hits)}건 (신규 {len(new)}건){' [최초 기록]' if warm else ''}")

        time.sleep(random.uniform(1.5, 3.5))  # 사이트별 간격 (차단 방지)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="1회만 체크하고 종료")
    ap.add_argument("--interval", type=int, default=60, help="폴링 주기(초)")
    ap.add_argument("--duration", type=int, default=0,
                    help="이 시간(초) 동안만 돌고 스스로 종료. 0이면 무한")
    ap.add_argument("--test", action="store_true", help="알림 테스트")
    args = ap.parse_args()

    if args.test:
        notify("테스트", "알림 경로가 살아있다.", "https://example.com")
        return

    if not (TG_TOKEN and TG_CHAT):
        log("경고: TG_TOKEN / TG_CHAT 미설정 → 콘솔·맥 알림만 동작")

    targets = load_targets()
    if not targets:
        log("감시할 극장이 하나도 없음 → targets.txt 확인 필요")
    else:
        log(f"감시 대상 {len(targets)}곳: " + ", ".join(t["name"] for t in targets))

    state = load_state()
    warm = not state  # 첫 실행이면 기준선만 잡는다
    session = requests.Session()

    started = time.time()
    if args.duration:
        log(f"{args.duration // 60}분 동안 {args.interval}초 간격으로 감시함")

    cycle = 0
    while True:
        cycle += 1
        check_all(targets, state, session, warm)
        save_state(state)
        warm = False

        if args.once:
            break

        elapsed = time.time() - started
        if args.duration and elapsed + args.interval > args.duration:
            log(f"예정된 시간이 끝나 종료함 (총 {cycle}회 확인). 곧 다음 작업이 이어받음.")
            break

        time.sleep(args.interval + random.uniform(0, 5))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("종료")
        sys.exit(0)
