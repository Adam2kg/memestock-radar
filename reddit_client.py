"""
Reddit data fetcher and sentiment analyzer.
Fetches hot posts from memestock subreddits, extracts tickers, and scores sentiment.
"""

import os
import re
import time
import threading
from collections import defaultdict
from datetime import datetime

import praw
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from dotenv import load_dotenv

from tickers import ALL_TICKERS, STOPWORDS
from utils import get_logger

log = get_logger("reddit_client")

# Subreddits where we scan for BanBet calls
_BANBET_SUBREDDITS = {"wallstreetbets"}
# Cap comments scanned per post for banbet (avoids deep tree fetches)
_BANBET_COMMENT_LIMIT = 25

load_dotenv()

# ── Configuration ────────────────────────────────────────────────────────────

SUBREDDITS = [
    "wallstreetbets",
    "Superstonk",
    "stocks",
    "investing",
    "pennystocks",
    "options",
    "StockMarket",
    "ValueInvesting",
]

CACHE_TTL = int(os.getenv("CACHE_TTL", "300"))
POST_LIMIT = int(os.getenv("POST_LIMIT", "50"))

# Regex: 1-5 uppercase letters, word-boundary anchored, optionally preceded by $
TICKER_RE = re.compile(r"(?<![A-Za-z])\$?([A-Z]{1,5})(?![A-Za-z])")

# ── Shared state (cache) ─────────────────────────────────────────────────────

_cache: dict = {}
_cache_lock = threading.Lock()
_last_fetch: dict[str, float] = {}
_analyzer = SentimentIntensityAnalyzer()


def _make_reddit() -> praw.Reddit:
    return praw.Reddit(
        client_id=os.getenv("REDDIT_CLIENT_ID"),
        client_secret=os.getenv("REDDIT_CLIENT_SECRET"),
        user_agent=os.getenv("REDDIT_USER_AGENT", "MemestockRadar/1.0"),
        read_only=True,
    )


# ── Ticker extraction ────────────────────────────────────────────────────────

def extract_tickers(text: str) -> list[str]:
    """Return deduplicated list of tickers found in text."""
    candidates = TICKER_RE.findall(text.upper())
    found = []
    seen = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        if c in STOPWORDS:
            continue
        if c in ALL_TICKERS or len(c) >= 2:
            found.append(c)
    return found


# ── Sentiment ────────────────────────────────────────────────────────────────

def score_text(text: str) -> float:
    """Return compound VADER score (-1 very negative … +1 very positive)."""
    if not text or not text.strip():
        return 0.0
    return _analyzer.polarity_scores(text)["compound"]


def label(score: float) -> str:
    if score >= 0.05:
        return "bullish"
    if score <= -0.05:
        return "bearish"
    return "neutral"


# ── BanBet scanning ──────────────────────────────────────────────────────────

def _scan_banbets_in_post(post, post_url: str, prefetched_comments: list | None = None) -> int:
    """
    Scan a PRAW post (body + top comments) for BanBet calls and persist any
    new ones.  Returns the count of new bets appended.

    Pass prefetched_comments (already retrieved via replace_more) to avoid
    a second API round-trip — replace_more is called exactly once per post
    in the main fetch loop and shared here.
    """
    try:
        from banbet_client import parse_banbet, append_bet
    except ImportError:
        return 0

    added = 0

    # 1. Scan post body
    body = post.selftext or ""
    if body:
        bet = parse_banbet(body, comment_id=f"post_{post.id}",
                           author=str(getattr(post.author, "name", "unknown")),
                           post_url=post_url)
        if bet and append_bet(bet):
            added += 1

    # 2. Scan pre-fetched top comments (no extra API call)
    comments = prefetched_comments or []
    for comment in comments[:_BANBET_COMMENT_LIMIT]:
        if not getattr(comment, "body", None):
            continue
        author_name = str(getattr(comment.author, "name", "unknown"))
        try:
            bet = parse_banbet(comment.body, comment_id=comment.id,
                               author=author_name, post_url=post_url)
            if bet and append_bet(bet):
                added += 1
        except Exception as exc:
            log.debug("BanBet comment parse failed for %s: %s", comment.id, exc)

    return added


