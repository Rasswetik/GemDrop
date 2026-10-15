/* Promocodes and promo polls. */
(() => {
  const K = window.AdminKit, {h, api} = K;
  const REW = [['balance', 'Баланс TON'], ['tickets', 'Билеты розыгрыша'], ['pumpkins', 'Тыквы (Хэллоуин)'], ['gift', 'Подарок'], ['wager_gift', 'Отыгрышный подарок'], ['deposit_bonus', 'Бонус на пополнение'], ['multi', 'Несколько наград'], ['medal', 'Только медаль']];

  /* Редактор набора наград (мультипромокод) */
  function bundleEditor(gifts, state) {
    const box = h('div', {class: 'stack'});
    const paid = gifts.filter(g => Number(g.price_ton) > 0), giftSel = item => {
      const g = K.giftSelect(paid, item.gift_id || '', v => item.gift_id = v); if (!item.gift_id) item.gift_id = g.select.value; return g.node;
    };
    const num = (item, key, props) => K.number(item[key] ?? '', {...props, oninput: e => item[key] = e.target.value});
    function paint() {
      box.replaceChildren();
      state.forEach((item, i) => {
        const fields = h('div', {class: 'form-grid'});
        if (item.type === 'gift' || item.type === 'wager_gift') {
          fields.append(K.field('Подарок', giftSel(item)), K.field('Количество', num(item, 'quantity', {min: 1, max: 50, step: 1})));
          if (item.type === 'wager_gift') fields.append(K.field('Отыгрыш X', num(item, 'wager_multiplier', {min: 1, max: 1000, step: 1})), K.field('Срок, дней · 0 = без срока', num(item, 'gift_expires_days', {min: 0, max: 3650, step: 1})));
        } else if (item.type === 'balance') fields.append(K.field('Сумма TON', num(item, 'amount', {min: '.01', step: '.01'})), K.field('Количество начислений', num(item, 'quantity', {min: 1, max: 50, step: 1})));
        else fields.append(K.field('Бонус, %', num(item, 'bonus_percent', {min: 0, max: 100, step: '.1'})), K.field('Или бонус, TON', num(item, 'bonus_fixed', {min: 0, step: '.01'})), K.field('Пополнение от, TON', num(item, 'min_deposit', {min: 0, step: '.01'})));
        box.append(h('div', {class: 'list-item', style: {flexDirection: 'column', alignItems: 'stretch'}},
          h('div', {class: 'row'}, K.field('Тип награды', K.select([['gift', 'Подарок'], ['wager_gift', 'Отыгрышный подарок'], ['balance', 'TON на баланс'], ['deposit_bonus', 'Бонус к пополнению']], item.type || 'gift', {onchange: e => { item.type = e.target.value; if (item.type === 'wager_gift' && !item.wager_multiplier) item.wager_multiplier = 25; paint(); }})),
            h('button', {type: 'button', class: 'small danger', text: 'Удалить', 'aria-label': 'Удалить награду', onclick: () => { state.splice(i, 1); if (!state.length) state.push({type: 'gift', quantity: 1}); paint(); }})), fields));
      });
    }
    paint();
    return {node: h('div', {class: 'stack'}, box, h('button', {type: 'button', class: 'small', text: '＋ Добавить награду', onclick: () => { state.push({type: 'gift', quantity: 1}); paint(); }})), payload: () => state.map(x => ({type: x.type, quantity: x.quantity || 1, gift_id: x.gift_id || '', wager_multiplier: x.wager_multiplier || 0, gift_expires_days: x.gift_expires_days || 0, amount: x.amount || 0, bonus_percent: x.bonus_percent || 0, bonus_fixed: x.bonus_fixed || 0, min_deposit: x.min_deposit || 0}))};
  }

  K.register({id: 'promocodesPage', title: 'Промокоды', async render(root) {
    const preset = K.preset; K.preset = null;
    const [gifts, creators, list0] = await Promise.all([K.catalog(), api('/api/admin/creators').then(d => d.items || []).catch(() => []), api('/api/admin/promocodes')]);

    /* ----- создание ----- */
    const code = K.input({maxLength: 40, placeholder: 'Оставьте пустым — код создастся сам'}), type = K.select(REW, 'balance'), target = K.select([['main', 'Основной баланс'], ['bonus', 'Бонусный баланс']], 'main');
    const amount = K.number(1, {min: '.01', step: '.01'}), tickets = K.number(10, {min: 1, step: 1}), pumpkins = K.number(100, {min: 1, step: 1});
    const gift = K.giftPicker('', {label: 'Подарок', models: true}), wX = K.number(25, {min: 1, max: 1000}), wExp = K.check('Подарок сгорает через N дней', false), wDays = K.number(7, {min: 1, max: 3650});
    const pct = K.number('', {min: 0, max: 100, step: '.1'}), fixed = K.number('', {min: 0, step: '.01'}), minDep = K.number(0, {min: 0, step: '.01'});
    const grp0 = (...n) => h('div', {class: 'stack'}, n);
    const bundleState = [{type: 'gift', quantity: 1}], bundle = bundleEditor(gifts, bundleState);
    const desc = K.input({placeholder: 'Для чего создан промокод (видно администраторам)'}), srcLabel = K.input({placeholder: 'Администрация'});
    const author = K.select([['0', 'Без автора'], ...creators.map(c => [String(c.id), `${c.name || 'Автор'}${c.username ? ' · @' + c.username : ''} · ID ${c.id}`])], preset?.author ? String(preset.author) : '0');
    const assigned = K.number(0, {min: 0, step: 1, inputMode: 'numeric'}), expires = K.number(0, {min: 0, step: 1}), actDep = K.number(0, {min: 0, step: '.01'}), maxUses = K.number(1, {min: 0, step: 1});
    const medalSel = K.select([['', 'Без медали'], ...((window.GDMedals && GDMedals.LIST) || []).filter(m => !m.hidden).map(m => [m.key, m.name])], '');
    const gMedal = grp0(K.field('Медаль в награду', medalSel, 'Выдаётся вместе с наградой. Для типа «Только медаль» выберите её здесь.'));
    const grp = (...n) => h('div', {class: 'stack'}, n);
    const gTarget = grp(K.field('Куда начислять', target)), gAmt = grp(K.field('Сумма TON', amount)), gTick = grp(K.field('Количество билетов', tickets)), gPump = grp(K.field('Количество тыкв', pumpkins));
    const gGift = grp(K.field('Подарок', gift.node)), gWager = grp(K.field('Отыгрыш X', wX), wExp, K.field('Сгорает через, дней', wDays)), gDep = grp(h('div', {class: 'form-grid'}, K.field('Бонус, %', pct), K.field('или фиксированный, TON', fixed)), K.field('Минимальное пополнение, TON', minDep)), gMulti = grp(bundle.node);
    const sync = () => {
      const t = type.value; gTarget.hidden = !['balance', 'gift', 'wager_gift', 'multi'].includes(t); gAmt.hidden = t !== 'balance'; gTick.hidden = t !== 'tickets'; gPump.hidden = t !== 'pumpkins';
      gGift.hidden = !['gift', 'wager_gift'].includes(t); gWager.hidden = t !== 'wager_gift'; gWager.lastChild.hidden = !wExp.querySelector('input').checked; gDep.hidden = t !== 'deposit_bonus'; gMulti.hidden = t !== 'multi';
    };
    type.onchange = sync; wExp.querySelector('input').onchange = sync; sync();

    const listBox = h('div', {class: 'list'}), count = h('span', {class: 'muted'});
    const paintList = d => {
      count.textContent = d.items.length;
      listBox.replaceChildren(...(d.items.length ? d.items.map(x => {
        const reward0 = x.reward_type === 'multi' ? (x.purpose || 'Несколько наград') : x.reward_type === 'deposit_bonus' ? (x.bonus_percent ? '+' + x.bonus_percent + '%' : `+${K.money(x.bonus_fixed)} TON`) + ' к депозиту от ' + K.money(x.min_deposit) + ' TON' : x.reward_type === 'balance' ? K.money(x.amount) + ' TON' : x.reward_type === 'tickets' ? `${K.num(x.tickets || x.amount)} билет(ов)` : x.reward_type === 'pumpkins' ? `${K.num(x.pumpkins || x.amount)} тыкв` : x.reward_type === 'wager_gift' ? `${x.gift_name} · X${Number(x.wager_multiplier || 0)} · цель ${K.money((x.gift_price || 0) * Number(x.wager_multiplier || 0))} TON${Number(x.gift_expires_days || 0) > 0 ? ` · сгорит через ${x.gift_expires_days} дн.` : ''}` : x.gift_name;
        const reward = x.reward_type === 'medal' ? 'Медаль «' + (x.medal_name || x.medal || '?') + '»' : (x.medal ? '🏅 «' + (x.medal_name || x.medal) + '» + ' : '') + reward0;
        const key = x.key || x.code, off = !(x.active && !x.expired);
        const info = [`${x.uses_count}/${x.max_uses === 0 ? '∞' : x.max_uses} активаций`, x.assigned_user_id ? 'user ' + x.assigned_user_id : 'для всех', x.expires_at ? (x.expired ? 'истёк' : 'до ' + new Date(x.expires_at).toLocaleDateString('ru-RU')) : 'без срока', Number(x.activation_min_deposit || 0) ? 'депозит от ' + K.money(x.activation_min_deposit) + ' TON' : ''].filter(Boolean).join(' · ');
        return h('div', {class: 'list-item', style: {flexWrap: 'wrap', opacity: off ? '.55' : '1'}},
          h('div', {class: 'grow'}, h('strong', {text: x.code}), h('small', {text: reward + ' · ' + info}), h('small', {text: [x.source || 'Администрация', x.description, ['balance', 'gift', 'wager_gift', 'multi'].includes(x.reward_type) ? (x.balance_target === 'bonus' ? 'бонусный' : 'основной') + ' баланс' : ''].filter(Boolean).join(' · ')}), x.scope === 'wheel' ? K.badge('Только для колеса', 'info') : null),
          h('div', {class: 'row'}, K.btn('Копировать', () => K.copy(x.code), 'small'),
            K.btn(x.active ? 'Отключить' : 'Включить', async () => { await api(`/api/admin/promocodes/${encodeURIComponent(key)}/toggle`, {}); await reload(); }, 'small'),
            K.btn('Удалить', async () => { if (!await K.confirm(`Удалить промокод ${x.code}${x.scope === 'wheel' ? ' (колесо)' : ''}?${Number(x.uses_count) > 0 ? ' У него уже есть активации — история по нему тоже удалится.' : ''}`, 'Удалить', true)) return; await api(`/api/admin/promocodes/${encodeURIComponent(key)}/delete`, {}); K.ok('Промокод удалён'); await reload(); }, 'small danger')));
      }) : [K.empty('Промокодов пока нет')]));
    };
    async function reload() { paintList(await api('/api/admin/promocodes')); }

    const form = K.form([K.field('Код', code), K.field('Награда', type), gTarget, gAmt, gTick, gPump, gGift, gWager, gDep, gMulti, gMedal, K.field('Описание', desc), K.field('Источник (подпись)', srcLabel),
      K.field('Автор', author), K.field('Только для пользователя ID (0 — для всех)', assigned),
      h('div', {class: 'form-grid'}, K.field('Срок, дней (0 — без срока)', expires), K.field('Мин. подтверждённый депозит, TON', actDep, '0 = без условия'), K.field('Лимит активаций · 0 = ∞', maxUses))], async () => {
      const t = type.value;
      const d = await api('/api/admin/promocodes', {code: code.value, reward_type: t, balance_target: target.value, amount: amount.value, tickets: tickets.value, pumpkins: pumpkins.value, gift_id: gift.value, wager_multiplier: wX.value, gift_expires_days: t === 'wager_gift' && wExp.querySelector('input').checked ? wDays.value : 0, max_uses: maxUses.value, bonus_percent: pct.value, bonus_fixed: fixed.value, min_deposit: minDep.value, activation_min_deposit: actDep.value, bundle_items: t === 'multi' ? bundle.payload() : undefined, medal: medalSel.value, assigned_user_id: assigned.value, author_user_id: author.value || 0, expires_in_days: expires.value, source_label: srcLabel.value, description: desc.value});
      code.value = ''; desc.value = ''; K.ok('Создан: ' + d.code); K.copy(d.code); await reload();
    }, 'Создать промокод');
    paintList(list0);

    /* ----- опросники ----- */
    const pollTitle = K.input({placeholder: 'Например: Кто победит?'}), pollMax = K.number('', {min: 0, step: 1, placeholder: '0 — без лимита'}), pollDays = K.number('', {min: 0, step: 1, placeholder: '0 — без срока'});
    const newOpt = () => ({name: '', code: '', reward_type: 'balance', amount: '1.00', tickets: 10, gift_id: '', wager_multiplier: 25, gift_expires_days: 0});
    let opts = [newOpt(), newOpt()];
    const optBox = h('div', {class: 'stack'}), pollList = h('div', {class: 'list'}), pollCount = h('span', {class: 'muted'});
    const paintOpts = () => {
      optBox.replaceChildren(...opts.map((o, i) => {
        const f = h('div', {class: 'form-grid'}, K.field('Название ячейки', K.input({value: o.name, placeholder: 'Например: Вася', oninput: e => o.name = e.target.value})), K.field('Промокод', K.input({value: o.code, placeholder: 'CHAMPION', oninput: e => { e.target.value = e.target.value.toUpperCase().replace(/\s+/g, ''); o.code = e.target.value; }})),
          K.field('Награда', K.select([['balance', 'TON'], ['tickets', 'Билеты'], ['gift', 'Подарок'], ['wager_gift', 'Отыгрыш NFT']], o.reward_type, {onchange: e => { o.reward_type = e.target.value; paintOpts(); }})));
        if (o.reward_type === 'tickets') f.append(K.field('Билеты', K.number(o.tickets, {min: 0, step: 1, oninput: e => o.tickets = e.target.value})));
        else if (o.reward_type === 'balance') f.append(K.field('TON', K.number(o.amount, {min: 0, step: '.01', oninput: e => o.amount = e.target.value})));
        else { const g = K.giftSelect(gifts, o.gift_id || gifts[0]?.id || '', v => o.gift_id = v); if (!o.gift_id) o.gift_id = g.select.value; f.append(K.field('Подарок', g.node));
          if (o.reward_type === 'wager_gift') f.append(K.field('Отыгрыш X', K.number(o.wager_multiplier, {min: 1, max: 1000, oninput: e => o.wager_multiplier = e.target.value})), K.field('Сгорает через, дней', K.number(o.gift_expires_days, {min: 0, max: 3650, oninput: e => o.gift_expires_days = e.target.value}))); }
        return h('div', {class: 'list-item', style: {flexDirection: 'column', alignItems: 'stretch'}}, f, h('div', null, h('button', {type: 'button', class: 'small danger', text: 'Убрать вариант', disabled: opts.length <= 2, onclick: () => { opts.splice(i, 1); paintOpts(); }})));
      }));
    };
    paintOpts();
    const paintPolls = d => {
      pollCount.textContent = d.items.length;
      pollList.replaceChildren(...(d.items.length ? d.items.map(p => h('div', {class: 'list-item', style: {flexDirection: 'column', alignItems: 'stretch', opacity: p.active ? '1' : '.55'}},
        h('div', {class: 'row'}, h('div', {class: 'grow'}, h('strong', {text: p.title}), h('small', {text: `${K.num(p.uses_count || 0)}${p.max_votes ? ' / ' + K.num(p.max_votes) : ''} голосов${p.expires_at ? ' · до ' + new Date(p.expires_at).toLocaleDateString('ru-RU') : ''}`})),
          p.active ? K.btn('Завершить', async () => { if (!await K.confirm('Завершить опрос и отключить все его промокоды?', 'Завершить', true)) return; await api(`/api/admin/promo-polls/${encodeURIComponent(p.id)}/close`, {}); await Promise.all([reloadPolls(), reload()]); }, 'small') : K.badge('Завершён')),
        h('div', {class: 'stack'}, (p.results || []).map(x => h('div', {class: 'list-item'}, h('div', {class: 'grow'}, h('strong', {text: x.name}), h('small', {text: x.code})), h('strong', {text: `${Number(x.percent || 0).toFixed(1).replace('.0', '')}% · ${x.votes}`})))))) : [K.empty('Опросов пока нет')]));
    };
    async function reloadPolls() { paintPolls(await api('/api/admin/promo-polls')); }
    reloadPolls().catch(() => {});
    const pollForm = K.form([K.field('Заголовок опроса', pollTitle), h('div', {class: 'form-grid'}, K.field('Макс. голосов', pollMax), K.field('Срок, дней', pollDays)), h('div', {class: 'group-title', text: 'Варианты'}), optBox, h('div', null, h('button', {type: 'button', class: 'small', text: '＋ Вариант', onclick: () => { if (opts.length >= 10) return K.toast('Максимум 10 вариантов', 'bad'); opts.push(newOpt()); paintOpts(); }}))], async () => {
      const d = await api('/api/admin/promo-polls', {title: pollTitle.value.trim(), max_votes: pollMax.value, expires_in_days: pollDays.value, options: opts.map(x => ({...x}))});
      K.ok('Опрос создан: ' + d.poll.title); pollTitle.value = ''; opts = [newOpt(), newOpt()]; paintOpts(); await Promise.all([reloadPolls(), reload()]);
    }, 'Создать опрос');

    root.append(K.head('Промокоды', 'Создание, отключение и удаление промокодов; опросники с голосованием по промокодам.', 'Игроки'),
      K.tabs([
        {id: 'create', label: 'Создать промокод', render: b => b.replaceChildren(K.card(null, null, form))},
        {id: 'list', label: 'Все промокоды', render: b => b.replaceChildren(K.card('Созданные промокоды', null, count, listBox))},
        {id: 'polls', label: 'Опросники', render: b => b.replaceChildren(K.card('Новый опросник', 'Каждый вариант — отдельный промокод; игрок может проголосовать один раз.', pollForm), K.card('Опросы', null, pollCount, pollList))}]));
  }});
})();
