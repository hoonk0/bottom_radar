"""Freshness decay for price-based indicators (Drawdown, SMA200).

A drawdown that's been at -18% for 3 months without making a new low is much
weaker information than a drawdown that JUST hit -18% today. We measure this
by "days since the recent low" within a freshness window (default 60 trading
days) and decay the sub-score accordingly.
"""
from __future__ import annotations

import pandas as pd


FRESHNESS_WINDOW = 60  # trading days


def days_since_lowest_close(closes: pd.Series, window: int = FRESHNESS_WINDOW) -> int:
    """How many trading days ago was the lowest close within the trailing window?
    0 = today is the lowest, 5 = 5 trading days ago was the lowest."""
    if len(closes) == 0:
        return 0
    tail = closes.iloc[-window:] if len(closes) >= window else closes
    min_pos = int(tail.values.argmin())
    return len(tail) - 1 - min_pos


def days_since_max_divergence(
    closes: pd.Series,
    sma_period: int = 200,
    window: int = FRESHNESS_WINDOW,
) -> int:
    """How many trading days ago was SPY most below its SMA200 within the window?"""
    if len(closes) < 2:
        return 0
    sma = closes.rolling(sma_period, min_periods=1).mean()
    div = closes / sma - 1.0
    tail = div.iloc[-window:] if len(div) >= window else div
    min_pos = int(tail.values.argmin())  # most-negative divergence
    return len(tail) - 1 - min_pos


def freshness_decay(base_score: int, days_since_low: int) -> tuple[int, str]:
    """Apply decay to a base score. Returns (new_score, note).

    - days <= 3:  full score (fresh)
    - days <= 10: -1 (slight stale)
    - days > 10:  -2 (very stale, sustained bear)
    """
    if base_score == 0:
        return 0, ""
    if days_since_low <= 3:
        return base_score, "fresh"
    if days_since_low <= 10:
        new_score = max(0, base_score - 1)
        return new_score, f"stale {days_since_low}d"
    new_score = max(0, base_score - 2)
    return new_score, f"stale {days_since_low}d"
