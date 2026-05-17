"""Fetch price series for VIX, SPY, QQQ via yfinance."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf


@dataclass
class PriceSeries:
    ticker: str
    closes: pd.Series        # indexed by date, float closes
    last_close: float
    prev_close: float
    as_of: pd.Timestamp

    @property
    def daily_change_pct(self) -> float:
        if self.prev_close == 0:
            return 0.0
        return (self.last_close / self.prev_close - 1.0) * 100.0


def _download(ticker: str, period: str = "1y", end: Optional[datetime] = None) -> pd.DataFrame:
    """Download closes for ticker. If end is given, fetches up to that date (inclusive)."""
    if end is None:
        df = yf.Ticker(ticker).history(period=period, auto_adjust=False)
    else:
        # backtest mode: use date range. Need enough history (1y+ buffer for RSI/drawdown).
        start = end - timedelta(days=400)
        # yfinance end is exclusive, so push one day forward
        df = yf.Ticker(ticker).history(
            start=start.strftime("%Y-%m-%d"),
            end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
            auto_adjust=False,
        )
    if df.empty:
        raise RuntimeError(f"No data returned for {ticker}")
    return df


def fetch_price_series(ticker: str, period: str = "1y", end: Optional[datetime] = None) -> PriceSeries:
    df = _download(ticker, period=period, end=end)
    closes = df["Close"].dropna().astype(float)
    if len(closes) < 2:
        raise RuntimeError(f"Insufficient data for {ticker}: {len(closes)} rows")
    return PriceSeries(
        ticker=ticker,
        closes=closes,
        last_close=float(closes.iloc[-1]),
        prev_close=float(closes.iloc[-2]),
        as_of=closes.index[-1],
    )


def fetch_vix(end: Optional[datetime] = None) -> PriceSeries:
    # Need only short window for VIX
    return fetch_price_series("^VIX", period="1mo" if end is None else "1y", end=end)


def fetch_spy(end: Optional[datetime] = None) -> PriceSeries:
    return fetch_price_series("SPY", period="1y", end=end)


def fetch_qqq(end: Optional[datetime] = None) -> PriceSeries:
    return fetch_price_series("QQQ", period="1mo" if end is None else "1y", end=end)
