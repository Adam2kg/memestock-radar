"""
banbet_client.py — BanBet prediction tracker for r/wallstreetbets.

The BanBet bot on WSB lets redditors write predictions like:
    "banbet! $GME 25$ by 04/20"
    "Banbet! GME to 30 by end of month"

This module:
  - Defines the regex that matches those calls (case-insensitive, flexible)
  - Provides SQLite-backed storage (banbets.db) — append-only with UNIQUE
    index on comment_id for dedup; no full-file-rewrite on each write
  - Provides read helpers for the API: get_banbets(), get_redditor_stats()

Resolution is MANUAL — a human marks bets won/lost via the dashboard.
Auto-price-checking is intentionally excluded.

Thread-safety: sqlite3 WAL mode + check_same_thread=False (one connection
per call via contextmanager) — safe for gunicorn --workers 1.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from utils import get_logger

log = get_logger("banbet_client")

# ── Database ──────────────────────────────────────────────────────────────────

_DB_FILE = Path(__file__).parent / "banbets.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bets (
    id          TEXT PRIMARY KEY,
    author      TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    target      REAL NOT NULL,
    timeline    TEXT DEFAULT 'unspecified',
    raw_line    TEXT DEFAULT '',
    post_url    TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    resolved    INTEGER NOT NULL DEFAULT 0,   -- 0=open, 1=resolved
    won         INTEGER,                       -- NULL=open, 1=won, 0=lost
    resolved_at TEXT,
    resolved_by TEXT DEFAULT '',
    notes       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_bets_author  ON bets (author);
CREATE INDEX IF NOT EXISTS idx_bets_ticker  ON bets (ticker);
CREATE INDEX IF NOT EXISTS idx_bets_created ON bets (created_at DESC);
"""


@contextmanager
def _conn():
    """Yield a short-lived SQLite connection in WAL mode."""
    con = sqlite3.connect(str(_DB_FILE), timeout=10, check_same_thread=False)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.executescript(_SCHEMA)
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ── Parsing ───────────────────────────────────────────────────────────────────

# Matches patterns like:
#   banbet! $GME 25$ by 04/20
#   banbet! GME to 30 by end of month
#   banbet! $TSLA $420.69 by eow
#   Banbet! SPY 550 eom          (trailing context without "by")
#   banbet!! GME 100 by eow      (double bang)
#
# Capture groups:
#   1 — ticker (with or without $)
#   2 — target price (may have $ prefix or suffix)
#   3 — timeline / deadline string (everything after "by/to/@")

