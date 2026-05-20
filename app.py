"""
Memestock Radar — Flask web app
Serves the dashboard and API endpoints for Reddit sentiment data.
"""

import os
from flask import Flask, render_template, jsonify, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)


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
    data   = request.get_json(force=True, silent=True) or {}
    bet_id = str(data.get("id", "")).strip()[:128]
    won    = data.get("won")
    notes  = str(data.get("notes", "")).strip()[:500]

    if not bet_id:
        return jsonify({"error": "id is required"}), 400
    if won is None or not isinstance(won, bool):
        return jsonify({"error": "won must be true or false"}), 400

    try:
        from banbet_client import resolve_bet
        found = resolve_bet(bet_id, won=won, resolved_by="dashboard", notes=notes)
        if not found:
            return jsonify({"error": "Bet not found"}), 404
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
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    print(f"\n  Memestock Radar running at http://{host}:{port}\n")
    app.run(debug=True, host=host, port=port)
