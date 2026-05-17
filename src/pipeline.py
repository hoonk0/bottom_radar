"""End-to-end pipeline: fetch all indicators, score them, return TotalScore + snapshots.

Used by both the live runner (`main.py`) and the backtest script.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from src.fetchers.aaii import fetch_aaii
from src.fetchers.fear_greed import fetch_fear_greed
from src.fetchers.prices import fetch_qqq, fetch_spy, fetch_vix
from src.indicators.drawdown import trailing_drawdown
from src.indicators.freshness import days_since_lowest_close
from src.indicators.rsi import latest_rsi
from src.indicators.score import (
    SubScore,
    TotalScore,
    combine,
    score_aaii_bearish,
    score_drawdown,
    score_fear_greed,
    score_rsi,
    score_vix,
)
from src.logger import get_logger
from src.telegram import Snapshot

logger = get_logger()


@dataclass
class PipelineResult:
    total: TotalScore
    snapshots: List[Snapshot]


def _error_sub(key: str, label: str, err: Exception) -> SubScore:
    return SubScore(
        key=key,
        label=label,
        value=None,
        score=0,
        level="normal",
        display="N/A",
        error=str(err)[:120],
    )


def run_pipeline(end: Optional[datetime] = None) -> PipelineResult:
    """Run all fetchers + scoring. Each fetcher is isolated by try/except so one
    failure does not block the others. `end` controls backtest mode."""

    subscores: List[SubScore] = []
    snapshots: List[Snapshot] = []

    # 1) SPY: feeds RSI, drawdown, and a snapshot.
    spy_series = None
    try:
        spy_series = fetch_spy(end=end)
        snapshots.append(Snapshot("SPY", spy_series.last_close, spy_series.daily_change_pct))

        try:
            rsi_val = latest_rsi(spy_series.closes)
            subscores.append(score_rsi(rsi_val))
        except Exception as e:
            logger.exception("rsi computation failed")
            subscores.append(_error_sub("rsi", "SPY RSI(14)", e))

        try:
            dd = trailing_drawdown(spy_series.closes, window=252)
            dsl = days_since_lowest_close(spy_series.closes)
            subscores.append(score_drawdown(dd, days_since_low=dsl))
        except Exception as e:
            logger.exception("drawdown computation failed")
            subscores.append(_error_sub("drawdown", "SPY 1y Drawdown", e))
    except Exception as e:
        logger.exception("SPY fetch failed")
        subscores.append(_error_sub("rsi", "SPY RSI(14)", e))
        subscores.append(_error_sub("drawdown", "SPY 1y Drawdown", e))

    # 2) VIX
    try:
        vix = fetch_vix(end=end)
        subscores.insert(0, score_vix(vix.last_close))
    except Exception as e:
        logger.exception("VIX fetch failed")
        subscores.insert(0, _error_sub("vix", "VIX", e))

    # 3) CNN F&G
    try:
        fg = fetch_fear_greed(end=end)
        subscores.append(score_fear_greed(fg.score))
    except Exception as e:
        logger.exception("F&G fetch failed")
        subscores.append(_error_sub("fear_greed", "CNN F&G", e))

    # 4) AAII Bearish
    try:
        aaii = fetch_aaii(end=end)
        subscores.append(score_aaii_bearish(aaii.bearish))
    except Exception as e:
        logger.exception("AAII fetch failed")
        subscores.append(_error_sub("aaii", "AAII Bearish", e))

    # 5) QQQ snapshot (no scoring contribution).
    try:
        qqq = fetch_qqq(end=end)
        snapshots.append(Snapshot("QQQ", qqq.last_close, qqq.daily_change_pct))
    except Exception:
        logger.exception("QQQ fetch failed (non-fatal)")

    total = combine(subscores)
    return PipelineResult(total=total, snapshots=snapshots)
