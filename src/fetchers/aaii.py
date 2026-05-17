"""Fetch AAII Investor Sentiment survey — weekly Bearish %.

AAII fronts the .xls behind Cloudflare; the server occasionally returns an HTML
JS-challenge page to non-browser-looking clients. Mitigations:
  - Browser-like User-Agent + Referer headers.
  - Module-level in-process cache (one download per process lifetime).
  - On-disk fallback at data/aaii_sentiment.xls used if the live fetch returns HTML.
The on-disk file is also what the backtest reads — historical Bearish rows are
in the same file.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/vnd.ms-excel,application/octet-stream,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.aaii.com/sentimentsurvey",
}
URL = "https://www.aaii.com/files/surveys/sentiment.xls"
FALLBACK_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "aaii_sentiment.xls"
XLS_MAGIC = b"\xd0\xcf\x11\xe0"


@dataclass
class AaiiReading:
    bearish: float       # 0..1 fraction
    bullish: float
    neutral: float
    as_of: datetime


_CACHED_BYTES: Optional[bytes] = None


def _looks_like_xls(content: bytes) -> bool:
    return len(content) > 1000 and content[:4] == XLS_MAGIC


def _download_bytes(use_cache: bool = True) -> bytes:
    """Download the AAII workbook, falling back to a bundled snapshot on failure.

    Cache hierarchy:
      1. In-process memo (so backtests run N dates with one HTTP request).
      2. Live HTTP fetch with browser-like headers.
      3. On-disk snapshot at data/aaii_sentiment.xls.
    """
    global _CACHED_BYTES
    if use_cache and _CACHED_BYTES is not None:
        return _CACHED_BYTES

    fetch_error: Optional[Exception] = None
    try:
        r = requests.get(URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        if _looks_like_xls(r.content):
            if use_cache:
                _CACHED_BYTES = r.content
            return r.content
        fetch_error = RuntimeError(
            f"AAII returned non-xls content ({r.headers.get('content-type')}, "
            f"{len(r.content)}B) — likely a Cloudflare challenge"
        )
    except Exception as e:
        fetch_error = e

    if FALLBACK_PATH.exists():
        content = FALLBACK_PATH.read_bytes()
        if _looks_like_xls(content):
            if use_cache:
                _CACHED_BYTES = content
            return content

    raise RuntimeError(f"AAII fetch failed and no local fallback: {fetch_error}")


def _read_excel(content: bytes) -> pd.DataFrame:
    """The AAII file has historically been .xls (xlrd) but they sometimes serve .xlsx.
    Try xlrd first, then openpyxl. Returns the first sheet as DataFrame with no header
    fixup — we hunt for the data block ourselves."""
    last_err: Optional[Exception] = None
    for engine in ("xlrd", "openpyxl"):
        try:
            return pd.read_excel(io.BytesIO(content), engine=engine, header=None)
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Could not parse AAII workbook: {last_err}")


def _cell_str(v) -> str:
    """Safe normalization of a cell value to a lowercase trimmed string."""
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        return str(v).strip().lower()
    except Exception:
        return ""


def _parse(df: pd.DataFrame, end: Optional[datetime] = None) -> AaiiReading:
    """Find header row containing 'Bullish'/'Neutral'/'Bearish' columns and parse rows below."""
    bull_col = bear_col = neut_col = date_col = None
    header_row = None
    for i in range(min(len(df), 20)):
        cells = [_cell_str(v) for v in df.iloc[i].tolist()]
        if "bullish" in cells and "bearish" in cells:
            header_row = i
            for j, val in enumerate(cells):
                if val == "bullish" and bull_col is None:
                    bull_col = j
                elif val == "bearish" and bear_col is None:
                    bear_col = j
                elif val == "neutral" and neut_col is None:
                    neut_col = j
                elif "date" in val and date_col is None:
                    date_col = j
            break
    if header_row is None or bear_col is None:
        raise RuntimeError("Could not locate header row in AAII workbook")
    if date_col is None:
        date_col = 0  # AAII format: date is always first column

    body = df.iloc[header_row + 1 :].copy()
    body["__date"] = pd.to_datetime(body.iloc[:, date_col], errors="coerce")
    body["__bull"] = pd.to_numeric(body.iloc[:, bull_col], errors="coerce")
    body["__neut"] = pd.to_numeric(body.iloc[:, neut_col], errors="coerce") if neut_col is not None else 0.0
    body["__bear"] = pd.to_numeric(body.iloc[:, bear_col], errors="coerce")
    body = body.dropna(subset=["__date", "__bear"]).sort_values("__date")

    if end is not None:
        cutoff = pd.Timestamp(end).normalize()
        body = body[body["__date"] <= cutoff]
        if body.empty:
            raise RuntimeError(f"No AAII rows on/before {end.date()}")

    last = body.iloc[-1]
    return AaiiReading(
        bearish=float(last["__bear"]),
        bullish=float(last["__bull"]),
        neutral=float(last["__neut"]),
        as_of=last["__date"].to_pydatetime(),
    )


def fetch_aaii(end: Optional[datetime] = None) -> AaiiReading:
    content = _download_bytes()
    df = _read_excel(content)
    return _parse(df, end=end)
