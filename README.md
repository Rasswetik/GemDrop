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
- `BOT_USERNAME` — optional fallback username without `@`. The app also resolves the current bot username automatically with Telegram `getMe`.
- `TONCENTER_API_KEY` — recommended for reliable server-side confirmation of TON deposits through TON Center API v3. The code can work without it, but public API rate limits may be stricter.
- `DATABASE_URL` — recommended if PostgreSQL is used.
- Or `DATA_DIR` — persistent directory for SQLite, e.g. a mounted Render Disk.

Do not set `PORT` manually on Render.

## Important 500 fix

The template no longer contains the CSS sequence `{@literal #}` immediately after a Jinja brace. The previous build had `@media(...){#minesPage...`, which Jinja parsed as the beginning of a `{# ... #}` template comment and raised `TemplateSyntaxError: Missing end of comment tag` on `/`.

## Referral system

Profile contains two compact buttons directly below the name/balance card: **Промокод** and **Рефералы**.

Referral links are generated as:

```text
https://t.me/<current_bot_username>?start=ref_<telegram_user_id>
```

The bot username is discovered automatically from Telegram `getMe` and cached in the database, so the referral link follows the bot that is actually running the Mini App.

A referral relationship is attached on `/start ref_<id>` only when:

- the invited user is not the referrer themself;
- the referrer exists;
- the invited user has not already made a verified TON deposit;
- the invited user does not already have another referrer.

Referral rewards are credited **only** from a server-verified on-chain TON deposit. Admin balance changes and admin deposits do not pay referral rewards. The percentage is configured in **Админ-панель → Управление пополнением**.

## Promocodes

Admin → **Промокоды** can create codes with:

- a custom code, or a random `GEM-XXXXXXXX` code when the field is empty;
- TON balance reward;
- gift reward from the current Portal catalog;
- activation limit (`0` = unlimited).

Each user can redeem a code only once. The successful redemption closes the input modal and opens the reward modal. TON reward is displayed as a centered amount with the TON PNG on the same line; gift reward shows the gift image and name.

## TON Connect and deposits

Admin → **Управление пополнением** stores:

- enable/disable TON Connect;
- recipient TON wallet;
- site name;
- site URL;
- icon URL;
- referral percentage for verified TON deposits.

The browser waits for TON Connect's connection restoration before deciding that a wallet is disconnected. The last connected address is also stored server-side so the modal can distinguish a remembered wallet from a genuinely never-connected wallet.

A deposit flow is:

1. Create a pending deposit order on the server.
2. Ask TON Connect to send the exact TON amount to the configured recipient.
3. Server checks the TON chain through TON Center.
4. Balance is credited only after the transaction is confirmed.
5. Referral bonus, if applicable, is credited at this same verified step exactly once.

## Mines

- Minimum bet: `0.10 TON`.
- Maximum bet: `300 TON`; validated in browser and server API.
- Mines: `1–20`.
- Any successful opened-cell multiplier is clamped to a minimum of **1.01x**.
- RTP is not returned by the player ladder API and is not displayed in the player interface.
- Only the newly opened cell receives the reveal animation.
- **Последние победы** appears below the game controls and shows Telegram avatar/name, bet, multiplier and TON/NFT result.

## Portal Market

Portal uses the Authorization value entered in **Admin → Portal Market**. Progressive import, short network timeouts, partial catalog saves and loop protection are preserved.

## Admin data

- **История пополнений** contains funding/balance adjustments rather than Mines bet history.
- Withdrawals have active and completed views.
- Completed withdrawals keep status, processing time and administrator information.
- Promocodes, referrals, TON deposit orders, wallet addresses and settings are persisted in the database.
