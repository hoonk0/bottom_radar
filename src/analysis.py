"""Historical analysis helpers — used by scripts/analyze.py CLI and src/bot.py Telegram bot.

Provides:
- fetch_all() : pull SPY/VIX/F&G/AAII series covering a target year range
- find_event_starts() : dedupe consecutive condition-true days into distinct events
- forward_return_pct() : N-day forward return on a closes series
- analyze_year() / analyze_indicator() : structured event reports
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import numpy as np
import pandas as pd
import requests
import yfinance as yf

from src.fetchers.aaii import _cell_str, _download_bytes, _read_excel
from src.indicators import score as score_mod
from src.indicators.freshness import FRESHNESS_WINDOW
from src.indicators.rsi import wilder_rsi


UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# (threshold, comparison) per (indicator, level).
THRESHOLDS = {
    "vix":        {1: (22.0, "ge"),   2: (28.0, "ge"),  3: (35.0, "ge")},
    "rsi":        {1: (35.0, "lt"),   2: (30.0, "lt"),  3: (25.0, "lt")},
    "drawdown":   {1: (-0.07, "le"),  2: (-0.12, "le"), 3: (-0.18, "le")},
    "fear_greed": {1: (25.0, "lt"),   2: (15.0, "lt"),  3: (10.0, "lt")},
    "aaii":       {1: (0.40, "gt"),   2: (0.45, "gt"),  3: (0.50, "gt")},
}

LABEL = {
    "vix": "VIX",
    "rsi": "SPY RSI(14)",
    "drawdown": "SPY 1y Drawdown",
    "fear_greed": "CNN F&G",
    "aaii": "AAII Bearish",
}

FG_EARLIEST_YEAR = 2021  # oldest start date the CNN F&G API accepts

# AAII is weekly so cluster-gap is shorter.
GAP_DAYS = {"vix": 10, "rsi": 10, "drawdown": 10, "fear_greed": 10, "aaii": 4}

LEVEL_TAG = {1: "주의(+1)", 2: "경계(+2)", 3: "강력(+3)"}

INDICATOR_ALIASES = {
    "vix": "vix",
    "rsi": "rsi",
    "drawdown": "drawdown", "dd": "drawdown",
    "fear_greed": "fear_greed", "fg": "fear_greed", "feargreed": "fear_greed", "f&g": "fear_greed",
    "aaii": "aaii", "bearish": "aaii",
}


# ----- Data structures -----

@dataclass
class Event:
    indicator: str
    level: int
    date: pd.Timestamp
    value: float
    forward_return_pct: Optional[float]

    @property
    def value_display(self) -> str:
        return _format_value(self.indicator, self.value)


@dataclass
class IndicatorReport:
    indicator: str
    level: int
    events: List[Event] = field(default_factory=list)

    @property
    def returns(self) -> List[float]:
        return [e.forward_return_pct for e in self.events if e.forward_return_pct is not None]

    @property
    def average_return(self) -> Optional[float]:
        r = self.returns
        return sum(r) / len(r) if r else None

    @property
    def win_rate(self) -> Optional[float]:
        r = self.returns
        if not r:
            return None
        wins = sum(1 for x in r if x > 0)
        return wins / len(r)


# ----- Fetch -----

def _strip_tz(s: pd.Series) -> pd.Series:
    idx = s.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    out = s.copy()
    out.index = pd.DatetimeIndex(idx).normalize()
    return out


def fetch_all(start_year: int, end_year: int, *, log=print) -> dict:
    """Fetch SPY/VIX/F&G/AAII covering [start_year-1, end_year+1]. Returns dict of Series."""
    buf_start = f"{start_year - 1}-01-01"
    buf_end = f"{end_year + 2}-01-01"

    log(f"  ↳ SPY {buf_start} → {buf_end} ...")
    spy = yf.Ticker("SPY").history(start=buf_start, end=buf_end, auto_adjust=False)
    log("  ↳ VIX ...")
    vix = yf.Ticker("^VIX").history(start=buf_start, end=buf_end, auto_adjust=False)

    log("  ↳ CNN F&G historical ...")
    try:
        # Without a start date the API returns only ~1 year. With one it goes back to
        # 2021-01-01 at most (earlier dates return HTTP 500).
        fg_start = max(start_year - 1, FG_EARLIEST_YEAR)
        r = requests.get(
            f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{fg_start}-01-01",
            headers={"User-Agent": UA}, timeout=20,
        )
        r.raise_for_status()
        fg_hist = r.json().get("fear_and_greed_historical", {}).get("data", [])
        fg = pd.Series(
            {pd.Timestamp(pt["x"], unit="ms").normalize(): float(pt["y"]) for pt in fg_hist}
        ).sort_index()
    except Exception as e:
        log(f"     (실패 — {e})")
        fg = pd.Series(dtype=float)

    log("  ↳ AAII xls ...")
    aaii_df = _read_excel(_download_bytes())
    aaii = _parse_aaii_full(aaii_df)

    return {
        "spy": _strip_tz(spy["Close"].dropna()),
        "vix": _strip_tz(vix["Close"].dropna()),
        "fg":  _strip_tz(fg) if not fg.empty else fg,
        "aaii": _strip_tz(aaii),
    }


def _parse_aaii_full(df: pd.DataFrame) -> pd.Series:
    header_row = bear_col = date_col = None
    for i in range(min(len(df), 20)):
        cells = [_cell_str(v) for v in df.iloc[i].tolist()]
        if "bullish" in cells and "bearish" in cells:
            header_row = i
            for j, val in enumerate(cells):
                if val == "bearish" and bear_col is None:
                    bear_col = j
                elif "date" in val and date_col is None:
                    date_col = j
            break
    if date_col is None:
        date_col = 0
    body = df.iloc[header_row + 1 :].copy()
    body["__date"] = pd.to_datetime(body.iloc[:, date_col], errors="coerce")
    body["__bear"] = pd.to_numeric(body.iloc[:, bear_col], errors="coerce")
    body = body.dropna(subset=["__date", "__bear"]).sort_values("__date")
    return pd.Series(body["__bear"].values, index=body["__date"].values)


def build_indicator_series(data: dict) -> dict:
    spy = data["spy"]
    return {
        "vix": data["vix"],
        "rsi": wilder_rsi(spy, 14),
        "drawdown": spy / spy.rolling(252, min_periods=1).max() - 1.0,
        "fear_greed": data["fg"],
        "aaii": data["aaii"],
    }


# ----- Event detection -----

def threshold_condition(series: pd.Series, threshold: float, comparison: str) -> pd.Series:
    if comparison == "ge":  return series >= threshold
    if comparison == "gt":  return series > threshold
    if comparison == "le":  return series <= threshold
    if comparison == "lt":  return series < threshold
    raise ValueError(f"unknown comparison {comparison}")


def find_event_starts(condition: pd.Series, gap_days: int = 10) -> list:
    """Distinct event starts — True today AND False for the previous `gap_days` bars."""
    if condition.empty:
        return []
    cond = condition.fillna(False).astype(bool)
    events = []
    for i in range(len(cond)):
        if cond.iloc[i]:
            lookback_start = max(0, i - gap_days)
            prior = cond.iloc[lookback_start:i]
            if prior.empty or not prior.any():
                events.append(cond.index[i])
    return events


def forward_return_pct(closes: pd.Series, date: pd.Timestamp, days_forward: int = 252) -> Optional[float]:
    if len(closes) == 0:
        return None
    try:
        start_idx = int(closes.index.searchsorted(date))
        if start_idx >= len(closes):
            return None
        end_idx = start_idx + days_forward
        if end_idx >= len(closes):
            return None
        return (float(closes.iloc[end_idx]) / float(closes.iloc[start_idx]) - 1.0) * 100.0
    except Exception:
        return None


# ----- Higher-level analysis -----

def analyze_year(year: int, data: dict, series: dict, levels: List[int]) -> List[IndicatorReport]:
    """All event reports for the given year, one per (indicator, level)."""
    year_start = pd.Timestamp(f"{year}-01-01")
    year_end = pd.Timestamp(f"{year}-12-31")
    spy = data["spy"]

    out: List[IndicatorReport] = []
    for key in ["vix", "rsi", "drawdown", "fear_greed", "aaii"]:
        ind = series[key]
        if ind.empty:
            continue
        in_year = ind[(ind.index >= year_start) & (ind.index <= year_end)]
        if in_year.empty:
            continue
        for lvl in levels:
            threshold, comparison = THRESHOLDS[key][lvl]
            cond = threshold_condition(in_year, threshold, comparison)
            event_dates = find_event_starts(cond, gap_days=GAP_DAYS[key])
            report = IndicatorReport(indicator=key, level=lvl, events=[])
            for d in event_dates:
                fr = forward_return_pct(spy, d, days_forward=252)
                report.events.append(Event(
                    indicator=key, level=lvl, date=d,
                    value=float(in_year.loc[d]),
                    forward_return_pct=fr,
                ))
            out.append(report)
    return out


def analyze_indicator(indicator: str, data: dict, series: dict, levels: List[int]) -> List[IndicatorReport]:
    """All historical events for a single indicator across the full available history."""
    ind = series[indicator]
    spy = data["spy"]
    if ind.empty:
        return [IndicatorReport(indicator=indicator, level=lvl) for lvl in levels]

    out: List[IndicatorReport] = []
    for lvl in levels:
        threshold, comparison = THRESHOLDS[indicator][lvl]
        cond = threshold_condition(ind, threshold, comparison)
        event_dates = find_event_starts(cond, gap_days=GAP_DAYS[indicator])
        report = IndicatorReport(indicator=indicator, level=lvl, events=[])
        for d in event_dates:
            fr = forward_return_pct(spy, d, days_forward=252)
            report.events.append(Event(
                indicator=indicator, level=lvl, date=d,
                value=float(ind.loc[d]),
                forward_return_pct=fr,
            ))
        out.append(report)
    return out


# ----- Formatting helpers (shared by CLI and bot) -----

def format_threshold(key: str, threshold: float, comparison: str) -> str:
    sign = {"ge": "≥", "gt": ">", "le": "≤", "lt": "<"}[comparison]
    if key in ("drawdown", "aaii"):
        return f"{sign} {threshold * 100:.0f}%"
    return f"{sign} {threshold:.0f}"


def _format_value(key: str, value: float) -> str:
    if key in ("drawdown", "aaii"):
        return f"{value * 100:.1f}%"
    if key == "rsi":
        return f"{value:.1f}"
    if key == "fear_greed":
        return f"{value:.0f}"
    return f"{value:.2f}"


def format_year_report(year: int, reports: List[IndicatorReport]) -> str:
    lines = [f"📅 *{year}년 분석 — SPY 1년 후 수익률*", ""]
    all_returns = []

    for rep in reports:
        threshold, comparison = THRESHOLDS[rep.indicator][rep.level]
        name = LABEL[rep.indicator]
        tag = LEVEL_TAG[rep.level]
        thr = format_threshold(rep.indicator, threshold, comparison)

        if not rep.events:
            lines.append(f"⚪ {name} {tag} ({thr}): 0건")
            continue

        lines.append(f"🔴 *{name}* {tag} ({thr}): *{len(rep.events)}건*")
        for ev in rep.events:
            if ev.forward_return_pct is None:
                lines.append(f"   · {ev.date.date()}  값 {ev.value_display}  →  데이터 부족")
            else:
                lines.append(f"   · {ev.date.date()}  값 {ev.value_display}  →  *{ev.forward_return_pct:+.1f}%*")
                all_returns.append(ev.forward_return_pct)
        if rep.average_return is not None:
            lines.append(f"   평균: {rep.average_return:+.1f}%")
        lines.append("")

    if all_returns:
        lines.append("─" * 25)
        lines.append(f"📈 전체 {len(all_returns)}건, 평균 *{sum(all_returns)/len(all_returns):+.1f}%*")
        wins = sum(1 for r in all_returns if r > 0)
        lines.append(f"   양수율 {wins}/{len(all_returns)} ({wins/len(all_returns)*100:.0f}%)")
    return "\n".join(lines)


def format_indicator_report(indicator: str, reports: List[IndicatorReport], max_rows: int = 15) -> str:
    """Show all historical events for one indicator. If many, truncate to most recent N."""
    name = LABEL[indicator]
    lines = [f"📊 *{name}* — 전체 역사 강력 발화 + SPY 1년 후 수익률", ""]

    for rep in reports:
        threshold, comparison = THRESHOLDS[rep.indicator][rep.level]
        tag = LEVEL_TAG[rep.level]
        thr = format_threshold(rep.indicator, threshold, comparison)

        if not rep.events:
            lines.append(f"⚪ {tag} ({thr}): 0건")
            continue

        lines.append(f"🔴 {tag} ({thr}): *{len(rep.events)}건*")
        # Show most recent max_rows (reverse chronological)
        recent = list(reversed(rep.events))[:max_rows]
        for ev in recent:
            if ev.forward_return_pct is None:
                lines.append(f"   · {ev.date.date()}  {ev.value_display}  →  데이터 부족")
            else:
                lines.append(f"   · {ev.date.date()}  {ev.value_display}  →  *{ev.forward_return_pct:+.1f}%*")
        if len(rep.events) > max_rows:
            lines.append(f"   ... 외 {len(rep.events) - max_rows}건")
        if rep.average_return is not None:
            lines.append(f"   전체 평균: *{rep.average_return:+.1f}%*  ({rep.win_rate * 100:.0f}% 양수)")
        lines.append("")
    return "\n".join(lines)


def resolve_indicator(name: str) -> Optional[str]:
    """Convert user-friendly indicator name to canonical key."""
    return INDICATOR_ALIASES.get(name.strip().lower().replace("&", "&"))


# ----- Score frequency (how often each indicator reads +1/+2/+3) -----

TRADING_DAYS_PER_YEAR = 252
INDICATOR_ORDER = ["vix", "rsi", "drawdown", "fear_greed", "aaii"]

# Human-readable thresholds as applied by src/indicators/score.py (inclusive bounds).
SCORE_RULE_TEXT = {
    "vix":        {1: "VIX ≥ 22",          2: "VIX ≥ 28",           3: "VIX ≥ 35"},
    "rsi":        {1: "RSI ≤ 35",          2: "RSI ≤ 30",           3: "RSI ≤ 25"},
    "drawdown":   {1: "고점 대비 ≤ -7%",   2: "고점 대비 ≤ -12%",   3: "고점 대비 ≤ -18%"},
    "fear_greed": {1: "F&G ≤ 25",          2: "F&G ≤ 15",           3: "F&G ≤ 10"},
    "aaii":       {1: "약세 응답 ≥ 40%",   2: "약세 응답 ≥ 45%",    3: "약세 응답 ≥ 50%"},
}
TOTAL_LEVEL_ORDER = [score_mod.NOTICE, score_mod.WATCH, score_mod.ALERT, score_mod.STRONG]


def daily_scores(data: dict, start: str = "2008-01-01") -> pd.DataFrame:
    """Replay the live scoring rules (src/indicators/score.py) on every SPY trading day.

    Columns are indicator keys holding 0..3 (NaN where the source has no data).
    AAII is weekly, so each day carries the latest survey, as the live bot sees it.
    Drawdown includes the freshness decay."""
    spy = data["spy"]
    idx = spy.index[spy.index >= pd.Timestamp(start)]
    series = build_indicator_series(data)

    def on_days(s: pd.Series, ffill: bool = False) -> pd.Series:
        if s.empty:
            return pd.Series(np.nan, index=idx)
        if ffill:
            return s.reindex(s.index.union(spy.index)).ffill().reindex(idx)
        return s.reindex(idx)

    scorers = {
        "vix": score_mod.score_vix,
        "rsi": score_mod.score_rsi,
        "fear_greed": score_mod.score_fear_greed,
        "aaii": score_mod.score_aaii_bearish,
    }
    out = {}
    for key, fn in scorers.items():
        vals = on_days(series[key], ffill=(key == "aaii"))
        out[key] = vals.map(lambda v: np.nan if pd.isna(v) else fn(v).score)

    dsl = spy.rolling(FRESHNESS_WINDOW, min_periods=1).apply(
        lambda w: len(w) - 1 - np.argmin(w), raw=True
    )
    dd = on_days(series["drawdown"])
    out["drawdown"] = pd.Series(
        [np.nan if pd.isna(v) else score_mod.score_drawdown(v, int(dsl[d])).score
         for d, v in dd.items()],
        index=idx,
    )
    return pd.DataFrame(out)[INDICATOR_ORDER]


def _per_year(count: int, n_days: int) -> float:
    return count / n_days * TRADING_DAYS_PER_YEAR if n_days else 0.0


def format_frequency_report(scores: pd.DataFrame) -> str:
    lines = [
        "📊 *지표별 점수 빈도* — 1년(약 252거래일) 중 평균 며칠",
        "_각 기준을 넘은 날 수 (예: RSI ≤ 30 은 25 이하인 날도 포함)_",
        "",
    ]
    for key in INDICATOR_ORDER:
        col = scores[key].dropna()
        if col.empty:
            lines.append(f"⚠️ {LABEL[key]}: 데이터 없음")
            continue
        n = len(col)
        # Cumulative: "RSI ≤ 30" counts every day at +2 or +3, matching how the
        # threshold reads.
        per = {lv: _per_year(int((col >= lv).sum()), n) for lv in (1, 2, 3)}
        any_pct = (col > 0).mean() * 100
        lines.append(
            f"*{LABEL[key]}* ({col.index[0].year}~, {n / TRADING_DAYS_PER_YEAR:.1f}년, "
            f"점수 있는 날 {any_pct:.0f}%)"
        )
        for lv in (1, 2, 3):
            lines.append(f"   +{lv} {SCORE_RULE_TEXT[key][lv]} → 연 {per[lv]:.0f}일")
        if key == "drawdown":
            lines.append("   _(신선도 감점 반영 후 점수 기준)_")
        lines.append("")

    # Total level — only over days where all five indicators have data.
    full = scores.dropna()
    if not full.empty:
        n = len(full)
        total = full.sum(axis=1).astype(int)
        strong_sub = (full == 3).any(axis=1)
        levels = [score_mod.determine_level(t, s) for t, s in zip(total, strong_sub)]
        counts = pd.Series(levels).value_counts()
        lines += [
            f"*합산 단계* ({full.index[0].year}~, {n / TRADING_DAYS_PER_YEAR:.1f}년, 5개 지표 모두 있는 기간)",
        ]
        for lv in TOTAL_LEVEL_ORDER:
            days = _per_year(int(counts.get(lv, 0)), n)
            lines.append(
                f"   {score_mod.LEVEL_EMOJI[lv]} {score_mod.LEVEL_LABEL_KO[lv]}({lv.upper()}): {days:.0f}일"
            )

    lines += [
        "",
        "_알람 횟수가 아니라 해당 점수였던 날 수예요. 알람은 7거래일 쿨다운이 있어 훨씬 적게 옵니다._",
    ]
    return "\n".join(lines)