_BANBET_RE = re.compile(
    r"""
    banbet!+                         # trigger word — allow "banbet!!" etc.
    \s+                              # whitespace
    \$?([A-Z]{1,5})\b               # ticker (optional $, 1-5 chars)
    .*?                              # intervening text (lazy)
    \$?([\d]+(?:[.,][\d]+)?)\$?     # price target (optional $ on either side)
    (?:                              # optional "by/to/@ <deadline>"
      \s+(?:by|to|@)\s+
      (.+?)
    )?
    (?:\s+\S.*?)?                    # any trailing context (e.g. "eom", "eow")
    \s*$                             # end of line
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Quick pre-check before expensive regex
_BANBET_QUICK = re.compile(r"banbet!", re.IGNORECASE)


def parse_banbet(text: str, comment_id: str, author: str, post_url: str = "") -> dict | None:
    """
    Try to extract a BanBet prediction from a comment body.

    Returns a bet dict ready for storage, or None if no match / parse fails.
    """
    if not _BANBET_QUICK.search(text):
        return None

    # Work line-by-line — the prediction is usually on a single line.
    # Cap line length to prevent catastrophic backtracking on pathological input.
    for line in text.splitlines():
        stripped = line.strip()[:500]
        m = _BANBET_RE.search(stripped)
        if not m:
            continue

        ticker   = m.group(1).upper()
        try:
            price = float(m.group(2).replace(",", "."))
        except (TypeError, ValueError):
            continue

        timeline = (m.group(3) or "").strip() or "unspecified"

        return {
            "id":         comment_id,
            "author":     author,
            "ticker":     ticker,
            "target":     price,
            "timeline":   timeline,
            "raw_line":   stripped[:300],
            "post_url":   post_url,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "resolved":   False,
            "won":        None,
            "resolved_at": None,
            "resolved_by": None,
            "notes":      "",
        }

    return None


# ── Write API ─────────────────────────────────────────────────────────────────

def append_bet(bet: dict) -> bool:
    """
    Insert a bet into the database.

    Returns True if inserted, False if already present (dedup by comment id).
    Thread-safe: SQLite WAL + UNIQUE constraint on id.
    """
    try:
        with _conn() as con:
            cur = con.execute(
                """
                INSERT OR IGNORE INTO bets
                    (id, author, ticker, target, timeline, raw_line, post_url,
                     created_at, resolved, won, resolved_at, resolved_by, notes)
                VALUES (?,?,?,?,?,?,?,?,0,NULL,NULL,'','')
                """,
                (
                    bet["id"], bet["author"], bet["ticker"], bet["target"],
                    bet.get("timeline", "unspecified"),
                    bet.get("raw_line", "")[:300],
                    bet.get("post_url", ""),
                    bet.get("created_at", datetime.now(timezone.utc).isoformat()),
                ),
            )
            inserted = cur.rowcount > 0
        if inserted:
            log.info("BanBet inserted: %s %s → %.2f by %s",
                     bet["author"], bet["ticker"], bet["target"],
                     bet.get("timeline", "?"))
        return inserted
    except Exception as exc:
        log.error("append_bet failed for %s: %s", bet.get("id"), exc)
        return False


def resolve_bet(comment_id: str, won: bool, resolved_by: str = "manual", notes: str = "") -> bool:
    """
    Mark a bet as resolved (won or lost).

    Returns True on success, False if ID not found.
    """
    try:
        with _conn() as con:
            cur = con.execute(
                """
                UPDATE bets
                SET resolved=1, won=?, resolved_at=?, resolved_by=?, notes=?
                WHERE id=?
                """,
                (
                    1 if won else 0,
                    datetime.now(timezone.utc).isoformat(),
                    resolved_by,
                    notes,
                    comment_id,
                ),
            )
            return cur.rowcount > 0
    except Exception as exc:
        log.error("resolve_bet failed for %s: %s", comment_id, exc)
        return False


# ── Read API ──────────────────────────────────────────────────────────────────

def get_banbets(
    ticker: str | None = None,
    author: str | None = None,
    resolved: bool | None = None,
    limit: int = 200,
) -> list[dict]:
    """
    Return stored bets, newest first.  Optional filters: ticker, author, resolved status.
    """
    clauses = []
    params: list = []

    if ticker:
        clauses.append("ticker = ?")
        params.append(ticker.upper())
    if author:
        clauses.append("author = ?")
        params.append(author)
    if resolved is True:
        clauses.append("resolved = 1")
    elif resolved is False:
        clauses.append("resolved = 0")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql   = f"SELECT * FROM bets {where} ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    try:
        with _conn() as con:
            rows = con.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception as exc:
        log.error("get_banbets failed: %s", exc)
        return []


def get_redditor_stats() -> list[dict]:
    """
    Compute per-redditor leaderboard from all bets.

    Returns list sorted by win_rate desc, then total desc:
        {
            "author":   str,
            "total":    int,
            "resolved": int,
            "wins":     int,
            "losses":   int,
            "pending":  int,
            "win_rate": float,  # 0.0–1.0
        }
    """
    sql = """
    SELECT
        author,
        COUNT(*)                             AS total,
        SUM(resolved)                        AS resolved_count,
        SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) AS wins,
        SUM(CASE WHEN won=0 THEN 1 ELSE 0 END) AS losses,
        SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END) AS pending
    FROM bets
    GROUP BY author
    """
    try:
        with _conn() as con:
            rows = con.execute(sql).fetchall()
    except Exception as exc:
        log.error("get_redditor_stats failed: %s", exc)
        return []

    stats = []
    for r in rows:
        resolved_count = r["resolved_count"] or 0
        wins           = r["wins"] or 0
        win_rate       = round(wins / resolved_count, 3) if resolved_count > 0 else 0.0
        stats.append({
            "author":   r["author"],
            "total":    r["total"],
            "resolved": resolved_count,
            "wins":     wins,
            "losses":   r["losses"] or 0,
            "pending":  r["pending"] or 0,
            "win_rate": win_rate,
        })

    return sorted(stats, key=lambda s: (s["win_rate"], s["total"]), reverse=True)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    # Normalise SQLite integers back to Python booleans for JSON serialisation
    d["resolved"] = bool(d.get("resolved", 0))
    won = d.get("won")
    d["won"] = None if won is None else bool(won)
    return d
