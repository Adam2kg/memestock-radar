"""
Composite scoring + 2x2 quadrant + confidence + momentum.

Composite = w_hype * norm_hype
          + w_red_sent * |reddit_sent|
          + w_news_sent * |news_sent|
          + w_momentum * momentum
all multiplied by the quadrant weight (bull-bull, bear-bear, etc.) and signed
by the dominant direction.

Confidence factors:
  - signal agreement (Reddit vs News)
  - news volume sufficiency
  - subreddit spread
  - engagement ratio (organic discussion vs hype-only)
"""

from __future__ import annotations

from typing import Iterable, List

from models import RedditSignal, NewsSignal, TradeCandidate
from utils import get_logger

log = get_logger("scorer")


# ── Quadrant logic ───────────────────────────────────────────────────────────

def _quadrant(reddit: RedditSignal, news: NewsSignal, matrix: dict) -> tuple[str, str, float, str]:
    """
    Returns (direction, interpretation, weight, mood_pair).
    direction: long | short | contrarian_long | fade_retail | hold
    """
    r_bull, r_bear = reddit.is_bullish, reddit.is_bearish
    n_bull, n_bear = news.is_bullish, news.is_bearish

    if r_bull and n_bull:
        return "long", "Reddit bullish + News bullish — confluence long", float(matrix.get("bull_bull", 1.0)), "++"
    if r_bear and n_bear:
        return "short", "Reddit bearish + News bearish — confluence short", float(matrix.get("bear_bear", 1.0)), "--"
    if r_bull and n_bear:
        return "contrarian_long", "Reddit bullish + News bearish — early retail / contrarian", float(matrix.get("bull_bear", 0.4)), "+-"
    if r_bear and n_bull:
        return "fade_retail", "Reddit bearish + News bullish — fade-retail setup", float(matrix.get("bear_bull", 0.3)), "-+"
    return "hold", "Mixed / neutral signals — no edge", 0.2, "00"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalize_hype(hype: float, max_hype: float) -> float:
    if max_hype <= 0:
        return 0.0
    return min(hype / max_hype, 1.0)


def _confidence(reddit: RedditSignal, news: NewsSignal, mood_pair: str, filters: dict) -> float:
    score = 0.0

    # Agreement (highest weight)
    score += 0.40 if mood_pair in ("++", "--") else 0.15 if mood_pair in ("+-", "-+") else 0.0

    # News volume sufficiency
    min_v = int(filters.get("min_news_volume", 2))
    if news.volume >= min_v * 2:
        score += 0.20
    elif news.volume >= min_v:
        score += 0.12
    else:
        score += 0.0

    # Subreddit spread
    min_s = int(filters.get("min_subreddit_spread", 2))
    if reddit.subreddit_count >= min_s + 1:
        score += 0.20
    elif reddit.subreddit_count >= min_s:
        score += 0.12

    # Engagement ratio (organic discussion)
    if reddit.engagement_ratio >= 1.5:
        score += 0.20
    elif reddit.engagement_ratio >= 1.0:
        score += 0.10

    return round(min(score, 1.0), 3)


def _momentum(ticker: str, today_score: float, history_window: List[dict], window_days: int) -> float:
    """Compare today's reddit sentiment against rolling avg from history."""
    if not history_window:
        return 0.0
    past = [h.get("avg_sentiment", 0.0) for h in history_window
            if h.get("ticker") == ticker][-window_days:]
    if not past:
        return 0.0
    avg = sum(past) / len(past)
    return round(today_score - avg, 3)


# ── Public API ───────────────────────────────────────────────────────────────

def score_candidates(
    reddit_signals: Iterable[RedditSignal],
    news_signals: dict[str, NewsSignal],
    cfg: dict,
    history: List[dict] | None = None,
) -> List[TradeCandidate]:
    """Build TradeCandidates for every ticker, sorted by composite score desc."""
    s = cfg["scoring"]
    weights = s["weights"]
    matrix = s["matrix"]
    filters = s["filters"]
    momentum_window = int(s["momentum"]["window_days"])
    history = history or []

    reddit_list = list(reddit_signals)
    if not reddit_list:
        return []
    max_hype = max((r.hype_index for r in reddit_list), default=1.0)

    candidates: List[TradeCandidate] = []
    for r in reddit_list:
        n = news_signals.get(r.ticker, NewsSignal(ticker=r.ticker))
        direction, interp, q_weight, mood = _quadrant(r, n, matrix)
        norm_hype = _normalize_hype(r.hype_index, max_hype)
        mom = _momentum(r.ticker, r.sentiment_score, history, momentum_window)

        st_contribution = 0.0
        st_weight = float(weights.get("stocktwits_sentiment", 0.0))
        if st_weight > 0:
            try:
                from stocktwits_client import fetch_stocktwits
                st = fetch_stocktwits(r.ticker)
                if st["available"] and st["total_messages"] > 0:
                    st_contribution = st_weight * abs(st["net_sentiment"])
            except Exception:
                pass

        magnitude = (
            weights["reddit_hype"]        * norm_hype
            + weights["reddit_sentiment"] * abs(r.sentiment_score)
            + weights["news_sentiment"]   * abs(n.sentiment_weighted)
            + weights["momentum"]         * abs(mom)
            + st_contribution
        ) * q_weight

        # Sign by dominant direction
        sign = 0
        if direction in ("long", "contrarian_long"):
            sign = 1
        elif direction in ("short", "fade_retail"):
            sign = -1
        composite = round(sign * magnitude, 4)

        confidence = _confidence(r, n, mood, filters)
        rationale = _rationale(r, n, direction, mom, confidence)

        candidates.append(TradeCandidate(
            ticker=r.ticker,
            reddit=r,
            news=n,
            composite_score=composite,
            confidence=confidence,
            direction=direction,
            interpretation=interp,
            momentum=mom,
            rationale=rationale,
        ))

    candidates.sort(key=lambda c: (c.confidence, abs(c.composite_score)), reverse=True)
    return candidates


