"""
claude_analyst.py — Claude-powered trade-thesis and gem-note generator.

Uses Anthropic's Claude Opus 4.7 with adaptive thinking and prompt caching.
The stable system prompt is cached across all ticker calls in a single daily run,
so you pay for the cache write once and benefit on the remaining 2-4 calls.

If the API key is absent or any call fails, every function falls back to the
pre-assembled `candidate.rationale` / `gem_score` string — the agent always sends.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models.trade_candidate import TradeCandidate

_client = None  # lazy singleton — avoids import error if anthropic not installed


def _get_client():
    global _client
    if _client is None:
        import anthropic  # noqa: PLC0415
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


# ── Stable system prompt — cached across all calls in one daily run ───────────

SYSTEM_PROMPT = """\
You are a concise, no-nonsense equity analyst assistant specialising in retail-driven momentum stocks.
You receive structured signal data (Reddit sentiment, news sentiment, price momentum, confidence score)
for a single ticker and write a tight, actionable analyst note.

Rules:
- Be factual and grounded in the data provided. Never fabricate price targets or news not given to you.
- Avoid hype language ("moon", "rocket", "diamond hands"). Use professional financial phrasing.
- Flag any contradictions between Reddit sentiment and news sentiment explicitly.
- State direction and conviction in the first sentence.
- Do not repeat the raw numbers verbatim — synthesise them into narrative.
- If confidence is below 0.60 include a brief caveat.
"""


def _build_candidate_context(c: "TradeCandidate") -> str:
    """Format a TradeCandidate into a compact user message for Claude."""
    lines = [
        f"Ticker: ${c.ticker}",
        f"Suggested direction: {c.direction.upper()}",
        f"Confidence: {c.confidence:.2f}  |  Composite score: {c.composite_score:+.3f}",
        f"Signal quadrant: {c.interpretation}",
        "",
        "Reddit signals:",
        f"  Mentions: {c.reddit.mentions}  |  Hype index: {c.reddit.hype_index:.2f}",
        f"  Sentiment: {c.reddit.sentiment_score:+.3f} ({c.reddit.sentiment_label})",
        f"  Subreddits: {c.reddit.subreddit_count}  |  Engagement ratio: {c.reddit.engagement_ratio:.2f}x",
        "",
        "News signals:",
        f"  Articles (last 3d): {c.news.volume}  |  Avg sentiment: {c.news.sentiment_avg:+.3f}",
        f"  Recency-weighted sentiment: {c.news.sentiment_weighted:+.3f}",
    ]

    if c.news.articles:
        lines.append("")
        lines.append("Top headlines (newest first):")
        for a in c.news.articles[:5]:
            src = a.get("source", "")
            sent = a.get("sentiment", 0.0)
            lines.append(f"  [{sent:+.2f}] {a['headline']} ({src})")

    if c.momentum != 0.0:
        lines.append("")
        lines.append(f"3-day momentum delta: {c.momentum:+.3f}")

    return "\n".join(lines)


def _build_gem_context(g: "TradeCandidate") -> str:
    """Compact context for a hidden-gem note (less detail needed)."""
    lines = [
        f"Ticker: ${g.ticker}  [HIDDEN GEM — low Reddit visibility, real news signal]",
        f"Reddit mentions: {g.reddit.mentions}  |  Reddit sentiment: {g.reddit.sentiment_score:+.3f}",
        f"News articles: {g.news.volume}  |  News sentiment (weighted): {g.news.sentiment_weighted:+.3f}",
        f"Engagement ratio: {g.reddit.engagement_ratio:.2f}x baseline",
        f"Gem score: {g.gem_score:.2f}  |  Novel ticker: {g.is_novel}",
    ]
    if g.news.articles:
        a = g.news.articles[0]
        lines.append(f"Lead headline: {a['headline']} ({a.get('source', '')})")
    return "\n".join(lines)


# ── Public API ────────────────────────────────────────────────────────────────

def _acc(token_acc: dict | None, response) -> None:
    """Accumulate token usage from a response into an optional accumulator dict."""
    if token_acc is None:
        return
    try:
        u = response.usage
        token_acc["input"]  = token_acc.get("input",  0) + (u.input_tokens  or 0)
        token_acc["output"] = token_acc.get("output", 0) + (u.output_tokens or 0)
        token_acc["calls"]  = token_acc.get("calls",  0) + 1
    except Exception:
        pass


def generate_thesis(candidate: "TradeCandidate", token_acc: dict | None = None) -> str:
    """
    Return a 2-3 sentence trade thesis for a main pick.

    Falls back to candidate.rationale on any error (missing key, API failure, etc.).
    Pass an optional mutable dict as ``token_acc`` to accumulate usage stats:
        {"input": int, "output": int, "calls": int}
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or api_key in ("your_anthropic_api_key_here", ""):
        return candidate.rationale

    try:
        client = _get_client()
        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=300,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"{_build_candidate_context(candidate)}\n\n"
                        "Write a 2-3 sentence trade thesis. "
                        "Lead with direction and conviction. "
                        "End with the key risk or caveat."
                    ),
                }
            ],
        )
        _acc(token_acc, response)
        # Extract the text block (thinking blocks are separate)
        for block in response.content:
            if block.type == "text":
                return block.text.strip()
        return candidate.rationale

    except Exception as exc:  # noqa: BLE001
        # Log but never crash the agent
        try:
            from utils import get_logger
            get_logger("claude_analyst").warning("Thesis generation failed for %s: %s", candidate.ticker, exc)
        except Exception:
            pass
        return candidate.rationale


