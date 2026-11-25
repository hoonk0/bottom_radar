"""Scheduled status briefing — posts the current 5-indicator scores regardless of level.

Separate from the crisis alert (src/main.py): no cooldown, no state. Meant to run from
cron twice a day, e.g. 08:00 KST (after the US close) and 23:30 KST (just after the
US open, when VIX/F&G and SPY's last bar are live intraday values).

Targets: TELEGRAM_BRIEF_CHAT_ID, falling back to TELEGRAM_CHAT_ID (comma-separated).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from src.logger import get_logger
from src.pipeline import run_pipeline
from src.telegram import format_status, send_error, send_telegram

KST = ZoneInfo("Asia/Seoul")
logger = get_logger()

FOOTER = "_3점 이상이면 위기 알림이 따로 발송돼요. /guide 지표 설명_"


def build_title(now: datetime) -> str:
    stamp = now.strftime("%m/%d %H:%M")
    if 4 <= now.hour < 16:
        return f"🌅 *아침 지표 브리핑* — _{stamp} KST, 미국장 마감 기준_"
    return f"🌙 *밤 지표 브리핑* — _{stamp} KST, 미국장 장중 기준_"


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    raw = os.environ.get("TELEGRAM_BRIEF_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")
    chat_ids = [c.strip() for c in raw.split(",") if c.strip()]
    owner = os.environ.get("TELEGRAM_CHAT_ID", "").split(",")[0].strip()
    dry_run = os.environ.get("DRY_RUN") == "1"

    try:
        result = run_pipeline()
    except Exception as e:
        logger.exception("pipeline crashed")
        send_error(token, owner, f"briefing pipeline crash: {e}")
        return 1
    if result.total.successful_fetches == 0:
        send_error(token, owner, "briefing: all data sources failed")
        return 2

    message = format_status(build_title(datetime.now(KST)), result.total, result.snapshots)
    message += "\n\n" + FOOTER
    logger.info("--- briefing ---\n%s\n----------------", message)
    if dry_run or not token or not chat_ids:
        logger.info("dry run or missing token/chat id — not sending")
        return 0

    failed = 0
    for cid in chat_ids:
        try:
            send_telegram(token, cid, message)
        except Exception as e:
            failed += 1
            logger.exception("briefing send to %s failed", cid)
            send_error(token, owner, f"briefing send to {cid} failed: {e}")
    return 1 if failed == len(chat_ids) else 0


if __name__ == "__main__":
    sys.exit(main())