# ── Core fetch logic ─────────────────────────────────────────────────────────

def fetch_subreddit(subreddit_name: str, force: bool = False) -> dict:
    """
    Fetch and analyse a single subreddit.
    Returns a dict with per-ticker aggregates and a list of recent posts.
    Results are cached for CACHE_TTL seconds.
    """
    with _cache_lock:
        age = time.time() - _last_fetch.get(subreddit_name, 0)
        if not force and age < CACHE_TTL and subreddit_name in _cache:
            return _cache[subreddit_name]

    reddit = _make_reddit()
    sub = reddit.subreddit(subreddit_name)

    ticker_data: dict[str, dict] = defaultdict(lambda: {
        "mentions": 0,
        "scores": [],
        "posts": [],
        "upvotes": 0,
        "comments": 0,
    })

    recent_posts = []
    sub_total_comments = 0
    sub_total_posts = 0

    do_banbet = subreddit_name in _BANBET_SUBREDDITS
    banbet_new = 0

    try:
        for post in sub.hot(limit=POST_LIMIT):
            title = post.title or ""
            body = post.selftext or ""
            full_text = f"{title} {body}"
            num_comments = getattr(post, "num_comments", 0) or 0
            sub_total_comments += num_comments
            sub_total_posts += 1

            tickers = extract_tickers(full_text)
            post_score = score_text(full_text)
            upvotes = post.score or 0
            post_url = f"https://reddit.com{post.permalink}"

            if not tickers and not do_banbet:
                continue

            # Fetch top comments exactly once per post — shared for both
            # sentiment scoring and BanBet scanning (avoids double API calls).
            comment_texts = []
            top_comments: list = []
            try:
                post.comments.replace_more(limit=0)
                top_comments = list(post.comments)[:_BANBET_COMMENT_LIMIT]
                for c in top_comments[:10]:
                    comment_texts.append(c.body or "")
            except Exception:
                pass

            # BanBet scan (WSB only) — reuses already-fetched comments
            if do_banbet:
                banbet_new += _scan_banbets_in_post(post, post_url,
                                                    prefetched_comments=top_comments)

            if not tickers:
                continue

            comment_score = score_text(" ".join(comment_texts)) if comment_texts else post_score
            combined_score = 0.6 * post_score + 0.4 * comment_score

            for ticker in tickers:
                td = ticker_data[ticker]
                td["mentions"] += 1
                td["scores"].append(combined_score)
                td["upvotes"] += upvotes
                td["comments"] += num_comments
                if len(td["posts"]) < 5:
                    td["posts"].append({
                        "title": title,
                        "url": post_url,
                        "score": round(combined_score, 3),
                        "upvotes": upvotes,
                        "label": label(combined_score),
                        "created": datetime.utcfromtimestamp(post.created_utc).strftime("%Y-%m-%d %H:%M UTC"),
                    })

            recent_posts.append({
                "title": title,
                "url": post_url,
                "upvotes": upvotes,
                "tickers": tickers,
                "sentiment": label(post_score),
                "score": round(post_score, 3),
                "created": datetime.utcfromtimestamp(post.created_utc).strftime("%Y-%m-%d %H:%M UTC"),
            })

    except Exception as e:
        return {"error": str(e), "subreddit": subreddit_name, "tickers": {}, "posts": []}

    sub_avg_comments = (sub_total_comments / sub_total_posts) if sub_total_posts else 0.0

    # Aggregate ticker stats
    aggregated = {}
    for ticker, td in ticker_data.items():
        scores = td["scores"]
        avg_score = sum(scores) / len(scores) if scores else 0.0
        avg_ticker_comments = td["comments"] / td["mentions"] if td["mentions"] else 0.0
        engagement_ratio = (avg_ticker_comments / sub_avg_comments) if sub_avg_comments else 1.0
        aggregated[ticker] = {
            "ticker": ticker,
            "mentions": td["mentions"],
            "sentiment_score": round(avg_score, 3),
            "sentiment_label": label(avg_score),
            "upvotes": td["upvotes"],
            "comments": td["comments"],
            "engagement_ratio": round(engagement_ratio, 3),
            "posts": td["posts"],
            "hype_index": round(td["mentions"] * (1 + max(avg_score, 0)) * (1 + min(td["upvotes"] / 1000, 5)), 2),
        }

    result = {
        "subreddit": subreddit_name,
        "fetched_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "post_count": len(recent_posts),
        "tickers": aggregated,
        "recent_posts": recent_posts[:20],
        "banbets_found": banbet_new,  # new BanBets appended this fetch (0 for non-WSB subs)
    }

    with _cache_lock:
        _cache[subreddit_name] = result
        _last_fetch[subreddit_name] = time.time()

    return result


