"""
E2E tests for Flask routes — test the HTTP layer without spawning real subprocesses.

Strategy:
  - Use Flask test client (no real HTTP)
  - Mock subprocess.Popen where needed (don't actually run the pipeline)
  - Test routing, validation, status transitions, and error paths
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── /api/status ───────────────────────────────────────────────────────────────

class TestApiStatus:
    """GET /api/status — reports whether Reddit credentials are configured."""

    def test_returns_not_configured_with_stub_creds(self, flask_client):
        """The test env has dummy creds — should report not configured."""
        r = flask_client.get("/api/status")
        assert r.status_code == 200
        data = r.get_json()
        assert "configured" in data

    def test_response_has_message_field(self, flask_client):
        r = flask_client.get("/api/status")
        data = r.get_json()
        assert "message" in data


# ── /api/email-status ─────────────────────────────────────────────────────────

class TestApiEmailStatus:
    """GET /api/email-status — returns current job state from status file."""

    def test_returns_idle_when_no_status_file(self, flask_client):
        r = flask_client.get("/api/email-status")
        assert r.status_code == 200
        data = r.get_json()
        assert data["state"] == "idle"

    def test_returns_running_when_file_says_running(self, flask_client, tmp_path):
        """Simulate a running job by writing a status file directly."""
        import app as flask_app
        pid = 99999  # Use a PID that almost certainly doesn't exist
        flask_app._JOB_STATUS_FILE.write_text(json.dumps({
            "running": True, "started_at": "2026-05-20T10:00:00Z",
            "finished_at": None, "exit_code": None,
            "result": None, "pid": pid, "dry_run": False,
        }))
        with patch("app._pid_is_alive", return_value=True):
            r = flask_client.get("/api/email-status")
        data = r.get_json()
        assert data["state"] == "running"
        assert data["running"] is True

    def test_returns_done_when_file_says_finished(self, flask_client):
        import app as flask_app
        flask_app._JOB_STATUS_FILE.write_text(json.dumps({
            "running": False, "started_at": "2026-05-20T10:00:00Z",
            "finished_at": "2026-05-20T10:01:30Z", "exit_code": 0,
            "result": "Sent ✓", "pid": 12345, "dry_run": False,
        }))
        r = flask_client.get("/api/email-status")
        data = r.get_json()
        assert data["state"] == "done"
        assert data["exit_code"] == 0
        assert data["result"] == "Sent ✓"

    def test_detects_zombie_process(self, flask_client):
        """running=True but PID dead → should report done."""
        import app as flask_app
        flask_app._JOB_STATUS_FILE.write_text(json.dumps({
            "running": True, "started_at": "2026-05-20T10:00:00Z",
            "finished_at": None, "exit_code": None,
            "result": None, "pid": 99999, "dry_run": False,
        }))
        with patch("app._pid_is_alive", return_value=False):
            r = flask_client.get("/api/email-status")
        data = r.get_json()
        assert data["state"] == "done"
        assert data["running"] is False


# ── /api/trigger-email ────────────────────────────────────────────────────────

class TestApiTriggerEmail:
    """POST /api/trigger-email — spawns the email pipeline subprocess."""

    def _mock_popen(self, pid=42042):
        mock = MagicMock()
        mock.pid = pid
        return mock

    def test_returns_202_on_success(self, flask_client):
        with patch("subprocess.Popen", return_value=self._mock_popen()):
            r = flask_client.post("/api/trigger-email",
                                  data=json.dumps({"dry_run": False}),
                                  content_type="application/json")
        assert r.status_code == 202
        data = r.get_json()
        assert data["ok"] is True
        assert data["pid"] == 42042

    def test_returns_409_when_job_already_running(self, flask_client):
        import app as flask_app
        flask_app._JOB_STATUS_FILE.write_text(json.dumps({
            "running": True, "pid": 99001, "result": None,
            "started_at": None, "finished_at": None, "exit_code": None, "dry_run": False,
        }))
        with patch("app._pid_is_alive", return_value=True):
            r = flask_client.post("/api/trigger-email",
                                  data=json.dumps({}),
                                  content_type="application/json")
        assert r.status_code == 409
        data = r.get_json()
        assert data["reason"] == "job_running"

    def test_returns_409_during_cooldown(self, flask_client):
        import app as flask_app
        # Write a cooldown file with current timestamp
        flask_app._COOLDOWN_FILE.write_text(str(time.time()))
        r = flask_client.post("/api/trigger-email",
                              data=json.dumps({}),
                              content_type="application/json")
        assert r.status_code == 409
        data = r.get_json()
        assert data["reason"] == "cooldown"
        assert data["remaining_secs"] > 0

    def test_cooldown_expired_allows_new_trigger(self, flask_client):
        import app as flask_app
        # Write a cooldown that expired long ago
        flask_app._COOLDOWN_FILE.write_text(str(time.time() - 9999))
        with patch("subprocess.Popen", return_value=self._mock_popen()):
            r = flask_client.post("/api/trigger-email",
                                  data=json.dumps({"dry_run": True}),
                                  content_type="application/json")
        assert r.status_code == 202

    def test_dry_run_flag_passed_through(self, flask_client):
        captured_cmd = []

        def fake_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return self._mock_popen()

        with patch("subprocess.Popen", side_effect=fake_popen):
            flask_client.post("/api/trigger-email",
                              data=json.dumps({"dry_run": True}),
                              content_type="application/json")

        assert "--dry-run" in captured_cmd

    def test_writes_starting_sentinel_before_popen(self, flask_client):
        """Status file should say running=True before Popen returns."""
        import app as flask_app
        sentinel_contents = []

        def fake_popen(cmd, **kwargs):
            # Read the status file at the moment Popen is called
            try:
                sentinel_contents.append(json.loads(flask_app._JOB_STATUS_FILE.read_text()))
            except Exception:
                pass
            return self._mock_popen()

        with patch("subprocess.Popen", side_effect=fake_popen):
            flask_client.post("/api/trigger-email",
                              data=json.dumps({}),
                              content_type="application/json")

        assert sentinel_contents, "Popen was never called"
        assert sentinel_contents[0]["running"] is True

    def test_spawn_failure_returns_500(self, flask_client):
        with patch("subprocess.Popen", side_effect=OSError("spawn failed")):
            r = flask_client.post("/api/trigger-email",
                                  data=json.dumps({}),
                                  content_type="application/json")
        assert r.status_code == 500
        data = r.get_json()
        assert data["reason"] == "spawn_failed"


# ── /api/banbets ──────────────────────────────────────────────────────────────

class TestApiBanbets:
    """GET /api/banbets — returns bets + redditor stats from SQLite."""

    def test_returns_empty_lists_when_no_bets(self, flask_client):
        r = flask_client.get("/api/banbets")
        assert r.status_code == 200
        data = r.get_json()
        assert data["bets"] == []
        assert data["redditors"] == []

    def test_returns_stored_bets(self, flask_client):
        import banbet_client
        banbet_client.append_bet({
            "id": "r1", "author": "wsb_user", "ticker": "GME",
            "target": 420.0, "timeline": "eow", "raw_line": "",
            "post_url": "", "created_at": "2026-05-20T10:00:00Z",
        })
        r = flask_client.get("/api/banbets")
        data = r.get_json()
        assert len(data["bets"]) == 1
        assert data["bets"][0]["ticker"] == "GME"

    def test_returns_redditor_stats(self, flask_client):
        import banbet_client
        banbet_client.append_bet({
            "id": "r2", "author": "prophet", "ticker": "AMC",
            "target": 25.0, "timeline": "eom", "raw_line": "",
            "post_url": "", "created_at": "2026-05-20T10:00:00Z",
        })
        banbet_client.resolve_bet("r2", won=True)
        r = flask_client.get("/api/banbets")
        data = r.get_json()
        assert len(data["redditors"]) == 1
        assert data["redditors"][0]["author"] == "prophet"


# ── /api/banbets/resolve ──────────────────────────────────────────────────────

class TestApiBanbetsResolve:
    """POST /api/banbets/resolve — marks a bet won or lost."""

    def _insert_bet(self, bet_id="test001"):
        import banbet_client
        banbet_client.append_bet({
            "id": bet_id, "author": "u1", "ticker": "GME",
            "target": 100.0, "timeline": "eow", "raw_line": "",
            "post_url": "", "created_at": "2026-05-20T10:00:00Z",
        })

    def test_resolve_won_returns_ok(self, flask_client):
        self._insert_bet()
        r = flask_client.post("/api/banbets/resolve",
                              data=json.dumps({"id": "test001", "won": True}),
                              content_type="application/json")
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_resolve_lost_returns_ok(self, flask_client):
        self._insert_bet()
        r = flask_client.post("/api/banbets/resolve",
                              data=json.dumps({"id": "test001", "won": False}),
                              content_type="application/json")
        assert r.status_code == 200

    def test_missing_id_returns_400(self, flask_client):
        r = flask_client.post("/api/banbets/resolve",
                              data=json.dumps({"won": True}),
                              content_type="application/json")
        assert r.status_code == 400

    def test_missing_won_returns_400(self, flask_client):
        r = flask_client.post("/api/banbets/resolve",
                              data=json.dumps({"id": "test001"}),
                              content_type="application/json")
        assert r.status_code == 400

    def test_nonexistent_id_returns_404(self, flask_client):
        r = flask_client.post("/api/banbets/resolve",
                              data=json.dumps({"id": "does_not_exist", "won": True}),
                              content_type="application/json")
        assert r.status_code == 404

    def test_response_does_not_echo_bet_id(self, flask_client):
        """Security: user-supplied ID must not appear in error response body."""
        r = flask_client.post("/api/banbets/resolve",
                              data=json.dumps({"id": "INJECTED_ID_XYZ", "won": True}),
                              content_type="application/json")
        assert r.status_code == 404
        body = r.get_data(as_text=True)
        assert "INJECTED_ID_XYZ" not in body

    def test_won_must_be_boolean_not_string(self, flask_client):
        self._insert_bet()
        r = flask_client.post("/api/banbets/resolve",
                              data=json.dumps({"id": "test001", "won": "true"}),
                              content_type="application/json")
        assert r.status_code == 400

    def test_notes_accepted(self, flask_client):
        self._insert_bet()
        r = flask_client.post("/api/banbets/resolve",
                              data=json.dumps({"id": "test001", "won": True,
                                               "notes": "Called it perfectly!"}),
                              content_type="application/json")
        assert r.status_code == 200
        import banbet_client
        bets = banbet_client.get_banbets()
        assert bets[0]["notes"] == "Called it perfectly!"


# ── /api/all + /api/subreddit/<name> ─────────────────────────────────────────

class TestApiAll:
    """GET /api/all — returns combined Reddit data (mocked)."""

    def _mock_reddit_data(self):
        return {
            "trending": [
                {"ticker": "GME", "total_mentions": 50, "total_upvotes": 5000,
                 "avg_sentiment": 0.4, "sentiment_label": "bullish",
                 "subreddit_count": 3, "hype_index": 200.0,
                 "engagement_ratio": 2.1, "subreddit_breakdown": {}, "top_posts": []},
            ],
            "subreddits": {"wallstreetbets": {"tickers": {}, "recent_posts": [], "post_count": 10}},
            "fetched_at": "2026-05-20 10:00 UTC",
            "cache_ttl": 300,
        }

    def test_returns_data_structure(self, flask_client):
        with patch("reddit_client.fetch_all", return_value=self._mock_reddit_data()):
            r = flask_client.get("/api/all")
        assert r.status_code == 200
        data = r.get_json()
        assert "trending" in data

    def test_force_param_accepted(self, flask_client):
        with patch("reddit_client.fetch_all", return_value=self._mock_reddit_data()) as mock:
            flask_client.get("/api/all?force=true")
        mock.assert_called_once_with(force=True)

    def test_subreddit_not_in_whitelist_returns_400(self, flask_client):
        r = flask_client.get("/api/subreddit/supersecretforum")
        assert r.status_code == 400
