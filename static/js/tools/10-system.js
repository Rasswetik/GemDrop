/* System tools: maintenance, sections, games, transfers, loader. */
(() => {
  const K = window.AdminKit, {h, api} = K;

  /* ---------- Технические работы ---------- */
  K.register({id: 'maintenancePage', title: 'Технические работы', async render(root) {
    const d = await api('/api/admin/maintenance');
    const enabled = K.switchRow('Режим технических работ', 'Игроки увидят экран «Технические работы» с обратным отсчётом. Администраторы продолжают пользоваться сайтом.', d.enabled);
    const left = d.ends_at ? Math.ceil((d.ends_at - d.now) / 60000) : 0;
    const minutes = K.number(d.enabled && left > 0 ? left : '', {min: 0, max: 20160, step: 1, placeholder: 'Например, 30', inputMode: 'numeric'});
    const message = K.textarea(d.message || d.default_message || '', {maxLength: 500, rows: 3});
    const status = h('div');
    const paint = s => status.replaceChildren(K.notice(s.enabled ? 'Технические работы включены: игроки видят экран обслуживания.' : 'Технические работы выключены: сайт работает в обычном режиме.', s.enabled ? 'warn' : 'good'));
    paint(d);
    const form = K.form([enabled, K.field('Сколько примерно осталось, минут', minutes, '0 — без таймера. Время можно обновить в любой момент, отсчёт начнётся заново.'), K.field('Сообщение для игроков', message)], async () => {
      const on = enabled.input.checked;
      if (on && !await K.confirm('Включить технические работы? Все игроки, кроме администраторов, увидят экран техработ.', 'Включить', true)) return;
      const body = {enabled: on, message: message.value};
      if (minutes.value !== '') body.minutes = minutes.value;
      paint(await api('/api/admin/maintenance', body)); K.ok(on ? 'Технические работы включены.' : 'Технические работы выключены.');
    });
    root.append(K.head('Технические работы', 'Включайте режим обслуживания перед обновлениями: игроки увидят заставку, а администраторы продолжат работать.', 'Система'), status, K.card('Настройки', null, form));
  }});

  /* ---------- Разделы приложения ---------- */
  K.register({id: 'sectionSettingsPage', title: 'Разделы приложения', async render(root) {
    const d = await api('/api/admin/section-settings'), s = d.sections || {};
    const rows = {
      mines: K.switchRow('Мины', 'Основной режим Mines', s.mines),
      upgrade: K.switchRow('Апгрейды', 'Режим улучшения подарков', s.upgrade),
      giveaways: K.switchRow('Розыгрыши', 'Билеты, активные конкурсы и победители', s.giveaways),
      profile: K.switchRow('Профиль', 'Инвентарь, бонусы и настройки игрока', s.profile),
      black: K.switchRow('Black / Onyx Black', 'Показывать подарки с фонами Black и Onyx Black в каталоге и инвентаре', d.black_backgrounds_enabled)
    };
    const save = async () => {
      const next = {mines: rows.mines.input.checked, upgrade: rows.upgrade.input.checked, giveaways: rows.giveaways.input.checked, profile: rows.profile.input.checked, black_backgrounds_enabled: rows.black.input.checked};
      if (!['mines', 'upgrade', 'giveaways', 'profile'].some(k => next[k])) { K.toast('Оставьте включённым хотя бы один раздел.', 'bad'); for (const k of ['mines', 'upgrade', 'giveaways', 'profile']) rows[k].input.checked = !!s[k]; return; }
      const r = await api('/api/admin/section-settings', next); Object.assign(s, r.sections || {}); K.ok('Настройки разделов сохранены.');
    };
    for (const row of Object.values(rows)) row.input.addEventListener('change', () => { row.input.disabled = true; K.action(null, save).finally(() => row.input.disabled = false); });
    root.append(K.head('Управление разделами', 'Включайте и скрывайте разделы нижней панели приложения. Данные режимов при выключении не удаляются. Изменения применяются сразу.', 'Интерфейс'), h('div', {class: 'stack'}, Object.values(rows)));
  }});

  /* ---------- Управление играми ---------- */
  const GAMES = [['mines', 'Мины'], ['upgrade', 'Апгрейды'], ['crash', 'Crash'], ['arena', 'Арена'], ['hilo', 'Hi-Lo'], ['limbo', 'Limbo'], ['road', 'Hamster Road'], ['plinko', 'Plinko']];
  const BADGES = [['', 'Без значка'], ['new', 'NEW'], ['hot', 'HOT'], ['top', 'TOP'], ['beta', 'BETA'], ['soon', 'СКОРО']];
  const MODES = [['on', 'Включена'], ['admin', 'Только админам'], ['off', 'Выключена']];
  K.register({id: 'gamesAdminPage', title: 'Управление играми', async render(root) {
    let d = await api('/api/admin/section-settings');
    const names = Object.fromEntries(GAMES);
    const clone = o => JSON.parse(JSON.stringify(o));
    let modes = clone(d.game_modes), layout = clone(d.game_layout);
    const norm = l => { const order = (l.order || []).filter(k => names[k]); for (const [k] of GAMES) if (!order.includes(k)) order.push(k); return {order, badges: {...l.badges}, wide: {...l.wide}}; };
    layout = norm(layout);
    let saved = {modes: clone(modes), layout: clone(layout)};
    const list = h('div', {class: 'list'}), save = h('button', {type: 'button', class: 'primary', text: 'Сохранить'}), hint = h('span', {class: 'muted'});
    const dirty = () => JSON.stringify([modes, layout]) !== JSON.stringify([saved.modes, saved.layout]);
    function paint() {
      list.replaceChildren();
      layout.order.forEach((key, i) => {
        const seg = h('div', {class: 'segmented'}, MODES.map(([m, t]) => h('button', {type: 'button', text: t, 'aria-pressed': String(modes[key] === m), onclick: () => { modes[key] = m; paint(); }})));
        const move = dir => () => { const j = i + dir, o = layout.order; [o[i], o[j]] = [o[j], o[i]]; paint(); };
        list.append(h('div', {class: 'list-item', style: {flexWrap: 'wrap'}},
          h('strong', {text: i + 1 + '.', style: {width: '24px'}}),
          h('div', {class: 'grow'}, h('strong', {text: names[key]}), h('small', {text: {on: 'Доступна всем игрокам', admin: 'Видят и запускают только администраторы', off: 'Карточка серая и недоступна'}[modes[key]]})),
          seg,
          K.select(BADGES, layout.badges[key] || '', {'aria-label': 'Значок на карточке', style: {width: '140px'}, onchange: e => { e.target.value ? layout.badges[key] = e.target.value : delete layout.badges[key]; paint(); }}),
          K.select([['0', 'Обычная'], ['1', 'Двойная']], layout.wide[key] ? '1' : '0', {'aria-label': 'Размер карточки', style: {width: '120px'}, onchange: e => { e.target.value === '1' ? layout.wide[key] = true : delete layout.wide[key]; paint(); }}),
          h('div', {class: 'row'}, h('button', {type: 'button', class: 'small', text: '↑', disabled: i === 0, 'aria-label': 'Поднять выше', onclick: move(-1)}), h('button', {type: 'button', class: 'small', text: '↓', disabled: i === layout.order.length - 1, 'aria-label': 'Опустить ниже', onclick: move(1)}))));
      });
      save.disabled = !dirty(); hint.textContent = dirty() ? 'Есть несохранённые изменения' : '';
    }
    save.onclick = () => K.action(save, async () => {
      const body = {};
      if (JSON.stringify(modes) !== JSON.stringify(saved.modes)) body.games = modes;
      if (JSON.stringify(layout) !== JSON.stringify(saved.layout)) body.game_layout = layout;
      const r = await api('/api/admin/section-settings', body);
      if (!r || r.ok !== true) throw new Error('Сервер не подтвердил сохранение');
      if (body.games) { const bad = Object.keys(modes).filter(k => r.game_modes[k] !== modes[k]); if (bad.length) throw new Error('Сервер сохранил другие значения — повторите попытку'); }
      saved = {modes: clone(modes), layout: clone(layout)}; paint(); K.ok('Настройки игр сохранены.');
    });
    paint();
    root.append(K.head('Управление играми', '«Включена» — доступна всем, «Только админам» — игру видят и запускают только администраторы, «Выключена» — карточка остаётся на месте, но недоступна. Стрелками меняется порядок карточек, список справа задаёт значок.', 'Игры'), list, h('div', {class: 'sticky-save'}, hint, save));
  }});

  /* ---------- Переводы между игроками ---------- */
  K.register({id: 'transferSettingsPage', title: 'Переводы между игроками', async render(root) {
    const d = await api('/api/admin/transfers/settings');
    const rows = d.levels.map(x => {
      const fee = K.number(x.fee_percent, {min: 0, max: 30, step: 0.1, 'aria-label': `Комиссия для уровня ${x.level}, %`}), on = h('input', {type: 'checkbox', checked: x.enabled, 'aria-label': `Переводы для уровня ${x.level}`});
      const save = K.btn('Сохранить', async () => { await api(`/api/admin/transfers/settings/${x.level}`, {fee_percent: fee.value, enabled: on.checked}); K.ok(`Уровень ${x.level}: сохранено.`); }, 'small');
      return [h('strong', {text: 'Уровень ' + x.level}), fee, h('label', {class: 'check'}, on, 'Переводы разрешены'), save];
    });
    const wrap = h('div'); K.table(wrap, ['Уровень', 'Комиссия, %', 'Доступ', ''], rows, {cards: true});
    root.append(K.head('Управление переводами', 'Комиссия удерживается сверх суммы перевода. Доступ открывается наградой уровня «Переводы TON».', 'Финансы'), K.card(null, null, wrap));
  }});

  /* ---------- Экран загрузки ---------- */
  K.register({id: 'loaderSettingsPage', title: 'Экран загрузки', async render(root) {
    const d = await api('/api/admin/loader-settings'), builtin = d.path === 'builtin';
    const input = K.input({value: builtin ? '' : d.path, placeholder: '/static/gifs/…'}), preview = h('img', {class: 'preview-img', alt: 'Превью загрузки', src: builtin ? '' : d.path, style: {maxHeight: '220px', display: builtin ? 'none' : 'block'}}), current = h('p', {class: 'muted'});
    const setCurrent = path => { current.textContent = path === 'builtin' ? 'Сейчас: встроенная SVG-анимация' : 'Сейчас: свой файл ' + path; };
    setCurrent(d.path);
    const grid = h('div', {class: 'catalog-grid'});
    if (!d.catalog?.length) grid.append(K.empty('В static/gifs пока нет файлов'));
    for (const item of d.catalog || []) grid.append(h('button', {type: 'button', class: item.path === d.path ? 'on' : '', onclick: e => { input.value = item.path; preview.src = item.path; preview.style.display = 'block'; grid.querySelectorAll('button').forEach(b => b.classList.remove('on')); e.currentTarget.classList.add('on'); }}, h('img', {src: item.path, alt: item.name, loading: 'lazy'}), h('span', {text: item.name})));
    const save = async (path, label) => { const r = await api('/api/admin/loader-settings', {path}); setCurrent(r.path); K.ok(label); };
    root.append(K.head('Экран загрузки', 'Что видят игроки при открытии приложения. Во время Halloween-ивента загрузка автоматически переключается на тыкву.', 'Система'),
      K.card('Встроенная анимация', 'Лёгкая SVG-анимация: не требует загрузки файлов и сама становится статичной в режиме энергосбережения.', K.btn('Использовать встроенную SVG-анимацию', () => save('builtin', 'Включена встроенная SVG-анимация.'), 'primary'), current),
      K.card('Своё изображение или GIF', 'Необязательно. Выберите файл из каталога static/gifs или укажите путь вручную.', h('div', {class: 'stack wide'}, K.field('Путь к изображению', input), preview, h('div', {class: 'group-title', text: 'Каталог static/gifs'}), grid, h('div', null, K.btn('Сохранить своё изображение', () => save(input.value.trim(), 'Экран загрузки сохранён.'), 'primary')))));
  }});
})();
