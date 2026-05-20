"""
Tests for banbet_client.py — BanBet prediction parser and SQLite storage.

RED: All tests are written before running to establish what we expect.
"""

from __future__ import annotations

import pytest


# ── parse_banbet: regex coverage ──────────────────────────────────────────────

class TestParseBanbet:
    """parse_banbet() should extract ticker, price, and timeline from WSB comments."""

    def test_standard_with_dollar_signs(self, banbet_db):
        bet = banbet_db.parse_banbet("banbet! $GME 25$ by 04/20", "c1", "u1")
        assert bet is not None
        assert bet["ticker"] == "GME"
        assert bet["target"] == 25.0
        assert "04/20" in bet["timeline"]

    def test_uppercase_no_dollar(self, banbet_db):
        bet = banbet_db.parse_banbet("Banbet! GME to 30 by end of month", "c2", "u1")
        assert bet is not None
        assert bet["ticker"] == "GME"
        assert bet["target"] == 30.0

    def test_all_caps_no_by_keyword(self, banbet_db):
        """BANBET! SPY 550 eom — trailing word without by/to/@"""
        bet = banbet_db.parse_banbet("BANBET! SPY 550 eom", "c3", "u1")
        assert bet is not None
        assert bet["ticker"] == "SPY"
        assert bet["target"] == 550.0

    def test_decimal_price(self, banbet_db):
        bet = banbet_db.parse_banbet("banbet! $TSLA $420.69 by eow", "c4", "u1")
        assert bet is not None
        assert bet["ticker"] == "TSLA"
        assert abs(bet["target"] - 420.69) < 0.001

    def test_lowercase_ticker(self, banbet_db):
        bet = banbet_db.parse_banbet("banbet! gme 420 by next week", "c5", "u1")
        assert bet is not None
        assert bet["ticker"] == "GME"  # normalized to uppercase

    def test_at_sign_delimiter(self, banbet_db):
        bet = banbet_db.parse_banbet("banbet! $PLTR 45 @ earnings", "c6", "u1")
        assert bet is not None
        assert bet["ticker"] == "PLTR"
        assert bet["target"] == 45.0

    def test_double_bang(self, banbet_db):
        bet = banbet_db.parse_banbet("banbet!! GME 100 by eow", "c7", "u1")
        assert bet is not None
        assert bet["ticker"] == "GME"

    def test_no_trigger_word_returns_none(self, banbet_db):
        bet = banbet_db.parse_banbet("GME going to the moon 100", "c8", "u1")
        assert bet is None

    def test_ticker_too_long_returns_none(self, banbet_db):
        bet = banbet_db.parse_banbet("banbet! TOOLONGXYZ 100 by eow", "c9", "u1")
        assert bet is None

    def test_k_suffix_price_returns_none(self, banbet_db):
        """'100k' is not a valid plain number — should not match."""
        bet = banbet_db.parse_banbet("banbet! GME 100k by eom", "c10", "u1")
        assert bet is None

    def test_missing_price_returns_none(self, banbet_db):
        bet = banbet_db.parse_banbet("banbet! GME moon shot", "c11", "u1")
        assert bet is None

    def test_returns_correct_author_and_url(self, banbet_db):
        bet = banbet_db.parse_banbet(
            "banbet! $BB 7.50", "c12", "wsb_prophet",
            post_url="https://reddit.com/r/wsb/xyz"
        )
        assert bet is not None
        assert bet["author"] == "wsb_prophet"
        assert bet["post_url"] == "https://reddit.com/r/wsb/xyz"

    def test_multiline_text_picks_matching_line(self, banbet_db):
        text = "Great DD everyone\nbanbet! $AMC 25.50 by friday\nPOSITIONS: calls"
        bet = banbet_db.parse_banbet(text, "c13", "u1")
        assert bet is not None
        assert bet["ticker"] == "AMC"

    def test_line_length_cap_prevents_backtracking(self, banbet_db):
        """A very long line should not hang — truncated at 500 chars."""
        long_line = "banbet! " + "x " * 300 + "GME 100"
        # Should not hang; may or may not match depending on ticker position
        result = banbet_db.parse_banbet(long_line, "c14", "u1")
        # Key: this completes without timeout/hang


