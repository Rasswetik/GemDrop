/* Deposit settings: TON, Stars, gifts, pumpkins and Telegram Relayer. */
(() => {
  const K = window.AdminKit, {h, api} = K;
  const RELAY_STATUS = {online: 'Подключён', login_required: 'Нужен вход', code_sent: 'Код отправлен', need_password: 'Нужен 2FA', qr_wait: 'Ждём QR', qr_starting: 'Создаём QR', logged_out: 'Отключён', login_error: 'Ошибка входа', error: 'Ошибка проверки'};
  const EV_STATUS = {delivered: 'В инвентаре', credited: 'TON зачислен', unmatched: 'Не найден игрок', unpriced: 'Нет цены', not_nft: 'Не NFT', baseline: 'Исходное состояние', withdrawn: 'Выведен', completed: 'Успешно', sending: 'Отправляется', queued: 'В очереди', not_found: 'NFT не найден', insufficient_stars: 'Не хватает Stars', blocked: 'Передача заблокирована', recipient_unavailable: 'Не найден получатель', disabled: 'Автовывод выключен', error: 'Ошибка', login_required: 'Нужен вход'};
  const WAIT = ['qr_wait', 'qr_starting', 'code_sent', 'need_password'];
  let qrLib = null; const loadQr = () => window.QRCode ? Promise.resolve() : qrLib || (qrLib = new Promise((res, rej) => { const s = document.createElement('script'); s.src = 'https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js'; s.onload = res; s.onerror = () => rej(new Error('Не удалось загрузить QR-модуль')); document.head.append(s); }));

  K.register({id: 'depositSettingsPage', title: 'Управление пополнением', async render(root) {
    const t = await api('/api/admin/ton-settings'); let alive = true, timer = 0;
    const tonOn = K.switchRow('Пополнение через TON Connect', 'Показывать подключение кошелька пользователям', !!t.enabled), recipient = K.input({value: t.recipient_wallet || '', placeholder: 'Адрес кошелька'}), siteName = K.input({value: t.site_name || 'GemDrop'}), siteUrl = K.input({type: 'url', value: t.site_url || ''}), icon = K.input({type: 'url', value: t.icon_url || ''}), refPct = K.number(t.referral_percent ?? 10, {min: 0, max: 100, step: '.1'});
    const starsOn = K.switchRow('Пополнение через Telegram Stars', 'Показывать оплату Telegram Stars пользователям', t.stars_enabled !== false), starsRate = K.number(t.stars_per_ton ?? 100, {min: 1, step: 1});
    const pumpOn = K.switchRow('Тыквы за пополнение', 'Работает только пока идёт событие Halloween. Это дополнительно к бонусному балансу: игрок получает и бонус, и тыквы.', !!t.pumpkins_enabled), pumpRate = K.number(t.pumpkins_per_ton ?? 100, {min: 0, step: 1});
    const giftsOn = K.switchRow('Пополнение NFT-подарком', 'Третий способ пополнения без суммы и промокода', t.gifts_enabled !== false), relayUser = K.input({value: t.relay_username || 'Gemdrop_relay'});
    const tonForm = K.form([h('div', {class: 'group-title', text: 'TON Connect'}), tonOn, K.field('Кошелёк получатель', recipient), h('div', {class: 'form-grid'}, K.field('Название в TON Connect', siteName), K.field('URL сайта', siteUrl), K.field('URL иконки', icon)), K.field('Реферальный процент с подтверждённых TON-пополнений', refPct),
      h('div', {class: 'group-title', text: 'Telegram Stars'}), starsOn, K.field('Курс: сколько Stars за 1 TON баланса', starsRate, 'Например: 100 = за пополнение 1 TON пользователь платит 100 Stars. После успешной оплаты вывод блокируется на 21 день.'),
      h('div', {class: 'group-title', text: 'Тыквы (Halloween)'}), pumpOn, K.field('Курс: сколько тыкв за 1 TON пополнения', pumpRate, 'Например: 100 — за пополнение 5 TON игрок получит 500 тыкв. Начисляется при пополнении через TON и Telegram Stars.'),
      h('div', {class: 'group-title', text: 'NFT-подарки'}), giftsOn, K.field('Telegram аккаунт-получатель', relayUser)], async () => {
      await api('/api/admin/ton-settings', {enabled: tonOn.input.checked, recipient_wallet: recipient.value, site_name: siteName.value, site_url: siteUrl.value, icon_url: icon.value, referral_percent: refPct.value, stars_enabled: starsOn.input.checked, stars_per_ton: starsRate.value, gifts_enabled: giftsOn.input.checked, pumpkins_enabled: pumpOn.input.checked, pumpkins_per_ton: pumpRate.value, relay_username: relayUser.value});
      K.ok('Настройки пополнения сохранены');
    });

    /* --- Relayer --- */
    const live = h('div'), stat = h('div'), qrBox = h('div'), events = h('div', {class: 'list'}), autos = h('div', {class: 'list'}), metrics = h('div', {class: 'metrics'});
    const apiId = K.input({inputMode: 'numeric', autocomplete: 'off'}), apiHash = K.input({type: 'password', autocomplete: 'off', placeholder: 'Введите, чтобы заменить'}), autoOn = K.check('Фоново проверять новые NFT', true), autoWd = K.check('Если нужный NFT есть на Relayer, передавать его пользователю автоматически', true), mode = K.select([['inventory', 'Подарок в инвентарь'], ['balance', 'Стоимость в TON на баланс — запасной режим']], 'inventory');
    const phone = K.input({type: 'tel', placeholder: '+7…'}), code = K.input({inputMode: 'numeric', placeholder: 'Код из Telegram'}), pass = K.input({type: 'password', placeholder: 'Пароль 2FA'});
    const showQr = async url => { qrBox.replaceChildren(); if (!url) return; const box = h('div', {style: {background: '#fff', padding: '12px', display: 'inline-block', borderRadius: '12px'}}); qrBox.append(box, h('small', {class: 'muted', text: 'Настройки → Устройства → Подключить устройство'})); try { await loadQr(); new window.QRCode(box, {text: url, width: 210, height: 210, correctLevel: window.QRCode.CorrectLevel.M}); } catch (_) { box.textContent = url; box.style.wordBreak = 'break-all'; box.style.maxWidth = '260px'; } };
    const evRow = (e, auto) => h('div', {class: 'list-item'}, K.gift({image_url: e.image_url, name: e.gift_name}, 40),
      h('div', {class: 'grow'}, h('strong', {text: (e.gift_name || 'Telegram NFT') + (e.fragment_number ? ' #' + e.fragment_number : '')}), h('small', {text: auto ? `${e.user_name || 'Игрок'}${e.username ? ' @' + e.username : ''} · ID ${e.user_id}` : (e.sender_user_id ? `Отправитель ${e.sender_user_id}${e.sender_name ? ' · ' + e.sender_name : ''}` : 'Отправитель не определён')}), e.error ? h('small', {class: 'muted', text: e.error}) : null),
      h('div', null, h('strong', {text: auto ? (e.transfer_stars ? e.transfer_stars + ' ⭐' : '—') : (e.price_ton ? K.money(e.price_ton) + ' TON' : '—')}), K.badge(EV_STATUS[e.status] || e.status || '—', ['delivered', 'credited', 'completed'].includes(e.status) ? 'good' : ['error', 'blocked', 'insufficient_stars'].includes(e.status) ? 'bad' : 'warn')),
      !auto && e.external_url ? K.link('Открыть', e.external_url, 'button small') : null);
    function paint(d) {
      const st = d.state || {}, au = d.auth || {}, status = st.status || au.status || 'login_required', online = !!st.authorized, acc = st.account || {};
      live.replaceChildren(K.badge(online ? 'Подключён' : (RELAY_STATUS[status] || status), online ? 'good' : WAIT.includes(status) ? 'warn' : 'bad'));
      stat.replaceChildren(K.notice(online ? `${acc.first_name || acc.username || 'Telegram'}${acc.username ? ' · @' + acc.username : ''} — ID ${acc.id || '—'} · автопроверка ${d.enabled ? 'включена' : 'выключена'}` : (st.error || 'Войдите в Telegram-аккаунт, который принимает подарки.'), online ? 'good' : 'warn'));
      metrics.replaceChildren(K.metric('Баланс Stars', K.num(d.stars_balance || 0), '⭐'), K.metric('NFT на аккаунте', K.num(d.saved_gifts_count || 0)));
      autoOn.querySelector('input').checked = d.enabled !== false; autoWd.querySelector('input').checked = d.auto_withdraw_enabled !== false; mode.value = d.delivery_mode || 'inventory'; if (d.api_id) apiId.placeholder = 'Сохранён: ' + d.api_id;
      events.replaceChildren(...(d.events?.length ? d.events.map(e => evRow(e, false)) : [K.empty('Пока нет событий')])); autos.replaceChildren(...(d.auto_withdrawals?.length ? d.auto_withdrawals.map(e => evRow(e, true)) : [K.empty('Автовыводов пока нет')]));
      showQr(au.status === 'qr_wait' ? au.qr_url : ''); qrBtn.textContent = ['qr_wait', 'qr_starting'].includes(au.status) ? 'Обновить QR' : 'Войти по QR';
      clearTimeout(timer); if (WAIT.includes(au.status) && alive) timer = setTimeout(() => load().catch(() => {}), 1800);
    }
    const qrBtn = K.btn('Войти по QR', async () => { const d = await api('/api/admin/relayer/login/qr', {}); if (d.qr_url) await showQr(d.qr_url); K.ok(d.status === 'qr_wait' ? 'QR готов — отсканируйте его' : 'Готовим QR…'); await load(); });
    async function load() { try { const d = await api('/api/admin/relayer/status'); paint(d); return d; } catch (e) { stat.replaceChildren(K.notice('Relayer недоступен: ' + e.message, 'bad')); throw e; } }
    const relayForm = K.form([autoOn, autoWd, K.field('Что выдавать за входящий NFT', mode, 'Режимы взаимоисключающие. «Подарок в инвентарь» — сам NFT остаётся полноценным предметом игрока.'), h('div', {class: 'form-grid'}, K.field('API ID', apiId), K.field('API Hash', apiHash))], async () => {
      await api('/api/admin/relayer/settings', {enabled: autoOn.querySelector('input').checked, api_id: apiId.value.trim(), api_hash: apiHash.value.trim(), relay_username: relayUser.value.trim(), gifts_enabled: giftsOn.input.checked, delivery_mode: mode.value, auto_withdraw_enabled: autoWd.querySelector('input').checked});
      apiHash.value = ''; K.ok('Relayer сохранён'); await load();
    });
    const login = h('div', {class: 'stack'}, h('div', {class: 'row'}, qrBtn), qrBox,
      h('div', {class: 'row'}, h('div', {class: 'grow'}, phone), K.btn('Отправить код', async () => { const d = await api('/api/admin/relayer/login/phone', {phone: phone.value.trim()}); K.ok(d.authorized ? 'Аккаунт уже подключён' : 'Код отправлен в Telegram'); await load(); })),
      h('div', {class: 'row'}, h('div', {class: 'grow'}, code), K.btn('Войти', async () => { const d = await api('/api/admin/relayer/login/code', {code: code.value.trim()}); code.value = ''; K.ok(d.status === 'need_password' ? 'Введите пароль 2FA' : 'Relayer подключён'); await load(); })),
      h('div', {class: 'row'}, h('div', {class: 'grow'}, pass), K.btn('Подтвердить 2FA', async () => { await api('/api/admin/relayer/login/password', {password: pass.value}); pass.value = ''; K.ok('Relayer подключён'); await load(); })));
    const actions = h('div', {class: 'row'}, K.btn('Проверить новые NFT', async () => { const d = await api('/api/admin/relayer/scan', {}); K.ok(`Проверено: ${d.processed || 0} · зачислено: ${d.credited || 0}`); await load(); }),
      K.btn('Обновить баланс и NFT', async () => { await api('/api/admin/relayer/check-balance', {}); K.ok('Баланс и NFT обновлены'); await load(); }),
      K.btn('Отключить аккаунт', async () => { if (!await K.confirm('Отключить Telegram-аккаунт Relayer?', 'Отключить', true)) return; await api('/api/admin/relayer/logout', {}); K.ok('Relayer отключён'); await load(); }, 'danger'));
    load().catch(() => {});
    root.append(K.head('Управление пополнением', 'Способы пополнения: TON Connect, Telegram Stars, NFT-подарки через Relayer и тыквы Halloween.', 'Финансы'), K.card('Способы пополнения', null, tonForm),
      K.card('Telegram Relayer', 'Автопроверка входящих NFT и зачисление пользователю.', live, stat, metrics, actions, relayForm),
      K.card('Вход в аккаунт Relayer', null, login),
      K.card('Последние NFT', 'Зачисленные, неопознанные и ожидающие цены.', K.btn('Обновить', async () => { await load(); }, 'small'), events), K.card('Автовыводы', 'Передачи NFT пользователям через Relayer.', autos));
    return () => { alive = false; clearTimeout(timer); };
  }});
})();
