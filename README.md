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

## Вход из обычного браузера

Для работы команды входа нужны тот же `BOT_TOKEN`, публичный HTTPS `WEBAPP_URL`, подключённый Telegram webhook и неизменный `SECRET_KEY` между запусками. Общая база данных должна сохраняться между перезапусками; для SQLite нужен постоянный `DATA_DIR`, либо настройте `DATABASE_URL`. Сайт создаёт одноразовую команду `/auf КОД` сроком на пять минут. Пользователь отправляет её своему боту в личном чате; бот подтверждает вход, браузер открывает профиль с тем же Telegram ID, балансом и подарками. Код нельзя использовать повторно.

## Important 500 fix

The template no longer contains the CSS sequence `{@literal #}` immediately after a Jinja brace. The previous build had `@media(...){#minesPage...`, which Jinja parsed as the beginning of a `{# ... #}` template comment and raised `TemplateSyntaxError: Missing end of comment tag` on `/`.

## Referral system

Profile contains two compact buttons directly below the name/balance card: **Промокод** and **Бонусы**. Реферальная программа находится во вкладке «Рефералы» раздела «Бонусы».

Referral links are generated as:

```text
https://t.me/<current_bot_username>?start=ref_<telegram_user_id>
```

The bot username is discovered automatically from Telegram `getMe` and cached in the database, so the referral link follows the bot that is actually running the Mini App.

A referral relationship is attached on `/start ref_<id>` only when:

- the invited user is not the referrer themself;
- the referrer exists;
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
- On mobile, the content area uses Telegram safe-area information (when available) plus an increased phone-only top offset so the header stays below the system/Telegram chrome.
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

`/start ref_<id>` is written to the database before the bot greeting is sent. The greeting is returned directly through the Telegram webhook response, avoiding a second outbound Bot API request. The Mini App button also carries `?ref=<id>` as a fallback. `/api/auth` additionally recognizes a signed Telegram `start_param=ref_<id>` if the app is ever opened with a `startapp` link.


## Wallet window update

The TON top-up modal now has two explicit states. When connected it shows the active wallet/provider, the connected address, an on-chain TON balance fetched through TON Center API v3, a manual balance refresh control, and an **Отвязать кошелек** action that calls TON Connect `disconnect()` and removes the remembered address from the app database.

`TONCENTER_API_KEY` is strongly recommended because the same TON Center access is used for both deposit verification and wallet-balance display.

## RTP and multiplier model

Mines coefficients are calculated from the cumulative probability of surviving the selected number of opened cells:

`fair multiplier = C(25, opened) / C(25 - mines, opened)`

The configured RTP is then applied as the payout factor. Money calculations are rounded to cents with `ROUND_HALF_UP`, and Portal prices are normalized to two decimals when refreshed. A successful visible multiplier is never below `1.01x`.

Because a 1-mine first step has a fair multiplier of about `1.04167x`, a standard global RTP below about 97% cannot coexist with both uniform random mine placement and the mandatory `1.01x` minimum. The admin range is therefore `97–99.9%` for ordinary Mines. The separate promo-wager curve is `89–96.9%` and can only be used from 3 mines upward. Results are not secretly biased per user.

Each round stores an RTP snapshot so changing admin settings cannot change the payout curve of a round already in progress.

## Promo wager refinements

- Promo-wager items remain visually marked in the inventory.
- Promo progress/claim and successful wager modals use the normal blue GemDrop styling instead of red panels.
- Promo wagering requires at least 3 mines in both the client and server.
- Choosing a promo gift automatically raises the selected mines to 3 when needed.
- After any gift-backed round ends, the TON bet input is reset instead of keeping the old gift price.
- Promo-wager rounds use the separate lower payout curve configured in Admin → RTP.

## Portal automatic price refresh

Admin → Portal Market now includes **Автообновление цен**. It can be enabled with an interval from 15 to 1440 minutes. The schedule and next-run timestamp are stored in the persistent database and reuse the saved Portal Authorization. Auto-refresh runs while the Render web service is awake and preserves the last working catalog if Portal is unavailable.

## Mobile speed / input update

