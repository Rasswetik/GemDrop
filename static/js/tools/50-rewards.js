/* Rewards: prize wheel. */
(() => {
  const K = window.AdminKit, {h, api} = K;
  const KIND = {deposit_bonus: 'Промокод на пополнение', balance: 'TON на баланс', gift: 'Подарок в инвентарь', wager_gift: 'Подарок на отыгрыш'};
  const fmt = n => Number(n || 0).toLocaleString('ru-RU', {maximumFractionDigits: 2});

  /* ---------- Колесо призов ---------- */
  K.register({id: 'wheelAdminPage', title: 'Колесо призов', async render(root) {
    let data = await api('/api/admin/wheel'), editing = null;
    const top = h('div', {class: 'stack'}), prizesBox = h('div', {class: 'list'}), formBox = h('div');

    const prizeThumb = p => p.image_url ? h('img', {class: 'thumb', src: p.image_url, alt: '', loading: 'lazy', style: {width: '44px', height: '44px'}}) : h('div', {class: 'thumb', style: {width: '44px', height: '44px', display: 'grid', placeItems: 'center'}, text: p.kind === 'balance' ? '💎' : p.kind === 'deposit_bonus' ? '🎟' : '🎁'});
    const title = p => p.title || KIND[p.kind];

    function paintTop() {
      const pub = K.switchRow('Открыть колесо всем игрокам', 'Пока переключатель выключен, колесо видят только администраторы.', !!data.settings.public);
      const channel = K.input({value: data.settings.channel || '', placeholder: 'case_app', maxLength: 40});
      const settings = K.form([pub, K.field('Канал с кодами (без @, необязательно)', channel)], async () => { await api('/api/admin/wheel/settings', {public: pub.input.checked, channel: channel.value.trim()}); K.ok('Сохранено'); });
      const code = K.input({placeholder: 'Пусто — случайный код', maxLength: 32}), uses = K.number(1, {min: 0, step: 1, inputMode: 'numeric'}), days = K.number(0, {min: 0, step: 1, inputMode: 'numeric'});
      const codes = h('div', {class: 'row', style: {flexWrap: 'wrap'}}, data.codes.length ? data.codes.map(c => h('button', {type: 'button', class: 'small', title: 'Скопировать', text: `${c.code} · ${c.uses}/${c.max_uses || '∞'}${c.active ? '' : ' · выкл'}`, onclick: () => K.copy(c.code)})) : [K.empty('Кодов пока нет')]);
      const codeForm = K.form([h('div', {class: 'form-grid'}, K.field('Код', code), K.field('Лимит активаций (0 — без лимита)', uses), K.field('Срок, дней (0 — без срока)', days))], async () => {
        const d = await api('/api/admin/wheel/codes', {code: code.value.trim(), max_uses: Number(uses.value || 0), expires_in_days: Number(days.value || 0)});
        await K.copy(d.code); K.ok('Код ' + d.code + ' создан и скопирован'); await reload();
      }, 'Создать код');
      top.replaceChildren(K.card('Доступ', null, settings), K.card('Коды для прокрутки', 'Нажмите на код, чтобы скопировать.', codeForm, codes));
    }

    function paintPrizes() {
      prizesBox.replaceChildren();
      if (!data.prizes.length) prizesBox.append(K.empty('Призов нет — добавьте первый ниже'));
      for (const p of data.prizes) prizesBox.append(h('div', {class: 'list-item', style: {opacity: p.active ? '1' : '.55', flexWrap: 'wrap'}},
        prizeThumb(p), h('div', {class: 'grow'}, h('strong', {text: title(p)}), h('small', {text: `${KIND[p.kind]} · шанс ${p.chance.toFixed(2)}% · вес ${fmt(p.weight)}`})),
        h('div', {class: 'row'}, h('button', {type: 'button', class: 'small', text: 'Изменить', onclick: () => { editing = p; paintForm(); formBox.scrollIntoView({behavior: 'smooth', block: 'start'}); }}),
          K.btn(p.active ? 'Выключить' : 'Включить', async () => { await api('/api/admin/wheel/prizes/' + p.id, {active: !p.active}); await reload(); }, 'small'),
          K.btn('Удалить', async () => { if (!await K.confirm(`Удалить приз «${title(p)}»?`, 'Удалить', true)) return; await api(`/api/admin/wheel/prizes/${p.id}/delete`, {}); if (editing?.id === p.id) editing = null; await reload(); }, 'small danger'))));
    }

    function paintForm() {
      const p = editing || {kind: 'deposit_bonus', promo_days: 7, gift_expires_days: 0, weight: 1};
      const kind = K.select(Object.entries(KIND), p.kind), percent = K.number(p.percent || '', {min: 1, max: 1000, step: 1, placeholder: '35'}), minDep = K.number(p.min_deposit || '', {min: 0, step: '0.01', placeholder: '0'}), promoDays = K.number(p.promo_days ?? 7, {min: 0, step: 1}), amount = K.number(p.amount || '', {min: '0.01', step: '0.01', placeholder: '10'});
      const wager = K.number(p.wager_multiplier || '', {min: 1, max: 1000, step: '0.1', placeholder: '20'}), giftDays = K.number(p.gift_expires_days ?? 0, {min: 0, step: 1}), label = K.input({value: p.title || '', maxLength: 140, placeholder: 'Подставится автоматически'}), weight = K.number(p.weight ?? 1, {min: 0, step: '0.01'});
      const picker = K.giftPicker(p.gift_id || '', {label: 'Подарок из Portal'}), file = h('input', {type: 'file', accept: 'image/png,image/jpeg,image/webp,image/gif,image/svg+xml'}), hint = h('small', {class: 'muted'});
      const gsec = h('div', {class: 'stack'}, K.field('Подарок из Portal', picker.node)), dep = h('div', {class: 'form-grid'}, K.field('Бонус, %', percent), K.field('Мин. пополнение, TON', minDep), K.field('Срок промокода, дн.', promoDays)), bal = K.field('Сумма, TON', amount), wag = h('div', {class: 'form-grid'}, K.field('X отыгрыша', wager), K.field('Жизнь подарка, дн.', giftDays));
      const sync = () => {
        const k = kind.value; dep.hidden = k !== 'deposit_bonus'; bal.hidden = k !== 'balance'; gsec.hidden = !(k === 'gift' || k === 'wager_gift'); wag.hidden = k !== 'wager_gift';
        const others = data.prizes.filter(x => x.active && x.id !== editing?.id).reduce((s, x) => s + x.weight, 0), w = Number(weight.value || 0);
        hint.textContent = others + w > 0 ? `Шанс этого приза ≈ ${(w / (others + w) * 100).toFixed(2)}% (общий вес ${fmt(others + w)})` : '';
      };
      kind.onchange = sync; weight.oninput = sync; sync();
      const submit = async () => {
        const k = kind.value, g = picker.gift;
        if ((k === 'gift' || k === 'wager_gift') && !picker.value) throw new Error('Выберите подарок из Portal');
        const f = new FormData(); f.append('kind', k); f.append('weight', weight.value || '0'); f.append('title', label.value.trim());
        if (k === 'deposit_bonus') { f.append('percent', percent.value); f.append('min_deposit', minDep.value || '0'); f.append('promo_days', promoDays.value || '0'); }
        if (k === 'balance') f.append('amount', amount.value);
        if (k === 'gift' || k === 'wager_gift') { f.append('gift_name', g?.name || p.gift_name || ''); f.append('gift_price', g?.price_ton ?? p.gift_price ?? ''); f.append('gift_id', picker.value); f.append('gift_expires_days', giftDays.value || '0'); }
        if (k === 'wager_gift') f.append('wager_multiplier', wager.value);
        if (file.files[0]) f.append('image', file.files[0]);
        await api(editing ? '/api/admin/wheel/prizes/' + editing.id : '/api/admin/wheel/prizes', f);
        K.ok(editing ? 'Приз сохранён' : 'Приз добавлен'); editing = null; await reload();
      };
      const form = K.form([K.field('Тип приза', kind), dep, bal, gsec, wag, K.field('Подпись (необязательно)', label), K.field('Шанс (вес). Чем больше число, тем чаще выпадает', weight, null), hint,
        K.field('Своя картинка (необязательно)', file, 'Для подарка картинка берётся из Portal автоматически. Для пополнения и баланса без картинки рисуется билет.')], submit, editing ? 'Сохранить' : 'Добавить приз');
      const cancel = editing ? K.btn('Отмена', async () => { editing = null; paintForm(); }) : null;
      formBox.replaceChildren(K.card(editing ? 'Изменить приз' : 'Добавить приз', null, form, cancel));
    }

    async function reload() { data = await api('/api/admin/wheel'); paintTop(); paintPrizes(); paintForm(); stats.textContent = `${data.prizes.length} шт. · прокруток: ${data.spins}`; }
    const stats = h('span', {class: 'muted'});
    root.append(K.head('Колесо призов', 'Призы, шансы и коды для прокрутки колеса.', 'Награды'), top, K.card('Призы', null, stats, prizesBox), formBox);
    paintTop(); paintPrizes(); paintForm(); stats.textContent = `${data.prizes.length} шт. · прокруток: ${data.spins}`;
  }});

  /* ---------- Freebet ---------- */
  K.register({id: 'freebetsAdminPage', title: 'Freebet', async render(root) {
    const preset = K.preset; K.preset = null;
    const [list0, creators] = await Promise.all([api('/api/admin/freebets'), api('/api/admin/creators').then(d => d.items || []).catch(() => [])]);
    const REW = [['balance', 'Баланс TON'], ['pumpkins', 'Тыквы (Хэллоуин)'], ['gift', 'Подарок'], ['wager_gift', 'Отыгрышный подарок'], ['deposit_bonus', 'Бонус на пополнение'], ['multi', 'Несколько наград']];
    const code = K.input({maxLength: 32, placeholder: 'FB_... (можно оставить пустым)'});
    const author = K.select([['0', 'Без автора'], ...creators.map(c => [String(c.id), (c.name || 'Автор') + (c.username ? ' · @' + c.username : '') + ' · ID ' + c.id])], preset?.author ? String(preset.author) : '0');
    const type = K.select(REW, 'balance'), target = K.select([['main', 'Основной баланс'], ['bonus', 'Бонусный баланс']], 'main');
    const amount = K.number(1, {min: '0.01', step: '0.01'}), pumpkins = K.number(100, {min: 1, max: 100000000, step: 1});
    const gift = K.giftPicker('', {label: 'Подарок', models: true}), wagerX = K.number(25, {min: 1, max: 1000}), lives = K.number(1, {min: 1, max: 100, step: 1}), giftDays = K.number(0, {min: 0, max: 3650, step: 1});
    const burn = K.check('Сгораемый подарок (один общий Fragment-подарок на всех)', false), burnUrl = K.input({type: 'url', placeholder: 'https://fragment.com/gift/...'});
    const pct = K.number('', {min: 0, max: 100, step: '0.1', placeholder: '%'}), fixed = K.number('', {min: 0, step: '0.01', placeholder: 'TON'}), minDep = K.number(0, {min: 0, step: '0.01'});
    const mBal = K.check('TON на баланс', false), mAmt = K.number(1, {min: '0.01', step: '0.01'}), mGiftOn = K.check('Обычный подарок', false), mGift = K.giftPicker('', {label: 'Подарок', models: true}), mWOn = K.check('Отыгрышный подарок', false), mWGift = K.giftPicker('', {label: 'Отыгрышный подарок', models: true}), mWX = K.number(25, {min: 1, max: 1000}), mWDays = K.number(0, {min: 0, max: 3650}), mDOn = K.check('Бонус к пополнению', false), mPct = K.number('', {min: 0, max: 100, step: '0.1', placeholder: '%'}), mFix = K.number('', {min: 0, step: '0.01', placeholder: 'TON'}), mMin = K.number(0, {min: 0, step: '0.01'});
    const reqSub = K.switchRow('Только для участников чата', 'Проверять участие в привязанном Telegram-чате/канале через getChatMember.', true);
    const minLevel = K.number(0, {min: 0, step: 1}), tgLevel = K.number(0, {min: 0, step: 1}), turnover = K.number(0, {min: 0, step: '0.01'}), actDep = K.number(0, {min: 0, step: '0.01'}), expDays = K.number(0, {min: 0, max: 3650, step: 1}), maxUses = K.number(1, {min: 0, max: 1000000, step: 1});
    const grp = (...n) => h('div', {class: 'stack'}, n);
    const fBal = grp(K.field('Куда начислять / происхождение награды', target, 'Бонусные подарки требуют нового депозита после получения перед выводом.'));
    const fAmt = grp(K.field('Сумма TON', amount)), fPump = grp(K.field('Количество тыкв', pumpkins, 'Текст сообщения меняется в разделе уведомлений («Фрибет в тыквах активирован»).'));
    const fGift = grp(K.field('Подарок', gift.node)), fWager = grp(K.field('Отыгрыш X', wagerX), K.field('Жизни подарка', lives, '1 жизнь = подарок сгорит после первого проигрыша.'), K.field('Сгорает через, дней', giftDays), burn, h('div', {id: 'burnUrl'}, K.field('Ссылка на подарок Fragment', burnUrl)));
    const fDep = grp(h('div', {class: 'form-grid'}, K.field('Бонус, %', pct), K.field('или фиксированный, TON', fixed)), K.field('Минимальное пополнение, TON', minDep));
    const fMulti = grp(mBal, K.field('Сумма TON', mAmt), mGiftOn, mGift.node, mWOn, mWGift.node, K.field('Отыгрыш X', mWX), K.field('Сгорит через дней', mWDays), mDOn, h('div', {class: 'form-grid'}, K.field('Бонус, %', mPct), K.field('Бонус, TON', mFix)), K.field('Пополнение от, TON', mMin));
    const sync = () => {
      const t = type.value; fBal.hidden = !['balance', 'gift', 'wager_gift', 'multi'].includes(t); fAmt.hidden = t !== 'balance'; fPump.hidden = t !== 'pumpkins'; fGift.hidden = !['gift', 'wager_gift'].includes(t);
      fWager.hidden = t !== 'wager_gift'; fWager.querySelector('#burnUrl').hidden = !burn.querySelector('input').checked; fDep.hidden = t !== 'deposit_bonus'; fMulti.hidden = t !== 'multi';
    };
    type.onchange = sync; burn.querySelector('input').onchange = sync; sync();
    const comps = () => { const c = {}; const on = x => x.querySelector('input').checked; if (on(mBal)) c.balance = {amount: mAmt.value}; if (on(mGiftOn)) c.gift = {gift_id: mGift.value}; if (on(mWOn)) c.wager_gift = {gift_id: mWGift.value, wager_multiplier: mWX.value, gift_expires_days: mWDays.value}; if (on(mDOn)) c.deposit_bonus = {bonus_percent: mPct.value, bonus_fixed: mFix.value, min_deposit: mMin.value}; return c; };
    const listBox = h('div', {class: 'list'}), hint = h('div'), count = h('span', {class: 'muted'});
    const paint = d => {
      count.textContent = d.items.length;
      hint.replaceChildren(K.notice(d.channel?.chat_id ? 'Участие проверяется в Telegram-чате/канале: ' + (d.channel.title || d.channel.chat_id) : 'Чат/канал не привязан. Для Freebet с условием участника сначала настройте раздел «Пост в канал».'));
      listBox.replaceChildren(...(d.items.length ? d.items.map(x => {
        let extra = '';
        if (x.require_subscription) extra += ' · участники чата'; if (x.min_level) extra += ' · GemDrop LVL ' + x.min_level; if (x.min_telegram_level) extra += ' · TG Rating ' + x.min_telegram_level;
        if (x.min_turnover) extra += ' · оборот ' + K.money(x.min_turnover) + ' TON'; if (x.min_deposit) extra += ' · депозит от ' + K.money(x.min_deposit) + ' TON';
        if (x.burn_pool_enabled) extra += ' · сгораемый подарок · ' + (x.pool_claimed ? 'победитель определён' : 'ждёт победителя'); if (x.author_user_id) extra += ' · автор ID ' + x.author_user_id;
        if (['balance', 'gift', 'wager_gift', 'multi'].includes(x.reward_type)) extra += ' · ' + (x.balance_target === 'bonus' ? 'бонусный' : 'основной') + ' баланс';
        return h('div', {class: 'list-item', style: {flexWrap: 'wrap', opacity: x.active ? '1' : '.55'}},
          h('div', {class: 'grow'}, h('strong', {text: x.code}), h('small', {text: x.purpose}), h('small', {text: `${x.uses_count}/${x.max_uses === 0 ? '∞' : x.max_uses} активаций${extra}`}), x.link ? h('small', {text: x.link, style: {wordBreak: 'break-all'}}) : null),
          h('div', {class: 'row'}, K.btn('Копировать ссылку', () => K.copy(x.link), 'small'),
            K.btn(x.active ? 'Отключить' : 'Включить', async () => { await api(`/api/admin/freebets/${encodeURIComponent(x.code)}/toggle`, {}); await reload(); }, 'small'),
            K.btn('Удалить', async () => { if (!await K.confirm(`Удалить фрибет ${x.code}? Использованный фрибет будет только отключён.`, 'Удалить', true)) return; await K.del('/api/admin/freebets/' + encodeURIComponent(x.code)); await reload(); }, 'small danger')));
      }) : [K.empty('Фрибетов пока нет')]));
    };
    async function reload() { paint(await api('/api/admin/freebets')); }
    const form = K.form([K.field('Код', code), K.field('Персональный Freebet автора (необязательно)', author, 'Если выбрать автора, Freebet появится у него в панели со статистикой активаций.'), K.field('Награда', type), fBal, fAmt, fPump, fGift, fWager, fDep, fMulti,
      h('div', {class: 'group-title', text: 'Условия'}), reqSub,
      h('div', {class: 'form-grid'}, K.field('Мин. уровень GemDrop', minLevel), K.field('Telegram Rating level', tgLevel), K.field('Мин. оборот, TON', turnover), K.field('Мин. подтверждённый депозит, TON', actDep, '0 = без условия'), K.field('Фрибет действует, дней', expDays), K.field('Лимит активаций · 0 = ∞', maxUses))], async () => {
      const t = type.value;
      const d = await api('/api/admin/freebets', {code: code.value, reward_type: t, balance_target: target.value, amount: amount.value, pumpkins: pumpkins.value, gift_id: gift.value, wager_multiplier: wagerX.value, wager_attempts: lives.value, gift_expires_days: giftDays.value, burn_pool_enabled: burn.querySelector('input').checked, burn_fragment_url: burnUrl.value, bonus_percent: pct.value, bonus_fixed: fixed.value, min_deposit: minDep.value, components: t === 'multi' ? comps() : undefined, require_subscription: reqSub.input.checked, min_level: minLevel.value, min_telegram_level: tgLevel.value, min_turnover: turnover.value, activation_min_deposit: actDep.value, expires_in_days: expDays.value, max_uses: maxUses.value, author_user_id: author.value});
      code.value = ''; K.ok('Фрибет создан'); await reload(); if (d.link) K.copy(d.link);
    }, 'Создать фрибет');
    paint(list0);
    root.append(K.head('Freebet', 'Награда активируется только через ссылку вида /start freebet_CODE.', 'Награды'), K.card('Новый фрибет', null, form), K.card('Созданные фрибеты', null, count, hint, listBox));
  }});
})();
