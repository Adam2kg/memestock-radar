"""
Daily Trade-of-the-Day agent.

Pipeline:
  1. Fetch Reddit signals (cross-subreddit aggregation, force-refresh).
  2. Pull news for the top-N tickers (configured backend).
  3. Score → quadrant + confidence + momentum.
  4. Filter on hard thresholds.
  5. Pick top trade(s), build email, send via Gmail SMTP.
  6. Append today's snapshot to history.jsonl for momentum next run.

Cron-safe entrypoint:
    0 13 * * 1-5  cd /path/to/memestock-radar && /path/to/venv/bin/python daily_agent.py
"""

from __future__ import annotations

import json
import os
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import List

from models import RedditSignal, TradeCandidate
from news_client import fetch_news_for_tickers
from scorer import score_candidates, filter_candidates, find_hidden_gems
from utils import load_config, get_logger
from claude_analyst import generate_thesis, generate_gem_note

log = get_logger("agent")

# ── Claude pricing ($ per million tokens, as of 2025-05) ──────────────────────
# Adjust if Anthropic changes rates.  Used only for the informational footer.
_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7":   {"input": 5.00,  "output": 25.00},
    "claude-sonnet-4-6": {"input": 3.00,  "output": 15.00},
    "default":           {"input": 3.00,  "output": 15.00},
}


def _estimate_cost(token_acc: dict) -> float:
    """Rough total cost estimate in USD from accumulated token counts.

    We can't know which tokens came from which model without more bookkeeping,
    so we use a single blended rate (opus input + sonnet output average).
    Good enough for an informational footer.
    """
    if not token_acc:
        return 0.0
    # Blend: treat all input at opus rate (worst-case), output at sonnet rate
    inp  = token_acc.get("input",  0)
    out  = token_acc.get("output", 0)
    cost = (inp * _PRICING["claude-opus-4-7"]["input"] + out * _PRICING["claude-sonnet-4-6"]["output"]) / 1_000_000
    return round(cost, 6)


# ── Retry helper ─────────────────────────────────────────────────────────────

