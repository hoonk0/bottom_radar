"""Wilder's RSI(14) — standard reference implementation."""
from __future__ import annotations

import pandas as pd


def wilder_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """Return RSI series with the same index as `closes` (first `period` values are NaN)."""
    if len(closes) < period + 1:
        raise ValueError(f"Need at least {period + 1} closes, got {len(closes)}")

    delta = closes.astype(float).diff()
    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)

    # Wilder smoothing: first value is simple average over `period`, subsequent values
    # use (prev * (period-1) + current) / period.
    avg_gain = gains.copy() * 0.0
    avg_loss = losses.copy() * 0.0
    avg_gain.iloc[:period] = float("nan")
    avg_loss.iloc[:period] = float("nan")

    first_gain = gains.iloc[1 : period + 1].mean()
    first_loss = losses.iloc[1 : period + 1].mean()
    avg_gain.iloc[period] = first_gain
    avg_loss.iloc[period] = first_loss

    for i in range(period + 1, len(closes)):
        avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gains.iloc[i]) / period
        avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + losses.iloc[i]) / period

    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # If avg_loss is 0 (no losses) RSI is 100
    rsi = rsi.where(avg_loss != 0, 100.0)
    return rsi


def latest_rsi(closes: pd.Series, period: int = 14) -> float:
    return float(wilder_rsi(closes, period).iloc[-1])
