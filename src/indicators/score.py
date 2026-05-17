"""Scoring rules: convert raw indicator values into 0..3 sub-scores and combine."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from src.indicators.freshness import freshness_decay


# Indicator-level and total-level shared label space.
#   For sub-scores (per-indicator): NORMAL/WATCH/ALERT/STRONG correspond to +0/+1/+2/+3.
#   For total scores: NOTICE/WATCH/ALERT/STRONG are the four sending tiers; NOTICE is
#   the new "below Watch" tier used when total ∈ [3,5] but no individual indicator is
#   at +3. WATCH is reserved for "total ∈ [3,5] with at least one +3 sub-score" so the
#   visual contrast cleanly distinguishes single-indicator extremes from broad mildness.
NORMAL = "normal"
NOTICE = "notice"
WATCH = "watch"
ALERT = "alert"
STRONG = "strong"

LEVEL_EMOJI = {
    NORMAL: "⚪",
    NOTICE: "🟢",
    WATCH: "🟡",
    ALERT: "🟠",
    STRONG: "🔴",
}

LEVEL_LABEL_KO = {
    NORMAL: "정상",
    NOTICE: "사전경보",
    WATCH: "주의",
    ALERT: "경계",
    STRONG: "강력",
}


@dataclass
class SubScore:
    key: str                # e.g. "vix"
    label: str              # e.g. "VIX"
    value: Optional[float]  # raw value (None if fetch failed)
    score: int              # 0..3
    level: str              # NORMAL/WATCH/ALERT/STRONG
    display: str            # formatted value for the message
    error: Optional[str] = None  # populated if fetch failed


def _score_levels(value: float, thresholds: List[tuple[float, int]], higher_is_worse: bool) -> tuple[int, str]:
    """`thresholds` is a sorted list of (threshold, score) from mildest to most extreme.
    If higher_is_worse=True, value >= threshold qualifies.
    If higher_is_worse=False, value <= threshold qualifies."""
    score = 0
    for threshold, s in thresholds:
        if higher_is_worse:
            if value >= threshold:
                score = s
        else:
            if value <= threshold:
                score = s
    level = {0: NORMAL, 1: WATCH, 2: ALERT, 3: STRONG}[score]
    return score, level


def score_vix(value: float) -> SubScore:
    score, level = _score_levels(
        value,
        thresholds=[(22.0, 1), (28.0, 2), (35.0, 3)],
        higher_is_worse=True,
    )
    return SubScore("vix", "VIX", value, score, level, f"{value:.2f}")


def score_rsi(value: float) -> SubScore:
    score, level = _score_levels(
        value,
        thresholds=[(35.0, 1), (30.0, 2), (25.0, 3)],
        higher_is_worse=False,
    )
    return SubScore("rsi", "SPY RSI(14)", value, score, level, f"{value:.1f}")


def score_drawdown(value: float, days_since_low: Optional[int] = None) -> SubScore:
    """value is a fraction, e.g. -0.085 for -8.5%.

    If `days_since_low` is given, apply freshness decay: a drawdown that hasn't
    made a new low recently has weaker information value than a fresh one.
    """
    base_score, _ = _score_levels(
        value,
        thresholds=[(-0.07, 1), (-0.12, 2), (-0.18, 3)],
        higher_is_worse=False,
    )
    score, note = _apply_freshness(base_score, days_since_low)
    level = {0: NORMAL, 1: WATCH, 2: ALERT, 3: STRONG}[score]
    display = f"{value * 100:.1f}%"
    if note and note != "fresh":
        display += f" ({note})"
    return SubScore("drawdown", "SPY 1y Drawdown", value, score, level, display)


def score_fear_greed(value: float) -> SubScore:
    score, level = _score_levels(
        value,
        thresholds=[(25.0, 1), (15.0, 2), (10.0, 3)],
        higher_is_worse=False,
    )
    return SubScore("fear_greed", "CNN F&G", value, score, level, f"{value:.0f}")


def score_aaii_bearish(value: float) -> SubScore:
    """value is fraction in 0..1, e.g. 0.45 for 45%."""
    pct = value * 100.0
    score, level = _score_levels(
        pct,
        thresholds=[(40.0, 1), (45.0, 2), (50.0, 3)],
        higher_is_worse=True,
    )
    return SubScore("aaii", "AAII Bearish", value, score, level, f"{pct:.1f}%")


def _apply_freshness(base_score: int, days_since_low: Optional[int]) -> tuple[int, str]:
    if days_since_low is None:
        return base_score, ""
    return freshness_decay(base_score, days_since_low)


@dataclass
class TotalScore:
    subscores: List[SubScore]
    total: int                          # 0..18 (6 indicators × max 3)
    level: str                          # NORMAL/WATCH/ALERT/STRONG
    has_strong_sub: bool                # any individual sub at STRONG
    notify: bool                        # should we send telegram?

    successful_fetches: int = field(default=0)
    failed_fetches: List[str] = field(default_factory=list)


MAX_SCORE = 15  # 5 indicators × 3 points each


def determine_level(total: int, has_strong_sub: bool = False) -> str:
    """5-tier total-level mapping. WATCH and NOTICE share the same total range (3–5)
    but split by whether any individual indicator hit +3 strong:
      - total ≥ 10:                STRONG
      - total 6–9:                 ALERT
      - total 3–5 + strong sub:    WATCH   (a single indicator is screaming)
      - total 3–5 + no strong sub: NOTICE  (broad mild weakness — heads-up)
      - total 0–2:                 NORMAL  (a strong sub-score alone implies total ≥ 3)
    """
    if total >= 10:
        return STRONG
    if total >= 6:
        return ALERT
    if total >= 3:
        return WATCH if has_strong_sub else NOTICE
    return NORMAL


def combine(subscores: List[SubScore]) -> TotalScore:
    """Combine subscores into a TotalScore. Failed (error) subscores contribute 0."""
    total = sum(s.score for s in subscores if s.error is None)
    has_strong = any(s.level == STRONG and s.error is None for s in subscores)
    level = determine_level(total, has_strong_sub=has_strong)
    # Anything that isn't NORMAL wants to fire (subject to cooldown in should_notify).
    notify = level != NORMAL

    failed = [s.key for s in subscores if s.error is not None]
    return TotalScore(
        subscores=subscores,
        total=total,
        level=level,
        has_strong_sub=has_strong,
        notify=notify,
        successful_fetches=len(subscores) - len(failed),
        failed_fetches=failed,
    )
