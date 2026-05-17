"""매일 점수를 계산해 알람이 몇 번 점등됐는지 시뮬레이션.

Usage:
    python -m scripts.sweep                       # 올해 1/1 ~ 오늘
    python -m scripts.sweep --start 2026-01-01 --end 2026-05-16
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import requests
import yfinance as yf

from src.fetchers.aaii import _cell_str, _download_bytes, _read_excel
from src.indicators.freshness import days_since_lowest_close
from src.indicators.rsi import wilder_rsi
from src.indicators.score import (
    SubScore,
    NORMAL,
    combine,
    score_aaii_bearish,
    score_drawdown,
    score_fear_greed,
    score_rsi,
    score_vix,
)
from src.state import State, should_notify, update_state


UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def fetch_all(start: datetime, end: datetime) -> dict:
    """Bulk-fetch every series once. Buffer ~400 days for SMA200/Drawdown warmup."""
    buf_start = (start - timedelta(days=420)).strftime("%Y-%m-%d")
    end_str = (end + timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"  ↳ SPY history ...", flush=True)
    spy = yf.Ticker("SPY").history(start=buf_start, end=end_str, auto_adjust=False)
    print(f"  ↳ VIX history ...", flush=True)
    vix = yf.Ticker("^VIX").history(start=buf_start, end=end_str, auto_adjust=False)

    print(f"  ↳ CNN F&G historical ...", flush=True)
    r = requests.get(
        "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
        headers={"User-Agent": UA}, timeout=20,
    )
    r.raise_for_status()
    fg_hist = r.json().get("fear_and_greed_historical", {}).get("data", [])
    fg = pd.Series(
        {pd.Timestamp(pt["x"], unit="ms", tz="UTC").tz_convert(None).normalize(): float(pt["y"])
         for pt in fg_hist}
    ).sort_index()

    print(f"  ↳ AAII xls ...", flush=True)
    aaii_df = _read_excel(_download_bytes())
    aaii = _parse_aaii_full(aaii_df)

    # Normalize all indices to tz-naive midnight so the lookups compare cleanly.
    def _strip_tz(s: pd.Series) -> pd.Series:
        idx = s.index
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        s = s.copy()
        s.index = pd.DatetimeIndex(idx).normalize()
        return s

    return {
        "spy": _strip_tz(spy["Close"].dropna()),
        "vix": _strip_tz(vix["Close"].dropna()),
        "fg": _strip_tz(fg),
        "aaii": _strip_tz(aaii),
    }


def _parse_aaii_full(df: pd.DataFrame) -> pd.Series:
    """Return AAII Bearish (0..1) full historical series indexed by date."""
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


def _err(key: str, label: str, msg: str) -> SubScore:
    return SubScore(key=key, label=label, value=None, score=0,
                    level=NORMAL, display="N/A", error=msg)


def compute_day(d: pd.Timestamp, data: dict) -> "object":
    """Compute the 6 sub-scores using only data with index <= d."""
    subs = []

    # 1) VIX
    vix_slice = data["vix"][:d]
    subs.append(score_vix(float(vix_slice.iloc[-1])) if len(vix_slice)
                else _err("vix", "VIX", "no data"))

    # 2-3) SPY-derived
    spy_slice = data["spy"][:d]
    if len(spy_slice) >= 252:
        try:
            rsi_val = float(wilder_rsi(spy_slice, 14).iloc[-1])
            subs.append(score_rsi(rsi_val))
        except Exception as e:
            subs.append(_err("rsi", "SPY RSI(14)", str(e)))
        tail252 = spy_slice.iloc[-252:]
        dd = float(spy_slice.iloc[-1]) / float(tail252.max()) - 1.0
        dsl = days_since_lowest_close(spy_slice)
        subs.append(score_drawdown(dd, days_since_low=dsl))
    else:
        subs.append(_err("rsi", "SPY RSI(14)", "insufficient history"))
        subs.append(_err("drawdown", "SPY Drawdown", "insufficient history"))

    # 5) F&G
    fg_slice = data["fg"][:d]
    subs.append(score_fear_greed(float(fg_slice.iloc[-1])) if len(fg_slice)
                else _err("fear_greed", "CNN F&G", "no data"))

    # 6) AAII (weekly — latest <= d)
    aaii_slice = data["aaii"][:d]
    subs.append(score_aaii_bearish(float(aaii_slice.iloc[-1])) if len(aaii_slice)
                else _err("aaii", "AAII Bearish", "no data"))

    return combine(subs)


def run(start: datetime, end: datetime, verbose: bool = False):
    print(f"\n=== Sweep {start.date()} → {end.date()} ===")
    print("Fetching data ...", flush=True)
    data = fetch_all(start, end)

    days = data["spy"].index
    days = days[(days.date >= start.date()) & (days.date <= end.date())]
    print(f"\n→ {len(days)}개 거래일 분석\n")

    state = State()
    fired: list[dict] = []
    near_misses: list[dict] = []   # score >= 4 or strong sub (Watch level + above)

    LEVEL_EMOJI = {"normal": "⚪", "notice": "🟢", "watch": "🟡", "alert": "🟠", "strong": "🔴"}

    for d in days:
        total = compute_day(d, data)
        d_utc = datetime.combine(d.date(), datetime.min.time()).replace(tzinfo=timezone.utc)
        cooldown_ok, reason = should_notify(state, total.level, total.has_strong_sub, today=d_utc)
        actually_fires = total.notify and cooldown_ok

        if actually_fires:
            update_state(state, today=d_utc, score=total.total, level=total.level, notified=True)
            fired.append({
                "date": d.date(), "level": total.level, "total": total.total,
                "strong_sub": total.has_strong_sub,
                "subs": [(s.key, s.display, s.score, s.error) for s in total.subscores],
            })
        elif total.total >= 4 or total.has_strong_sub:
            near_misses.append({
                "date": d.date(), "level": total.level, "total": total.total,
                "strong_sub": total.has_strong_sub, "reason": reason,
            })

    # --- Output ---
    if fired:
        print(f"🔔 알람 발화: {len(fired)}회\n")
        for f in fired:
            emoji = LEVEL_EMOJI[f["level"]]
            print(f"  {emoji} {f['date']}  {f['level'].upper():6}  {f['total']:>2}/15"
                  f"{'  (강력 sub: ' + ','.join(k for k,_,sc,e in f['subs'] if sc==3 and not e) + ')' if f['strong_sub'] else ''}")
            for key, disp, sc, err in f["subs"]:
                if err is None:
                    print(f"     · {key:11} {disp:>9}  +{sc}")
                else:
                    print(f"     · {key:11} {'N/A':>9}  ERR ({err[:30]})")
            print()
    else:
        print("🔔 알람 발화: 0회\n")

    if near_misses:
        print(f"🟡 Watch 수준 도달 (알람 안 보냈지만 4점 이상 or 강력 sub 발생): {len(near_misses)}회")
        for n in near_misses[:15]:
            emoji = LEVEL_EMOJI[n["level"]]
            strong_tag = "  (+3 strong sub)" if n["strong_sub"] else ""
            print(f"  {emoji} {n['date']}  {n['level']:7} {n['total']:>2}/15{strong_tag}  — {n['reason']}")
        if len(near_misses) > 15:
            print(f"  ... 외 {len(near_misses) - 15}일")
    else:
        print("🟡 Watch 수준 도달일: 0일 (시장이 매우 조용함)")


if __name__ == "__main__":
    today = datetime.now()
    p = argparse.ArgumentParser()
    p.add_argument("--start", default=f"{today.year}-01-01")
    p.add_argument("--end", default=today.strftime("%Y-%m-%d"))
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    run(
        datetime.strptime(args.start, "%Y-%m-%d"),
        datetime.strptime(args.end, "%Y-%m-%d"),
        verbose=args.verbose,
    )