def _with_retry(fn, *args, retries: int = 3, backoff: float = 2.0, label: str = "", **kwargs):
    """Call fn(*args, **kwargs) up to `retries` times with exponential backoff."""
    for attempt in range(1, retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if attempt == retries:
                raise
            wait = backoff ** (attempt - 1)
            log.warning("%s failed (attempt %d/%d): %s — retrying in %.0fs",
                        label or fn.__name__, attempt, retries, exc, wait)
            time.sleep(wait)


# ── Reddit → RedditSignal adapter ────────────────────────────────────────────

def _to_reddit_signals(trending: List[dict], top_n: int) -> List[RedditSignal]:
    out = []
    for t in trending[:top_n]:
        out.append(RedditSignal(
            ticker=t["ticker"],
            mentions=t["total_mentions"],
            upvotes=t["total_upvotes"],
            sentiment_score=t["avg_sentiment"],
            sentiment_label=t["sentiment_label"],
            subreddit_count=t["subreddit_count"],
            subreddit_breakdown=t["subreddit_breakdown"],
            hype_index=t["hype_index"],
            engagement_ratio=t.get("engagement_ratio", 1.0),
        ))
    return out


# ── History (rolling window) ─────────────────────────────────────────────────

def _load_history(path: Path, days: int) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text().strip().splitlines()
    snapshots = [json.loads(l) for l in lines if l.strip()]
    return snapshots[-days * 100:]  # generous slice; scorer narrows further


def _append_history(path: Path, signals: List[RedditSignal]) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with path.open("a") as f:
        for s in signals:
            f.write(json.dumps({
                "date": today,
                "ticker": s.ticker,
                "mentions": s.mentions,
                "avg_sentiment": s.sentiment_score,
                "hype_index": s.hype_index,
                "subreddit_count": s.subreddit_count,
            }) + "\n")


# ── Email ────────────────────────────────────────────────────────────────────

_DIRECTION_COLOR = {
    "long": "#22c55e",
    "contrarian_long": "#86efac",
    "short": "#ef4444",
    "fade_retail": "#fca5a5",
    "hold": "#94a3b8",
}
_SENTIMENT_COLOR = {
    "bullish": "#22c55e",
    "bearish": "#ef4444",
    "neutral": "#eab308",
}


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _ticker_card_html(c: TradeCandidate, thesis: str, is_top: bool = False) -> str:
    dir_color = _DIRECTION_COLOR.get(c.direction, "#94a3b8")
    border = "2px solid #6366f1" if is_top else "1px solid #252a35"
    return f"""
<div style="background:#1e2330;border:{border};border-radius:10px;padding:16px 20px;margin-bottom:12px">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
    <span style="font-size:22px;font-weight:700;color:#6366f1">${_esc(c.ticker)}</span>
    <span style="background:{dir_color}22;color:{dir_color};padding:3px 10px;border-radius:20px;
                 font-size:12px;font-weight:700;text-transform:uppercase">{_esc(c.direction.replace("_"," "))}</span>
  </div>
  <div style="font-size:11px;color:#64748b;margin-bottom:10px">{_esc(c.interpretation)}</div>
  <div style="display:flex;gap:12px;margin-bottom:12px;flex-wrap:wrap">
    <div style="background:#0d1117;border:1px solid #252a35;border-radius:6px;padding:8px 12px;min-width:80px">
      <div style="font-size:9px;color:#64748b;text-transform:uppercase;letter-spacing:.06em">Confidence</div>
      <div style="font-size:18px;font-weight:700;color:#e2e8f0;margin-top:2px">{c.confidence:.0%}</div>
    </div>
    <div style="background:#0d1117;border:1px solid #252a35;border-radius:6px;padding:8px 12px;min-width:80px">
      <div style="font-size:9px;color:#64748b;text-transform:uppercase;letter-spacing:.06em">Score</div>
      <div style="font-size:18px;font-weight:700;color:#e2e8f0;margin-top:2px">{c.composite_score:+.3f}</div>
    </div>
    <div style="background:#0d1117;border:1px solid #252a35;border-radius:6px;padding:8px 12px;min-width:80px">
      <div style="font-size:9px;color:#64748b;text-transform:uppercase;letter-spacing:.06em">Mentions</div>
      <div style="font-size:18px;font-weight:700;color:#e2e8f0;margin-top:2px">{c.reddit.mentions}</div>
    </div>
    <div style="background:#0d1117;border:1px solid #252a35;border-radius:6px;padding:8px 12px;min-width:80px">
      <div style="font-size:9px;color:#64748b;text-transform:uppercase;letter-spacing:.06em">Reddit sent.</div>
      <div style="font-size:18px;font-weight:700;
                  color:{"#22c55e" if c.reddit.sentiment_score >= 0.05 else "#ef4444" if c.reddit.sentiment_score <= -0.05 else "#eab308"};
                  margin-top:2px">{c.reddit.sentiment_score:+.3f}</div>
    </div>
  </div>
  <div style="font-size:13px;color:#cbd5e1;line-height:1.5;margin-bottom:10px">{_esc(thesis)}</div>
</div>"""


def _token_footer_html(token_acc: dict | None) -> str:
    """Render a small HTML token-cost block for the email footer, or empty string."""
    if not token_acc or token_acc.get("calls", 0) == 0:
        return ""
    cost = _estimate_cost(token_acc)
    inp  = f"{token_acc.get('input',  0):,}"
    out  = f"{token_acc.get('output', 0):,}"
    calls = token_acc.get("calls", 0)
    return f"""
  <div style="margin-top:12px;padding:10px 14px;background:#0d1117;border:1px solid #1e2330;
              border-radius:6px;font-size:10px;color:#475569;font-family:monospace;text-align:left">
    🤖 <strong style="color:#64748b">AI usage this run</strong>:
    {calls} call(s) &nbsp;·&nbsp;
    {inp} input tokens &nbsp;·&nbsp; {out} output tokens &nbsp;·&nbsp;
    est. cost <strong style="color:#94a3b8">≈&thinsp;${cost:.4f}</strong>
    <span style="color:#334155;margin-left:8px">(blended Opus/Sonnet rate)</span>
  </div>"""


def _format_email(
    picks: List[TradeCandidate],
    all_filtered: List[TradeCandidate],
    all_candidates: List[TradeCandidate],
    gems: List[TradeCandidate],
    cfg: dict,
    theses: dict[str, str] | None = None,
    gem_notes: dict[str, str] | None = None,
    token_acc: dict | None = None,
) -> tuple[str, str, str]:
    """
    Build subject, plain-text body, and HTML body for the daily email.

    Returns (subject, plain_body, html_body).
    theses:    {ticker: thesis_text}
    gem_notes: {ticker: note_text}
    token_acc: {"input": int, "output": int, "calls": int} — from Claude API calls
    """
    theses = theses or {}
    gem_notes = gem_notes or {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ── Subject ──────────────────────────────────────────────────────────
    if not picks:
        gem_tag = f" — {len(gems)} hidden gem(s)" if gems else ""
        subject = f"[Memestock Radar] {today} — {cfg['agent']['fallback_message']}{gem_tag}"
    else:
        top = picks[0]
        subject = (f"[Memestock Radar] {today} — "
                   f"{top.direction.upper()} ${top.ticker} (conf {top.confidence:.0%})")

    # ── Summary stats ─────────────────────────────────────────────────────
    conf_values = [c.confidence for c in all_filtered]
    avg_conf = sum(conf_values) / len(conf_values) if conf_values else 0.0
    bull_count = sum(1 for c in all_filtered if c.direction in ("long", "contrarian_long"))
    bear_count = sum(1 for c in all_filtered if c.direction in ("short", "fade_retail"))

    # ── Plain text ────────────────────────────────────────────────────────
    plain_lines: list[str] = [f"MEMESTOCK RADAR — {today}", "=" * 60, ""]

    if not picks:
        plain_lines += [cfg["agent"]["fallback_message"], "",
                        "No tickers cleared the confidence/volume thresholds today."]
    else:
        top = picks[0]
        plain_lines += [
            f"TRADE OF THE DAY",
            f"  ${top.ticker}  {top.direction.upper()}  conf={top.confidence:.0%}  score={top.composite_score:+.3f}",
            f"  {top.interpretation}", "",
            "  " + theses.get(top.ticker, top.rationale), "",
        ]
        if len(picks) > 1:
            plain_lines.append("Other candidates:")
            for c in picks[1:]:
                plain_lines.append(
                    f"  ${c.ticker:<6}  {c.direction:<18}  conf={c.confidence:.0%}  score={c.composite_score:+.3f}")
            plain_lines.append("")

    plain_lines += [
        f"Summary: {len(all_candidates)} scanned → {len(all_filtered)} passed filter "
        f"(bull={bull_count}, bear={bear_count}, avg conf={avg_conf:.0%})",
        "",
    ]

    # Token footer (plain)
    if token_acc and token_acc.get("calls", 0) > 0:
        cost = _estimate_cost(token_acc)
        plain_lines += [
            "─" * 60,
            f"AI usage this run: {token_acc.get('calls',0)} call(s)  "
            f"in={token_acc.get('input',0):,} tok  out={token_acc.get('output',0):,} tok  "
            f"est. cost ≈ ${cost:.4f}",
            "",
        ]

    if gems:
        plain_lines += ["HIDDEN GEMS", "-" * 60]
        for g in gems:
            novelty = " [NEW]" if g.is_novel else ""
            plain_lines.append(
                f"  ${g.ticker}{novelty}  gem={g.gem_score:.2f}  "
                f"mentions={g.reddit.mentions}  news={g.news.volume}")
            note = gem_notes.get(g.ticker)
            if note:
                plain_lines.append(f"  {note}")
        plain_lines.append("")

    plain_body = "\n".join(plain_lines)

    # ── HTML ──────────────────────────────────────────────────────────────
    picks_html = ""
    if not picks:
        picks_html = (
            f'<p style="color:#94a3b8;font-size:14px">{_esc(cfg["agent"]["fallback_message"])}</p>'
            '<p style="color:#64748b">No tickers cleared the confidence/volume thresholds today.</p>'
        )
    else:
        picks_html += '<h2 style="color:#6366f1;font-size:14px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:12px">Trade of the Day</h2>'
        picks_html += _ticker_card_html(picks[0], theses.get(picks[0].ticker, picks[0].rationale), is_top=True)
        if len(picks) > 1:
            picks_html += '<h2 style="color:#6366f1;font-size:14px;text-transform:uppercase;letter-spacing:.08em;margin:16px 0 12px">Other Candidates</h2>'
            for c in picks[1:]:
                picks_html += _ticker_card_html(c, theses.get(c.ticker, c.rationale))

    gems_html = ""
    if gems:
        gem_cards = []
        for g in gems:
            novelty_badge = (' <span style="background:#6366f122;color:#818cf8;padding:2px 6px;'
                             'border-radius:10px;font-size:10px">NEW</span>' if g.is_novel else "")
            note = gem_notes.get(g.ticker, "")
            headline = ""
            if not note and g.news.articles:
                a = g.news.articles[0]
                url = a.get("url", "")
                hl = _esc(a["headline"])
                headline = f'<div style="font-size:12px;color:#94a3b8;margin-top:6px">{"<a href=" + repr(url) + " style=color:#818cf8>" + hl + "</a>" if url else hl}</div>'
            sent_color = "#22c55e" if g.news.sentiment_weighted >= 0.05 else "#ef4444" if g.news.sentiment_weighted <= -0.05 else "#eab308"
            gem_cards.append(f"""
<div style="background:#1e2330;border:1px solid #252a35;border-radius:8px;padding:14px 16px;margin-bottom:10px">
  <div style="font-size:18px;font-weight:700;color:#6366f1;margin-bottom:4px">${_esc(g.ticker)}{novelty_badge}</div>
  <div style="display:flex;gap:16px;font-size:11px;color:#64748b;margin-bottom:6px">
    <span>gem score <strong style="color:#e2e8f0">{g.gem_score:.2f}</strong></span>
    <span>reddit mentions <strong style="color:#e2e8f0">{g.reddit.mentions}</strong></span>
    <span>news <strong style="color:#e2e8f0">{g.news.volume}</strong></span>
    <span>news sent <strong style="color:{sent_color}">{g.news.sentiment_weighted:+.2f}</strong></span>
  </div>
  {('<div style="font-size:13px;color:#cbd5e1">' + _esc(note) + '</div>') if note else headline}
</div>""")
        gems_html = f"""
<h2 style="color:#6366f1;font-size:14px;text-transform:uppercase;letter-spacing:.08em;margin:24px 0 12px">
  Hidden Gems — quiet on Reddit, real news signal
</h2>
{"".join(gem_cards)}"""

    html_body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0d0f14;color:#e2e8f0;font-family:'SF Mono','Fira Code',Consolas,monospace">
<div style="max-width:640px;margin:0 auto;padding:24px 16px">

  <!-- Header -->
  <div style="display:flex;align-items:center;justify-content:space-between;
              border-bottom:1px solid #252a35;padding-bottom:16px;margin-bottom:24px">
    <div style="font-size:20px;font-weight:700;letter-spacing:.05em;color:#6366f1">
      MEMESTOCK<span style="color:#e2e8f0"> RADAR</span>
    </div>
    <div style="font-size:11px;color:#64748b">{today}</div>
  </div>

  <!-- Summary stats bar -->
  <div style="background:#151820;border:1px solid #252a35;border-radius:8px;
              padding:12px 16px;margin-bottom:24px;display:flex;gap:20px;flex-wrap:wrap">
    <div><span style="font-size:9px;color:#64748b;text-transform:uppercase;letter-spacing:.06em;display:block">Scanned</span>
         <strong style="font-size:16px;color:#e2e8f0">{len(all_candidates)}</strong></div>
    <div><span style="font-size:9px;color:#64748b;text-transform:uppercase;letter-spacing:.06em;display:block">Passed Filter</span>
         <strong style="font-size:16px;color:#e2e8f0">{len(all_filtered)}</strong></div>
    <div><span style="font-size:9px;color:#64748b;text-transform:uppercase;letter-spacing:.06em;display:block">Bull / Bear</span>
         <strong style="font-size:16px"><span style="color:#22c55e">{bull_count}</span>&thinsp;/&thinsp;<span style="color:#ef4444">{bear_count}</span></strong></div>
    <div><span style="font-size:9px;color:#64748b;text-transform:uppercase;letter-spacing:.06em;display:block">Avg Confidence</span>
         <strong style="font-size:16px;color:#e2e8f0">{avg_conf:.0%}</strong></div>
    {f'<div><span style="font-size:9px;color:#64748b;text-transform:uppercase;letter-spacing:.06em;display:block">Gems</span><strong style="font-size:16px;color:#818cf8">{len(gems)}</strong></div>' if gems else ''}
  </div>

  <!-- Picks -->
  {picks_html}

  <!-- Hidden gems -->
  {gems_html}

  <!-- Footer -->
  <div style="border-top:1px solid #252a35;margin-top:32px;padding-top:16px;
              font-size:10px;color:#334155;text-align:center">
    Memestock Radar · signals from Reddit + news · not financial advice
  </div>
  {_token_footer_html(token_acc)}

</div>
</body>
</html>"""

    return subject, plain_body, html_body


def _send_email(subject: str, plain_body: str, html_body: str, cfg: dict) -> None:
    """Send email using credentials exclusively from environment variables.

    Required env vars: GMAIL_SENDER, GMAIL_RECIPIENT, GMAIL_APP_PASSWORD
    Optional env vars: SMTP_SERVER (default: smtp.gmail.com), SMTP_PORT (default: 587)
    """
    e = cfg.get("email", {})
    if not e.get("enabled", True):
        log.info("Email disabled — skipping send.")
        return

    sender     = os.environ.get("GMAIL_SENDER", "")
    recipient  = os.environ.get("GMAIL_RECIPIENT", "")
    password   = os.environ.get("GMAIL_APP_PASSWORD", "")
    smtp_server = os.environ.get("SMTP_SERVER", e.get("smtp_server", "smtp.gmail.com"))
    smtp_port   = int(os.environ.get("SMTP_PORT", e.get("smtp_port", 587)))

    if not sender or not recipient or not password:
        log.error("Email credentials missing — set GMAIL_SENDER, GMAIL_RECIPIENT, GMAIL_APP_PASSWORD")
        raise ValueError("Missing email credentials in environment")

    recipients = [r.strip() for r in recipient.split(",") if r.strip()]
    msg = MIMEMultipart("alternative")
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, recipients, msg.as_string())
        log.info("Email sent to %s", ", ".join(recipients))
    except Exception as ex:
        log.error("Email send failed: %s", ex)
        raise


# ── Entry point ──────────────────────────────────────────────────────────────

def run(dry_run: bool = False, no_news: bool = False) -> int:
    cfg = load_config()
    mode_tags = []
    if dry_run:
        mode_tags.append("DRY-RUN")
    if no_news:
        mode_tags.append("NO-NEWS")
    tag = f"[{' | '.join(mode_tags)}] " if mode_tags else ""
    log.info("%sStarting daily agent — news source: %s", tag, cfg["news"]["source"])

    # 1. Reddit (with retry)
    from reddit_client import fetch_all
    try:
        reddit_data = _with_retry(fetch_all, force=True, label="reddit fetch")
    except Exception as exc:
        log.error("Reddit fetch failed after retries: %s — aborting.", exc)
        return 1
    if not reddit_data.get("trending"):
        log.error("No Reddit trending data — aborting.")
        return 1

    top_n = int(cfg["reddit"]["top_n_tickers"])
    gem_pool = int(cfg["reddit"].get("gem_pool_size", 0))
    pool_size = top_n + gem_pool
    reddit_signals = _to_reddit_signals(reddit_data["trending"], pool_size)
    main_signals = reddit_signals[:top_n]
    log.info("Pulled %d Reddit tickers (main=%d, gem-pool=%d)",
             len(reddit_signals), len(main_signals), max(0, len(reddit_signals) - top_n))

    # 2. News (skippable for testing; retried on failure)
    if no_news:
        log.info("--no-news: skipping news fetch, all news signals will be empty.")
        from models import NewsSignal
        news_signals = {r.ticker: NewsSignal(ticker=r.ticker) for r in reddit_signals}
    else:
        tickers = [r.ticker for r in reddit_signals]
        try:
            news_signals = _with_retry(fetch_news_for_tickers, tickers, cfg, label="news fetch")
            log.info("Fetched news for %d tickers", len(news_signals))
        except Exception as exc:
            log.warning("News fetch failed after retries: %s — continuing with empty news signals.", exc)
            from models import NewsSignal
            news_signals = {t: NewsSignal(ticker=t) for t in tickers}

    # 3 + 4. Score + filter
    history_path = Path(cfg["history"]["path"])
    history = _load_history(history_path, days=max(
        cfg["scoring"]["momentum"]["window_days"] + 1,
        int(cfg.get("hidden_gems", {}).get("novelty_lookback_days", 7)) + 1,
    ))

    candidates = score_candidates(reddit_signals, news_signals, cfg, history=history)
    main_tickers = {s.ticker for s in main_signals}
    main_candidates = [c for c in candidates if c.ticker in main_tickers]
    filtered = filter_candidates(main_candidates, cfg)
    gems = find_hidden_gems(candidates, cfg, history=history)
    log.info("Scored %d candidates — %d passed main filter, %d hidden gems",
             len(candidates), len(filtered), len(gems))

    # 5. Claude theses (optional — skipped gracefully if key absent or dry-run with no-news)
    picks = filtered[: cfg["agent"]["top_picks_in_email"]]
    theses: dict[str, str] = {}
    gem_notes: dict[str, str] = {}
    token_acc: dict = {"input": 0, "output": 0, "calls": 0}
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if anthropic_key and anthropic_key not in ("your_anthropic_api_key_here",):
        log.info("Generating Claude theses for %d pick(s) + %d gem(s)…", len(picks), len(gems))
        for c in picks:
            theses[c.ticker] = generate_thesis(c, token_acc=token_acc)
            log.info("  thesis %s: %s…", c.ticker, theses[c.ticker][:80])
        for g in gems:
            gem_notes[g.ticker] = generate_gem_note(g, token_acc=token_acc)
            log.info("  gem note %s: %s…", g.ticker, gem_notes[g.ticker][:80])
        log.info("Claude tokens: in=%d out=%d calls=%d est_cost=$%.4f",
                 token_acc["input"], token_acc["output"], token_acc["calls"],
                 _estimate_cost(token_acc))
    else:
        log.info("ANTHROPIC_API_KEY not set — using assembled rationale strings.")

    # 6. Email
    subject, plain_body, html_body = _format_email(
        picks, filtered, candidates, gems, cfg,
        theses=theses, gem_notes=gem_notes, token_acc=token_acc)

    if dry_run:
        print("\n" + "=" * 70)
        print("DRY RUN — email not sent")
        print("=" * 70)
        print(f"Subject: {subject}")
        print("-" * 70)
        print(plain_body)
        print("=" * 70)
        print(f"\nSummary: {len(picks)} pick(s), {len(gems)} gem(s), "
              f"{len(filtered)} passed filter out of {len(candidates)} scored.")
        return 0

    _send_email(subject, plain_body, html_body, cfg)

    # 7. History append (always — for momentum next run)
    _append_history(history_path, reddit_signals)
    log.info("Appended %d rows to %s", len(reddit_signals), history_path)

    log.info("Done.")
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Memestock Radar daily agent")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the email to stdout instead of sending it. Nothing is written to history.",
    )
    parser.add_argument(
        "--no-news", action="store_true",
        help="Skip news fetching (useful when FINNHUB_API_KEY is not yet set). "
             "Scoring will run on Reddit signals only.",
    )
    args = parser.parse_args()
    sys.exit(run(dry_run=args.dry_run, no_news=args.no_news))
