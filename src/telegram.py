"""Telegram bot sender + message formatting."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

import requests

from src.indicators.score import (
    ALERT,
    LEVEL_EMOJI,
    LEVEL_LABEL_KO,
    MAX_SCORE,
    NORMAL,
    NOTICE,
    STRONG,
    SubScore,
    TotalScore,
    WATCH,
)


API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


@dataclass
class Snapshot:
    """Optional per-ticker price snapshot for the message footer."""
    ticker: str
    price: float
    daily_change_pct: float


HEADER_BY_LEVEL = {
    STRONG: "🔴 *SPY 매수 신호 — STRONG*",
    ALERT: "🟠 *SPY 매수 신호 — Alert*",
    WATCH: "🟡 *SPY Watch — 단일 지표 폭발*",
    NOTICE: "🟢 *SPY Notice — 약세 초기 신호*",
    NORMAL: "⚪ SPY 정상",
}

HIST_NOTE_BY_LEVEL = {
    STRONG: "_과거 유사 점수(10+) 점등 후 1년 평균 수익률 대략 +25~35%_",
    ALERT: "_과거 유사 점수(6~9) 점등 후 1년 평균 수익률 대략 +14~18%_",
    WATCH: "_단일 지표가 +3 강력 — 일반 조정일 수도 깊은 약세 입구일 수도. 추적 권장._",
    NOTICE: "_여러 지표가 살짝 점등 — 일반 5~7% 조정 수준. 추적만 권장, 매수는 일러._",
    NORMAL: "",
}


def _fmt_sub(s: SubScore) -> str:
    emoji = LEVEL_EMOJI[s.level]
    if s.error:
        return f"⚠️ {s.label}: 조회 실패 ({s.error})"
    label_ko = LEVEL_LABEL_KO[s.level]
    if s.level == NORMAL:
        return f"{emoji} {s.label}: {s.display} (정상)"
    return f"{emoji} {s.label}: {s.display} ({label_ko}, +{s.score})"


def format_message(
    total: TotalScore,
    today: datetime,
    snapshots: Optional[List[Snapshot]] = None,
) -> str:
    header = HEADER_BY_LEVEL.get(total.level, HEADER_BY_LEVEL[NORMAL])
    date_line = f"_{today.strftime('%Y-%m-%d')} KST_"
    lines = [
        header,
        date_line,
        "",
        f"*합산 점수: {total.total} / {MAX_SCORE}*",
        "",
    ]
    for s in total.subscores:
        lines.append(_fmt_sub(s))

    if snapshots:
        lines.append("")
        for snap in snapshots:
            arrow = "📈" if snap.daily_change_pct >= 0 else "📉"
            lines.append(
                f"{arrow} {snap.ticker}: ${snap.price:.2f} (전일 {snap.daily_change_pct:+.2f}%)"
            )

    note = HIST_NOTE_BY_LEVEL.get(total.level, "")
    if note:
        lines.append("")
        lines.append(note)

    if total.failed_fetches:
        lines.append("")
        lines.append(f"_⚠️ 일부 지표 조회 실패: {', '.join(total.failed_fetches)}_")

    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str, parse_mode: str = "Markdown") -> dict:
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    r = requests.post(
        API_BASE.format(token=token),
        json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def send_error(token: str, chat_id: str, message: str) -> None:
    """Send a plain-text error message (no markdown to avoid parse failures)."""
    if not token or not chat_id:
        return
    try:
        requests.post(
            API_BASE.format(token=token),
            json={"chat_id": chat_id, "text": f"⚠️ bottom_radar error\n{message}"},
            timeout=15,
        )
    except Exception:
        pass
