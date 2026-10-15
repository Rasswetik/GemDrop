/* Halloween event: schedule, mode and banner natively; advanced editors (lots, boss, pass, trick, layout) in an embedded legacy view. */
(() => {
  const K = window.AdminKit, {h, api} = K;
  const stamp = ms => ms ? new Date(ms).toLocaleString('ru-RU', {day: '2-digit', month: 'long', hour: '2-digit', minute: '2-digit'}) : '';
  const MODES = [['full', 'Всё сразу: новое оформление + событие открыто'], ['theme', 'Новое оформление включено, событие закрыто (серая кнопка с отсчётом)'], ['teaser', 'Анонс: обычное оформление, в «Играх» серая кнопка события с отсчётом']];

  K.register({id: 'halloweenPage', title: 'Halloween Event', async render(root) {
    let d = await api('/api/admin/halloween');
    const state = h('div'), banner = h('div');
    const enabled = K.switchRow('Режим Halloween', 'Включает оформление у всех игроков', !!d.enabled), adminsOnly = K.switchRow('Только для администраторов', 'Предпросмотр: событие видят только админы, остальные игроки видят обычный сайт. Выключите — и событие увидят все.', !!d.admins_only), effects = K.switchRow('Фоновые анимации', 'Летучие мыши, призраки, мерцание. Тема остаётся и без них; на слабых устройствах и в режиме энергосбережения анимации отключаются сами.', d.effects !== false);
    const start = K.input({type: 'datetime-local', value: K.localInput(d.starts_at)}), end = K.input({type: 'datetime-local', value: K.localInput(d.ends_at)}), mode = K.select(MODES, d.mode || 'full'), opens = K.input({type: 'datetime-local', value: K.localInput(d.opens_at)}), cardStyle = K.select([['big', 'Большая карточка'], ['normal', 'Обычная карточка (как у игры)'], ['strip', 'Тонкая полоса над режимами']], d.card_style || 'big');
    const opensField = K.field('Когда откроется событие (отсчёт на кнопке)', opens);
    const sync = () => { opensField.hidden = mode.value === 'full'; }; mode.onchange = sync; sync();
    const view = x => {
      d = x; const now = x.now || Date.now(); let tone = '', txt = 'Выключен';
      if (x.active) { tone = 'good'; txt = (x.admins_only ? 'Идёт только для админов' : 'Идёт сейчас') + (x.ends_at ? ' · до ' + stamp(x.ends_at) : ''); if (x.phase === 'teaser') txt = 'Анонс: обычный дизайн, серая кнопка · открытие ' + stamp(x.opens_at); else if (x.phase === 'theme') txt = 'Оформление включено, событие закрыто до ' + stamp(x.opens_at); }
      else if (x.enabled && x.starts_at && now < x.starts_at) { tone = 'warn'; txt = 'Запланирован · старт ' + stamp(x.starts_at); } else if (x.enabled) { tone = 'warn'; txt = 'Включён, но время вышло'; }
      state.replaceChildren(K.notice(txt, tone));
      banner.replaceChildren(x.banner_url && /^(\/|https:)/.test(x.banner_url) ? h('img', {src: x.banner_url + '?' + Date.now() % 1e6, alt: 'Баннер события', style: {maxWidth: '100%', maxHeight: '220px', borderRadius: '12px'}}) : K.empty('Баннер не загружен — показывается рисованная сцена'));
    };
    view(d);
    const save = async extra => {
      const s = start.value ? new Date(start.value).getTime() : 0, e = end.value ? new Date(end.value).getTime() : 0;
      if (s && e && e <= s) throw new Error('Конец должен быть позже начала');
      const op = opens.value ? new Date(opens.value).getTime() : 0; if (mode.value !== 'full' && enabled.input.checked && !op) throw new Error('Укажите, когда откроется событие — это отсчёт на кнопке');
      view(await api('/api/admin/halloween', Object.assign({enabled: enabled.input.checked, admins_only: adminsOnly.input.checked, effects: effects.input.checked, starts_at: s, ends_at: e, mode: mode.value, opens_at: op, card_style: cardStyle.value}, extra || {}))); K.ok('Halloween сохранён');
    };
    const preset = (label, fn) => K.btn(label, async () => { enabled.input.checked = true; start.value = ''; end.value = K.localInput(fn()); await save(); }, 'small');
    const form = K.form([enabled, adminsOnly, effects, h('div', {class: 'form-grid'}, K.field('Начало (пусто — сразу)', start), K.field('Конец (пусто — без ограничения)', end)), K.field('Режим запуска', mode), opensField, K.field('Вид кнопки события на главной', cardStyle)], () => save(), 'Сохранить');
    adminsOnly.input.addEventListener('change', () => { if (enabled.input.checked) K.action(null, () => save()); });
    const file = h('input', {type: 'file', accept: 'image/*'});
    const advanced = h('div');
    root.append(K.head('Halloween Event', 'Тематическое оформление всего приложения: палитра, SVG-иллюстрации игр, летучие мыши и тыквенная загрузка.', 'События'),
      K.card('Состояние', null, state, h('div', {class: 'row', style: {flexWrap: 'wrap'}}, preset('Сутки', () => Date.now() + 864e5), preset('7 дней', () => Date.now() + 7 * 864e5), preset('До 1 ноября', () => { const n = new Date(); return new Date(n.getMonth() >= 10 ? n.getFullYear() + 1 : n.getFullYear(), 10, 1, 0, 0).getTime(); }), K.btn('Выключить сейчас', async () => { enabled.input.checked = false; await save(); }, 'danger small'))),
      K.card('Настройки события', 'Ивент активен, пока переключатель включён и текущее время внутри периода. По окончании оформление вернётся автоматически.', form),
      K.card('Баннер события', 'PNG/JPG/WebP до 3 МБ.', banner, file, h('div', {class: 'row'}, K.btn('Загрузить', async () => { const f = file.files[0]; if (!f) throw new Error('Сначала выберите файл'); if (f.size > 3 * 1024 * 1024) throw new Error('Файл больше 3 МБ'); const fd = new FormData(); fd.append('image', f); view(await api('/api/admin/halloween/banner', fd)); file.value = ''; K.ok('Баннер загружен'); }, 'primary'),
        K.btn('Убрать баннер', async () => { view(await api('/api/admin/halloween/banner', {remove: true})); K.ok('Баннер убран'); }, 'small'))),
      K.card('Лоты, босс, пропуск, «сладость или гадость», кнопки и расположение', 'Редакторы с перетаскиванием и баннерами пока открываются в исходном виде внутри этой страницы.',
        K.btn('Открыть расширенные настройки', async () => { if (advanced.firstChild) { advanced.replaceChildren(); return; } advanced.append(h('iframe', {src: '/admin/tools?page=halloweenPage', title: 'Расширенные настройки Halloween', style: {width: '100%', height: '80vh', border: '1px solid var(--line, #e2e8f0)', borderRadius: '12px', background: '#0e0f1c'}})); }), advanced));
  }});
})();
