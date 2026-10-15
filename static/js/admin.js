(() => {
  'use strict';
  const K = window.AdminKit, $ = K.$, {el, h, money, time, api, msg = K.toast} = K;
  const methods = {ton: 'TON Connect', cryptobot: 'CryptoBot', stars: 'Stars', nft: 'NFT'};
  const state = {view: 'dashboard', offsets: K.offsets, userId: null, chart: null, procurement: null, toolCleanup: null};
  const button = K.btn, action = K.action, table = K.table, pager = K.pager, metric = K.metric;
  K.methods = methods;

  /* Navigation: [id, label, description, keywords]. ids that match a built-in view open that view, the rest are native tools. */
  const NAV = [
    ['Обзор', [['dashboard', 'Статистика', 'Деньги, игры и аудитория за период'], ['settings', 'Все инструменты', 'Полный список разделов панели']]],
    ['Финансы', [
      ['payments', 'История пополнений', 'Реальные пополнения TON, CryptoBot, Stars, NFT'],
      ['nfts', 'Полученные NFT', 'Передачи на Relayer и их зачисление'],
      ['withdrawals', 'Выводы и закупки', 'Заявки на вывод подарков, минимум пополнений'],
      ['grants', 'Начисления администратора', 'Ручные изменения баланса'],
      ['transactionsPage', 'Операции баланса', 'Журнал всех транзакций игроков'],
      ['depositSettingsPage', 'Управление пополнением', 'TON, Stars, Relayer и получение NFT'],
      ['cryptobot', 'CryptoBot', 'Подключение Crypto Pay и webhook'],
      ['portalPage', 'Portal Market', 'Каталог, модели, цены, Partner token'],
      ['transferSettingsPage', 'Переводы между игроками', 'Комиссии и доступ по уровням']]],
    ['Игроки', [
      ['users', 'Пользователи', 'Поиск, карточка игрока, управление'],
      ['promocodesPage', 'Промокоды', 'Шаблоны, коды и награды'],
      ['freebetsAdminPage', 'Freebet', 'Коды и награды'],
      ['giveawaysAdminPage', 'Розыгрыши', 'Призы, билеты и участники'],
      ['rewardTasksAdminPage', 'Задания и билеты', 'Условия и награды'],
      ['leaderboardAdminPage', 'Лидерборд', 'Недельный топ, награды и срок'],
      ['medalsAdminPage', 'Медали', 'Каталог, создание, выдача и тексты'],
      ['wheelAdminPage', 'Колесо призов', 'Призы, шансы и коды'],
      ['levelsAdminPage', 'Уровни и награды', 'Награды за оборот'],
      ['creatorsAdminPage', 'Авторы', 'Demo-режим и управление авторами'],
      ['antifraudPage', 'Мульти-аккаунты и баны', 'Проверка пользователей']]],
    ['Игры', [
      ['gamesAdminPage', 'Управление играми', 'Режимы, порядок и значки игр'],
      ['rtpPage', 'Отдача игр (RTP)', 'Вероятности и лимиты ставок'],
      ['rollAdminPage', 'Roll и шансы', 'Настройка Roll'],
      ['gameHistoryPage', 'История игр', 'Ставки и результаты'],
      ['winsAdminPage', 'Последние победы', 'Лента выигрышей']]],
    ['Коммуникации', [
      ['broadcastAdminPage', 'Рассылка', 'Сообщения и аудитория'],
      ['postAdminPage', 'Публикации', 'Подготовка постов'],
      ['externalChannelsPage', 'Сторонние каналы', 'Источники и подписки'],
      ['botSettingsPage', 'Настройки бота', 'Меню и приветствия'],
      ['noticeTemplatesPage', 'Тексты уведомлений', 'Все уведомления бота']]],
    ['Система', [
      ['sectionSettingsPage', 'Разделы приложения', 'Доступность разделов нижней панели'],
      ['maintenancePage', 'Технические работы', 'Режим обслуживания и таймер'],
      ['loaderSettingsPage', 'Экран загрузки', 'Анимация и свой файл'],
      ['giftEmojiAdminPage', 'Эмодзи подарков', 'Иконки в сообщениях бота']]],
    ['События', [
      ['halloweenPage', 'Halloween', 'Событие, лоты и награды'],
      ['xhuntAdminPage', 'X-Hunt', 'Охота за множителем']]]
  ];
  const BUILTIN = new Set(['dashboard', 'payments', 'nfts', 'withdrawals', 'users', 'grants', 'cryptobot', 'settings']);
  const flat = NAV.flatMap(([group, items]) => items.map(([id, label, desc]) => ({id, label, desc, group, view: BUILTIN.has(id)})));
  K.nav = flat;
  const hrefOf = item => item.view ? '#' + item.id : '#tool=' + encodeURIComponent(item.id);
  let closedGroups = new Set();
  try { closedGroups = new Set(JSON.parse(localStorage.getItem('gd_admin_nav_closed') || '[]')); } catch (_) {}

  function buildNav() {
    const nav = $('navigation'); nav.replaceChildren();
    for (const [group, items] of NAV) {
      const box = h('div', {class: 'nav-group' + (closedGroups.has(group) ? ' closed' : '')});
      const title = h('button', {type: 'button', class: 'nav-title', text: group});
      title.onclick = () => { box.classList.toggle('closed'); box.classList.contains('closed') ? closedGroups.add(group) : closedGroups.delete(group); try { localStorage.setItem('gd_admin_nav_closed', JSON.stringify([...closedGroups])); } catch (_) {} };
      const list = h('div', {class: 'nav-items'});
      for (const [id, label] of items) { const item = flat.find(x => x.id === id); list.append(h('a', {href: hrefOf(item), dataset: {id}, text: label})); }
      box.append(title, list); nav.append(box);
    }
  }
  function markActive(id) {
    document.querySelectorAll('#navigation a').forEach(a => { const on = a.dataset.id === id; a.classList.toggle('active', on); if (on) { a.closest('.nav-group')?.classList.remove('closed'); a.scrollIntoView({block: 'nearest'}); } });
  }

  /* ---------- shell ---------- */
  function closeMenu() { document.body.classList.remove('menu-open'); $('menuShade').hidden = true; $('menuToggle').setAttribute('aria-expanded', 'false'); }
  $('menuToggle').onclick = () => { const open = !document.body.classList.contains('menu-open'); document.body.classList.toggle('menu-open', open); $('menuShade').hidden = !open; $('menuToggle').setAttribute('aria-expanded', String(open)); };
  $('menuShade').onclick = $('menuClose').onclick = closeMenu;
  document.addEventListener('keydown', e => { if (e.key === 'Escape') { closeMenu(); closeSearch(); } if (e.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || '')) { e.preventDefault(); $('globalSearch').focus(); } });
  $('logout').onclick = () => action($('logout'), async () => { await api('/api/logout', {}); try { sessionStorage.setItem('gemdrop_manual_logout', '1'); } catch (_) {} location.assign('/'); });
  document.querySelectorAll('[data-close]').forEach(b => b.onclick = () => b.closest('dialog').close());
  $('refresh').onclick = () => action($('refresh'), () => { if (state.view === 'tool') return openTool(currentToolId(), true); return loadView(); });

  /* ---------- global search ---------- */
  let searchIndex = -1;
  function closeSearch() { $('searchResults').hidden = true; searchIndex = -1; }
  function runSearch() {
    const q = $('globalSearch').value.trim(), box = $('searchResults');
    if (!q) return closeSearch();
    const low = q.toLocaleLowerCase('ru'), hits = flat.filter(x => (x.label + ' ' + x.desc + ' ' + x.group).toLocaleLowerCase('ru').includes(low)).slice(0, 7);
    box.replaceChildren();
    const player = h('a', {href: '#users', dataset: {q}}, h('strong', {text: /^\d+$/.test(q) ? 'Открыть игрока ID ' + q : 'Найти игрока: ' + q}), h('small', {text: 'Имя, @username или Telegram ID'}));
    player.onclick = e => { e.preventDefault(); closeSearch(); $('globalSearch').value = ''; if (/^\d+$/.test(q)) inspector.open(Number(q)); else { location.hash = 'users'; setTimeout(() => { const f = $('userFilters'); f.elements.q.value = q; state.offsets.users = 0; loadUsers().catch(K.fail); }, 50); } };
    box.append(player);
    for (const x of hits) { const a = h('a', {href: hrefOf(x)}, h('strong', {text: x.label}), h('small', {text: x.group + ' · ' + x.desc})); a.onclick = () => { closeSearch(); $('globalSearch').value = ''; }; box.append(a); }
    box.hidden = false; searchIndex = -1;
  }
  $('globalSearch').oninput = K.debounce(runSearch, 120);
  $('globalSearch').onfocus = runSearch;
  $('globalSearch').onkeydown = e => {
    const links = [...$('searchResults').querySelectorAll('a')];
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); if (!links.length) return; searchIndex = (searchIndex + (e.key === 'ArrowDown' ? 1 : -1) + links.length) % links.length; links.forEach((a, i) => a.classList.toggle('on', i === searchIndex)); }
    if (e.key === 'Enter') { e.preventDefault(); (links[searchIndex] || links[Math.min(1, links.length - 1)] || links[0])?.click(); }
  };
  document.addEventListener('click', e => { if (!e.target.closest('.header-search')) closeSearch(); });

  /* ---------- routing ---------- */
  const currentToolId = () => decodeURIComponent((location.hash.slice(1).startsWith('tool=') ? location.hash.slice(6) : '') || '');
  function navigate() {
    const hash = location.hash.slice(1) || 'dashboard', isTool = hash.startsWith('tool=');
    state.view = isTool ? 'tool' : (BUILTIN.has(hash) ? hash : 'dashboard');
    document.querySelectorAll('[data-view]').forEach(n => n.hidden = n.id !== state.view);
    markActive(isTool ? currentToolId() : state.view);
    closeMenu(); window.scrollTo({top: 0});
    if (isTool) return openTool(currentToolId()).catch(K.fail);
    cleanupTool();
    loadView().catch(K.fail);
  }
  window.addEventListener('hashchange', navigate);
  function cleanupTool() { try { state.toolCleanup && state.toolCleanup(); } catch (_) {} state.toolCleanup = null; }
  async function openTool(id) {
    cleanupTool();
    const root = $('toolRoot'), frame = $('toolFrame'), def = K.tools.get(id), item = flat.find(x => x.id === id);
    if (!item) { location.hash = 'settings'; return; }
    document.title = item.label + ' — GemDrop admin';
    root.replaceChildren();
    if (!def) { /* tool not available natively: fall back to the legacy page inside the same panel */
      root.hidden = true; frame.hidden = false; frame.src = '/admin/tools?page=' + encodeURIComponent(id); return;
    }
    frame.hidden = true; frame.removeAttribute('src'); root.hidden = false;
    root.append(h('div', {class: 'crumbs'}, h('a', {href: '#settings', text: 'Инструменты'}), h('span', {text: '›'}), h('span', {text: item.group}), h('span', {text: '›'}), h('b', {text: item.label})));
    const body = h('div'); root.append(body);
    const cleanup = await def.render(body, K);
    if (typeof cleanup === 'function') state.toolCleanup = cleanup;
  }

  /* ---------- tools hub ---------- */
  function renderTools() {
    const q = $('toolSearch').value.trim().toLocaleLowerCase('ru'), root = $('toolList'); root.replaceChildren();
    for (const [group, items] of NAV) {
      const shown = items.map(([id]) => flat.find(x => x.id === id)).filter(x => x.id !== 'dashboard' && x.id !== 'settings' && (x.label + ' ' + x.desc).toLocaleLowerCase('ru').includes(q));
      if (!shown.length) continue;
      root.append(h('div', {class: 'group-title', text: group}), h('div', {class: 'tools'}, shown.map(x => h('a', {class: 'tool-card', href: hrefOf(x)}, h('strong', {text: x.label}), h('small', {text: x.desc})))));
    }
    if (!root.children.length) root.append(K.empty('Ничего не найдено.'));
  }
  $('toolSearch').oninput = renderTools;

  async function loadView() {
    const loader = {dashboard: loadDashboard, payments: loadPayments, nfts: loadNfts, withdrawals: loadWithdrawals, users: loadUsers, grants: loadGrants, cryptobot: loadCrypto, settings: async () => { document.title = 'Инструменты — GemDrop admin'; renderTools(); }}[state.view];
    if (state.view !== 'settings') document.title = (flat.find(x => x.id === state.view)?.label || 'Админ-панель') + ' — GemDrop admin';
    if (loader) await loader();
  }

  /* ---------- shared table helpers ---------- */
  function userCell(row) {
    const n = el('div'), b = el('button', row.name || row.user_name || row.sender_name || 'Игрок'); b.type = 'button';
    const id = Object.hasOwn(row, 'user_id') ? row.user_id : Object.hasOwn(row, 'sender_user_id') ? row.sender_user_id : row.id;
    b.onclick = () => openUser(id);
    if (!id) { b.disabled = true; b.textContent = row.sender_name || 'Отправитель не определён'; }
    n.append(b, el('small', (row.username ? '@' + row.username + ' · ' : '') + (id ? 'ID ' + id : 'ID неизвестен')));
    return n;
  }
  function query(form, key) { const params = new URLSearchParams(new FormData($(form))); params.set('offset', state.offsets[key] || 0); params.set('limit', 50); return params; }

  /* ---------- dashboard ---------- */
  const svgNS = 'http://www.w3.org/2000/svg';
  function svg(tag, attrs, text) { const n = document.createElementNS(svgNS, tag); Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v)); if (text != null) n.textContent = text; return n; }
  const C = {cash: '#059669', nft: '#7c3aed', draw: '#2563eb'};
  function drawChart(id, series, counts = false) {
    const host = $(id), width = Math.max(260, host.clientWidth), height = host.clientHeight || 330, L = 55, R = 12, T = 22, B = 36, w = width - L - R, h2 = height - T - B;
    const canvas = svg('svg', {viewBox: `0 0 ${width} ${height}`}); host.replaceChildren(canvas);
    const peak = Math.max(1, ...series.map(r => counts ? Math.max(r.deposit_count, r.withdrawal_count) : Math.max(r.deposits, r.withdrawals)));
    const max = counts ? Math.max(4, Math.ceil(peak / 4) * 4) : peak * 1.12;
    const x = i => L + (i + .5) * w / series.length, y = n => T + h2 - n / max * h2;
    for (let i = 0; i <= 4; i++) { const yy = T + i * h2 / 4; canvas.append(svg('line', {x1: L, x2: width - R, y1: yy, y2: yy, class: 'grid-line'}), svg('text', {x: L - 7, y: yy + 4, 'text-anchor': 'end'}, counts ? Math.ceil(max * (1 - i / 4)) : Number((max * (1 - i / 4)).toFixed(2)).toLocaleString('ru-RU'))); }
    const step = Math.max(1, Math.ceil(series.length / (width < 500 ? 3 : 7)));
    series.forEach((r, i) => { if (i % step === 0 || i === series.length - 1) canvas.append(svg('text', {x: x(i), y: height - 9, 'text-anchor': 'middle'}, r.day.slice(8) + '.' + r.day.slice(5, 7))); });
    if (!counts) { const bw = Math.max(1, Math.min(30, w / series.length * .7)); series.forEach((r, i) => { const cash = Math.max(0, r.deposits - r.nft); canvas.append(svg('rect', {x: x(i) - bw / 2, y: y(cash), width: bw, height: h2 * cash / max, fill: C.cash, rx: 2}), svg('rect', {x: x(i) - bw / 2, y: y(r.deposits), width: bw, height: h2 * r.nft / max, fill: C.nft, rx: 2})); }); }
    for (const [field, color] of (counts ? [['deposit_count', C.cash], ['withdrawal_count', C.draw]] : [['withdrawals', C.draw]])) {
      const path = series.map((r, i) => (i ? 'L' : 'M') + x(i) + ',' + y(r[field])).join(' ');
      canvas.append(svg('path', {d: path, fill: 'none', stroke: color, 'stroke-width': 2.5, 'stroke-linejoin': 'round', 'stroke-linecap': 'round'}));
      if (series.length === 1) canvas.append(svg('circle', {cx: x(0), cy: y(series[0][field]), r: 4, fill: color}));
    }
    const marker = svg('line', {x1: 0, x2: 0, y1: T, y2: T + h2, stroke: '#64748b', 'stroke-dasharray': '4 3', visibility: 'hidden'}); canvas.append(marker);
    const out = counts ? 'countPoint' : 'chartPoint';
    function point(e) {
      const rect = canvas.getBoundingClientRect(), xx = (e.clientX - rect.left) * width / rect.width, i = Math.max(0, Math.min(series.length - 1, Math.floor((xx - L) / w * series.length))), r = series[i]; if (!r) return;
      marker.setAttribute('x1', x(i)); marker.setAttribute('x2', x(i)); marker.setAttribute('visibility', 'visible');
      $(out).textContent = counts ? `${r.day} · Пополнений ${r.deposit_count} · Выводов ${r.withdrawal_count}` : `${r.day} · Пополнения ${money(r.deposits)} TON (${r.deposit_count}), из них NFT ${money(r.nft)} TON · Выводы ${money(r.withdrawals)} TON (${r.withdrawal_count})`;
    }
    canvas.onpointermove = point; canvas.onclick = point;
  }
  let dashboardRevision = 0;
  async function loadDashboard() {
    const revision = ++dashboardRevision;
    const [d, pending] = await Promise.all([api('/api/admin/dashboard?days=' + $('chartDays').value), api('/api/admin/withdrawals?view=pending').catch(() => ({items: []}))]);
    if (revision !== dashboardRevision) return;
    state.chart = d; const t = d.totals, net = t.deposits - t.withdrawals, ggr = t.stake - t.payout, margin = t.stake ? ggr / t.stake * 100 : null;
    const att = $('attention'); att.replaceChildren();
    if (pending.items.length) { const n = K.notice('', 'warn'); n.append(h('strong', {text: `Ожидают вывода: ${pending.items.length} ${pending.items.length === 1 ? 'заявка' : 'заявок'}. `}), h('a', {href: '#withdrawals', text: 'Открыть выводы →'})); att.append(n); }
    $('metricsMoney').replaceChildren(
      metric('Реальные пополнения', money(t.deposits) + ' TON', K.num(t.deposit_count) + ' операций' + (t.deposit_count ? ' · средний чек ' + money(t.deposits / t.deposit_count) : '')),
      metric('Завершённые выводы NFT', money(t.withdrawals) + ' TON', K.num(t.withdrawal_count) + ' подарков', 'violet'),
      metric('Чистый поток', K.signed(net) + ' TON', 'Пополнения минус выводы', net >= 0 ? 'good' : 'bad'),
      metric('Ожидают вывода', K.num(t.pending_count), money(t.pending_value) + ' TON в очереди', t.pending_count ? 'warn' : 'sky'));
    $('metricsGames').replaceChildren(
      metric('Игровой оборот', money(t.stake) + ' TON', K.num(t.game_count || 0) + ' завершённых игр' + (t.game_count ? ' · средняя ставка ' + money(t.stake / t.game_count) : '')),
      metric('Доход проекта (GGR)', K.signed(ggr) + ' TON', margin == null ? 'Нет ставок' : 'Маржа ' + K.pct(margin), ggr >= 0 ? 'good' : 'bad'),
      metric('Фактический RTP', t.stake ? K.pct(t.payout / t.stake * 100, 2) : '—', 'Возврат игрокам: ' + money(t.payout) + ' TON', 'violet'),
      metric('Играли за период', K.num(t.active_players || 0), t.users ? K.pct((t.active_players || 0) / t.users * 100) + ' от всех пользователей' : '', 'sky'));
    $('metricsPeople').replaceChildren(
      metric('Пользователи', K.num(t.users), K.num(t.new_users || 0) + ' новых за период'),
      metric('Пополняли за период', K.num(t.depositors || 0), t.active_players ? 'Конверсия игроков в плательщиков: ' + K.pct((t.depositors || 0) / t.active_players * 100) : '', 'good'),
      metric('Балансы игроков', money(t.balance) + ' TON', 'Обязательства перед игроками', 'warn'),
      metric('Начисления администратора', money(t.admin_grants) + ' TON', 'Не входят в пополнения', 'violet'));
    drawChart('moneyChart', d.series); drawChart('countChart', d.series, true);
    window.AdminCharts.render({el, svg, money, methods}, d);
    const maxStake = Math.max(1, ...d.games.map(g => g.stake));
    K.table($('gamesTable'), ['Игра', 'Раундов', 'Игроков', 'Оборот, TON', 'Возврат, TON', 'GGR, TON', 'RTP'], d.games.map(g => {
      const bar = h('div', {class: 'bar-row', style: {gridTemplateColumns: '1fr', gap: '5px'}}, h('strong', {text: g.label}), h('div', {class: 'bar-track'}, h('div', {class: 'bar-fill', style: {width: Math.max(2, g.stake / maxStake * 100) + '%'}})));
      return [bar, K.num(g.count), K.num(g.players), money(g.stake), money(g.payout), h('span', {class: g.ggr >= 0 ? 'pos' : 'neg', text: K.signed(g.ggr)}), g.rtp == null ? '—' : K.pct(g.rtp, 2)];
    }), {numeric: [1, 2, 3, 4, 5, 6], empty: 'За выбранный период завершённых игр нет.'});
    table($('seriesTable'), ['Дата UTC', 'Пополнения TON', 'Из них NFT', 'Выводы TON', 'Оборот TON', 'Возврат TON', 'Новые', 'Играли'], d.series.map(r => [r.day, money(r.deposits), money(r.nft), money(r.withdrawals), money(r.stake), money(r.payout), r.new_users, r.active_players]), {cards: false, numeric: [1, 2, 3, 4, 5, 6, 7]});
  }
  $('chartDays').onchange = () => loadDashboard().catch(K.fail);
  $('exportCsv').onclick = () => {
    const d = state.chart; if (!d) return;
    const rows = [['date', 'deposits_ton', 'nft_ton', 'withdrawals_ton', 'deposit_count', 'withdrawal_count', 'stake_ton', 'payout_ton', 'games', 'new_users', 'active_players', 'depositors']].concat(d.series.map(r => [r.day, r.deposits, r.nft, r.withdrawals, r.deposit_count, r.withdrawal_count, r.stake, r.payout, r.game_count, r.new_users, r.active_players, r.depositors]));
    const url = URL.createObjectURL(new Blob(['﻿' + rows.map(r => r.join(',')).join('\n')], {type: 'text/csv;charset=utf-8'}));
    const a = h('a', {href: url, download: `gemdrop-stats-${d.days}d.csv`}); document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  new ResizeObserver(() => { if (state.chart && state.view === 'dashboard') { drawChart('moneyChart', state.chart.series); drawChart('countChart', state.chart.series, true); window.AdminCharts.render({el, svg, money, methods}, state.chart); } }).observe($('moneyChart'));

  /* ---------- payments / nfts / users / grants ---------- */
  function paymentRows(items) { return items.map(r => { const nft = el('div', r.gift_name || methods[r.method] || r.method); if (r.model) nft.append(el('small', r.model)); if (/^https:\/\/(t\.me|fragment\.com)\//.test(r.external_url || '')) { const a = el('a', 'Открыть NFT'); a.href = r.external_url; a.target = '_blank'; a.rel = 'noopener'; nft.append(el('br'), a); } const ref = el('div', r.reference); ref.append(el('small', time(r.created_at))); return [userCell(r), K.badge(methods[r.method] || r.method, r.method === 'nft' ? 'violet' : 'accent'), money(r.amount) + ' TON', nft, ref]; }); }
  let paymentRevision = 0;
  async function loadPayments() { const rev = ++paymentRevision, d = await api('/api/admin/payments?' + query('paymentFilters', 'payments')); if (rev !== paymentRevision) return; table($('paymentTable'), ['Пользователь', 'Способ', 'Сумма', 'Подарок / назначение', 'Операция / время'], paymentRows(d.items)); pager($('paymentPager'), d, 'payments', loadPayments); }
  async function loadNfts() {
    const d = await api('/api/admin/nft-receipts?' + query('nftFilters', 'nfts'));
    const statuses = {delivered: ['Зачислен в инвентарь', 'good'], credited: ['Зачислен на баланс', 'good'], unpriced: ['Нет оценки', 'warn'], unmatched: ['Не найден отправитель', 'bad'], baseline: ['Был на аккаунте до запуска', ''], not_nft: ['Не NFT', '']};
    table($('nftTable'), ['Отправитель', 'NFT', 'Portal / принято', 'Результат', 'Получено'], d.items.map(r => { const gift = el('div', r.gift_name + ' #' + (r.fragment_number || '—')); if (/^https:\/\/t\.me\/nft\//.test(r.external_url)) { const a = el('a', 'Открыть'); a.href = r.external_url; a.target = '_blank'; a.rel = 'noopener'; gift.append(el('br'), a); } const st = statuses[r.status] || [r.status, '']; return [userCell(r), gift, money(r.portal_price) + ' / ' + money(r.floor_price) + ' TON', K.badge(st[0], st[1]), time(r.created_at)]; }));
    pager($('nftPager'), d, 'nfts', loadNfts);
  }
  let userRevision = 0;
  async function loadUsers() {
    const rev = ++userRevision, d = await api('/api/admin/players?' + query('userFilters', 'users')); if (rev !== userRevision) return;
    table($('userTable'), ['Пользователь', 'Регистрация', 'Баланс / бонус', 'Пополнения', 'Остаток отыгрыша', 'Статус'], d.items.map(r => [userCell(r), r.created_at ? time(r.created_at) : '—', money(r.balance) + ' / ' + money(r.bonus_balance) + ' TON', money(r.deposits) + ' TON', money(r.wager_remaining) + ' TON', r.banned ? K.badge('Заблокирован', 'bad') : r.withdrawal_blocked ? K.badge('Вывод ограничен', 'warn') : K.badge('Вывод доступен', 'good')]));
    pager($('userPager'), d, 'users', loadUsers);
  }
  async function loadGrants() {
    const d = await api('/api/admin/grants?offset=' + (state.offsets.grants || 0) + '&limit=50');
    const names = {admin_deposit: 'Начисление на основной', main_balance_add: 'Добавление на основной', main_balance_set: 'Установка основного', bonus_balance_add: 'Добавление бонусов', bonus_balance_set: 'Установка бонусов', referral_balance_add: 'Добавление на реферальный', referral_balance_set: 'Установка реферального'};
    table($('grantTable'), ['Пользователь', 'Сумма операции', 'Операция / администратор', 'Время'], d.items.map(r => [userCell(r), h('strong', {class: r.amount >= 0 ? 'pos' : 'neg', text: K.signed(r.amount) + ' TON'}), (names[r.action] || r.action) + ' · admin ' + r.admin_id, time(r.created_at)]));
    pager($('grantPager'), d, 'grants', loadGrants);
  }
  for (const [id, key, fn] of [['paymentFilters', 'payments', loadPayments], ['nftFilters', 'nfts', loadNfts], ['userFilters', 'users', loadUsers]]) $(id).onsubmit = e => { e.preventDefault(); state.offsets[key] = 0; fn().catch(K.fail); };
  $('userFilters').elements.q.oninput = K.debounce(() => { state.offsets.users = 0; loadUsers().catch(K.fail); }, 300);

  /* ---------- user inspector wiring ---------- */
  function paintState(s) { const root = $('withdrawalState'); root.replaceChildren(el('strong', s.allowed ? 'Общий доступ к выводу открыт' : 'Вывод ограничен', s.allowed ? 'good' : 'bad')); s.reasons.forEach(r => root.append(el('p', r.text, 'bad'))); root.append(el('p', `Учтено пополнений: ${money(s.deposited)} TON · Минимум: ${money(s.minimum)} TON`), el('p', `Отыгрыш: ${money(s.wager_progress)} / ${money(s.wager_required)} TON · Осталось: ${money(s.wager_remaining)} TON`)); $('wagerProgress').max = Math.max(1, s.wager_required); $('wagerProgress').value = Math.min(s.wager_progress, s.wager_required); $('wagerForm').elements.amount.value = Number(s.wager_remaining).toFixed(2); $('unlockStars').hidden = !s.stars_until; }
  async function openUser(id) { return inspector.open(id); }
  K.openUser = openUser;
  async function loadUser(id = state.userId) { return inspector.load(id); }
  async function saveUser(path, data, method) { const uid = state.userId; if (state.userLoadedId !== uid) throw new Error('Дождитесь загрузки карточки.'); const result = await api('/api/admin/users/' + uid + path, data, method); if (state.userId !== uid) return; await loadUser(uid); if (state.userId !== uid) return; if (state.view === 'users') await loadUsers(); if (state.userId !== uid) return; msg('Сохранено.', 'good'); return result; }
  $('wagerForm').onsubmit = e => { e.preventDefault(); const mode = e.submitter?.value || 'set_remaining'; action(e.submitter, () => saveUser('/wager', {mode, amount: $('wagerForm').elements.amount.value})); };
  $('clearWager').onclick = () => action($('clearWager'), () => saveUser('/wager', {mode: 'clear'}));
  $('accessForm').onsubmit = e => { e.preventDefault(); const f = e.target.elements; action(e.submitter, () => saveUser('/withdrawal-access', {enabled: f.enabled.checked, reason: f.reason.value, min_ton_connect_deposit_override: f.minimum.value === '' ? null : f.minimum.value})); };
  $('unlockStars').onclick = () => action($('unlockStars'), () => saveUser('/stars-withdrawal-unlock', {}));
  $('balanceForm').onsubmit = e => { e.preventDefault(); const f = e.target.elements; action(e.submitter, () => saveUser('/balance', {kind: f.kind.value, mode: f.mode.value, amount: f.amount.value})); };
  $('ticketsForm').onsubmit = e => { e.preventDefault(); const f = e.target.elements; action(e.submitter, () => saveUser('/tickets', {mode: f.mode.value, amount: f.amount.value})); };
  $('banForm').onsubmit = e => { e.preventDefault(); const f = e.target.elements; action(e.submitter, () => saveUser('/ban', {banned: f.banned.checked, reason: f.reason.value})); };

  /* ---------- withdrawals ---------- */
  async function loadWithdrawals() {
    const [d, settings] = await Promise.all([api('/api/admin/withdrawals?view=' + $('withdrawalView').value), api('/api/admin/withdrawal-settings')]);
    $('minimumForm').elements.minimum.value = settings.min_ton_connect_deposit;
    const root = $('withdrawalList'); root.replaceChildren();
    if (!d.items.length) root.append(el('div', 'Заявок нет.', 'empty'));
    for (const r of d.items) {
      const card = el('article', null, 'card');
      card.append(h('div', {class: 'card-head'}, h('h2', {text: `#${r.id} · ${r.gift_name} · ${money(r.price_ton)} TON`}), K.badge(r.status, r.status === 'pending' ? 'warn' : r.status === 'approved' ? 'good' : 'bad')));
      const facts = el('div', null, 'facts'); facts.append(userCell(r), el('span', time(r.created_at))); card.append(facts);
      if (r.auto_status) card.append(el('p', `Доставка: ${r.auto_status}`, r.auto_error ? 'bad' : 'muted'));
      if (r.auto_error) card.append(el('p', r.auto_error, 'bad'));
      if (r.status === 'pending') {
        const actions = el('div', null, 'actions');
        actions.append(button('Закупка: коллекция + модель', () => openProcurement(r.id)), button('Повторить доставку', async () => { await api('/api/admin/withdrawals/' + r.id + '/retry', {}); msg('Доставка поставлена в очередь.', 'good'); await loadWithdrawals(); }), button('Подтвердить ручную передачу', async () => { if (!await K.confirm('NFT уже передан пользователю? Подтверждение завершит заявку.')) return; await api('/api/admin/withdrawals/' + r.id + '/approve', {}); await loadWithdrawals(); }, 'primary'), button('Отклонить и вернуть NFT', async () => { if (!await K.confirm('Отклонить заявку и вернуть NFT с комиссией пользователю?', 'Отклонить', true)) return; await api('/api/admin/withdrawals/' + r.id + '/reject', {}); await loadWithdrawals(); }, 'danger'));
        card.append(actions);
      }
      root.append(card);
    }
  }
  $('minimumForm').onsubmit = e => { e.preventDefault(); action(e.submitter, async () => { await api('/api/admin/withdrawal-settings', {min_ton_connect_deposit: $('minimumForm').elements.minimum.value}); msg('Общий минимум сохранён.', 'good'); }); };
  $('withdrawalView').onchange = () => loadWithdrawals().catch(K.fail);
  async function openProcurement(id) { const d = await api('/api/admin/withdrawals/' + id + '/procurement'); state.procurement = d; $('procurementDetails').replaceChildren(el('p', 'Коллекция: ' + d.collection), el('p', 'Модель: ' + (d.model || 'Любая модель этой коллекции')), el('p', 'Фон: ' + (d.backdrop || 'Любой')), el('p', 'Relayer: @' + d.relay_username), el('p', 'Получатель: ID ' + d.recipient_id)); if (d.number) $('procurementDetails').append(el('p', 'Номер исходного NFT: ' + d.number)); $('procurementSteps').replaceChildren(...d.steps.map(x => el('li', x))); $('portalOpen').href = d.portal_url; $('procurementDialog').showModal(); }
  $('copyTraits').onclick = () => action($('copyTraits'), async () => { const d = state.procurement; await navigator.clipboard.writeText([d.collection, d.model, d.backdrop].filter(Boolean).join(' · ')); msg('Коллекция и модель скопированы.', 'good'); });
  $('retryDelivery').onclick = () => action($('retryDelivery'), async () => { await api('/api/admin/withdrawals/' + state.procurement.id + '/retry', {}); $('procurementDialog').close(); await loadWithdrawals(); msg('Доставка поставлена в очередь.', 'good'); });

  /* ---------- CryptoBot ---------- */
  function paintCrypto(d) { const f = $('cryptoForm').elements; f.enabled.checked = d.enabled; f.testnet.checked = d.testnet; f.token.value = ''; f.token.disabled = d.env_token; $('cryptoWebhook').value = d.webhook_url || ''; $('copyWebhook').disabled = !d.webhook_url; $('cryptoState').textContent = d.configured ? `${d.app_name || 'Crypto Pay'} · приложение ${d.app_id || '—'} · ${d.testnet ? 'тестовая' : 'основная'} сеть · ${d.enabled ? 'пополнение включено' : 'пополнение скрыто'}` : 'Приложение ещё не подключено.'; }
  async function loadCrypto() { paintCrypto(await api('/api/admin/cryptobot')); }
  $('cryptoForm').onsubmit = e => { e.preventDefault(); const f = e.target.elements; action(e.submitter, async () => { const d = await api('/api/admin/cryptobot', {token: f.token.value, enabled: f.enabled.checked, testnet: f.testnet.checked}); paintCrypto(d); msg('Приложение проверено, настройки сохранены.', 'good'); }); };
  $('copyWebhook').onclick = () => action($('copyWebhook'), async () => { await navigator.clipboard.writeText($('cryptoWebhook').value); msg('URL скопирован.', 'good'); });

  const inspector = window.AdminInspector.create({api, el, table, pager: (id, d, key, fn) => pager($(id), d, key, fn), paymentRows, userCell, msg, time, money, metric, state, paintState, action, saveUser});
  buildNav();
  api('/api/me').then(d => { $('operator').textContent = d.user.name || 'ID ' + d.user.id; navigate(); }).catch(e => msg(e.message, 'bad'));
})();
