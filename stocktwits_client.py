"""
stocktwits_client.py — StockTwits public sentiment feed.

Uses the free, unauthenticated symbol stream endpoint. No API key required.
Each ticker response contains up to 30 messages; sentiment is extracted from
the optional `entities.sentiment.basic` field ("Bullish" | "Bearish" | null).

Results are cached in-process for CACHE_TTL seconds to stay polite to the API.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import TypedDict

from utils import get_logger

log = get_logger("stocktwits")

CACHE_TTL = 60  # seconds
_BASE = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"

_cache: dict[str, tuple[float, "StockTwitsSentiment"]] = {}


class StockTwitsSentiment(TypedDict):
    ticker: str
    bullish_ratio: float
    bearish_ratio: float
    net_sentiment: float   # bullish_ratio - bearish_ratio, range [-1, 1]
    total_messages: int
    available: bool        # False when the API call failed or returned nothing


def fetch_stocktwits(ticker: str) -> StockTwitsSentiment:
    """
    Return StockTwits sentiment for a single ticker.

    On any network error or unexpected response shape, returns a zeroed-out
    result with available=False so callers can safely ignore it.
    """
    now = time.time()
    if ticker in _cache:
        ts, data = _cache[ticker]
        if now - ts < CACHE_TTL:
            return data

    result = _fetch_raw(ticker)
    _cache[ticker] = (now, result)
    return result


def fetch_stocktwits_batch(tickers: list[str]) -> dict[str, StockTwitsSentiment]:
    """Fetch StockTwits sentiment for multiple tickers (sequential, cache-aware)."""
    return {t: fetch_stocktwits(t) for t in tickers}


def _fetch_raw(ticker: str) -> StockTwitsSentiment:
    url = _BASE.format(ticker=ticker)
    empty = StockTwitsSentiment(
        ticker=ticker,
        bullish_ratio=0.5,
        bearish_ratio=0.5,
        net_sentiment=0.0,
        total_messages=0,
        available=False,
    )

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MemestockRadar/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            log.warning("StockTwits rate-limited for %s — skipping", ticker)
        elif exc.code == 404:
            log.debug("StockTwits: ticker %s not found", ticker)
        else:
            log.debug("StockTwits HTTP %s for %s", exc.code, ticker)
        return empty
    except Exception as exc:
        log.debug("StockTwits fetch error for %s: %s", ticker, exc)
        return empty

    messages = raw.get("messages") or []
    total = len(messages)
    if not total:
        return StockTwitsSentiment(**{**empty, "available": True})

    bullish = sum(
        1 for m in messages
        if (m.get("entities") or {}).get("sentiment", {}).get("basic") == "Bullish"
    )
    bearish = sum(
        1 for m in messages
        if (m.get("entities") or {}).get("sentiment", {}).get("basic") == "Bearish"
    )

    b_ratio = round(bullish / total, 3)
    r_ratio = round(bearish / total, 3)
    return StockTwitsSentiment(
        ticker=ticker,
        bullish_ratio=b_ratio,
        bearish_ratio=r_ratio,
        net_sentiment=round(b_ratio - r_ratio, 3),
        total_messages=total,
        available=True,
    )
