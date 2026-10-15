/* Games & economy tools: RTP, Roll, history, balance operations, wins feeds. */
(() => {
  const K = window.AdminKit, {h, api, money} = K;

  /* ---------- Отдача игр (RTP) ---------- */
  const RTP_FIELDS = [
    ['rtp', 'Обычный Mines, %', 2, 100, 'Коэффициенты в Mines всегда честные (1 / вероятность пройти клетки) и от отдачи не зависят. Отдача меняет только вероятность: с шансом (100 − отдача)% первая открытая клетка оказывается миной.'],
    ['promo_rtp', 'Базовый промо-отыгрыш, %', 1, 96.9, 'Для обычных промо-подарков используется эта отдача. Компенсационные отыгрышные подарки дополнительно получают персональную прибавку от текущего игрового минуса.'],
    ['loss_rtp_max_boost', 'Макс. прибавка отдачи от игрового минуса, п.п.', 0, 15, 'Кривая: до 1 TON — без прибавки; около 5 TON — ~25% максимума; 25 TON — ~50%; 100 TON — ~75%; 500+ TON — весь максимум. Применяется только к компенсационному отыгрышу и ограничивается итоговой отдачей 99.5%.', 0.5],
    ['upgrade_rtp', 'Апгрейд, отдача %', 1, 100, 'Шанс = отдача × цена вашего подарка / цена цели. Итоговый шанс ограничен 80%.'],
    ['crash_rtp', 'Crash (ракета), отдача %', 80, 99.9, 'Шанс долететь до x равен отдаче / x — при любом множителе вывода игрок в среднем получает именно этот процент от ставки. Применяется со следующего раунда.'],
    ['plinko_rtp', 'Plinko, отдача %', 50, 100, 'Крайние множители таблиц фиксированы (до 1k), отдача подбирает значения в середине. Применяется со следующего броска.'],
    ['hilo_rtp', 'Hi-Lo, отдача %', 80, 99.9, 'Множитель за верный ответ = отдача / шанс. Минимум за ответ — 1.01x, поэтому на крайних картах фактическая отдача чуть выше. Применяется к новым ответам сразу.']
  ];
  K.register({id: 'rtpPage', title: 'Отдача игр (RTP)', async render(root) {
    const d = await api('/api/admin/rtp'), inputs = {};
    const form = K.form(RTP_FIELDS.map(([key, label, min, max, hint, step]) => { inputs[key] = K.number(Number(d[key] ?? (key === 'loss_rtp_max_boost' ? 8 : 97)).toFixed(1), {min, max, step: step || 0.1, name: key}); return K.field(label, inputs[key], hint); }),
      async () => { const body = Object.fromEntries(Object.entries(inputs).map(([k, i]) => [k, i.value])); const r = await api('/api/admin/rtp', {rtp: body.rtp, promo_rtp: body.promo_rtp, upgrade_rtp: body.upgrade_rtp, loss_rtp_max_boost: body.loss_rtp_max_boost, crash_rtp: body.crash_rtp, hilo_rtp: body.hilo_rtp, plinko_rtp: body.plinko_rtp}); for (const [k, i] of Object.entries(inputs)) if (r[k] != null) i.value = Number(r[k]).toFixed(1); K.ok('Отдача сохранена.'); }, 'Сохранить отдачу');
    const statsBox = h('div', {class: 'cols-2'});
    const stat = (title, w) => {
      const t = w?.total || {};
      return K.card(title, null, h('div', {class: 'metrics', style: {gridTemplateColumns: 'repeat(3,1fr)'}}, K.metric('RTP', t.rtp == null ? '—' : K.pct(t.rtp, 2), K.num(t.rounds || 0) + ' игр'), K.metric('Ставки', money(t.bet || 0), 'TON', 'sky'), K.metric('Результат', K.signed(t.result || 0), 'TON для проекта', Number(t.result || 0) <= 0 ? 'bad' : 'good')),
        K.simpleTable(['Игра', 'Игр', 'Ставки → выплаты', 'RTP'], (w?.games || []).map(g => [g.game + (g.game === 'Mines' ? ` (≥10x: ${g.high10 || 0}, ≥20x: ${g.high20 || 0}, max ${Number(g.max_x || 0).toFixed(2)}x)` : ''), K.num(g.rounds || 0), `${money(g.bet)} → ${money(g.payout)}`, g.rtp == null ? '—' : K.pct(g.rtp, 2)]), {numeric: [1, 3]}));
    };
    const loadStats = async () => { try { const s = await api('/api/admin/rtp/stats'); statsBox.replaceChildren(stat('Последние 24 часа', s.day), stat('Последние 7 дней', s.week)); } catch (e) { statsBox.replaceChildren(K.notice(e.message, 'bad')); } };
    loadStats();
    root.append(K.head('Отдача игр', 'Отдача — долгосрочный процент возврата игроку. Фактический RTP ниже считается по реальным ставкам без промо-отыгрыша.', 'Игры', K.btn('Обновить статистику', loadStats)),
      h('div', {class: 'cols-2', style: {alignItems: 'start'}}, K.card('Настройки отдачи', null, form), h('div', null, h('div', {class: 'group-title', text: 'Фактический RTP'}), statsBox)));
  }});

  /* ---------- Roll и шансы ---------- */
  K.register({id: 'rollAdminPage', title: 'Roll и шансы', async render(root) {
    const [gifts, d] = await Promise.all([K.catalog(), api('/api/admin/rolls')]);
    let rolls = d.rolls.map(r => ({...r, price_ton: (r.price / 100).toFixed(2), entries: r.entries.map(e => ({...e}))}));
    const box = h('div', {class: 'list'});
    const entry = () => ({kind: 'gift', gift_id: gifts[0]?.id || '', weight: 100, boost: 1});
    function paint() {
      box.replaceChildren();
      rolls.forEach((r, ri) => {
        const sum = r.entries.reduce((a, x) => a + Number(x.weight || 0), 0);
        const entries = r.entries.map((e, ei) => {
          const kind = K.select([['gift', 'Подарок'], ['boost', 'Boost'], ['empty', 'Пусто']], e.kind, {onchange: ev => { e.kind = ev.target.value; paint(); }});
          const weight = K.number(e.weight || 100, {min: 1, max: 10000, onchange: ev => { e.weight = ev.target.value; paint(); }});
          let detail = null;
          if (e.kind === 'gift') { const gp = K.giftPicker(e.gift_id, {onchange: v => e.gift_id = v}); e.gift_id = e.gift_id || gifts[0]?.id; detail = gp.node; }
          else if (e.kind === 'boost') detail = K.field('Множитель весов подарков (1–3)', K.number(e.boost || 1.25, {step: 0.1, min: 1, max: 3, oninput: ev => e.boost = ev.target.value}));
          return h('div', {class: 'list-item', style: {flexWrap: 'wrap', alignItems: 'flex-start'}}, h('div', {class: 'form-grid', style: {flex: 1}}, K.field('Тип сектора', kind), K.field('Вес', weight), detail), K.badge('Шанс ' + (sum ? Number(e.weight || 0) / sum * 100 : 0).toFixed(2) + '%', 'accent'), h('button', {type: 'button', class: 'danger small', text: 'Удалить', onclick: () => { r.entries.splice(ei, 1); paint(); }}));
        });
        box.append(K.card(null, null,
          h('div', {class: 'form-grid'}, K.field('Название Roll', K.input({value: r.name || '', placeholder: 'Название Roll', oninput: ev => r.name = ev.target.value})), K.field('Цена в TON', K.number(r.price_ton, {min: 0.01, step: 0.01, oninput: ev => r.price_ton = ev.target.value}))),
          h('p', {class: 'hint', text: 'Шанс сектора = его вес / сумма весов. Boost увеличивает веса подарков на следующей прокрутке.'}), h('div', {class: 'list'}, entries),
          h('div', {class: 'actions'}, h('button', {type: 'button', text: '＋ Сектор', disabled: r.entries.length >= 24, onclick: () => { r.entries.push(entry()); paint(); }}), h('button', {type: 'button', class: 'danger', text: 'Удалить Roll', onclick: () => { rolls.splice(ri, 1); paint(); }}))));
      });
      if (!rolls.length) box.append(K.empty('Roll пока нет. Нажмите «Добавить Roll».'));
    }
    paint();
    root.append(K.head('Roll и шансы', 'Настройка колеса Roll: цена прокрутки и секторы с их весами.', 'Игры'), box,
      h('div', {class: 'sticky-save'}, h('button', {type: 'button', text: '＋ Добавить Roll', onclick: () => { rolls.push({name: 'Новый Roll', price_ton: '0.10', entries: [entry(), {kind: 'empty', weight: 100, boost: 1}]}); paint(); }}),
        K.btn('Сохранить Roll', async () => { const r = await api('/api/admin/rolls', {rolls}); rolls = r.rolls.map(x => ({...x, price_ton: (x.price / 100).toFixed(2)})); paint(); K.ok('Настройки Roll сохранены.'); }, 'primary')));
  }});

  /* ---------- Операции баланса ---------- */
  const TX = {deposit: 'Пополнение администратором', ton_deposit: 'Пополнение TON', stars_deposit: 'Пополнение Stars', gift_deposit: 'Пополнение NFT-подарком', referral_bonus: 'Реферальное начисление', admin_balance: 'Корректировка баланса', promo_balance: 'Промокод на баланс', promo_wager_attempt_lost: 'Потрачен шанс отыгрышного подарка', promo_wager_burn: 'Отыгрышный подарок сгорел'};
  K.register({id: 'transactionsPage', title: 'Операции баланса', async render(root) {
    const q = K.input({type: 'search', placeholder: 'Имя, @username или ID', 'aria-label': 'Поиск операций'}), out = h('div'), info = h('p', {class: 'hint'});
    const load = async () => { const d = await api('/api/admin/transactions?q=' + encodeURIComponent(q.value)); info.textContent = d.items.length >= 500 ? 'Показаны последние 500 операций — уточните поиск.' : `Найдено операций: ${d.items.length}`; K.table(out, ['Игрок', 'Операция', 'Сумма', 'Баланс после', 'Время / детали'], d.items.map(x => [h('div', null, h('button', {type: 'button', text: x.name || 'Игрок', onclick: () => K.openUser(x.user_id)}), h('small', {text: (x.username ? '@' + x.username + ' · ' : '') + 'ID ' + x.user_id})), TX[x.kind] || x.kind, x.amount ? h('strong', {class: x.amount > 0 ? 'pos' : 'neg', text: K.signed(x.amount) + ' TON'}) : '—', x.balance_after == null ? '—' : money(x.balance_after) + ' TON', h('div', null, K.time(x.created_at), x.details ? h('small', {text: x.details}) : null)]), {numeric: [3]}); };
    const form = h('form', {class: 'filters', onsubmit: e => { e.preventDefault(); load().catch(K.fail); }}, q, h('button', {class: 'primary', text: 'Найти'}));
    q.addEventListener('input', K.debounce(() => load().catch(K.fail), 350));
    root.append(K.head('Операции баланса', 'Пополнения, ручные корректировки, реферальные и промо-начисления.', 'Финансы'), form, info, out);
    await load();
  }});

  /* ---------- История игр ---------- */
  K.register({id: 'gameHistoryPage', title: 'История игр', async render(root) {
    const game = K.select([['all', 'Все игры'], ['mines', 'Мины'], ['upgrade', 'Апгрейды'], ['arena', 'Арена']]), user = K.input({inputMode: 'numeric', placeholder: 'ID пользователя', 'aria-label': 'ID пользователя'});
    const list = h('div', {class: 'list'}), more = h('button', {type: 'button', class: 'ghost', text: 'Показать ещё', hidden: true});
    let offset = 0, busy = false;
    const chance = c => { const n = Number(c); return n >= 0.01 ? n.toFixed(2) + '%' : n > 0 ? n.toPrecision(2) + '%' : '—'; };
    async function load(reset) {
      if (busy) return; busy = true; more.disabled = true;
      try {
        if (reset) { offset = 0; list.replaceChildren(); }
        const d = await api('/api/admin/game-history?' + K.query({offset, game: game.value, user_id: user.value.trim()}));
        for (const x of d.items) {
          const detail = x.game === 'arena' ? `Арена #${x.id} · ставка ${money(x.stake)} TON` + (x.gift_name ? ` · подарки: ${x.gift_name}` + (x.ton_part > 0 ? ` + ${money(x.ton_part)} TON` : '') : '') + ` · шанс ${chance(x.chance)}` + (x.outcome === 'won' ? ` · выплата ${money(x.payout)} TON · x${Number(x.x || 0).toFixed(2)}` : '')
            : (x.game === 'mines' ? 'Мины' : 'Апгрейд') + ` · ${money(x.stake)} TON` + (x.game === 'upgrade' ? ` · ${x.source_name} → ${x.gift_name} · ${chance(x.chance)}` : '') + (x.outcome === 'won' ? ` · ${money(x.payout)} TON` : '');
          list.append(h('div', {class: 'list-item'}, h('div', {class: 'grow'}, h('strong', {text: (x.name || 'Игрок') + ' · ' + x.user_id}), h('small', {text: detail}), h('small', {text: K.time(x.date)})), K.badge({won: 'Выигрыш', lost: 'Проигрыш', active: 'В игре'}[x.outcome] || x.outcome, x.outcome === 'won' ? 'good' : x.outcome === 'lost' ? 'bad' : 'warn'), h('button', {type: 'button', class: 'small', text: 'Игрок', onclick: () => K.openUser(x.user_id)})));
        }
        offset += d.items.length; more.hidden = !d.has_more && d.items.length < 1 ? true : d.has_more === false; if (d.has_more === undefined) more.hidden = !d.items.length;
        if (!list.children.length) list.append(K.empty('Игр не найдено.'));
      } finally { busy = false; more.disabled = false; }
    }
    more.onclick = () => K.action(more, () => load(false));
    root.append(K.head('История игр', 'Завершённые и текущие игры игроков в Mines, Upgrade и Арене.', 'Игры'), h('form', {class: 'filters', onsubmit: e => { e.preventDefault(); K.action(null, () => load(true)); }}, K.field('Игра', game), K.field('Игрок', user), h('button', {class: 'primary', text: 'Найти'})), list, h('div', {class: 'row end'}, more));
    await load(true);
  }});

  /* ---------- Последние победы и ТОП дня ---------- */
  const TOP_MODES = [['mines', 'Mines'], ['upgrade', 'Upgrade'], ['hilo', 'Hi-Lo']];
  const rewardSummary = r => !r || r.type === 'none' ? 'Без награды' : r.type === 'gram' ? money(r.amount || 0) + ' GRAM' : (r.gift_name || 'Подарок') + (r.price_ton ? ' · ' + money(r.price_ton) + ' TON' : '');
  K.register({id: 'winsAdminPage', title: 'Последние победы', async render(root) {
    const feeds = h('div', {class: 'metrics'}), status = h('p', {class: 'muted', role: 'status'});
    const loadFeeds = async () => { const d = await api('/api/admin/wins-feeds'); feeds.replaceChildren(K.metric('Мины', K.num(d.mines), 'записей в ленте'), K.metric('Апгрейд', K.num(d.upgrade), 'записей в ленте', 'violet'), K.metric('Крафт', K.num(d.craft ?? 0), 'записей в ленте', 'sky'), K.metric('Hi-Lo', K.num(d.hilo ?? 0), 'записей в ленте', 'good')); };
    const clear = (mode, label) => K.btn(label, async () => { if (!await K.confirm(mode === 'all' ? 'Очистить все публичные ленты побед?' : 'Очистить ленту «' + label.replace('Очистить ', '') + '»?', 'Очистить', true)) return; await api('/api/admin/wins-feeds/clear', {mode}); status.textContent = 'Лента очищена. Новые победы появятся здесь сразу.'; await loadFeeds(); }, mode === 'all' ? 'danger' : '');
    const [data, gifts] = await Promise.all([api('/api/admin/daily-top-rewards'), K.catalog()]);
    const cards = TOP_MODES.map(([mode, title]) => {
      const r = data.rewards?.[mode] || {type: 'none'}, s = data.schedules?.[mode] || {};
      const type = K.select([['none', 'Без награды'], ['gram', 'GRAM'], ['catalog', 'Подарок Portal'], ['fragment', 'Подарок Fragment']], r.type || 'none');
      const gram = K.number(r.amount || '', {min: 0.01, step: 0.01, placeholder: 'Количество GRAM'}), fragment = K.input({type: 'url', value: r.fragment_url || '', placeholder: 'https://fragment.com/gift/...'});
      const picker = K.giftPicker(r.gift_id ? String(r.gift_id) : '', {allowEmpty: false});
      const end = h('input', {type: 'datetime-local', value: s.end_at ? K.localInput(Date.parse(s.end_at)) : ''}), current = h('small', {class: 'muted', text: 'Сейчас: ' + rewardSummary(r)}), sched = h('small', {class: 'muted', text: s.end_at ? 'Окончание: ' + K.time(s.end_at) + (s.period_label ? ' · Период: ' + s.period_label : '') : 'Время не задано'});
      const fields = () => { gram.hidden = type.value !== 'gram'; picker.node.hidden = type.value !== 'catalog'; fragment.hidden = type.value !== 'fragment'; }; type.onchange = fields; fields();
      return K.card('ТОП дня · ' + title, null, h('div', {class: 'stack wide'}, K.field('Награда', type), gram, picker.node, fragment, current,
        h('div', {class: 'actions'}, K.btn('Сохранить награду', async () => { const body = {type: type.value}; if (body.type === 'gram') body.amount = gram.value; if (body.type === 'catalog') body.gift_id = picker.value; if (body.type === 'fragment') body.fragment_url = fragment.value; const d = await api('/api/admin/daily-top-rewards/' + mode, body); current.textContent = 'Сейчас: ' + rewardSummary(d.reward); K.ok('Награда ТОП дня сохранена.'); }, 'primary'), K.btn('Удалить награду', async () => { await api('/api/admin/daily-top-rewards/' + mode, {type: 'none'}); type.value = 'none'; fields(); current.textContent = 'Сейчас: Без награды'; K.ok('Награда удалена.'); })),
        h('div', {class: 'sep'}), K.field('Закончить ТОП в', end), sched,
        K.btn('Сохранить время', async () => { if (!end.value) throw new Error('Укажите дату и время окончания ТОП дня.'); const date = new Date(end.value); if (!Number.isFinite(date.getTime()) || date.getTime() <= Date.now()) throw new Error('Время окончания должно быть в будущем.'); const d = await api('/api/admin/daily-top-schedule/' + mode, {end_at: date.toISOString()}); sched.textContent = 'Окончание: ' + K.time(d.schedule?.end_at); K.ok('Время ТОП дня сохранено.'); }),
        h('div', {class: 'sep'}), K.btn('Сбросить ТОП-ДРОП ' + title, async () => { if (!await K.confirm('Сбросить ТОП-ДРОП ' + title + ' и начать новый период?', 'Сбросить', true)) return; const d = await api('/api/admin/daily-top-reset/' + mode, {}); if (d.schedule?.end_at) sched.textContent = 'Окончание: ' + K.time(d.schedule.end_at); K.ok('ТОП-ДРОП ' + title + ' сброшен.'); }, 'danger')));
    });
    await loadFeeds();
    root.append(K.head('Последние победы', 'Очищайте публичные ленты побед и настраивайте награды за ТОП дня отдельно для Mines, Upgrade и Hi-Lo.', 'Игры'),
      feeds, K.card('Очистка лент', 'Удаляет записи из публичных лент победителей и живой ленты на главной. Данные игр в базе не затрагиваются.', h('div', {class: 'actions', style: {marginTop: 0}}, clear('mines', 'Очистить Мины'), clear('upgrade', 'Очистить Апгрейд'), clear('craft', 'Очистить Крафт'), clear('hilo', 'Очистить Hi-Lo'), clear('plinko', 'Очистить Plinko'), clear('limbo', 'Очистить Limbo'), clear('road', 'Очистить Hamster Road'), clear('live', 'Очистить живую ленту (главная)'), clear('all', 'Очистить все ленты')), status),
      h('div', {class: 'group-title', text: 'Награды за ТОП дня'}), h('div', {class: 'cols-3', style: {alignItems: 'start'}}, cards));
  }});
})();
