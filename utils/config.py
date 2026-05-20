from __future__ import annotations

from pathlib import Path
import yaml


_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: str | Path = _DEFAULT_PATH) -> dict:
    """Load config.yaml — API keys are NOT stored here.

    All secrets (REDDIT_*, GMAIL_*, ANTHROPIC_API_KEY, FINNHUB_API_KEY, etc.)
    are read directly from environment variables in the modules that use them.
    config.yaml only contains non-secret settings (scoring weights, thresholds, etc.).

    Copy config.example.yaml → config.yaml to get started.
    """
    with open(path, "r") as f:
        return yaml.safe_load(f)
