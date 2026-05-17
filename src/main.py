"""Entry point — run once per cron tick."""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src.logger import get_logger
from src.pipeline import run_pipeline
from src.state import State, should_notify, update_state
from src.telegram import format_message, send_error, send_telegram


REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / "state.json"
KST = ZoneInfo("Asia/Seoul")

logger = get_logger()


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    error_chat_id = os.environ.get("TELEGRAM_ERROR_CHAT_ID", chat_id)
    dry_run = os.environ.get("DRY_RUN") == "1"
    today_utc = datetime.now(timezone.utc)
    today_kst = today_utc.astimezone(KST)

    try:
        result = run_pipeline()
    except Exception as e:
        logger.exception("pipeline crashed")
        send_error(token, error_chat_id, f"pipeline crash: {e}\n{traceback.format_exc()[:500]}")
        return 1

    total = result.total
    logger.info(
        "score=%s level=%s strong_sub=%s ok=%d failed=%s",
        total.total, total.level, total.has_strong_sub,
        total.successful_fetches, total.failed_fetches,
    )
    for s in total.subscores:
        logger.info("  %s: value=%s score=%d level=%s err=%s",
                    s.key, s.display, s.score, s.level, s.error)

    # All fetchers failed → error alert.
    if total.successful_fetches == 0:
        logger.error("all fetchers failed")
        send_error(token, error_chat_id,
                   f"All data sources failed: {', '.join(total.failed_fetches)}")
        return 2

    state = State.load(STATE_PATH)
    notify, reason = should_notify(state, total.level, total.has_strong_sub, today=today_utc)
    logger.info("notify decision: %s — %s", notify, reason)

    if total.notify and notify:
        message = format_message(total, today_kst, snapshots=result.snapshots)
        logger.info("--- message preview ---\n%s\n-----------------------", message)
        if dry_run:
            logger.info("DRY_RUN=1 set — skipping telegram send")
        elif not token or not chat_id:
            logger.warning("missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — skipping send")
        else:
            try:
                send_telegram(token, chat_id, message)
                logger.info("telegram message sent")
            except Exception as e:
                logger.exception("telegram send failed")
                send_error(token, error_chat_id, f"telegram send failed: {e}")

        update_state(state, today=today_utc, score=total.total, level=total.level, notified=True)
    else:
        update_state(state, today=today_utc, score=total.total, level=total.level, notified=False)

    state.save(STATE_PATH)
    logger.info("state saved to %s", STATE_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
