"""
News fetcher + sentiment scorer.

Pluggable backends:
  - finnhub      (default; native sentiment + buzz when available)
  - newsapi      (https://newsapi.org)
  - alphavantage (https://www.alphavantage.co — news_sentiment endpoint)

All backends return a NewsSignal per ticker with normalized fields:
  sentiment_avg, sentiment_weighted (recency-weighted), volume.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import List

import requests
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from models import NewsSignal
from utils import get_logger

log = get_logger("news")
_analyzer = SentimentIntensityAnalyzer()

FINNHUB_BASE = "https://finnhub.io/api/v1"
NEWSAPI_BASE = "https://newsapi.org/v2"
ALPHAVANTAGE_BASE = "https://www.alphavantage.co/query"


# ── Sentiment helpers ────────────────────────────────────────────────────────

def _vader(text: str) -> float:
    if not text or not text.strip():
        return 0.0
    return _analyzer.polarity_scores(text)["compound"]


def _recency_weight(article_ts: float, now: float, window_seconds: float) -> float:
    """Linear decay 1.0 (now) → 0.2 (window edge). Older than window → 0.2."""
    age = max(now - article_ts, 0.0)
    if age >= window_seconds:
        return 0.2
    return 1.0 - 0.8 * (age / window_seconds)


def _aggregate(ticker: str, articles: List[dict], lookback_days: int) -> NewsSignal:
    """Articles must be normalized: {headline, summary, ts, sentiment?}."""
    if not articles:
        return NewsSignal(ticker=ticker)

    now = time.time()
    window = lookback_days * 86400
    scores = []
    weighted_num, weight_den = 0.0, 0.0

    for a in articles:
        s = a.get("sentiment")
        if s is None:
            text = f"{a.get('headline','')} {a.get('summary','')}"
            s = _vader(text)
            a["sentiment"] = s
        scores.append(s)
        w = _recency_weight(a.get("ts", now), now, window)
        weighted_num += s * w
        weight_den += w

    return NewsSignal(
        ticker=ticker,
        articles=articles,
        sentiment_avg=round(sum(scores) / len(scores), 3),
        sentiment_weighted=round(weighted_num / weight_den if weight_den else 0.0, 3),
        volume=len(articles),
    )


# ── Finnhub backend ──────────────────────────────────────────────────────────

def _fetch_finnhub(ticker: str, api_key: str, lookback_days: int, max_articles: int) -> NewsSignal:
    if not api_key or api_key.startswith("YOUR_"):
        log.warning("Finnhub key missing — returning empty signal for %s", ticker)
        return NewsSignal(ticker=ticker)

    today = datetime.now(timezone.utc).date()
    frm = (today - timedelta(days=lookback_days)).isoformat()
    to = today.isoformat()

    try:
        r = requests.get(
            f"{FINNHUB_BASE}/company-news",
            params={"symbol": ticker, "from": frm, "to": to, "token": api_key},
            timeout=10,
        )
        r.raise_for_status()
        raw = r.json() or []
    except Exception as e:
        log.error("Finnhub company-news failed for %s: %s", ticker, e)
        raw = []

    raw = sorted(raw, key=lambda a: a.get("datetime", 0), reverse=True)[:max_articles]
    articles = [{
        "headline": a.get("headline", ""),
        "summary": a.get("summary", ""),
        "url": a.get("url", ""),
        "source": a.get("source", ""),
        "ts": float(a.get("datetime", 0)),
    } for a in raw]

    sig = _aggregate(ticker, articles, lookback_days)

    # Native finnhub sentiment (if available)
    try:
        r2 = requests.get(
            f"{FINNHUB_BASE}/news-sentiment",
            params={"symbol": ticker, "token": api_key},
            timeout=10,
        )
        r2.raise_for_status()
        ns = r2.json() or {}
        sig.finnhub_buzz = float(ns.get("buzz", {}).get("buzz", 0.0) or 0.0)
        sig.finnhub_company_score = float(ns.get("companyNewsScore", 0.0) or 0.0)
    except Exception as e:
        log.debug("Finnhub news-sentiment unavailable for %s: %s", ticker, e)

    return sig


# ── NewsAPI backend ──────────────────────────────────────────────────────────

def _fetch_newsapi(ticker: str, api_key: str, lookback_days: int, max_articles: int) -> NewsSignal:
    if not api_key:
        log.warning("NewsAPI key missing — empty signal for %s", ticker)
        return NewsSignal(ticker=ticker)

    frm = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    try:
        r = requests.get(
            f"{NEWSAPI_BASE}/everything",
            params={
                "q": f"\"{ticker}\" stock",
                "from": frm,
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": max_articles,
                "apiKey": api_key,
            },
            timeout=10,
        )
        r.raise_for_status()
        raw = r.json().get("articles", [])
    except Exception as e:
        log.error("NewsAPI failed for %s: %s", ticker, e)
        raw = []

    articles = []
    for a in raw:
        ts = 0.0
        if a.get("publishedAt"):
            try:
                ts = datetime.fromisoformat(a["publishedAt"].replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
        articles.append({
            "headline": a.get("title", ""),
            "summary": a.get("description", ""),
            "url": a.get("url", ""),
            "source": (a.get("source") or {}).get("name", ""),
            "ts": ts,
        })
    return _aggregate(ticker, articles, lookback_days)


# ── Alpha Vantage backend ────────────────────────────────────────────────────

def _fetch_alphavantage(ticker: str, api_key: str, lookback_days: int, max_articles: int) -> NewsSignal:
    if not api_key:
        log.warning("Alpha Vantage key missing — empty signal for %s", ticker)
        return NewsSignal(ticker=ticker)

    try:
        r = requests.get(
            ALPHAVANTAGE_BASE,
            params={
                "function": "NEWS_SENTIMENT",
                "tickers": ticker,
                "limit": max_articles,
                "apikey": api_key,
            },
            timeout=15,
        )
        r.raise_for_status()
        raw = r.json().get("feed", [])
    except Exception as e:
        log.error("AlphaVantage failed for %s: %s", ticker, e)
        raw = []

    articles = []
    for a in raw:
        ts = 0.0
        if a.get("time_published"):
            try:
                ts = datetime.strptime(a["time_published"], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                pass
        # AV provides per-ticker sentiment in ticker_sentiment[]
        sentiment = None
        for ts_entry in a.get("ticker_sentiment", []):
            if ts_entry.get("ticker") == ticker:
                try:
                    sentiment = float(ts_entry.get("ticker_sentiment_score", 0.0))
                except Exception:
                    sentiment = None
                break
        articles.append({
            "headline": a.get("title", ""),
            "summary": a.get("summary", ""),
            "url": a.get("url", ""),
            "source": a.get("source", ""),
            "ts": ts,
            "sentiment": sentiment,  # native if present, else VADER fallback in _aggregate
        })
    return _aggregate(ticker, articles, lookback_days)


# ── Public API ───────────────────────────────────────────────────────────────

def fetch_news_signal(ticker: str, cfg: dict) -> NewsSignal:
    """Fetch news + sentiment for a single ticker using configured backend.

    API keys are read exclusively from environment variables:
      FINNHUB_API_KEY, NEWSAPI_API_KEY, ALPHAVANTAGE_API_KEY
    They are never stored in config.yaml.
    """
    import os
    n = cfg.get("news", {})
    source = n.get("source", "finnhub")
    lookback = int(n.get("lookback_days", 3))
    maxn = int(n.get("max_articles_per_ticker", 15))

    if source == "finnhub":
        return _fetch_finnhub(ticker, os.getenv("FINNHUB_API_KEY", ""), lookback, maxn)
    if source == "newsapi":
        return _fetch_newsapi(ticker, os.getenv("NEWSAPI_API_KEY", ""), lookback, maxn)
    if source == "alphavantage":
        return _fetch_alphavantage(ticker, os.getenv("ALPHAVANTAGE_API_KEY", ""), lookback, maxn)
    raise ValueError(f"Unknown news source: {source}")


def fetch_news_for_tickers(tickers: List[str], cfg: dict, sleep_between: float = 0.25) -> dict[str, NewsSignal]:
    out: dict[str, NewsSignal] = {}
    for t in tickers:
        out[t] = fetch_news_signal(t, cfg)
        time.sleep(sleep_between)  # gentle rate-limit cushion
    return out
