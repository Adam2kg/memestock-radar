"""
trigger_runner.py — subprocess entry point for the manual email trigger.

Called by app.py via subprocess.Popen. Writes job status to a JSON file
so the Flask process can poll it without sharing in-process state.

Usage (internal — do not call directly):
    python trigger_runner.py /tmp/radar-job.json [--dry-run]
"""

from __future__ import annotations

import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

_STATUS_FILE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/radar-job.json")
_DRY_RUN = "--dry-run" in sys.argv

# ── Hard timeout ──────────────────────────────────────────────────────────────
# If daily_agent.run() hangs (stuck API call, SMTP timeout, etc.) kill this
# subprocess after RADAR_JOB_TIMEOUT seconds to unblock future triggers.
# SIGALRM is POSIX-only (Linux/macOS); silently skipped on Windows.
_TIMEOUT_SECS = int(os.environ.get("RADAR_JOB_TIMEOUT", "3600"))  # 1 h default


def _timeout_handler(signum, frame):
    raise TimeoutError(f"Job killed: exceeded {_TIMEOUT_SECS}s (RADAR_JOB_TIMEOUT)")


if hasattr(signal, "SIGALRM"):
    signal.signal(signal.SIGALRM, _timeout_handler)


def _write_status(data: dict) -> None:
    """Atomic write: temp file + replace to avoid partial-read corruption."""
    tmp = _STATUS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(_STATUS_FILE)


started_at = datetime.now(timezone.utc).isoformat()
_write_status({
    "running": True,
    "started_at": started_at,
    "finished_at": None,
    "exit_code": None,
    "result": None,
    "pid": os.getpid(),
    "dry_run": _DRY_RUN,
})

exit_code = 1
result = "Unknown error"

# Arm the timeout alarm before the long-running work
if hasattr(signal, "SIGALRM"):
    signal.alarm(_TIMEOUT_SECS)

try:
    # Add project dir to path so imports work when cwd is the app root
    sys.path.insert(0, str(Path(__file__).parent))
    from daily_agent import run  # noqa: PLC0415
    exit_code = run(dry_run=_DRY_RUN)
    result = "Sent ✓" if exit_code == 0 else "Pipeline failed — check logs"
except TimeoutError as exc:
    result = f"Timeout: {exc}"
    exit_code = 2
except Exception as exc:
    result = f"Error: {exc}"
    exit_code = 1
finally:
    # Disarm the alarm whether we succeeded, failed, or timed out
    if hasattr(signal, "SIGALRM"):
        signal.alarm(0)

_write_status({
    "running": False,
    "started_at": started_at,
    "finished_at": datetime.now(timezone.utc).isoformat(),
    "exit_code": exit_code,
    "result": result,
    "pid": os.getpid(),
    "dry_run": _DRY_RUN,
})
