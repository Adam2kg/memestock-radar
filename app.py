"""
Memestock Radar — Flask web app
Serves the dashboard and API endpoints for Reddit sentiment data.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, render_template, jsonify, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── Email trigger state ───────────────────────────────────────────────────────

_JOB_STATUS_FILE = Path(os.getenv("RADAR_JOB_FILE", "/tmp/radar-job.json"))
_COOLDOWN_FILE   = Path(os.getenv("RADAR_COOLDOWN_FILE", "/tmp/radar-cooldown"))
_COOLDOWN_SECS   = int(os.getenv("RADAR_COOLDOWN_SECS", "300"))  # 5 min default
_trigger_lock    = threading.Lock()   # one trigger attempt at a time (single worker only)

# Lazy import so the app still starts even if .env isn't ready yet
def get_reddit():
    from reddit_client import fetch_all, fetch_subreddit, SUBREDDITS, CACHE_TTL
    return fetch_all, fetch_subreddit, SUBREDDITS, CACHE_TTL


@app.route("/")
def index():
    fetch_all, fetch_subreddit, SUBREDDITS, CACHE_TTL = get_reddit()
    return render_template("index.html", subreddits=SUBREDDITS, cache_ttl=CACHE_TTL)


@app.route("/api/all")
def api_all():
    force = request.args.get("force", "false").lower() == "true"
    fetch_all, _, _, _ = get_reddit()
    try:
        data = fetch_all(force=force)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/subreddit/<name>")
def api_subreddit(name: str):
    from reddit_client import SUBREDDITS
    # Whitelist — only fetch configured subreddits
    if name not in SUBREDDITS:
        return jsonify({"error": "Subreddit not in allowed list"}), 400
    force = request.args.get("force", "false").lower() == "true"
    _, fetch_subreddit, _, _ = get_reddit()
    try:
        data = fetch_subreddit(name, force=force)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _pid_is_alive(pid: int) -> bool:
    """Check if a process is still running (POSIX only)."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _read_job_status() -> dict | None:
    """Read /tmp/radar-job.json, return None if missing/corrupt."""
    try:
        return json.loads(_JOB_STATUS_FILE.read_text())
    except Exception:
        return None


@app.route("/api/trigger-email", methods=["POST"])
def api_trigger_email():
    """
    Spawn trigger_runner.py as a subprocess.  Returns immediately.

    Returns 409 if:
      - a job is already running (PID alive)
      - within the cooldown window after last successful send

    The subprocess writes its own status to _JOB_STATUS_FILE so the
    client can poll /api/email-status without touching in-process state.

    NOTE: requires gunicorn --workers 1 (single worker).  The lock only
    prevents double-submissions within one worker; the cooldown file guards
    across potential worker restarts.
    """
    with _trigger_lock:
        # 1. Already running?
        status = _read_job_status()
        if status and status.get("running"):
            pid = status.get("pid")
            if pid and _pid_is_alive(pid):
                return jsonify({
                    "ok": False,
                    "reason": "job_running",
                    "message": "An email job is already running.",
                    "pid": pid,
                }), 409

        # 2. Cooldown check
        if _COOLDOWN_FILE.exists():
            try:
                last_sent = float(_COOLDOWN_FILE.read_text())
                elapsed = time.time() - last_sent
                if elapsed < _COOLDOWN_SECS:
                    remaining = int(_COOLDOWN_SECS - elapsed)
                    return jsonify({
                        "ok": False,
                        "reason": "cooldown",
                        "message": f"Please wait {remaining}s before triggering again.",
                        "remaining_secs": remaining,
                    }), 409
            except Exception:
                pass  # corrupt cooldown file — ignore

        # 3. Spawn runner subprocess
        dry_run = request.json.get("dry_run", False) if request.is_json else False
        runner  = Path(__file__).parent / "trigger_runner.py"
        cmd     = [sys.executable, str(runner), str(_JOB_STATUS_FILE)]
        if dry_run:
            cmd.append("--dry-run")

        # Write a "starting" sentinel BEFORE Popen so /api/email-status
        # never returns idle between the 202 response and subprocess startup.
        # trigger_runner.py will overwrite this with running=True + PID.
        try:
            _JOB_STATUS_FILE.write_text(json.dumps({
                "running": True,
                "started_at": None,
                "finished_at": None,
                "exit_code": None,
                "result": "Starting…",
                "pid": None,
                "dry_run": dry_run,
            }))
        except Exception:
            pass  # non-fatal — subprocess will write its own status

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Detach from this process so gunicorn timeouts don't kill it
                start_new_session=True,
            )
        except Exception as exc:
            # Clean up the sentinel we just wrote
            try:
                _JOB_STATUS_FILE.write_text(json.dumps({
                    "running": False, "result": f"Spawn failed: {exc}", "exit_code": 1,
                    "started_at": None, "finished_at": None, "pid": None, "dry_run": dry_run,
                }))
            except Exception:
                pass
            return jsonify({"ok": False, "reason": "spawn_failed", "message": str(exc)}), 500

        # 4. Write cooldown marker (will be updated by runner on finish, but
        #    we set it now so rapid double-clicks are rejected immediately)
        try:
            _COOLDOWN_FILE.write_text(str(time.time()))
        except Exception:
            pass

        return jsonify({
            "ok": True,
            "pid": proc.pid,
            "dry_run": dry_run,
            "message": "Email job started.",
        }), 202


