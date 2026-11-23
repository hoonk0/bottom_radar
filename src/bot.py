"""Telegram interactive bot — long-polls Telegram and answers query commands.

Commands:
  /help                     — list commands
  /status                   — today's 5 indicator scores + total
  /year YYYY [YYYY ...]     — strong events in given year(s) + 1y forward SPY return
  /indicator NAME           — historical strong events for one indicator
                              (vix | rsi | drawdown | fear_greed | aaii)
  /guide                    — what each indicator means + scoring thresholds
  /freq                     — how many days per year each indicator scores +1/+2/+3
  /about                    — short intro

Run:
  set -a && source .env && set +a
  python -m src.bot

The bot replies only to messages from TELEGRAM_CHAT_ID (yourself). All other senders
are silently ignored.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests

from src.analysis import (
    analyze_indicator,
    analyze_year,
    build_indicator_series,
    daily_scores,
    fetch_all,
    format_frequency_report,
    format_indicator_report,
    format_year_report,
    resolve_indicator,
    INDICATOR_ALIASES,
    LABEL,
)
from src.indicators.score import LEVEL_EMOJI
from src.logger import get_logger
from src.pipeline import run_pipeline

logger = get_logger("bot")
KST = ZoneInfo("Asia/Seoul")

API_BASE = "https://api.telegram.org/bot{token}"

HELP_TEXT = """🤖 *bottom_radar 명령어*

`/status` — 오늘 5지표 현재 점수
`/guide` — 지표 설명 + 점수 기준
`/freq` — 지표별 1년에 몇 일 점수가 나오는지
`/year 2022` — 연도별 강력 발화 + 1년 후 수익률
`/year 2020 2022 2025` — 여러 연도
`/indicator vix` — 지표 전체 역사 강력 발화
   사용 가능: vix · rsi · drawdown · fear\\_greed · aaii
`/about` — 봇 소개

*지표 한 줄 요약* (각 0~3점, 합산 15점 만점)
· VIX — 공포지수. 높을수록 패닉
· RSI — 단기 과매도. 낮을수록 많이 빠짐
· Drawdown — 1년 고점 대비 하락폭
· F&G — CNN 공포탐욕지수. 낮을수록 공포
· AAII — 개인투자자 약세 응답 비율

매일 07:00 KST 자동 알람은 cron 으로 별도 실행됩니다."""


GUIDE_TEXT = """📖 *지표 설명*  (각 지표 0~3점, 합산 0~15점)

*1. VIX* — 공포지수
S&P500 옵션 가격으로 계산한 향후 30일 예상 변동성. 평소 12~20, 급락장에서 치솟음.
+1: ≥22 · +2: ≥28 · +3: ≥35

*2. SPY RSI(14)* — 과매도
최근 14거래일 상승폭 대비 하락폭으로 계산한 0~100 값. 30 아래면 통상 과매도.
+1: ≤35 · +2: ≤30 · +3: ≤25

*3. SPY 1y Drawdown* — 고점 대비 하락폭
최근 1년(252거래일) 최고 종가 대비 지금 얼마나 빠졌는지.
+1: ≤-7% · +2: ≤-12% · +3: ≤-18%
신선도 감점: 최근 60거래일 최저점이 4~10일 전이면 -1, 10일보다 오래되면 -2. 오래 지속된 약세장에서 같은 신호가 계속 켜지는 것을 막음.

*4. CNN Fear & Greed* — 시장 심리
CNN이 주가 모멘텀, 신고가/신저가, 풋콜 비율, 정크본드 수요 등 7개 지표로 만든 0~100 지수. 25 미만이 '극단적 공포'.
+1: ≤25 · +2: ≤15 · +3: ≤10

*5. AAII Bearish* — 개인투자자 비관
미국개인투자자협회 주간 설문(목요일 발표)에서 "6개월 뒤 하락"이라 답한 비율. 장기 평균 약 31%.
+1: ≥40% · +2: ≥45% · +3: ≥50%

