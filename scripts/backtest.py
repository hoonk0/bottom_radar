"""Backtest the scoring system on 4 well-known market bottoms.

Usage:  python -m scripts.backtest [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import List

# Allow running as `python scripts/backtest.py` from the repo root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.pipeline import run_pipeline


CASES: List[tuple[str, str, str]] = [
    # (date, label, expected_level)
    ("2020-03-16", "코로나 패닉",        "strong"),
    ("2022-10-12", "인플레 바닥",        "alert"),
    ("2024-08-05", "엔캐리 청산",        "alert"),
    ("2025-04-07", "관세 충격",          "strong"),
]


def run(as_json: bool = False) -> int:
    rows = []
    for date_str, label, expected in CASES:
        end = datetime.strptime(date_str, "%Y-%m-%d")
        result = run_pipeline(end=end)
        total = result.total

        per_sub = {}
        for s in total.subscores:
            per_sub[s.key] = {
                "value": s.value,
                "display": s.display,
                "score": s.score,
                "level": s.level,
                "error": s.error,
            }

        rows.append({
            "date": date_str,
            "label": label,
            "expected_level": expected,
            "actual_level": total.level,
            "total_score": total.total,
            "has_strong_sub": total.has_strong_sub,
            "would_notify": total.notify,
            "match": _matches_expectation(total.level, total.notify, expected),
            "failed_fetches": total.failed_fetches,
            "subscores": per_sub,
        })

    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0

    _print_table(rows)
    return 0


def _matches_expectation(actual_level: str, would_notify: bool, expected: str) -> bool:
    """Pass if actual_level meets expectation OR (expected != strong AND notification would fire).

    The 'strong sub-score forces notify' rule means a Watch-level total can still
    trigger an Alert-equivalent notification. For expected=='alert' we accept any
    notify-eligible state; for expected=='strong' we require level==strong."""
    rank = {"normal": 0, "watch": 1, "alert": 2, "strong": 3}
    if rank.get(actual_level, 0) >= rank.get(expected, 0):
        return True
    if expected == "alert" and would_notify:
        return True
    return False


def _print_table(rows):
    print("\n=== Backtest results ===\n")
    header = f"{'Date':12} {'Case':14} {'Expect':8} {'Actual':8} {'Score':>5}  Sub-scores"
    print(header)
    print("-" * len(header))
    for r in rows:
        subs = " ".join(
            f"{k}:{v['display']}({v['score']})" if v["error"] is None else f"{k}:ERR"
            for k, v in r["subscores"].items()
        )
        status = "✓" if r["match"] else "✗"
        notify_flag = "→notify" if r["would_notify"] else ""
        print(
            f"{r['date']:12} {r['label']:14} {r['expected_level']:8} "
            f"{r['actual_level']:8} {r['total_score']:>5} {notify_flag:8}  {subs}  {status}"
        )
        if r["failed_fetches"]:
            print(f"  (failed: {', '.join(r['failed_fetches'])})")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = p.parse_args()
    sys.exit(run(as_json=args.json))
