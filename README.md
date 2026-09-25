# GemDrop Flask / Telegram Mini App

## Render

Start command:

```bash
gunicorn app:app
```

Required environment variables:

- `BOT_TOKEN` — token of the Telegram bot that opens the Mini App.
- `ADMIN_IDS` — Telegram user IDs of administrators separated by commas (current project defaults: `5257227756,8468542825`).
- `SECRET_KEY` — stable random secret; keep the same value between deploys.
- `WEBAPP_URL` — public HTTPS URL of the Mini App. On Render `RENDER_EXTERNAL_URL` is also supported.
- `BOT_USERNAME` — bot username without `@` (used by referral links).

Database:

- `DATABASE_URL` — recommended on Render if PostgreSQL is used.
- Or `DATA_DIR` — persistent disk directory when using SQLite, for example `/opt/render/project/src/data` if that path is backed by a Render Disk.

Do not set `PORT` manually on Render.

## Portal Market

Portal import is intentionally restored to the older working flow: the server uses the Authorization value entered in **Admin → Portal Market** for that import. A stale `PORTAL_KEY` environment variable is not silently substituted when the field is empty.

Portal authentication values can expire. Paste the current `Authorization` header from Portal when an authenticated request is required. The code accepts the complete `tma ...` or `Bearer ...` value.

## RTP

Admin → **RTP игры** controls one shared RTP for all real players. Default is 97%. The project does not contain deposit-based or player-specific hidden outcome manipulation.

## Admin additions

- Pending gift withdrawals with approve/reject.
- Transaction history (bets, wins, deposits, gift sales, balance edits, withdrawal events).
- Portal import logs.