- TON Connect SDK is loaded asynchronously after the main game is visible. The startup loader no longer waits for `connectionRestored`, and wallet restoration has a short fallback timeout so a wallet provider/CDN outage cannot freeze the app on “Подключаем TON Connect…”.
- The main loader waits only for Telegram authentication + profile. Inventory, recent wins, multiplier ladder and TON Connect continue in parallel after the UI appears.
- Mobile top spacing now uses Telegram safe-area information when available and adds a larger phone-only offset. The app also asks Telegram to leave explicit fullscreen mode and keeps vertical swipes enabled.
- The TON bet field uses a text/decimal editing mode: it may be temporarily empty while the user types. It is normalized on blur/start, clamps immediately to the current balance/300 TON maximum, and is disabled only when the balance is below 0.10 TON.
- A Mines round no longer performs an unnecessary ladder request after every opened cell; the already-loaded ladder is simply advanced locally.

## Referral / bot latency update

- Existing users can now be bound to a referrer if they have never been bound before; referral rewards still apply only to future server-confirmed TON deposits.
- Cached bot usernames are tied to a fingerprint of the current `BOT_TOKEN`, preventing a referral link from silently pointing at an old bot after the token is changed.
- `/start` greetings are returned directly as a Telegram webhook `sendMessage` response instead of making a second outbound HTTP request, reducing greeting latency.

## Configurable loading GIF

Admin → **Экран загрузки** lets an administrator:

- enter a `/static/...` path manually;
- enter a full HTTPS image URL;
- choose one of the images found in `static/gifs`;
- preview it and save it to the persistent database.

The default remains `/static/gifs/shard.gif`.

## Roll

The bottom navigation now includes **Roll**. Admin → **Roll и шансы** lets you create multiple named rolls with a TON price and 2–24 sectors. Add gift sectors from the imported Portal catalog, a **Boost** sector (1–3×), or a **Без подарка** sector. A sector's integer weight divided by the sum of all sector weights is its chance and its portion of the wheel. Rolls are displayed in ascending price order.

The server chooses the outcome using `secrets.randbelow`, subtracts the price, records the spin and grants the gift to inventory in a database transaction. Boost multiplies gift-sector weights on the **next** spin, then expires; the wheel and prize pool update to reflect the new chances. A gift must have a matched PNG in the Portal catalog to be added. Import the catalog first if the gift picker is empty. Changes persist in the existing database, so keep the Render disk or PostgreSQL storage mounted across deployments.

### Roll screen update

The wheel now moves slowly while the Roll screen is idle and stops on the server-selected sector after pressing the single **Крутить** button. The button opens the same TON deposit window used by Mines when the balance is insufficient. Roll selection stays in the slider; Boost and the next-roll shortcut are no longer buttons. The prize pool shows each sector's current probability (including any pending Boost) and the gift's TON value. The bottom navigation and its safe-area spacing have been adjusted for narrow Telegram screens.

## Levels and deposit bonus codes

Twenty levels are seeded into the database on startup. Level 1 starts at zero turnover; by default, level N requires `N × (N − 1) / 2` TON in paid stakes. Admin → **Уровни и награды** saves all 20 thresholds and rewards in one atomic operation, and can adjust thresholds and assign a TON balance reward, Portal gift, wager gift, generated personal code, or generated deposit bonus code. Turnover increases when a Mines bet starts or a Roll spin is paid, including a gift bet at its stored TON value. Deposits and administrative balance adjustments do not increase turnover. Players claim unlocked rewards exactly once from the profile progress panel. Reward tiles are displayed as three fixed-width squares per row in the scrolling dialog. Codes generated as level rewards have one activation and can be redeemed by anyone who knows the code; the player can reopen a claimed level to view its code.

Admin → **Промокоды** now includes a deposit bonus: choose either a percentage or a fixed TON amount and an optional minimum deposit. The user activates it in the deposit window. The bonus is added only when an actual TON transaction is verified. A smaller deposit does not consume the code; a qualifying deposit consumes it once. Only one active deposit bonus code is permitted per user. The deposit window has **Убрать промокод**; removing one frees the slot for another. A removed unspent code may later be reactivated without using a second activation. Existing orders that were already created with the removed code retain their attached bonus if the TON transfer is verified. Keep a persistent database on deployment to retain levels, claims, and redeemed codes.

The active deposit code is attached to the deposit order at creation. Activating a code after an order was created does not retroactively add a bonus. Deposit order responses include the expected bonus; final credit remains conditional on network verification and the code being unused.

## Upgrade, action history, and TON transfers

