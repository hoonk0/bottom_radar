"""연도별 / 지표별 강력 발화 분석 — CLI 진입점.

Usage:
    python -m scripts.analyze year 2022
    python -m scripts.analyze year 2020 2022 2025
    python -m scripts.analyze indicator vix
    python -m scripts.analyze indicator aaii --levels 2 3

도움말:
    python -m scripts.analyze --help
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analysis import (
    analyze_indicator,
    analyze_year,
    build_indicator_series,
    fetch_all,
    format_indicator_report,
    format_year_report,
    resolve_indicator,
    INDICATOR_ALIASES,
)


def cmd_year(args):
    years = sorted(set(args.years))
    print(f"\nFetching data for {years} ...")
    data = fetch_all(min(years), max(years))
    series = build_indicator_series(data)
    for y in years:
        reports = analyze_year(y, data, series, sorted(args.levels))
        print()
        print(format_year_report(y, reports))


def cmd_indicator(args):
    key = resolve_indicator(args.indicator)
    if key is None:
        print(f"⚠️ 알 수 없는 지표 '{args.indicator}'.")
        print(f"   가능: {', '.join(sorted(set(INDICATOR_ALIASES.values())))}")
        return 1
    end_year = datetime.now().year
    start_year = 2008  # yfinance covers SPY back to 1993, but realistic data starts ~2008
    print(f"\nFetching {start_year} ~ {end_year} for '{key}' ...")
    data = fetch_all(start_year, end_year)
    series = build_indicator_series(data)
    reports = analyze_indicator(key, data, series, sorted(args.levels))
    print()
    print(format_indicator_report(key, reports, max_rows=args.max_rows))
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    py = sub.add_parser("year", help="연도별 강력 발화 분석")
    py.add_argument("years", nargs="+", type=int, help="연도 (1개 이상)")
    py.add_argument("--levels", nargs="+", type=int, default=[3], choices=[1, 2, 3],
                    help="점수 등급 (default: 3)")
    py.set_defaults(func=cmd_year)

    pi = sub.add_parser("indicator", help="지표별 전체 역사 강력 발화")
    pi.add_argument("indicator", help="vix | rsi | drawdown | fear_greed | aaii")
    pi.add_argument("--levels", nargs="+", type=int, default=[3], choices=[1, 2, 3],
                    help="점수 등급 (default: 3)")
    pi.add_argument("--max-rows", type=int, default=15, help="표시할 최대 발화 수 (default: 15)")
    pi.set_defaults(func=cmd_indicator)

    args = p.parse_args()
    sys.exit(args.func(args) or 0)
