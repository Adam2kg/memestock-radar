# Memestock Radar

> Reddit-powered trade signal scanner — WSB mention tracking, sentiment scoring, and a daily AI-written email digest.

Monitors WSB and related subreddits for ticker mentions, scores each signal across a Reddit × News sentiment quadrant, and delivers a daily trade-of-the-day email. Claude optionally writes the trade thesis. Runs as a Flask dashboard or headless via cron.

## What it does

- **Cross-subreddit aggregation** — scrapes r/wallstreetbets and others for ticker mentions + engagement
- **Quadrant scoring** — maps Reddit sentiment × news sentiment into long / short / contrarian / fade-retail signals
- **Confidence rating** — weighs signal agreement, news volume, subreddit spread, and engagement quality
- **Momentum tracking** — persists daily history to catch multi-day runners early
- **BanBet tracker** — pulls WSB user bets and resolution outcomes
- **Daily email digest** — cron job picks the top trade, builds an email, sends via Gmail SMTP
- **Claude AI thesis** (optional) — if `ANTHROPIC_API_KEY` is set, Claude writes the rationale; otherwise falls back to assembled strings
- **Docker-ready** — single `docker compose up`

## Stack

Python · Flask · PRAW · VADER Sentiment · Finnhub · Claude API (optional) · Docker

## Setup

```bash
cp .env.example .env
# Fill in REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, FINNHUB_API_KEY, GMAIL_* — see .env.example
pip install -r requirements.txt
python app.py          # dashboard at http://localhost:5050
```

Or with Docker:

```bash
docker compose up
```

## API keys needed

| Key | Where to get | Required |
|---|---|---|
| `REDDIT_CLIENT_ID` / `SECRET` | reddit.com/prefs/apps → create "script" app | Yes |
| `FINNHUB_API_KEY` | finnhub.io (free tier: 60 req/min) | Yes |
| `GMAIL_SENDER` + `GMAIL_APP_PASSWORD` | Google account → App Passwords | For email digest |
| `ANTHROPIC_API_KEY` | console.anthropic.com | Optional |

## Daily digest (cron)

```cron
0 13 * * 1-5  cd /path/to/memestock-radar && /path/to/venv/bin/python daily_agent.py
```

Fires weekdays at 13:00. Picks the highest-confidence trade candidate, generates the thesis, and emails it.

## License

MIT