def _rationale(r: RedditSignal, n: NewsSignal, direction: str, momentum: float, confidence: float) -> str:
    parts = [
        f"{r.mentions} Reddit mentions across {r.subreddit_count} subs",
        f"Reddit sentiment {r.sentiment_score:+.2f} ({r.sentiment_label})",
        f"News sentiment {n.sentiment_weighted:+.2f} on {n.volume} articles",
        f"engagement {r.engagement_ratio:.2f}x baseline",
    ]
    if abs(momentum) >= 0.05:
        parts.append(f"momentum {momentum:+.2f} vs prior days")
    parts.append(f"direction={direction}, confidence={confidence:.2f}")
    return "; ".join(parts)


def find_hidden_gems(
    candidates: List[TradeCandidate],
    cfg: dict,
    history: List[dict] | None = None,
) -> List[TradeCandidate]:
    """
    Surface low-volume Reddit tickers with real news coverage + organic engagement.

    A 'gem' is a ticker the main trade filter would reject for low Reddit volume,
    but where *something else* says it's worth a look:
      - news volume + sentiment exist (not just one rogue Reddit post)
      - engagement ratio is above baseline (the few mentions got discussion)
      - optionally, the ticker is novel (not seen in recent history)

    Gems are scored independently of the main composite — we don't want them
    competing on Reddit hype, since by definition they have none.
    """
    g = cfg.get("hidden_gems", {})
    if not g.get("enabled", True):
        return []

    history = history or []
    seen = _seen_tickers(history, days=int(g.get("novelty_lookback_days", 7)))

    max_mentions = int(g.get("max_reddit_mentions", 3))
    min_news_vol = int(g.get("min_news_volume", 2))
    min_news_sent = float(g.get("min_news_sentiment", 0.10))
    min_engagement = float(g.get("min_engagement_ratio", 1.0))
    require_novelty = bool(g.get("require_novelty", False))

    gems: List[TradeCandidate] = []
    for c in candidates:
        if c.reddit.mentions > max_mentions:
            continue
        if c.news.volume < min_news_vol:
            continue
        if abs(c.news.sentiment_weighted) < min_news_sent:
            continue
        if c.reddit.engagement_ratio < min_engagement:
            continue
        is_novel = c.ticker not in seen
        if require_novelty and not is_novel:
            continue

        # Gem-specific scoring: news-led, novelty-boosted, engagement-aware
        score = (
            0.45 * abs(c.news.sentiment_weighted)
            + 0.20 * min(c.news.volume / 10.0, 1.0)
            + 0.15 * min(c.reddit.engagement_ratio / 3.0, 1.0)
            + 0.20 * (1.0 if is_novel else 0.0)
        )
        c.is_novel = is_novel
        c.gem_score = round(score, 3)
        gems.append(c)

    gems.sort(key=lambda c: c.gem_score, reverse=True)
    return gems[: int(g.get("max_picks", 3))]


def _seen_tickers(history: List[dict], days: int) -> set[str]:
    """Tickers that appear in the last `days` of history."""
    if not history:
        return set()
    # history rows are append-ordered; assume each day has many rows.
    # Take the trailing slice generously and de-dup by ticker.
    cutoff_rows = history[-days * 100:]
    return {row.get("ticker") for row in cutoff_rows if row.get("ticker")}


def filter_candidates(candidates: List[TradeCandidate], cfg: dict) -> List[TradeCandidate]:
    """Apply hard filters from config."""
    f = cfg["scoring"]["filters"]
    out = []
    for c in candidates:
        if c.reddit.mentions < f["min_reddit_mentions"]:
            continue
        if c.reddit.subreddit_count < f["min_subreddit_spread"]:
            continue
        if c.news.volume < f["min_news_volume"]:
            continue
        if c.confidence < f["confidence_floor"]:
            continue
        if c.direction == "hold":
            continue
        out.append(c)
    return out