# ── Storage: append_bet ───────────────────────────────────────────────────────

class TestAppendBet:
    """append_bet() should store bets and deduplicate by comment id."""

    def _make_bet(self, comment_id="c001", ticker="GME", target=100.0, author="u1"):
        from banbet_client import parse_banbet
        return {
            "id": comment_id, "author": author, "ticker": ticker,
            "target": target, "timeline": "eow", "raw_line": "banbet! GME 100 by eow",
            "post_url": "", "created_at": "2026-05-20T10:00:00Z",
        }

    def test_first_insert_returns_true(self, banbet_db):
        bet = self._make_bet()
        assert banbet_db.append_bet(bet) is True

    def test_duplicate_returns_false(self, banbet_db):
        bet = self._make_bet()
        banbet_db.append_bet(bet)
        assert banbet_db.append_bet(bet) is False

    def test_different_ids_both_stored(self, banbet_db):
        banbet_db.append_bet(self._make_bet("c001"))
        banbet_db.append_bet(self._make_bet("c002"))
        bets = banbet_db.get_banbets()
        assert len(bets) == 2

    def test_stored_bet_has_correct_fields(self, banbet_db):
        banbet_db.append_bet(self._make_bet("c001", ticker="TSLA", target=420.0, author="elon_fan"))
        bets = banbet_db.get_banbets()
        assert bets[0]["ticker"] == "TSLA"
        assert bets[0]["target"] == 420.0
        assert bets[0]["author"] == "elon_fan"
        assert bets[0]["resolved"] is False
        assert bets[0]["won"] is None


# ── Storage: resolve_bet ──────────────────────────────────────────────────────

class TestResolveBet:
    """resolve_bet() should mark bets won/lost and return correct status."""

    def _insert(self, banbet_db, comment_id="c001"):
        banbet_db.append_bet({
            "id": comment_id, "author": "u1", "ticker": "GME", "target": 100.0,
            "timeline": "eow", "raw_line": "", "post_url": "", "created_at": "2026-05-20T10:00:00Z",
        })

    def test_resolve_won(self, banbet_db):
        self._insert(banbet_db)
        assert banbet_db.resolve_bet("c001", won=True) is True
        bets = banbet_db.get_banbets()
        assert bets[0]["resolved"] is True
        assert bets[0]["won"] is True

    def test_resolve_lost(self, banbet_db):
        self._insert(banbet_db)
        banbet_db.resolve_bet("c001", won=False)
        bets = banbet_db.get_banbets()
        assert bets[0]["won"] is False

    def test_resolve_nonexistent_returns_false(self, banbet_db):
        assert banbet_db.resolve_bet("nonexistent", won=True) is False

    def test_resolve_stores_notes(self, banbet_db):
        self._insert(banbet_db)
        banbet_db.resolve_bet("c001", won=True, notes="As predicted!")
        bets = banbet_db.get_banbets()
        assert bets[0]["notes"] == "As predicted!"

    def test_resolve_stores_resolved_by(self, banbet_db):
        self._insert(banbet_db)
        banbet_db.resolve_bet("c001", won=False, resolved_by="dashboard")
        bets = banbet_db.get_banbets()
        assert bets[0]["resolved_by"] == "dashboard"


# ── get_banbets filters ───────────────────────────────────────────────────────

