"""Drawdown from rolling 1-year high."""
from __future__ import annotations

import pandas as pd


def trailing_drawdown(closes: pd.Series, window: int = 252) -> float:
    """Return drawdown (negative number, e.g. -0.085) of the last close vs. the rolling
    max over the last `window` bars (inclusive)."""
    if len(closes) == 0:
        raise ValueError("Empty series")
    tail = closes.iloc[-window:] if len(closes) > window else closes
    peak = float(tail.max())
    last = float(tail.iloc[-1])
    if peak == 0:
        return 0.0
    return last / peak - 1.0
