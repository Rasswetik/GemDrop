/* Players: levels and creators. */
(() => {
  const K = window.AdminKit, {h, api} = K;

  /* ---------- Уровни и награды ---------- */
  const REWARDS = [['none', 'Без награды'], ['balance', 'TON на баланс'], ['tickets', 'Билеты розыгрыша'], ['gift', 'Подарок'], ['wager_gift', 'Отыгрышный подарок'], ['personal_promo', 'Личный промокод'], ['deposit_promo', 'Промокод на депозит'], ['multi_promo', 'Мультипромокод'], ['transfer_unlock', 'Разблокировать переводы TON']];
  const serialize = r => ({...r, amount: r.amount_ton, bonus_fixed: r.bonus_fixed_ton, min_deposit: r.min_deposit_ton,
    components: Object.fromEntries(Object.entries(r.components || {}).map(([k, v]) => [k, {...v, amount: v.amount_ton, bonus_fixed: v.bonus_fixed_ton, min_deposit: v.min_deposit_ton}]))});

  K.register({id: 'levelsAdminPage', title: 'Уровни и награды', async render(root) {
    let rows = [], gifts = [];
    const list = h('div', {class: 'stack'});
    const giftOpts = () => gifts.map(g => [String(g.id), g.name]);
    const fld = (parent, label, value, set, type, options) => {
      const c = options ? K.select(options, value ?? options[0][0], {onchange: e => set(e.target.value)}) : type === 'number' ? K.number(value ?? '', {min: 0, step: '0.01', oninput: e => set(e.target.value)}) : K.input({value: value ?? '', oninput: e => set(e.target.value)});
      parent.append(K.field(label, c)); return c;
    };
    const giftFields = (parent, o, wager) => {
      fld(parent, 'Подарок из Portal', o.gift_id || gifts[0]?.id || '', v => o.gift_id = v, 'text', giftOpts());
      if (wager) { fld(parent, 'Отыгрыш X', o.wager_multiplier || 25, v => o.wager_multiplier = v, 'number'); fld(parent, 'Сгорание подарка, дней · 0 = выключено', o.gift_expires_days || 0, v => o.gift_expires_days = v, 'number'); }
    };
    const multi = (parent, r) => {
      r.components = r.components || {};
      for (const [kind, title] of [['balance', 'Баланс TON'], ['gift', 'Обычный подарок'], ['wager_gift', 'Отыгрышный подарок'], ['deposit_bonus', 'Бонус на пополнение']]) {
        const box = K.check(title, !!r.components[kind], {onchange: e => {
          if (e.target.checked) r.components[kind] = kind === 'balance' ? {amount_ton: '1.00'} : kind === 'gift' ? {gift_id: gifts[0]?.id || ''} : kind === 'wager_gift' ? {gift_id: gifts[0]?.id || '', wager_multiplier: 25, gift_expires_days: 0} : {bonus_percent: 30, bonus_fixed_ton: 0, min_deposit_ton: 0};
          else delete r.components[kind];
          paint();
        }});
        parent.append(box);
        const c = r.components[kind]; if (!c) continue;
        if (kind === 'balance') fld(parent, 'Сумма TON', c.amount_ton ?? '1.00', v => c.amount_ton = v, 'number');
        if (kind === 'gift' || kind === 'wager_gift') giftFields(parent, c, kind === 'wager_gift');
        if (kind === 'deposit_bonus') { fld(parent, 'Бонус, %', c.bonus_percent || 0, v => c.bonus_percent = v, 'number'); fld(parent, 'Бонус, TON', c.bonus_fixed_ton || 0, v => c.bonus_fixed_ton = v, 'number'); fld(parent, 'Минимальный депозит, TON', c.min_deposit_ton || 0, v => c.min_deposit_ton = v, 'number'); }
      }
    };
    function paint() {
      list.replaceChildren();
      for (const item of rows) {
        const r = item.reward || (item.reward = {type: 'none'});
        const fields = h('div', {class: 'form-grid'});
        const card = K.card('Уровень ' + item.level, null, h('div', {class: 'form-grid'},
          K.field('Необходимый оборот, TON', K.number(item.required_turnover, {min: 0, step: '0.01', oninput: e => item.required_turnover = e.target.value})),
          K.field('Награда', K.select(REWARDS, r.type, {onchange: e => {
            const v = r.type = e.target.value;
            if (v === 'balance' && !r.amount_ton) r.amount_ton = '1.00';
            if (v === 'tickets' && !r.tickets) r.tickets = 10;
            if (v === 'personal_promo') { r.promo_reward_type = r.promo_reward_type || 'balance'; r.amount_ton = r.amount_ton || '1.00'; }
            if ((v === 'gift' || v === 'wager_gift') && !r.gift_id) r.gift_id = gifts[0]?.id || '';
            if (v === 'wager_gift' && !r.wager_multiplier) r.wager_multiplier = 25;
            if (v === 'wager_gift' && r.gift_expires_days == null) r.gift_expires_days = 0;
            if (v === 'deposit_promo' && !r.bonus_percent && !r.bonus_fixed_ton) r.bonus_percent = 30;
            paint();
          }}))), fields);
        if (item.level > 1) card.querySelector('.card-head')?.append(K.btn('Удалить', async () => {
          if (!await K.confirm(`Удалить уровень ${item.level}? Следующие уровни будут автоматически сдвинуты.`, 'Удалить', true)) return;
          await K.del(`/api/admin/levels/${item.level}`); await load(); K.ok(`Уровень ${item.level} удалён`);
        }, 'small danger'));
        const content = r.type === 'personal_promo' ? r.promo_reward_type || 'balance' : r.type;
        if (r.type === 'personal_promo') fld(fields, 'Содержимое промокода', content, v => {
          r.promo_reward_type = v;
          if (v === 'balance' && !r.amount_ton) r.amount_ton = '1.00';
          if (v === 'gift' && !r.gift_id) r.gift_id = gifts[0]?.id || '';
          if (v === 'wager_gift') { r.gift_id = r.gift_id || gifts[0]?.id || ''; r.wager_multiplier = r.wager_multiplier || 25; if (r.gift_expires_days == null) r.gift_expires_days = 0; }
          paint();
        }, 'text', [['balance', 'TON на баланс'], ['gift', 'Подарок'], ['wager_gift', 'Отыгрышный подарок']]);
        if (content === 'balance') fld(fields, 'Сумма TON', r.amount_ton || '1.00', v => r.amount_ton = v, 'number');
        if (content === 'tickets') fld(fields, 'Количество билетов', r.tickets || 10, v => r.tickets = v, 'number');
        if (content === 'gift' || content === 'wager_gift') giftFields(fields, r, content === 'wager_gift');
        if (content === 'deposit_promo') { fld(fields, 'Бонус, % (или 0)', r.bonus_percent || 0, v => r.bonus_percent = v, 'number'); fld(fields, 'Фиксированный бонус, TON (или 0)', r.bonus_fixed_ton || 0, v => r.bonus_fixed_ton = v, 'number'); fld(fields, 'Минимальный депозит, TON', r.min_deposit_ton || 0, v => r.min_deposit_ton = v, 'number'); }
        if (content === 'multi_promo') multi(fields, r);
        if (['personal_promo', 'deposit_promo', 'multi_promo'].includes(r.type)) fld(fields, 'Срок промокода, дней · 0 = без срока', r.expires_days || 0, v => r.expires_days = v, 'number');
        list.append(card);
      }
    }
    const saveAll = async (silent) => { await api('/api/admin/levels/bulk', {levels: rows.map(i => ({level: i.level, required_turnover: i.required_turnover, reward: serialize(i.reward || {type: 'none'})}))}); if (!silent) K.ok('Все уровни и награды сохранены'); };
    async function load() { const [l, c] = await Promise.all([api('/api/admin/levels'), K.catalog()]); gifts = c; rows = l.levels; paint(); }
    await load();
    root.append(K.head('Уровни и награды', 'Оборот начисляется за ставки Mines, Upgrade и платные прокрутки Roll. После удаления уровня следующие автоматически сдвигаются.', 'Игроки',
      K.btn('＋ Добавить уровень', async () => { await saveAll(true); await api('/api/admin/levels', {}); await load(); K.ok('Новый уровень добавлен'); }),
      K.btn('Применить план 100 уровней', async () => {
        if (!await K.confirm('Заменить все уровни и награды планом на 100 уровней? Подарки будут подобраны из каталога Portal. Оборот и полученные награды игроков сохранятся.', 'Применить', true)) return;
        const d = await api('/api/admin/levels/plan/apply', {confirm: 'APPLY'}); K.ok('План применён: ' + d.levels + ' уровней'); await load();
      }),
      K.btn('Сохранить все уровни', () => saveAll(false), 'primary')), list);
  }});

  /* ---------- Авторы ---------- */
  const TIERS = [['base', 'Base'], ['creator', 'Creator'], ['super_creator', 'Super Creator'], ['god', 'God']];
  K.register({id: 'creatorsAdminPage', title: 'Управление авторами', async render(root) {
    let items = [], picked = null;
    const search = K.input({placeholder: 'ID, имя или @username', 'aria-label': 'Поиск пользователя'}), results = h('div', {class: 'list'}), chosen = h('div'), addBtn = h('button', {type: 'button', class: 'primary', text: 'Добавить автора', disabled: true}), list = h('div', {class: 'list'}), count = h('span', {class: 'muted'});
    const who = u => (u.name || 'Игрок') + (u.username ? ' · @' + u.username : '') + ' · ID ' + u.id;
    const pick = u => {
      picked = u; results.replaceChildren(); const exists = items.some(x => Number(x.id) === Number(u.id));
      chosen.replaceChildren(K.notice(who(u) + (exists ? ' — уже автор' : ' — будет выдан доступ'), exists ? 'warn' : 'good')); addBtn.disabled = exists;
    };
    const find = async q => {
      const d = await api('/api/admin/users' + (q ? '?q=' + encodeURIComponent(q) : ''), undefined);
      results.replaceChildren(...(d.users?.length ? d.users.map(u => h('button', {type: 'button', class: 'list-item', onclick: () => pick(u)}, h('div', {class: 'grow'}, h('strong', {text: u.name || 'Игрок'}), h('small', {text: (u.username ? '@' + u.username + ' · ' : '') + 'ID ' + u.id})), K.badge(items.some(x => Number(x.id) === Number(u.id)) ? 'Уже автор' : 'Выбрать'))) : [K.empty('Пользователи не найдены')]));
    };
    const debounced = K.debounce(q => find(q).catch(K.fail), 250);
    search.oninput = () => { picked = null; chosen.replaceChildren(); addBtn.disabled = true; const q = search.value.trim(); q ? debounced(q) : results.replaceChildren(); };
    addBtn.onclick = () => K.action(addBtn, async () => { await api(`/api/admin/creators/${picked.id}`, {}); K.ok('Автор добавлен. Кнопка «Панель автора» выдана.'); picked = null; chosen.replaceChildren(); search.value = ''; await load(); });
    function paint() {
      count.textContent = items.length; list.replaceChildren();
      if (!items.length) list.append(K.empty('Авторов пока нет. Найдите пользователя выше и добавьте его.'));
      for (const u of items) {
        const scale = h('small'), paintScale = () => { const cap = Number(u.creator_level_info?.daily_budget_ton || 0), used = Number(u.limit_usage?.budget_used || 0); scale.textContent = `Шкала: ${K.money(used)} / ${K.money(cap)} TON` + (u.creator_level_info?.wager_daily_limit ? ` · отыгрышных ${Number(u.limit_usage?.wager_count || 0)}/${u.creator_level_info.wager_daily_limit}` : ''); };
        paintScale();
        const tier = K.select(TIERS, u.creator_level || 'base', {'aria-label': 'Уровень автора', onchange: async e => { const old = u.creator_level || 'base'; e.target.disabled = true; try { const x = await api(`/api/admin/creators/${u.id}/level`, {level: e.target.value}); u.creator_level = x.creator_level; K.ok('Уровень автора изменён'); } catch (err) { e.target.value = old; K.fail(err); } finally { e.target.disabled = false; } }});
        const pct = K.number(100, {min: 1, max: 100, step: 1, 'aria-label': 'Процент восстановления шкалы', style: {width: '80px'}});
        list.append(h('div', {class: 'list-item', style: {flexWrap: 'wrap'}},
          h('div', {class: 'grow'}, h('button', {type: 'button', text: u.name || 'Игрок', onclick: () => K.openUser(u.id)}), h('small', {text: (u.username ? '@' + u.username + ' · ' : '') + 'ID ' + u.id}), h('small', {text: (u.demo_enabled ? 'DEMO включён' : 'Обычный режим') + (u.panel_hidden ? ' · панель скрыта' : ' · панель видна')}), scale),
          h('div', {class: 'row'}, tier, pct, K.btn('Восстановить шкалу', async () => { const p = Number(String(pct.value).replace(',', '.')); if (!(p > 0 && p <= 100)) throw new Error('Процент от 1 до 100'); const x = await api(`/api/admin/creators/${u.id}/restore-limit`, {percent: p}); u.limit_usage = x.limit_usage; paintScale(); K.ok(p >= 100 ? 'Шкала восстановлена полностью' : 'Шкала восстановлена на ' + p + '%'); }, 'small'),
            h('a', {class: 'button small', href: '#tool=freebetsAdminPage', text: 'Выдать Freebet', onclick: () => { K.preset = {author: u.id}; }}), h('a', {class: 'button small', href: '#tool=promocodesPage', text: 'Выдать промокод', onclick: () => { K.preset = {author: u.id}; }}),
            K.btn('Удалить автора', async () => { if (!await K.confirm('Удалить этого пользователя из авторов и забрать доступ к панели автора?', 'Удалить', true)) return; await K.del('/api/admin/creators/' + u.id); K.ok('Автор удалён. Доступ к панели забран.'); await load(); }, 'small danger'))));
      }
    }
    async function load() { items = (await api('/api/admin/creators')).items || []; K.creators = items; paint(); }
    await load();
    root.append(K.head('Управление авторами', 'Только добавленные здесь пользователи получают кнопку «Панель автора» в профиле.', 'Creators'),
      K.card('Добавить автора', 'Найдите пользователя и выберите его из списка', K.field('Пользователь', search), results, chosen, addBtn),
      K.card('Авторы с доступом', 'Удаление сразу убирает доступ и кнопку из профиля', count, list));
  }});
})();
