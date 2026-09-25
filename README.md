# GemDrop Flask / Telegram Mini App

## Render

Start command:

```bash
gunicorn app:app
```

Environment variables:

- `BOT_TOKEN` — token of the Telegram bot that opens the Mini App.
- `ADMIN_IDS` — Telegram user IDs of administrators separated by commas.
- `SECRET_KEY` — stable random secret; keep the same value between deploys.
- `WEBAPP_URL` — public HTTPS URL of the Mini App. On Render `RENDER_EXTERNAL_URL` is also supported.
- `BOT_USERNAME` — bot username without `@`.
- `DATABASE_URL` — recommended if PostgreSQL is used.
- Or `DATA_DIR` — persistent directory for SQLite, e.g. a mounted Render Disk.

Do not set `PORT` manually on Render.

## Mines

- Minimum bet: `0.10 TON`.
- Maximum bet: `300 TON`; validated in both browser and server API.
- Mines: `1–20`.
- Any successful opened-cell multiplier is clamped to a minimum of **1.01x**.
- RTP is not returned by the player ladder API and is not displayed in the player interface.
- The Mines layout is refreshed to match the supplied reference structure while keeping the GemDrop cyan/dark palette.
- The random-cell button uses a proper SVG question icon.
- Opened cells use animated faceted diamond SVGs; only the newly opened cell receives the reveal animation.
- **Последние победы** appears below the game controls and shows Telegram avatar/name, bet, multiplier and TON/NFT result.

## Recent wins

New completed wins save a snapshot in the round: total win, multiplier and NFT information when applicable. This keeps the feed stable even if a gift is later sold or withdrawn.

Endpoint: `GET /api/game/recent-wins`.

## TON Connect / funding settings

Admin → **Управление пополнением** stores the following in the database:

- enable/disable TON Connect;
- recipient TON wallet;
- site name shown by TON Connect;
- site URL;
- icon URL.

These settings are used by `/tonconnect-manifest.json`. The recipient wallet is shown in the funding modal. Wallet connection works through TON Connect; automatic crediting of a real blockchain payment still requires server-side transaction verification and is not faked from client data.

## Portal Market

Portal uses the Authorization value entered in **Admin → Portal Market**. The progressive import/timeouts from the previous Portal fix are preserved so a slow external request does not block the whole catalog until the very end.

## RTP administration

Admin → **RTP игры** retains one shared payout setting. Hidden per-user outcome manipulation is not implemented.

## Admin data

- **История пополнений** contains funding/balance adjustments rather than Mines bet history.
- Withdrawals have active and completed views.
- Completed withdrawals keep status, processing time and administrator information.
- Important settings and withdrawal actions are persisted in the database/admin log.