def generate_gem_note(gem: "TradeCandidate", token_acc: dict | None = None) -> str:
    """
    Return a single-sentence note explaining why this ticker is a hidden gem.

    Falls back to a brief assembled string on any error.
    Pass an optional mutable dict as ``token_acc`` to accumulate usage stats.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or api_key in ("your_anthropic_api_key_here", ""):
        return _gem_fallback(gem)

    try:
        client = _get_client()
        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=150,
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"{_build_gem_context(gem)}\n\n"
                        "Write exactly one sentence explaining why this ticker is worth watching "
                        "despite low Reddit visibility. Be specific about the news or engagement signal."
                    ),
                }
            ],
        )
        _acc(token_acc, response)
        for block in response.content:
            if block.type == "text":
                return block.text.strip()
        return _gem_fallback(gem)

    except Exception as exc:  # noqa: BLE001
        try:
            from utils import get_logger
            get_logger("claude_analyst").warning("Gem note failed for %s: %s", gem.ticker, exc)
        except Exception:
            pass
        return _gem_fallback(gem)


def analyze_batch(candidates: "list[TradeCandidate]", token_acc: dict | None = None) -> list[dict]:
    """
    Analyze multiple candidates in a single API call with structured JSON output.

    Returns a list of dicts matching this schema per ticker:
        {
            "ticker": str,
            "stance": "bull" | "bear" | "neutral",
            "confidence": float,   # 0.0–1.0
            "thesis": str,         # 1-2 sentence summary
            "risks": [str],        # up to 3 risk strings
        }

    Falls back to assembled rationale dicts on any error.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or api_key in ("your_anthropic_api_key_here", ""):
        return [_batch_fallback(c) for c in candidates]

    if not candidates:
        return []

    if not api_key or api_key in ("your_anthropic_api_key_here", ""):
        return [_batch_fallback(c) for c in candidates]

    contexts = "\n\n---\n\n".join(_build_candidate_context(c) for c in candidates)
    tickers_csv = ", ".join(f"${c.ticker}" for c in candidates)

    user_msg = (
        f"Analyze these {len(candidates)} tickers: {tickers_csv}\n\n"
        f"{contexts}\n\n"
        "Return a JSON array — one object per ticker in the same order — with keys: "
        '"ticker", "stance" (bull|bear|neutral), "confidence" (0.0-1.0), '
        '"thesis" (1-2 sentences), "risks" (array of up to 3 strings). '
        "Respond with only the JSON array, no markdown fences."
    )

    try:
        client = _get_client()
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
        )
        _acc(token_acc, response)
        for block in response.content:
            if block.type == "text":
                import json as _json
                try:
                    raw = block.text.strip()
                    # Strip markdown code fences if the model wrapped the JSON
                    if raw.startswith("```"):
                        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                    parsed = _json.loads(raw)
                    if isinstance(parsed, list) and len(parsed) == len(candidates):
                        return parsed
                except (_json.JSONDecodeError, ValueError):
                    pass
        return [_batch_fallback(c) for c in candidates]

    except Exception as exc:  # noqa: BLE001
        try:
            from utils import get_logger
            get_logger("claude_analyst").warning("Batch analysis failed: %s", exc)
        except Exception:
            pass
        return [_batch_fallback(c) for c in candidates]


def _batch_fallback(c: "TradeCandidate") -> dict:
    stance = (
        "bull" if c.direction in ("long", "contrarian_long")
        else "bear" if c.direction in ("short", "fade_retail")
        else "neutral"
    )
    return {
        "ticker": c.ticker,
        "stance": stance,
        "confidence": c.confidence,
        "thesis": c.rationale,
        "risks": [],
    }


def _gem_fallback(gem: "TradeCandidate") -> str:
    """Assembled gem note when Claude is unavailable."""
    direction = "bullish" if gem.news.sentiment_weighted >= 0 else "bearish"
    return (
        f"Low Reddit noise ({gem.reddit.mentions} mention(s)) but {gem.news.volume} news articles "
        f"with {direction} sentiment ({gem.news.sentiment_weighted:+.2f}) "
        f"and {gem.reddit.engagement_ratio:.1f}x engagement ratio."
    )
