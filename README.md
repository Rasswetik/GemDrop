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

## Mines / limits

- Minimum bet: `0.10 TON`.
- Maximum bet: `300 TON` (validated both in the browser and on the server).
- Mines: `1–20`.
- The player-facing RTP label is hidden; Admin → **RTP игры** remains the control point for the shared RTP.
- Opened cells use animated inline SVG crystals instead of `mine1.png` / `mine2.png`.

## Admin additions

- Withdrawals have **Active** and **Completed requests** views. Completed rows keep status, processing time, and administrator information.
- **Funding history** shows deposits/referral credits/admin balance adjustments; Mines bets/wins are excluded from this admin screen while audit rows can remain in the database.
- Withdrawal approve/reject actions are additionally written to `admin_log`.
- Portal import logs.

## TON Connect

The `+` button to the right of the Mines balance opens TON Connect. Wallet connection is enabled; automatic balance crediting from a real on-chain payment is intentionally not simulated. Enable real deposits only together with server-side verification of the incoming TON transaction.