def fetch_all(force: bool = False) -> dict:
    """
    Fetch all configured subreddits and return a combined cross-subreddit analysis.
    """
    results = {}
    for sub in SUBREDDITS:
        results[sub] = fetch_subreddit(sub, force=force)
        time.sleep(1)  # polite pause between subreddits — PRAW rate-limits per request,
                       # this adds a subreddit-level delay to stay well within limits

    # Cross-subreddit aggregation
    cross: dict[str, dict] = defaultdict(lambda: {
        "ticker": "",
        "total_mentions": 0,
        "total_upvotes": 0,
        "total_comments": 0,
        "weighted_score_sum": 0.0,
        "weighted_engagement_sum": 0.0,
        "mention_weight_sum": 0.0,
        "subreddit_breakdown": {},
        "top_posts": [],
    })

    for sub_name, data in results.items():
        if "error" in data:
            continue
        for ticker, td in data["tickers"].items():
            entry = cross[ticker]
            entry["ticker"] = ticker
            entry["total_mentions"] += td["mentions"]
            entry["total_upvotes"] += td["upvotes"]
            entry["total_comments"] += td.get("comments", 0)
            # weight by mentions for average sentiment + engagement
            entry["weighted_score_sum"] += td["sentiment_score"] * td["mentions"]
            entry["weighted_engagement_sum"] += td.get("engagement_ratio", 1.0) * td["mentions"]
            entry["mention_weight_sum"] += td["mentions"]
            entry["subreddit_breakdown"][sub_name] = {
                "mentions": td["mentions"],
                "sentiment_score": td["sentiment_score"],
                "sentiment_label": td["sentiment_label"],
                "hype_index": td["hype_index"],
                "engagement_ratio": td.get("engagement_ratio", 1.0),
            }
            entry["top_posts"].extend(td["posts"])

    # Compute final scores
    trending = []
    for ticker, entry in cross.items():
        if entry["mention_weight_sum"] == 0:
            continue
        avg_sentiment = entry["weighted_score_sum"] / entry["mention_weight_sum"]
        avg_engagement = entry["weighted_engagement_sum"] / entry["mention_weight_sum"]
        sub_count = len(entry["subreddit_breakdown"])
        # Spread bonus: tickers mentioned across multiple subs get boosted
        spread_multiplier = 1 + 0.2 * (sub_count - 1)
        hype = round(entry["total_mentions"] * (1 + max(avg_sentiment, 0)) * spread_multiplier, 2)

        trending.append({
            "ticker": ticker,
            "total_mentions": entry["total_mentions"],
            "total_upvotes": entry["total_upvotes"],
            "total_comments": entry["total_comments"],
            "avg_sentiment": round(avg_sentiment, 3),
            "engagement_ratio": round(avg_engagement, 3),
            "sentiment_label": label(avg_sentiment),
            "subreddit_count": sub_count,
            "hype_index": hype,
            "subreddit_breakdown": entry["subreddit_breakdown"],
            "top_posts": sorted(entry["top_posts"], key=lambda p: p["upvotes"], reverse=True)[:5],
        })

    trending.sort(key=lambda x: x["hype_index"], reverse=True)

    return {
        "subreddits": results,
        "trending": trending[:50],
        "fetched_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "cache_ttl": CACHE_TTL,
    }
