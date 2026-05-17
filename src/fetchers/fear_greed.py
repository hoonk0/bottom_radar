"""Fetch CNN Fear & Greed Index."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import re
import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
PRIMARY_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
FALLBACK_URL = "https://money.cnn.com/data/fear-and-greed/"


@dataclass
class FearGreed:
    score: float
    as_of: datetime
    rating: Optional[str] = None
    source: str = "cnn-api"


def _fetch_primary(end: Optional[datetime] = None) -> FearGreed:
    url = PRIMARY_URL
    if end is not None:
        url = f"{PRIMARY_URL}/{end.strftime('%Y-%m-%d')}"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    data = r.json()

    if end is None:
        node = data.get("fear_and_greed", {})
        score = float(node["score"])
        as_of = datetime.fromisoformat(node["timestamp"].replace("Z", "+00:00")) \
            if isinstance(node.get("timestamp"), str) else datetime.now(timezone.utc)
        rating = node.get("rating")
        return FearGreed(score=score, as_of=as_of, rating=rating, source="cnn-api")

    # historical mode: pick the historical point closest to (but not after) end
    series = data.get("fear_and_greed_historical", {}).get("data", [])
    if not series:
        raise RuntimeError("No historical F&G data returned")
    end_ts = int(end.timestamp() * 1000)
    eligible = [pt for pt in series if pt["x"] <= end_ts]
    chosen = eligible[-1] if eligible else series[0]
    return FearGreed(
        score=float(chosen["y"]),
        as_of=datetime.fromtimestamp(chosen["x"] / 1000, tz=timezone.utc),
        rating=chosen.get("rating"),
        source="cnn-api-historical",
    )


def _fetch_fallback() -> FearGreed:
    r = requests.get(FALLBACK_URL, headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    html = r.text
    # crude scrape: look for "Fear &amp; Greed Now: NN"
    m = re.search(r"Fear\s*&(?:amp;)?\s*Greed[^0-9]{0,40}([0-9]{1,3})", html, re.IGNORECASE)
    if not m:
        raise RuntimeError("Could not parse F&G score from fallback HTML")
    return FearGreed(score=float(m.group(1)), as_of=datetime.now(timezone.utc), source="cnn-html")


def fetch_fear_greed(end: Optional[datetime] = None) -> FearGreed:
    """Fetch CNN F&G score. Falls back to HTML scrape if the JSON API fails."""
    try:
        return _fetch_primary(end=end)
    except Exception:
        if end is not None:
            # fallback HTML has no historical data
            raise
        return _fetch_fallback()
