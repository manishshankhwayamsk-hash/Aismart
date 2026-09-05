# KRX AI Smart — Telegram Bot

## Files
- `bot.py` — main bot (handlers, scheduler, keep-alive web server)
- `db.py` — Postgres persistence (users, chats, api keys, polls, topics)
- `providers.py` — auto-detects AI provider from key shape, calls it
- `keymanager.py` — rotates keys, tracks daily usage, alerts owner
- `requirements.txt`, `Procfile`, `runtime.txt` — Render deployment files

## Environment variables (set these in Render → Environment)
| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | yes | Telegram bot token from @BotFather |
| `OWNER_ID` | yes | Your numeric Telegram user ID (owner-only commands + alerts go here) |
| `DATABASE_URL` | yes | Postgres connection string (Render → New → PostgreSQL, copy "Internal Database URL") |
| `BROADCAST_INTERVAL_MIN` | no | Minutes between auto quiz broadcasts (default 60) |
| `LEADERBOARD_HOUR_UTC` | no | Hour (UTC, 0-23) the daily leaderboard posts (default 18) |
| `PORT` | no | Render sets this automatically |

## Deploy on Render
1. Push these files to a GitHub repo.
2. Render → New → **Web Service** → connect the repo.
3. Render auto-detects `requirements.txt`/`Procfile`. Runtime is pinned via `runtime.txt`.
4. Add a **PostgreSQL** instance (Render → New → PostgreSQL, free tier) and copy its
   Internal Database URL into `DATABASE_URL` on the web service.
5. Add `BOT_TOKEN` and `OWNER_ID` env vars.
6. Deploy. First boot creates all tables automatically (nothing is ever auto-deleted).
7. Point **UptimeRobot** (or similar) at `https://<your-render-url>/` every 5 minutes
   to keep the free-tier service from sleeping — this is what the built-in
   Flask `/` health route is for.

## Adding API keys (owner only, in a DM with the bot)
```
/addapi AIzaSy...............        → auto-detected as Gemini
/addapi sk-or-v1-...........        → auto-detected as OpenRouter
/addapi gsk_................        → auto-detected as Groq
/addapi hf_.................        → auto-detected as HuggingFace
/addapi csk-................        → auto-detected as Cerebras
/addapi github_pat_.........        → auto-detected as GitHub Models
/addapi <32-char key>               → auto-detected as Mistral
/addapi cf:<account_id>:<token>     → Cloudflare Workers AI (needs account id)
```
Add as many as you want, from as many providers as you want — no limit.
The bot rotates through them (least-used first) and automatically skips
any that hit their daily quota, until they reset the next day.

`/listapi` — see all keys and today's usage
`/removeapi <id>` — remove one

## Owner alerts
You (OWNER_ID) get a DM automatically when any key crosses 50%, 70%, 90%,
or 100% of its daily limit (default 1500 requests/day, editable per key
directly in the `api_keys` table if you need a different number).

## User commands
- `/start` — register + intro
- `/topics` — list quiz categories (kept from the previous bot)
- `/score` — your points
- `/leaderboard` — top 10 in this chat's data
- Native Telegram quiz-polls are graded by Telegram itself; the bot then
  applies **+5** for correct / **-2** for wrong and sends an AI motivational
  line mentioning you.

## Notes on design choices
- Quiz questions use Telegram's native **quiz poll** type so grading is
  100% reliable (no ambiguity from parsing free-text answers). Telegram
  marks the poll; the bot listens via `poll_answer` and awards points.
- All data lives in Postgres and is never deleted by any automatic process
  — only `/removeapi` removes rows, and only for API keys.
- In groups/channels, free-text AI auto-reply only triggers on @mention or
  reply-to-bot, to avoid spamming every message in a busy group. Auto quiz
  broadcast still goes to every registered chat regardless.