class TestGetBanbets:
    """get_banbets() should support filtering by ticker, author, resolved status."""

    def _insert_many(self, banbet_db):
        bets = [
            {"id": "a1", "author": "alice", "ticker": "GME", "target": 50.0, "timeline": "eow",
             "raw_line": "", "post_url": "", "created_at": "2026-05-20T10:00:00Z"},
            {"id": "a2", "author": "bob",   "ticker": "AMC", "target": 20.0, "timeline": "eom",
             "raw_line": "", "post_url": "", "created_at": "2026-05-20T10:01:00Z"},
            {"id": "a3", "author": "alice", "ticker": "AMC", "target": 25.0, "timeline": "eow",
             "raw_line": "", "post_url": "", "created_at": "2026-05-20T10:02:00Z"},
        ]
        for b in bets:
            banbet_db.append_bet(b)
        banbet_db.resolve_bet("a1", won=True)

    def test_filter_by_ticker(self, banbet_db):
        self._insert_many(banbet_db)
        amc_bets = banbet_db.get_banbets(ticker="AMC")
        assert len(amc_bets) == 2
        assert all(b["ticker"] == "AMC" for b in amc_bets)

    def test_filter_by_author(self, banbet_db):
        self._insert_many(banbet_db)
        alice_bets = banbet_db.get_banbets(author="alice")
        assert len(alice_bets) == 2

    def test_filter_resolved_true(self, banbet_db):
        self._insert_many(banbet_db)
        resolved = banbet_db.get_banbets(resolved=True)
        assert len(resolved) == 1
        assert resolved[0]["id"] == "a1"

    def test_filter_resolved_false(self, banbet_db):
        self._insert_many(banbet_db)
        open_bets = banbet_db.get_banbets(resolved=False)
        assert len(open_bets) == 2

    def test_newest_first_ordering(self, banbet_db):
        self._insert_many(banbet_db)
        bets = banbet_db.get_banbets()
        assert bets[0]["id"] == "a3"  # latest created_at

    def test_limit_respected(self, banbet_db):
        self._insert_many(banbet_db)
        bets = banbet_db.get_banbets(limit=2)
        assert len(bets) == 2


# ── get_redditor_stats ────────────────────────────────────────────────────────

class TestGetRedditorStats:
    """get_redditor_stats() should compute per-redditor win/loss leaderboard."""

    def test_empty_db_returns_empty_list(self, banbet_db):
        assert banbet_db.get_redditor_stats() == []

    def test_single_user_win_rate(self, banbet_db):
        for i, (won, comment_id) in enumerate([(True, "c1"), (True, "c2"), (False, "c3")]):
            banbet_db.append_bet({
                "id": comment_id, "author": "prophet", "ticker": "GME",
                "target": float(100 + i), "timeline": "eow", "raw_line": "",
                "post_url": "", "created_at": f"2026-05-20T10:0{i}:00Z",
            })
            banbet_db.resolve_bet(comment_id, won=won)

        stats = banbet_db.get_redditor_stats()
        assert len(stats) == 1
        s = stats[0]
        assert s["author"] == "prophet"
        assert s["total"] == 3
        assert s["wins"] == 2
        assert s["losses"] == 1
        assert abs(s["win_rate"] - 2/3) < 0.001

    def test_pending_bets_counted(self, banbet_db):
        banbet_db.append_bet({
            "id": "p1", "author": "lurker", "ticker": "TSLA",
            "target": 500.0, "timeline": "eow", "raw_line": "",
            "post_url": "", "created_at": "2026-05-20T10:00:00Z",
        })
        stats = banbet_db.get_redditor_stats()
        assert stats[0]["pending"] == 1
        assert stats[0]["win_rate"] == 0.0  # no resolved bets

    def test_sorted_by_win_rate_desc(self, banbet_db):
        """Redditor with higher win rate should appear first."""
        for cid, author, won in [("c1", "expert", True), ("c2", "expert", True),
                                   ("c3", "novice", True), ("c4", "novice", False)]:
            banbet_db.append_bet({
                "id": cid, "author": author, "ticker": "GME",
                "target": 100.0, "timeline": "eow", "raw_line": "",
                "post_url": "", "created_at": f"2026-05-20T10:00:00Z",
            })
            banbet_db.resolve_bet(cid, won=won)
        stats = banbet_db.get_redditor_stats()
        assert stats[0]["author"] == "expert"  # 100% win rate > 50%
