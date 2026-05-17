"""연도별 각 지표의 강력(+3) 타점 발화 시점과 SPY 1년 후 수익률 분석.

Usage:
    python -m scripts.analyze 2022
    python -m scripts.analyze 2020 2022 2025          # 여러 연도
    python -m scripts.analyze 2022 --levels 2 3       # 경계(+2)+강력(+3) 포함
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import requests
import yfinance as yf

from src.fetchers.aaii import _cell_str, _download_bytes, _read_excel
from src.indicators.rsi import wilder_rsi


UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# Per-indicator threshold definitions per level. (threshold, comparison)
# comparison: 'ge' = value >= threshold,  'le' = value <= threshold,  'lt' / 'gt' allowed.
THRESHOLDS = {
    "vix": {1: (22.0, "ge"), 2: (28.0, "ge"), 3: (35.0, "ge")},
    "rsi": {1: (35.0, "lt"), 2: (30.0, "lt"), 3: (25.0, "lt")},
    "drawdown": {1: (-0.07, "le"), 2: (-0.12, "le"), 3: (-0.18, "le")},
    "fear_greed": {1: (25.0, "lt"), 2: (15.0, "lt"), 3: (10.0, "lt")},
    "aaii": {1: (0.40, "gt"), 2: (0.45, "gt"), 3: (0.50, "gt")},
}

LABEL = {
    "vix": "VIX",
    "rsi": "SPY RSI(14)",
    "drawdown": "SPY 1y Drawdown",
    "fear_greed": "CNN F&G",
    "aaii": "AAII Bearish",
}

# AAII is weekly so cluster-gap is shorter (4 days = roughly a full week of missing readings).
GAP_DAYS = {
    "vix": 10,
    "rsi": 10,
    "drawdown": 10,
    "fear_greed": 10,
    "aaii": 4,
}


# ----- Data fetch -----

def _strip_tz(s: pd.Series) -> pd.Series:
    idx = s.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    s = s.copy()
    s.index = pd.DatetimeIndex(idx).normalize()
    return s


def fetch_all(start_year: int, end_year: int) -> dict:
    """Fetch SPY/VIX/F&G/AAII covering [start_year-1, end_year+1]."""
    buf_start = f"{start_year - 1}-01-01"
    buf_end = f"{end_year + 2}-01-01"

    print(f"  ↳ SPY history ({buf_start} ~ {buf_end}) ...", flush=True)
    spy = yf.Ticker("SPY").history(start=buf_start, end=buf_end, auto_adjust=False)
    print(f"  ↳ VIX history ...", flush=True)
    vix = yf.Ticker("^VIX").history(start=buf_start, end=buf_end, auto_adjust=False)

    print(f"  ↳ CNN F&G historical ...", flush=True)
    try:
        r = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers={"User-Agent": UA}, timeout=20,
        )
        r.raise_for_status()
        fg_hist = r.json().get("fear_and_greed_historical", {}).get("data", [])
        fg = pd.Series(
            {pd.Timestamp(pt["x"], unit="ms").normalize(): float(pt["y"]) for pt in fg_hist}
        ).sort_index()
    except Exception as e:
        print(f"     (실패 — {e})", flush=True)
        fg = pd.Series(dtype=float)

    print(f"  ↳ AAII xls ...", flush=True)
    aaii_df = _read_excel(_download_bytes())
    aaii = _parse_aaii_full(aaii_df)

    return {
        "spy": _strip_tz(spy["Close"].dropna()),
        "vix": _strip_tz(vix["Close"].dropna()),
        "fg": _strip_tz(fg) if not fg.empty else fg,
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


# ----- Indicator series construction -----

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
    if comparison == "ge":
        return series >= threshold
    if comparison == "gt":
        return series > threshold
    if comparison == "le":
        return series <= threshold
    if comparison == "lt":
        return series < threshold
    raise ValueError(f"unknown comparison {comparison}")


def find_event_starts(condition: pd.Series, gap_days: int = 10) -> list:
    """Dates where condition first becomes True after being False for >= gap_days bars.
    This deduplicates clusters — e.g. a 3-month-long Drawdown<=-18% counts once at entry,
    not 60 times. New events restart after the condition flips off for gap_days."""
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


# ----- Forward return -----

def forward_return_pct(closes: pd.Series, date: pd.Timestamp, days_forward: int = 252) -> Optional[float]:
    if len(closes) == 0:
        return None
    try:
        # Find closest trading day (start_idx)
        idx = closes.index.searchsorted(date)
        if idx >= len(closes):
            return None
        start_idx = idx
        end_idx = start_idx + days_forward
        if end_idx >= len(closes):
            return None
        start_price = float(closes.iloc[start_idx])
        end_price = float(closes.iloc[end_idx])
        return (end_price / start_price - 1.0) * 100.0
    except Exception:
        return None


# ----- Analysis -----

LEVEL_LABEL = {1: "주의(+1)", 2: "경계(+2)", 3: "강력(+3)"}


def analyze_year(year: int, data: dict, series: dict, levels: list[int]):
    print(f"\n===== {year}년 분석 — 1년 후 SPY 수익률 =====\n")

    year_start = pd.Timestamp(f"{year}-01-01")
    year_end = pd.Timestamp(f"{year}-12-31")
    spy = data["spy"]

    all_returns = []

    for indicator_key in ["vix", "rsi", "drawdown", "fear_greed", "aaii"]:
        ind_series = series[indicator_key]
        if ind_series.empty:
            print(f"📊 {LABEL[indicator_key]}: 데이터 없음\n")
            continue

        # Slice to year
        in_year = ind_series[(ind_series.index >= year_start) & (ind_series.index <= year_end)]
        if in_year.empty:
            print(f"📊 {LABEL[indicator_key]}: 해당 연도 데이터 없음\n")
            continue

        for lvl in levels:
            threshold, comparison = THRESHOLDS[indicator_key][lvl]
            cond = threshold_condition(in_year, threshold, comparison)
            events = find_event_starts(cond, gap_days=GAP_DAYS[indicator_key])

            level_tag = LEVEL_LABEL[lvl]
            threshold_text = _format_threshold(indicator_key, threshold, comparison)

            if not events:
                print(f"📊 {LABEL[indicator_key]} {level_tag} ({threshold_text}): 0건")
                continue

            print(f"📊 {LABEL[indicator_key]} {level_tag} ({threshold_text}): {len(events)}건")
            returns = []
            for d in events:
                fr = forward_return_pct(spy, d, days_forward=252)
                val = float(in_year.loc[d])
                val_str = _format_value(indicator_key, val)
                if fr is None:
                    print(f"   · {d.date()}  값 {val_str}  →  1년 후 수익률: 데이터 부족")
                else:
                    print(f"   · {d.date()}  값 {val_str}  →  1년 후: {fr:+.1f}%")
                    returns.append(fr)
                    all_returns.append((indicator_key, lvl, d.date(), fr))
            if returns:
                avg = sum(returns) / len(returns)
                print(f"   평균 1년 수익률: {avg:+.1f}%")
            print()

    # Overall summary
    if all_returns:
        rets = [r for _, _, _, r in all_returns]
        print("─" * 50)
        print(f"📈 전체 발화 합계 (중복 포함): {len(all_returns)}건")
        print(f"   평균 1년 수익률: {sum(rets)/len(rets):+.1f}%")
        print(f"   최고:           {max(rets):+.1f}%")
        print(f"   최저:           {min(rets):+.1f}%")
        # Hit rate (positive return)
        wins = sum(1 for r in rets if r > 0)
        print(f"   양수 수익률 비율: {wins}/{len(rets)} ({wins/len(rets)*100:.0f}%)")


def _format_threshold(key: str, threshold: float, comparison: str) -> str:
    sign = {"ge": "≥", "gt": ">", "le": "≤", "lt": "<"}[comparison]
    if key == "drawdown":
        return f"DD {sign} {threshold*100:.0f}%"
    if key == "aaii":
        return f"Bearish {sign} {threshold*100:.0f}%"
    if key == "vix":
        return f"VIX {sign} {threshold:.0f}"
    if key == "rsi":
        return f"RSI {sign} {threshold:.0f}"
    if key == "fear_greed":
        return f"F&G {sign} {threshold:.0f}"
    return f"{sign} {threshold}"


def _format_value(key: str, value: float) -> str:
    if key == "drawdown":
        return f"{value*100:.1f}%"
    if key == "aaii":
        return f"{value*100:.1f}%"
    if key == "rsi":
        return f"{value:.1f}"
    if key == "fear_greed":
        return f"{value:.0f}"
    return f"{value:.2f}"


# ----- Entry point -----

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("years", nargs="+", type=int, help="분석할 연도 (예: 2022 2025)")
    p.add_argument("--levels", nargs="+", type=int, default=[3],
                   choices=[1, 2, 3], help="포함할 점수 등급 (default: 3 강력만)")
    args = p.parse_args()

    print(f"\nFetching data for years {args.years} (with 1y forward buffer)...")
    data = fetch_all(min(args.years), max(args.years))
    series = build_indicator_series(data)

    for year in sorted(set(args.years)):
        analyze_year(year, data, series, sorted(args.levels))