@app.route("/api/email-status")
def api_email_status():
    """
    Return the current state of the most recent email trigger job.
    The frontend polls this every 2 s while a job is running.
    """
    status = _read_job_status()
    if status is None:
        return jsonify({
            "state": "idle",
            "message": "No job has run yet.",
        })

    running = status.get("running", False)
    pid     = status.get("pid")

    # Detect zombie: running=True but PID is gone (crash before final write)
    if running and pid and not _pid_is_alive(pid):
        running = False
        status["running"] = False
        status["result"]  = status.get("result") or "Process ended unexpectedly"

    return jsonify({
        "state":       "running" if running else "done",
        "running":     running,
        "started_at":  status.get("started_at"),
        "finished_at": status.get("finished_at"),
        "exit_code":   status.get("exit_code"),
        "result":      status.get("result"),
        "dry_run":     status.get("dry_run", False),
        "pid":         pid,
    })


@app.route("/api/banbets")
def api_banbets():
    """Return parsed BanBet predictions + per-redditor stats."""
    try:
        from banbet_client import get_banbets, get_redditor_stats
        bets  = get_banbets()
        stats = get_redditor_stats()
        return jsonify({"bets": bets, "redditors": stats})
    except ImportError:
        return jsonify({"bets": [], "redditors": [], "error": "banbet_client not available"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/banbets/resolve", methods=["POST"])
def api_banbets_resolve():
    """Manually mark a BanBet as won or lost."""
    data = request.get_json(force=True, silent=True) or {}
    bet_id = str(data.get("id", "")).strip()[:128]  # cap length
    won    = data.get("won")
    notes  = str(data.get("notes", "")).strip()[:500]  # cap notes length

    if not bet_id:
        return jsonify({"error": "id is required"}), 400
    if won is None or not isinstance(won, bool):
        return jsonify({"error": "won must be true or false"}), 400

    try:
        from banbet_client import resolve_bet
        found = resolve_bet(bet_id, won=won, resolved_by="dashboard", notes=notes)
        if not found:
            return jsonify({"error": "Bet not found"}), 404  # don't echo bet_id back
        return jsonify({"ok": True})
    except ImportError:
        return jsonify({"error": "banbet_client not available"}), 500
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/status")
def api_status():
    client_id = os.getenv("REDDIT_CLIENT_ID", "")
    configured = bool(client_id and client_id != "your_client_id_here")
    return jsonify({
        "configured": configured,
        "message": "Ready" if configured else "Missing Reddit API credentials in .env"
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5050"))
    host = os.getenv("FLASK_HOST", "127.0.0.1")  # set FLASK_HOST=0.0.0.0 in docker
    print(f"\n  Memestock Radar running at http://{host}:{port}\n")
    app.run(debug=True, host=host, port=port)