*합산 단계*
⚪ 0~2 정상 — 알람 없음
🟢 3~5 Notice — 여러 지표가 살짝 점등
🟡 3~5 Watch — +3 지표가 하나 이상
🟠 6~9 Alert — 매수 검토
🔴 10+ STRONG — 강한 바닥 신호

같은 단계는 7거래일 쿨다운, 단계가 오르면 즉시 알람.
`/freq` 로 각 점수가 1년에 며칠 나오는지 확인."""


ABOUT_TEXT = """📡 *bottom_radar*

5개 시장 바닥 지표(VIX · SPY RSI · 1y Drawdown · CNN F&G · AAII Bearish)로 합산 점수를 매겨 매수 시점을 알려주는 봇.

· 매일 07:00 KST 자동 체크
· 0~15점 5단계 (Normal·Notice·Watch·Alert·STRONG)
· 7거래일 쿨다운
· 위기 시에만 알람, 평소엔 침묵

`/help` 로 명령어 확인."""


# ------------------------ Telegram I/O ------------------------

def get_updates(token: str, offset: Optional[int], timeout: int = 30) -> dict:
    url = f"{API_BASE.format(token=token)}/getUpdates"
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(url, params=params, timeout=timeout + 10)
    r.raise_for_status()
    return r.json()


def send(token: str, chat_id: str, text: str, parse_mode: str = "Markdown") -> dict:
    url = f"{API_BASE.format(token=token)}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    r = requests.post(url, json=payload, timeout=20)
    if not r.ok:
        # Markdown parsing can fail on special chars — retry as plain text once.
        logger.warning("send failed (%s) — retrying as plain text", r.status_code)
        payload.pop("parse_mode", None)
        r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


BOT_COMMANDS = [
    ("status", "오늘 5지표 현재 점수"),
    ("guide", "지표 설명 + 점수 기준"),
    ("freq", "지표별 1년에 몇 일 점수가 나오는지"),
    ("year", "연도별 강력 발화 (예: /year 2022)"),
    ("indicator", "지표별 역사 (예: /indicator vix)"),
    ("help", "명령어 목록"),
    ("about", "봇 소개"),
]


def set_commands(token: str) -> None:
    """Register commands so they show up in Telegram's "/" menu button."""
    url = f"{API_BASE.format(token=token)}/setMyCommands"
    commands = [{"command": c, "description": d} for c, d in BOT_COMMANDS]
    r = requests.post(url, json={"commands": commands}, timeout=20)
    r.raise_for_status()


# ------------------------ Command handlers ------------------------

def cmd_status(_args) -> str:
    try:
        result = run_pipeline()
    except Exception as e:
        logger.exception("status pipeline failed")
        return f"⚠️ 파이프라인 에러: {e}"

    total = result.total
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    lines = [
        f"📊 *현재 점수* — _{now}_",
        "",
        f"*합산: {total.total} / 15  ({total.level.upper()})*",
        "",
    ]
    for s in total.subscores:
        emoji = LEVEL_EMOJI.get(s.level, "⚪")
        if s.error:
            lines.append(f"⚠️ {s.label}: 조회 실패")
        else:
            lines.append(f"{emoji} {s.label}: {s.display} (+{s.score})")

    if result.snapshots:
        lines.append("")
        for snap in result.snapshots:
            arrow = "📈" if snap.daily_change_pct >= 0 else "📉"
            lines.append(f"{arrow} {snap.ticker}: ${snap.price:.2f} ({snap.daily_change_pct:+.2f}%)")
    return "\n".join(lines)


def cmd_year(args: list[str]) -> str:
    if not args:
        return "사용법: `/year 2022` 또는 `/year 2020 2022 2025`"
    try:
        years = sorted({int(a) for a in args})
    except ValueError:
        return "⚠️ 연도는 4자리 숫자로 입력하세요. 예: `/year 2022`"

    now_year = datetime.now().year
    for y in years:
        if y < 2008 or y > now_year:
            return f"⚠️ 지원 범위 2008 ~ {now_year} 입니다 ({y} 제외됨)"

    logger.info("/year %s", years)
    data = fetch_all(min(years), max(years), log=lambda *_: None)
    series = build_indicator_series(data)

    chunks = []
    for y in years:
        reports = analyze_year(y, data, series, levels=[3])
        chunks.append(format_year_report(y, reports))
    return "\n\n".join(chunks)


