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
- **Отыгрыш NFT** reward: Portal gift + configurable wager multiplier `X`;
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


## Стабильность Portal и Mines
- Последний введённый Authorization Portal сохраняется в постоянной БД и повторно используется при следующих импортах/деплоях.
- Если сохранённый TMA Authorization истёк (401/403), импорт автоматически пробует публичный `/api/collections`; ранее сохранённый каталог при ошибке не удаляется.
- Для гарантированного сохранения между деплоями используйте PostgreSQL `DATABASE_URL` или постоянный Render Disk для `DATA_DIR`.
- Mines показывает 15 последних побед. Окно денежной победы использует компактную строку `+сумма` + PNG TON, как окно промокода.

## Mobile / Telegram opening

- The client no longer calls `Telegram.WebApp.expand()`. If the Telegram client supports it, the app asks to leave explicit fullscreen mode with `exitFullscreen()` and otherwise keeps the normal Mini App sheet behavior.
- On mobile, the content area is shifted down by `4vh` plus the safe-area inset so the header does not run into the system status area.
- Pinch zoom and double-tap zoom are disabled by the viewport settings and touch/gesture guards.
- TON bet input is disabled when the balance is below `0.10 TON`. A typed TON bet is clamped to the user's current balance and to the hard server limit of `300 TON`.

## Gift bets and promo wagering

Mines now supports choosing an inventory gift as the stake from the small gift button between **Ставка** and **Мины**.

- A regular gift is removed from inventory when the round starts. If the round loses, it is burned. If the round is cashed out, the game settles from the gift's stored Portal price exactly like a normal stake.
- A promo-wager gift is created by the new promocode type **Отыгрыш NFT**. Admin chooses the Portal gift and an `X` requirement (for example X25). Its target is `gift price × X`.
- Promo-wager gifts are marked in red and cannot be sold or withdrawn while locked.
- If a promo-wager gift loses in Mines, it burns. If it is cashed out, the calculated cashout goes only to its wager-progress; no TON or replacement NFT is paid to the player, and the promo gift returns to inventory.
- When the target is reached, opening the gift animates the progress to 100%, hides the progress area and reveals **Получить подарок**. Claiming converts the same item into an ordinary inventory gift that can then be sold or withdrawn.

## Referral reliability

`/start ref_<id>` is written to the database before the bot greeting is sent. The webhook returns quickly and the greeting is sent outside the response path. The Mini App button also carries `?ref=<id>` as a fallback. `/api/auth` additionally recognizes a signed Telegram `start_param=ref_<id>` if the app is ever opened with a `startapp` link.
