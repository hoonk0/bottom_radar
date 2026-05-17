"""Persistent state for de-duplicating alerts across runs."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import numpy as np


@dataclass
class HistoryEntry:
    date: str       # YYYY-MM-DD
    score: int
    level: str
    notified: bool


@dataclass
class State:
    last_run: Optional[str] = None       # ISO8601 UTC
    last_sent_level: Optional[str] = None
    last_sent_date: Optional[str] = None  # YYYY-MM-DD
    last_score: Optional[int] = None
    history: List[HistoryEntry] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        hist = [HistoryEntry(**h) for h in data.get("history", [])]
        return cls(
            last_run=data.get("last_run"),
            last_sent_level=data.get("last_sent_level"),
            last_sent_date=data.get("last_sent_date"),
            last_score=data.get("last_score"),
            history=hist,
        )

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n")


# Level rank for upgrade detection (5-tier: Notice < Watch since Watch contains a +3
# strong sub-score = stronger single signal even at the same total).
LEVEL_RANK = {"normal": 0, "notice": 1, "watch": 2, "alert": 3, "strong": 4}


def _trading_days_between(start_date: datetime, end_date: datetime) -> int:
    """Number of NYSE-ish business days between two datetimes (excluding weekends).
    Uses Mon-Fri as a proxy — close enough for cooldown logic (holidays add at most 1-2)."""
    return int(np.busday_count(start_date.date(), end_date.date()))


def should_notify(
    state: State,
    current_level: str,
    has_strong_sub: bool,
    today: Optional[datetime] = None,
    cooldown_trading_days: int = 7,
) -> tuple[bool, str]:
    """Apply de-dupe rules. Returns (should_send, reason).

    Cooldown is measured in TRADING DAYS (Mon-Fri), so 7 trading days ≈ 9-10 calendar
    days. This matches market-time semantics — after a Mon alert, next eligible is the
    following Wednesday."""
    today = today or datetime.now(timezone.utc)
    today_str = today.strftime("%Y-%m-%d")

    # Notice/Watch/Alert/Strong all fire (subject to cooldown). Only Normal is silent.
    if current_level == "normal":
        return False, "level=normal"
    last_level = state.last_sent_level
    last_date = state.last_sent_date

    # First-ever send.
    if last_level is None or last_date is None:
        return True, "first-ever notify"

    current_rank = LEVEL_RANK[current_level]
    last_rank = LEVEL_RANK[last_level]

    if current_rank > last_rank:
        return True, f"level upgraded {last_level}→{current_level}"

    # Same or lower level: apply trading-day cooldown.
    try:
        last_dt = datetime.strptime(last_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return True, "could not parse last_sent_date"

    td = _trading_days_between(last_dt, today)
    if td >= cooldown_trading_days:
        if has_strong_sub:
            return True, f"cooldown expired ({cooldown_trading_days}td) — strong sub-score"
        return True, f"cooldown expired ({cooldown_trading_days}td)"

    return False, (
        f"cooldown active (last={last_level} {td}td ago, "
        f"need ≥{cooldown_trading_days}td)"
    )


def update_state(
    state: State,
    *,
    today: datetime,
    score: int,
    level: str,
    notified: bool,
    max_history: int = 90,
) -> State:
    today_str = today.strftime("%Y-%m-%d")
    state.last_run = today.isoformat()
    state.last_score = score

    if notified:
        state.last_sent_level = level
        state.last_sent_date = today_str

    state.history.append(HistoryEntry(date=today_str, score=score, level=level, notified=notified))
    if len(state.history) > max_history:
        state.history = state.history[-max_history:]

    return state
