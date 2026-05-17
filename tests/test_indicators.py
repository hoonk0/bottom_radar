"""Unit tests for indicators + scoring + state — all pure functions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.indicators.drawdown import trailing_drawdown
from src.indicators.freshness import (
    days_since_lowest_close,
    freshness_decay,
)
from src.indicators.rsi import latest_rsi, wilder_rsi
from src.indicators.score import (
    ALERT,
    NORMAL,
    NOTICE,
    STRONG,
    WATCH,
    combine,
    determine_level,
    score_aaii_bearish,
    score_drawdown,
    score_fear_greed,
    score_rsi,
    score_vix,
)
from src.state import State, should_notify, update_state


# ---- RSI ----

# Classic textbook example from Wilder, New Concepts in Technical Trading Systems (1978).
# Using the 14 closes from the original example, RSI should be ~70.46.
WILDER_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
    45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00,
    46.03, 46.41, 46.22, 45.64, 46.21, 46.25, 45.71, 46.45,
    45.78, 45.35, 44.03, 44.18, 44.22, 44.57, 43.42, 42.66, 43.13,
]


def test_wilder_rsi_classic_example():
    s = pd.Series(WILDER_CLOSES)
    rsi = wilder_rsi(s, 14)
    # After 14 bars we should have a valid RSI. Compare known reference (~70.46 at the 14th bar).
    val_14 = rsi.iloc[14]
    assert 69.0 < val_14 < 72.0, f"RSI(14) at bar 14 expected ~70.5, got {val_14}"


def test_rsi_all_gains_is_100():
    s = pd.Series([float(i) for i in range(1, 30)])
    assert latest_rsi(s) == pytest.approx(100.0)


def test_rsi_needs_enough_bars():
    s = pd.Series([1.0, 2.0, 3.0])
    with pytest.raises(ValueError):
        wilder_rsi(s, 14)


# ---- Drawdown ----

def test_drawdown_simple():
    # peak 100, current 85 → -15%
    closes = pd.Series([50, 60, 70, 100, 90, 85])
    assert trailing_drawdown(closes) == pytest.approx(-0.15, abs=1e-6)


def test_drawdown_at_high_is_zero():
    closes = pd.Series([90, 95, 100])
    assert trailing_drawdown(closes) == pytest.approx(0.0)


def test_drawdown_window_truncation():
    # Older peak (200) outside the window should be ignored
    closes = pd.Series([200, 50, 60, 100, 80])
    assert trailing_drawdown(closes, window=4) == pytest.approx(-0.20)


# ---- Score thresholds ----

@pytest.mark.parametrize("value,expected_score,expected_level", [
    (15.0, 0, NORMAL),
    (22.0, 1, WATCH),
    (27.9, 1, WATCH),
    (28.0, 2, ALERT),
    (34.9, 2, ALERT),
    (35.0, 3, STRONG),
    (50.0, 3, STRONG),
])
def test_score_vix(value, expected_score, expected_level):
    s = score_vix(value)
    assert s.score == expected_score
    assert s.level == expected_level


@pytest.mark.parametrize("value,expected_score", [
    (60.0, 0),
    (34.9, 1),
    (30.0, 2),
    (24.9, 3),
])
def test_score_rsi(value, expected_score):
    assert score_rsi(value).score == expected_score


@pytest.mark.parametrize("value,expected_score", [
    (-0.05, 0),
    (-0.07, 1),
    (-0.12, 2),
    (-0.20, 3),
])
def test_score_drawdown(value, expected_score):
    assert score_drawdown(value).score == expected_score


@pytest.mark.parametrize("value,expected_score", [
    (50, 0),
    (24, 1),
    (14, 2),
    (9, 3),
])
def test_score_fear_greed(value, expected_score):
    assert score_fear_greed(value).score == expected_score


@pytest.mark.parametrize("value,expected_score", [
    (0.30, 0),
    (0.41, 1),
    (0.46, 2),
    (0.52, 3),
])
def test_score_aaii(value, expected_score):
    assert score_aaii_bearish(value).score == expected_score


# ---- Freshness decay ----

def test_days_since_lowest_close_today():
    # Today is the low.
    closes = pd.Series([100, 99, 98, 97, 90])
    assert days_since_lowest_close(closes, window=10) == 0


def test_days_since_lowest_close_past():
    # Low was 4 trading days ago, today bounced back.
    closes = pd.Series([100, 90, 91, 92, 93, 94])
    # Window covers all 6; min at index 1, last at index 5 → 4 days ago.
    assert days_since_lowest_close(closes, window=10) == 4


def test_freshness_decay_fresh():
    assert freshness_decay(3, days_since_low=0) == (3, "fresh")
    assert freshness_decay(3, days_since_low=3) == (3, "fresh")


def test_freshness_decay_slight_stale():
    s, note = freshness_decay(3, days_since_low=7)
    assert s == 2
    assert "stale" in note


def test_freshness_decay_very_stale():
    s, _ = freshness_decay(3, days_since_low=40)
    assert s == 1   # 3 - 2 = 1


def test_freshness_decay_zero_stays_zero():
    assert freshness_decay(0, days_since_low=50) == (0, "")


def test_score_drawdown_with_fresh_low_keeps_full():
    s = score_drawdown(-0.20, days_since_low=0)
    assert s.score == 3
    assert s.level == STRONG


def test_score_drawdown_with_stale_low_decays():
    s = score_drawdown(-0.20, days_since_low=40)
    # base +3, very stale → +1
    assert s.score == 1
    assert "stale" in s.display


def test_score_drawdown_without_freshness_keeps_original_behavior():
    # No days_since_low parameter — behaves like the original function.
    s = score_drawdown(-0.20)
    assert s.score == 3
    assert "stale" not in s.display


# ---- Combine + level mapping ----

def test_determine_level_boundaries():
    # 5-tier mapping: 3–5 splits NOTICE vs WATCH on has_strong_sub.
    assert determine_level(0) == NORMAL
    assert determine_level(2) == NORMAL
    assert determine_level(3, has_strong_sub=False) == NOTICE
    assert determine_level(3, has_strong_sub=True) == WATCH
    assert determine_level(5, has_strong_sub=False) == NOTICE
    assert determine_level(5, has_strong_sub=True) == WATCH
    assert determine_level(6) == ALERT
    assert determine_level(9) == ALERT
    assert determine_level(10) == STRONG
    assert determine_level(15) == STRONG


def test_combine_normal_does_not_notify():
    subs = [score_vix(15), score_rsi(60), score_drawdown(-0.02),
            score_fear_greed(50), score_aaii_bearish(0.30)]
    total = combine(subs)
    assert total.total == 0
    assert total.level == NORMAL
    assert not total.notify


def test_combine_alert_notifies():
    # 2 + 2 + 1 + 1 + 0 = 6 → Alert
    subs = [score_vix(29), score_rsi(28), score_drawdown(-0.08),
            score_fear_greed(20), score_aaii_bearish(0.30)]
    total = combine(subs)
    assert total.total == 6
    assert total.level == ALERT
    assert total.notify


def test_combine_strong_sub_at_low_total_is_watch():
    # VIX strong (+3) alone with nothing else firing: total=3, has_strong_sub=True → WATCH.
    subs = [score_vix(40), score_rsi(50), score_drawdown(-0.01),
            score_fear_greed(60), score_aaii_bearish(0.30)]
    total = combine(subs)
    assert total.total == 3
    assert total.level == WATCH
    assert total.has_strong_sub
    assert total.notify


def test_combine_low_total_no_strong_sub_is_notice():
    # Three indicators at +1 → total=3, no strong sub → NOTICE.
    subs = [score_vix(23), score_rsi(33), score_drawdown(-0.08),
            score_fear_greed(50), score_aaii_bearish(0.30)]
    total = combine(subs)
    assert total.total == 3
    assert total.level == NOTICE
    assert not total.has_strong_sub
    assert total.notify   # NOTICE still notifies (subject to cooldown)


def test_combine_strong_level_at_10():
    # 3 + 3 + 2 + 1 + 1 = 10 → STRONG
    subs = [score_vix(40), score_rsi(24), score_drawdown(-0.13),
            score_fear_greed(20), score_aaii_bearish(0.41)]
    total = combine(subs)
    assert total.total == 10
    assert total.level == STRONG


def test_combine_handles_failed_subscores():
    from src.indicators.score import SubScore
    failed = SubScore(key="vix", label="VIX", value=None, score=0,
                      level=NORMAL, display="N/A", error="fetch error")
    subs = [failed, score_rsi(28), score_drawdown(-0.10),
            score_fear_greed(20), score_aaii_bearish(0.42)]
    total = combine(subs)
    assert total.failed_fetches == ["vix"]
    # 0 + 2 + 1 + 1 + 1 = 5, no strong sub → NOTICE
    assert total.total == 5
    assert total.level == NOTICE


# ---- State machine ----

def test_should_notify_first_ever():
    state = State()
    today = datetime(2026, 5, 16, tzinfo=timezone.utc)
    notify, reason = should_notify(state, ALERT, has_strong_sub=False, today=today)
    assert notify
    assert "first" in reason.lower()


def test_should_notify_skips_only_normal():
    state = State()
    today = datetime(2026, 5, 16, tzinfo=timezone.utc)
    assert should_notify(state, NORMAL, False, today=today)[0] is False


def test_should_notify_notice_fires():
    # Notice level now sends (was silent before).
    state = State()
    today = datetime(2026, 5, 16, tzinfo=timezone.utc)
    notify, _ = should_notify(state, NOTICE, has_strong_sub=False, today=today)
    assert notify


def test_should_notify_watch_with_strong_sub_still_sends():
    state = State()
    today = datetime(2026, 5, 16, tzinfo=timezone.utc)
    notify, _ = should_notify(state, WATCH, has_strong_sub=True, today=today)
    assert notify


def test_should_notify_upgrade_notice_to_watch_bypasses_cooldown():
    # Yesterday Notice fired; today Watch (level upgrade because strong sub appeared)
    # → should fire even though cooldown active.
    state = State(last_sent_level=NOTICE, last_sent_date="2026-05-15")
    today = datetime(2026, 5, 16, tzinfo=timezone.utc)
    notify, reason = should_notify(state, WATCH, has_strong_sub=True, today=today)
    assert notify
    assert "upgrad" in reason.lower()


def test_should_notify_cooldown_active_trading_days():
    # 2026-05-14 (Thu) → 2026-05-16 (Sat): only 2 trading days, cooldown still active.
    state = State(last_sent_level=ALERT, last_sent_date="2026-05-14")
    today = datetime(2026, 5, 16, tzinfo=timezone.utc)
    notify, _ = should_notify(state, ALERT, False, today=today)
    assert not notify


def test_should_notify_cooldown_expired_trading_days():
    # 2026-05-01 (Fri) → 2026-05-16 (Sat): ~10 trading days, well past 7.
    state = State(last_sent_level=ALERT, last_sent_date="2026-05-01")
    today = datetime(2026, 5, 16, tzinfo=timezone.utc)
    notify, _ = should_notify(state, ALERT, False, today=today)
    assert notify


def test_should_notify_cooldown_uses_trading_days_not_calendar():
    # Friday alert → following Friday: 5 trading days, NOT 7. Cooldown should still be active.
    state = State(last_sent_level=ALERT, last_sent_date="2026-05-08")  # Fri
    today = datetime(2026, 5, 15, tzinfo=timezone.utc)                  # Fri (7 cal days later)
    notify, reason = should_notify(state, ALERT, False, today=today)
    assert not notify
    assert "5td" in reason or "5 td" in reason or "td ago" in reason


def test_should_notify_level_upgrade_ignores_cooldown():
    state = State(last_sent_level=ALERT, last_sent_date="2026-05-15")
    today = datetime(2026, 5, 16, tzinfo=timezone.utc)
    notify, reason = should_notify(state, STRONG, has_strong_sub=True, today=today)
    assert notify
    assert "upgrad" in reason.lower()


def test_update_state_persists_notification():
    state = State()
    today = datetime(2026, 5, 16, tzinfo=timezone.utc)
    update_state(state, today=today, score=7, level=ALERT, notified=True)
    assert state.last_sent_level == ALERT
    assert state.last_sent_date == "2026-05-16"
    assert state.last_score == 7
    assert len(state.history) == 1
    assert state.history[0].notified is True


def test_update_state_does_not_overwrite_last_sent_on_non_notify():
    state = State(last_sent_level=ALERT, last_sent_date="2026-05-10")
    today = datetime(2026, 5, 16, tzinfo=timezone.utc)
    update_state(state, today=today, score=2, level=NORMAL, notified=False)
    assert state.last_sent_level == ALERT  # unchanged
    assert state.last_sent_date == "2026-05-10"
    assert state.last_score == 2