The Roll source and admin settings remain in the project, but the Roll screen is inaccessible from the app navigation. **Апгрейд** replaces its bottom tab. The player chooses a TON stake (0.10–1,000,000 TON, limited by balance) or an inventory gift, then a more expensive Portal target. Only targets with a true calculated chance between 1% and 80% can be spun. Chance = `Upgrade RTP × stake price / target price`; the default RTP is 90% and can be adjusted in Admin → **RTP игры**. A TON stake is debited atomically with the spin; an ordinary gift stake consumes that gift, and a win awards the target gift. The server uses secure randomness and request IDs prevent double settlement.

A wager-locked gift follows the same wager rules as Mines: it is consumed on loss; on win, the **original** wager gift is returned and the target's TON price is added to its wager progress (capped at the original target). The target gift is not awarded separately during a wager spin. Older improperly upgraded wager gifts are restored on startup when the original identity can be reliably recovered from their promo code or catalog and recorded spin. Any ambiguous legacy item is left intact for manual review.

Admin → **Пользователи → [user]** can change a player level by setting turnover to that level threshold; previously claimed rewards remain claimed and are never duplicated. **Логи действий** shows paginated logins, Mines rounds and cells, Roll and Upgrade outcomes, deposits, promotions, level claims, withdrawals, transfer events, and balance transactions. PNG previews are included where a stored image is available. Events before this version retain the details already present in the existing database; new events include richer snapshots.

To enable TON transfers, set one level reward to **Разблокировать переводы TON** in **Уровни и награды**. The player must reach that level and claim the reward. Clicking the balance in their profile opens the transfer window. Recipients are resolved by exact registered Telegram username. The default fee is 5%, charged **in addition to** the specified transfer amount. Minimum amount is 0.10 TON. Admin → **Управление переводами** changes the fee (0–30%) and enabled status for each current level. Transfers, balance updates, and recipient notifications share one database transaction, with an idempotent request ID. The recipient sees a persistent receipt until pressing Continue.

### Мультипромокоды и награды (обновление)

В админке промокодов теперь можно выбрать «Несколько наград» и отметить любую комбинацию: TON на баланс, обычный подарок, отыгрышный подарок и бонус к депозиту. Тот же тип доступен в настройках наград уровня. Активация выдаёт выбранные немедленные награды одной операцией и последовательно показывает их на экране; бонус к депозиту применяется при подтверждении подходящего пополнения. Повторная активация не выдаёт награды заново. Новый депозитный промокод автоматически заменяет прежний.

Промокоды за уровни выдаются при нажатии на доступную награду в «Уровнях». После получения открывается раздел «Бонусы → Мои промокоды»; награда в «Уровнях» становится серой и больше не нажимается. Список промокодов показывает первые 3 записи и раскрывает ещё по 5. При проигрыше Upgrade от 1 TON начисляется случайный кэшбэк 0,5–3% (от 10 TON до 5%); начиная с 2 TON с небольшим шансом возможен дополнительный личный промокод. Отыгрышный подарок ограничен стоимостью 20 TON и требует отыгрыша не менее X10.

В карточке пользователя администратор может сразу выдать личный промокод на TON, пополнение, подарок или отыгрыш; для мультипромокода есть полная форма. Можно также скопировать готовый общий промокод как отдельный личный одноразовый код. В профиле кнопка «Бонусы» показывает число непросмотренных личных промокодов; счётчик обновляется при открытии приложения, возвращении в него и периодически при открытой странице. Инвентарь отсортирован по стоимости, а фон карточки зависит от цены подарка.

Красный счётчик на кнопке «Бонусы» теперь показывает только **непросмотренные** активные промокоды. При открытии конкретного кода просмотр записывается в базе и счётчик уменьшается, сам код остаётся доступным до использования или истечения срока. При активации нового депозитного промокода прежний активный депозитный бонус автоматически заменяется; вручную снимать его перед этим не нужно. Поле ввода очищается после удачной активации. Единое оформление для игры, профиля и админки находится в `static/css/gemdrop-ui.css`.

В апгрейде после проигрыша со ставкой TON сохраняются сумма ставки и цель. Когда оставшегося баланса недостаточно, прокрутка недоступна до пополнения. При ставке подарком и после выигрыша выбор сбрасывается. Уведомления показываются поверх модальных окон.
