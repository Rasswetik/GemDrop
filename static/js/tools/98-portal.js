/* Portal Market: partner token, catalog import, auto price refresh, payouts, logs. */
(() => {
  const K = window.AdminKit, {h, api} = K;
  const PAYOUT = {completed: 'Готово', portal_waiting_recipient: 'Ждём пользователя', portal_lookup: 'Поиск', portal_buying: 'Покупка', portal_bought: 'Куплен', portal_withdrawing: 'Выводится', portal_not_found: 'Ручной вывод', portal_insufficient_balance: 'Ручной вывод', portal_buy_failed: 'Ручной вывод', portal_withdraw_failed: 'Ручной вывод', portal_error: 'Ошибка', portal_not_configured: 'Не настроен'};
  const MANUAL = ['portal_not_found', 'portal_insufficient_balance', 'portal_buy_failed', 'portal_withdraw_failed', 'portal_error', 'portal_not_configured', 'portal_recovered', 'portal_unknown'];
  const ton = v => v == null ? '—' : Number(v).toFixed(3).replace(/0+$/, '').replace(/\.$/, '') + ' TON';

  K.register({id: 'portalPage', title: 'Portal Market', async render(root) {
    let alive = true, jobTimer = 0, modelTimer = 0, rendered = -1;
    const token = K.input({type: 'password', autocomplete: 'off', placeholder: 'Partner token · пусто = использовать сохранённый'});
    const runtime = h('div'), catalogState = h('div'), modelsState = h('small', {class: 'muted', text: 'Модели подарков: —'}), metrics = h('div', {class: 'metrics'}), payouts = h('div', {class: 'list'}), logs = h('div', {class: 'list'}), logCount = h('span', {class: 'muted'}), grid = h('div', {class: 'catalog-grid'}), gridCount = h('span', {class: 'muted'});
    const paintRuntime = d => {
      runtime.replaceChildren(K.notice(!d.configured ? 'Partner token не настроен. Вставьте token аккаунта Relayer и сохраните.' : d.error ? 'Portal Partner: ' + d.error : 'Portal Partner подключён · баланс и инвентарь Relayer доступны.', !d.configured || d.error ? 'bad' : 'good'));
      metrics.replaceChildren(K.metric('Баланс', ton(d.balance_ton)), K.metric('Доступно для покупок', ton(d.spendable_ton)), K.metric('Резерв', Number(d.reserve_ton ?? .3).toFixed(2) + ' TON'), K.metric('NFT в инвентаре', d.owned_count == null ? '—' : String(d.owned_count)));
      payouts.replaceChildren(...(d.withdrawals?.length ? d.withdrawals.map(x => { const manual = MANUAL.includes(x.status); return h('div', {class: 'list-item', style: {flexDirection: 'column', alignItems: 'stretch'}},
        h('div', {class: 'row'}, h('strong', {class: 'grow', text: `#${x.withdrawal_id} · ${x.requested_name || x.nft_name || 'Подарок'}`}), K.badge(PAYOUT[x.status] || x.status || '—', manual ? 'bad' : x.status === 'completed' ? 'good' : x.status === 'portal_waiting_recipient' ? 'warn' : '')),
        h('small', {text: `${x.user_name || 'Игрок'}${x.username ? ' @' + x.username : ''} · ID ${x.user_id}`}), h('small', {text: `Этап: ${x.stage || '—'}${x.source ? ' · ' + (x.source === 'market' ? 'куплен на маркете' : 'из Portal-инвентаря') : ''}${Number(x.purchase_price) > 0 ? ' · ' + x.purchase_price + ' TON' : ''}${x.attempts ? ' · попыток ' + x.attempts : ''}`}),
        x.error ? h('small', {class: 'muted', text: x.error + (manual ? ' · требуется ручной вывод' : '')}) : null); }) : [K.empty('Пока операций автовывода Portal нет')]));
    };
    const loadRuntime = async () => { try { paintRuntime(await api('/api/admin/portal/runtime')); } catch (e) { runtime.replaceChildren(K.notice(e.message, 'bad')); } };
    const loadLogs = async () => { try { const d = await api('/api/admin/portal/logs'); logCount.textContent = (d.logs || []).length; logs.replaceChildren(...((d.logs || []).length ? [...d.logs].reverse().map(l => h('div', {class: 'list-item', style: l.level === 'error' ? {color: 'var(--bad, #dc2626)'} : {}}, h('small', {text: `${new Date(l.ts).toLocaleString('ru-RU')} · ${l.message}`}))) : [K.empty('Логов пока нет')])); } catch (_) {} };
    const loadCatalog = async () => {
      const d = await api('/api/catalog'), items = d.gifts || []; gridCount.textContent = items.length; grid.replaceChildren(...items.slice(0, 120).map(g => h('div', {class: 'list-item', style: {flexDirection: 'column', alignItems: 'center'}}, K.gift(g, 56), h('small', {text: g.name || 'Подарок'}), h('strong', {text: g.price_ton == null ? '?' : K.money(g.price_ton) + ' TON'}))));
      catalogState.replaceChildren(K.notice(d.updated_at ? `Сохранено ${items.length} позиций · фоны: ${items.filter(g => g.background || g.backdrop).length} · PNG: ${items.filter(g => g.image_match).length} · ${new Date(d.updated_at).toLocaleString('ru-RU')}` : 'Каталог ещё не загружен.'));
    };
    const importBtn = h('button', {type: 'button', class: 'secondary', text: 'Обновить каталог'});
    async function watch() {
      clearTimeout(jobTimer); if (!alive) return;
      try {
        const job = await api('/api/admin/portal/job');
        if (job.state === 'running') { importBtn.disabled = true; importBtn.textContent = 'Загрузка…'; const c = Number(job.count || 0); if (c > 0 && c !== rendered) { rendered = c; await loadCatalog(); } catalogState.replaceChildren(K.notice(`Загружено коллекций: ${c}${job.stage === 'images' ? ' · сопоставляем PNG' : ''}`)); loadLogs(); jobTimer = setTimeout(watch, 1400); return; }
        importBtn.disabled = false; importBtn.textContent = 'Обновить каталог'; rendered = -1;
        if (job.state === 'done') { await loadCatalog(); loadLogs(); } if (job.state === 'error') { catalogState.replaceChildren(K.notice(job.error, 'bad')); loadLogs(); }
      } catch (e) { importBtn.disabled = false; catalogState.replaceChildren(K.notice(e.message, 'bad')); }
    }
    importBtn.onclick = () => K.action(importBtn, async () => { await api('/api/admin/portal/import', {key: token.value}); token.value = ''; watch(); });
    async function pollModels() { clearTimeout(modelTimer); if (!alive) return; try { const d = await api('/api/admin/gift-models/status'); modelsState.textContent = d.running ? `Загрузка моделей: ${d.done} / ${d.total}` : `Моделей: ${d.models} · коллекций с моделями: ${d.gifts} · без моделей: ${d.empty}` + (d.error ? ' · ' + d.error : ''); if (d.running) modelTimer = setTimeout(pollModels, 3000); } catch (e) { modelsState.textContent = e.message; } }

    const auto = await api('/api/admin/portal/auto').catch(e => ({error: e.message}));
    const autoOn = K.switchRow('Автообновление цен', 'Каталог и цены подарков обновляются по расписанию.', !!auto.enabled), autoInt = K.number(Number(auto.interval_minutes || 60), {min: 5, step: 1}), autoNext = h('small', {class: 'muted'});
    const paintNext = d => autoNext.textContent = d.error || (d.enabled && d.next_run_at ? 'Следующее обновление: ' + new Date(d.next_run_at * 1000).toLocaleString('ru-RU') : 'Следующее обновление не запланировано.'); paintNext(auto);
    const autoForm = K.form([autoOn, K.field('Интервал, минут', autoInt)], async () => { const d = await api('/api/admin/portal/auto', {enabled: autoOn.input.checked, interval_minutes: autoInt.value}); paintNext(d); K.ok('Автообновление цен сохранено'); });

    root.append(K.head('Portal Market', 'Каталог подарков, цены и автовывод через Portal Partner API.', 'Финансы', K.btn('Обновить', async () => { await Promise.all([loadRuntime(), loadLogs()]); })),
      K.card('Relayer · Portal Partner', 'Для автовывода используется Partner API. Каталог при недоступном Authorization умеет обновляться через публичный endpoint.', metrics, runtime,
        K.form([K.field('Partner token Portal Market аккаунта Relayer', token)], async () => { const d = await api('/api/admin/portal/partner', {token: token.value.trim()}); token.value = ''; paintRuntime(d); await loadLogs(); K.ok(d.error ? 'Token сохранён, но Portal вернул ошибку' : 'Portal Partner подключён'); }, 'Сохранить / проверить'),
        h('div', {class: 'row', style: {flexWrap: 'wrap'}}, importBtn,
          K.btn('Обновить PNG', async () => { const d = await api('/api/admin/portal/images/refresh', {}); await loadCatalog(); K.ok(`PNG: ${d.matched} из ${d.count}`); }),
          K.btn('Обновить модели', async () => { const d = await api('/api/admin/gift-models/refresh', {force: true}); K.ok(d.started ? 'Загрузка моделей запущена' : 'Уже идёт'); pollModels(); })), modelsState, catalogState),
      K.card('Автообновление цен', null, autoForm, autoNext), K.card('Автовыводы Portal', null, payouts), K.card('Логи Portal', null, logCount, logs), K.card('Коллекции', null, gridCount, grid));
    await Promise.all([loadRuntime(), loadLogs(), loadCatalog().catch(e => catalogState.replaceChildren(K.notice(e.message, 'bad')))]); watch(); pollModels();
    return () => { alive = false; clearTimeout(jobTimer); clearTimeout(modelTimer); };
  }});
})();