def cmd_indicator(args: list[str]) -> str:
    if not args:
        valid = ", ".join(sorted(set(INDICATOR_ALIASES.values())))
        return f"사용법: `/indicator vix`\n가능: {valid}"
    key = resolve_indicator(args[0])
    if key is None:
        valid = ", ".join(sorted(set(INDICATOR_ALIASES.values())))
        return f"⚠️ 알 수 없는 지표 '{args[0]}'.\n가능: {valid}"
    logger.info("/indicator %s", key)

    end_year = datetime.now().year
    data = fetch_all(2008, end_year, log=lambda *_: None)
    series = build_indicator_series(data)
    reports = analyze_indicator(key, data, series, levels=[3])
    return format_indicator_report(key, reports, max_rows=15)


def cmd_freq(_args) -> str:
    logger.info("/freq")
    data = fetch_all(2008, datetime.now().year, log=lambda *_: None)
    return format_frequency_report(daily_scores(data))


def handle_command(text: str) -> Optional[str]:
    """Parse `/cmd args...` and return a Markdown reply (or None to ignore)."""
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return None
    # Strip optional @botname suffix (group chats include /cmd@bot_username).
    cmd = parts[0].lstrip("/").split("@", 1)[0].lower()
    args = parts[1:]

    if cmd in ("help", "start"):
        return HELP_TEXT
    if cmd == "about":
        return ABOUT_TEXT
    if cmd == "guide":
        return GUIDE_TEXT
    if cmd == "freq":
        return cmd_freq(args)
    if cmd == "status":
        return cmd_status(args)
    if cmd == "year":
        return cmd_year(args)
    if cmd == "indicator":
        return cmd_indicator(args)
    return f"⚠️ 알 수 없는 명령: `/{cmd}`\n`/help` 로 사용 가능 명령어 확인."


# ------------------------ Main loop ------------------------

def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars are required")
        return 1

    offset = None
    logger.info("bot starting — listening for commands from chat_id=%s", chat_id)
    try:
        set_commands(token)
    except Exception:
        logger.exception("setMyCommands failed (continuing)")
    try:
        send(token, chat_id, "🤖 bottom\\_radar bot 가동\n`/help` 로 명령어 확인", "Markdown")
    except Exception:
        logger.exception("startup ping failed (continuing)")

    while True:
        try:
            data = get_updates(token, offset=offset, timeout=30)
            if not data.get("ok"):
                logger.error("getUpdates not ok: %s", data)
                time.sleep(5)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message") or {}
                text = msg.get("text", "")
                sender_chat = msg.get("chat", {}).get("id")

                # Authorization: only respond to the configured chat_id.
                if str(sender_chat) != str(chat_id):
                    logger.warning("ignoring chat_id=%s text=%r", sender_chat, text)
                    continue

                if not text or not text.startswith("/"):
                    continue

                logger.info("command from owner: %s", text)
                try:
                    reply = handle_command(text)
                except Exception as e:
                    logger.exception("command crashed")
                    reply = f"⚠️ 처리 중 에러: {e}\n```\n{traceback.format_exc()[-500:]}\n```"
                if reply:
                    try:
                        send(token, chat_id, reply, "Markdown")
                    except Exception:
                        logger.exception("reply send failed")
        except requests.exceptions.RequestException as e:
            logger.warning("network error: %s — retrying in 10s", e)
            time.sleep(10)
        except KeyboardInterrupt:
            logger.info("bot stopping (KeyboardInterrupt)")
            return 0
        except Exception:
            logger.exception("loop error — retrying in 10s")
            time.sleep(10)


if __name__ == "__main__":
    sys.exit(main())
