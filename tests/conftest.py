"""
Shared pytest fixtures for memestock-radar tests.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure the project root is on the path so imports work from the tests/ dir
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── Environment stubs ─────────────────────────────────────────────────────────
# Set minimal env vars so modules that read them at import time don't crash.

os.environ.setdefault("REDDIT_CLIENT_ID",     "test_client_id")
os.environ.setdefault("REDDIT_CLIENT_SECRET", "test_client_secret")
os.environ.setdefault("REDDIT_USER_AGENT",    "MemestockRadarTest/1.0")
os.environ.setdefault("ANTHROPIC_API_KEY",    "your_anthropic_api_key_here")  # triggers fallback
os.environ.setdefault("GMAIL_SENDER",         "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD",   "test_pass")
os.environ.setdefault("GMAIL_RECIPIENT",      "recipient@example.com")


# ── Flask test client ─────────────────────────────────────────────────────────

@pytest.fixture()
def flask_client(tmp_path):
    """
    Flask test client with all /tmp paths redirected to tmp_path.
    Prevents tests from touching real system state.
    """
    job_file     = tmp_path / "radar-job.json"
    cooldown_file = tmp_path / "radar-cooldown"
    banbet_db    = tmp_path / "banbets.db"

    # Patch path constants before importing app (or after, since they're module-level)
    import app as flask_app
    import banbet_client

    flask_app._JOB_STATUS_FILE = job_file
    flask_app._COOLDOWN_FILE   = cooldown_file
    banbet_client._DB_FILE     = banbet_db

    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as client:
        yield client

    # Restore originals (in case tests share the module)
    flask_app._JOB_STATUS_FILE = Path("/tmp/radar-job.json")
    flask_app._COOLDOWN_FILE   = Path("/tmp/radar-cooldown")
    banbet_client._DB_FILE     = PROJECT_ROOT / "banbets.db"


# ── Temporary banbet DB ───────────────────────────────────────────────────────

@pytest.fixture()
def banbet_db(tmp_path):
    """
    Fixture that redirects banbet_client to a fresh temporary SQLite DB.
    Restores the original path on teardown.
    """
    import banbet_client
    orig = banbet_client._DB_FILE
    banbet_client._DB_FILE = tmp_path / "test_banbets.db"
    yield banbet_client
    banbet_client._DB_FILE = orig


# ── Sample trade data ─────────────────────────────────────────────────────────

@pytest.fixture()
def sample_reddit_signal():
    from models import RedditSignal
    return RedditSignal(
        ticker="GME",
        mentions=42,
        upvotes=12000,
        sentiment_score=0.35,
        sentiment_label="bullish",
        subreddit_count=3,
        subreddit_breakdown={
            "wallstreetbets": {"mentions": 30, "sentiment_score": 0.4, "sentiment_label": "bullish",
                               "hype_index": 120.0, "engagement_ratio": 2.1},
            "stocks":         {"mentions": 8,  "sentiment_score": 0.2, "sentiment_label": "bullish",
                               "hype_index": 20.0,  "engagement_ratio": 1.1},
            "investing":      {"mentions": 4,  "sentiment_score": 0.1, "sentiment_label": "neutral",
                               "hype_index": 8.0,   "engagement_ratio": 0.9},
        },
        hype_index=200.0,
        engagement_ratio=2.1,
    )


@pytest.fixture()
def sample_news_signal():
    from models import NewsSignal
    return NewsSignal(
        ticker="GME",
        volume=5,
        sentiment_avg=0.25,
        sentiment_weighted=0.30,
        articles=[
            {"headline": "GameStop rally continues", "source": "Reuters",
             "sentiment": 0.4, "url": "https://example.com/1"},
            {"headline": "Short squeeze pressure builds", "source": "Bloomberg",
             "sentiment": 0.2, "url": "https://example.com/2"},
        ],
    )
