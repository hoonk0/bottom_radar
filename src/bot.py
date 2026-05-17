"""Telegram interactive bot — long-polls Telegram and answers query commands.

Commands:
  /help                     — list commands
  /status                   — today's 5 indicator scores + total
  /year YYYY [YYYY ...]     — strong events in given year(s) + 1y forward SPY return
  /indicator NAME           — historical strong events for one indicator
                              (vix | rsi | drawdown | fear_greed | aaii)
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
    fetch_all,
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

`/help` — 이 메시지
`/status` — 오늘 5지표 현재 점수
`/year 2022` — 연도별 강력 발화 + 1년 후 수익률
`/year 2020 2022 2025` — 여러 연도
`/indicator vix` — 지표 전체 역사 강력 발화
   사용 가능: vix · rsi · drawdown · fear\\_greed · aaii
`/about` — 봇 소개

매일 07:00 KST 자동 알람은 cron 으로 별도 실행됩니다."""


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
        send(token, chat_id, "🤖 _bottom\\_radar bot 가동_\n`/help` 로 명령어 확인", "Markdown")
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
