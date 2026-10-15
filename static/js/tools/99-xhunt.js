/* X-Hunt: multiplier hunting event. */
(() => {
  const K = window.AdminKit, {h, api} = K;
  const MODES = [['mines', 'Mines'], ['upgrade', 'Upgrade'], ['crash', 'Crash'], ['arena', 'Arena'], ['hilo', 'Hi-Lo'], ['limbo', 'Limbo'], ['road', 'Hamster Road']];
  const TYPES = [['none', '— нет —'], ['ton', 'TON на основной баланс'], ['bonus', 'TON на бонусный баланс'], ['catalog', 'Подарок из Portal'], ['fragment', 'Подарок Telegram (NFT, ссылка)'], ['promo', 'Личный промокод']];
  const fx = n => { n = Number(n || 0); return n >= 100 ? n.toFixed(0) : n >= 10 ? n.toFixed(1).replace(/\.0$/, '') : n.toFixed(2).replace(/\.?0+$/, ''); };
  const clock = s => { if (s == null) return '∞'; const d = Math.floor(s / 86400), hh = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60), c = s % 60, p = v => String(v).padStart(2, '0'); return (d ? d + 'д ' : '') + (d || hh ? p(hh) + ':' : '') + p(m) + ':' + p(c); };

  K.register({id: 'xhuntAdminPage', title: 'X-Hunt', async render(root) {
    const gifts = await K.catalog().catch(() => []);
    const box = h('div', {class: 'stack'}); let timer = 0, alive = true, A = {cfg: false}, data = null;

    function rewardEditor(i) {
      let r = A.rewards[i] || {type: 'none'};
      const sel = K.select(TYPES.filter(([v]) => i !== 0 || v !== 'none'), r.type || (i === 0 ? 'ton' : 'none'));
      const f = h('div', {class: 'stack'});
      const fields = () => {
        f.replaceChildren(); const t = sel.value; r = A.rewards[i] = {...A.rewards[i], type: t};
        if (t === 'ton' || t === 'bonus') f.append(K.field('Сумма, TON', K.number(r.amount || '', {min: '0.01', step: '0.01', oninput: e => r.amount = e.target.value})));
        else if (t === 'catalog') { const g = K.giftPicker(r.gift_id || '', {label: 'Подарок Portal', onchange: v => r.gift_id = v}); f.append(g.node); setTimeout(() => { if (!r.gift_id) r.gift_id = g.value; }, 600); }
        else if (t === 'fragment') f.append(K.field('Ссылка на NFT', K.input({type: 'url', placeholder: 'https://t.me/nft/PlushPepe-123', value: r.fragment_url || '', oninput: e => r.fragment_url = e.target.value})));
        else if (t === 'promo') {
          const kind = K.select([['balance', 'Промокод: TON на основной баланс'], ['bonus', 'Промокод: TON на бонусный баланс'], ['deposit_bonus', 'Промокод: бонус к пополнению, %']], r.promo_kind || 'balance'); r.promo_kind = kind.value;
          const a = K.number('', {min: 0, step: '0.01'}), md = K.number(r.min_deposit || '', {min: 0, step: '0.01', placeholder: 'Мин. пополнение, TON (необязательно)', oninput: e => r.min_deposit = e.target.value}), ex = K.number(r.expires_days || '', {min: 0, max: 365, placeholder: 'Срок действия, дней (0 — без срока)', oninput: e => r.expires_days = e.target.value});
          const paint = () => { r.promo_kind = kind.value; const dep = kind.value === 'deposit_bonus'; a.placeholder = dep ? 'Процент бонуса, 1–100' : 'Сумма, TON'; a.value = (dep ? r.percent : r.amount) || ''; md.hidden = !dep; };
          kind.onchange = paint; a.oninput = () => { if (kind.value === 'deposit_bonus') r.percent = a.value; else r.amount = a.value; }; paint(); f.append(kind, a, md, ex);
        }
      };
      sel.onchange = fields; fields();
      return h('div', {class: 'list-item', style: {flexDirection: 'column', alignItems: 'stretch'}}, h('strong', {text: A.finish === 'target' ? 'Награда победителю' : (i + 1) + ' место'}), sel, f);
    }

    function prizeRow(r, place) { return h('div', {class: 'list-item'}, place ? K.badge(String(place)) : null, r.image_url ? h('img', {class: 'thumb', src: r.image_url, alt: '', referrerPolicy: 'no-referrer', style: {width: '40px', height: '40px'}, onerror(e) { e.target.replaceWith(document.createTextNode('★')); }}) : h('span', {text: r.type === 'promo' ? '%' : '★'}), h('div', {class: 'grow'}, h('strong', {text: r.title || 'Приз'}), h('small', {text: r.subtitle || ''}))); }

    async function stop(award) { await api('/api/admin/xhunt/stop', {award}); K.ok(award ? 'X-Hunt завершён' : 'X-Hunt отменён'); await load(); }

    function formCard(d) {
      const last = d.last_config || {};
      if (!A.cfg) { A.cfg = true; A.finish = last.finish_by || 'time'; A.all = last.all_modes !== undefined ? !!last.all_modes : true; A.modes = new Set(last.modes || MODES.map(m => m[0]));
        A.rewards = [...(last.rewards || []), {type: 'none'}, {type: 'none'}, {type: 'none'}].slice(0, 3).map((r, i) => i === 0 && (!r || r.type === 'none') ? {type: 'ton'} : r);
        A.v = {hours: last.duration_min ? Math.floor(last.duration_min / 60) : 1, minutes: last.duration_min ? last.duration_min % 60 : 0, target: last.target_x || '', deadline: last.deadline_hours || 0, confirm: last.confirm_seconds != null ? last.confirm_seconds : 90, min_bet: last.min_bet != null ? last.min_bet : '0.5', tg: !!last.telegram, adm: !!last.include_admins, ns: last.notify_start !== false, ne: last.notify_end !== false}; }
      const v = A.v, chips = h('div', {class: 'segmented', style: {flexWrap: 'wrap'}}), fin = h('div', {class: 'stack'}), rbox = h('div', {class: 'stack'}), seg = h('div', {class: 'segmented'});
      const paintChips = () => { chips.replaceChildren(h('button', {type: 'button', text: 'Все режимы', 'aria-pressed': String(A.all), onclick: () => { A.all = !A.all; if (A.all) A.modes = new Set(MODES.map(m => m[0])); paintChips(); }}), ...MODES.map(([k, t]) => h('button', {type: 'button', text: t, 'aria-pressed': String(A.modes.has(k)), onclick: () => { A.modes.has(k) ? A.modes.delete(k) : A.modes.add(k); A.all = A.modes.size === MODES.length; paintChips(); }}))); };
      const paintRewards = () => { rbox.replaceChildren(...[...Array(A.finish === 'target' ? 1 : 3).keys()].map(rewardEditor)); };
      const paintFinish = () => {
        seg.replaceChildren(...[['time', 'По времени'], ['target', 'По достижению X']].map(([k, t]) => h('button', {type: 'button', text: t, 'aria-pressed': String(A.finish === k), onclick: () => { A.finish = k; paintFinish(); }})));
        fin.replaceChildren();
        if (A.finish === 'time') fin.append(h('div', {class: 'form-grid'}, K.field('Часы', K.number(v.hours, {min: 0, oninput: e => v.hours = e.target.value})), K.field('Минуты', K.number(v.minutes, {min: 0, max: 59, oninput: e => v.minutes = e.target.value}))), h('small', {class: 'muted', text: 'Лучший множитель за это время получает приз 1 места; можно задать призы за 2 и 3 места.'}));
        else fin.append(h('div', {class: 'form-grid'}, K.field('Целевой X (например 50)', K.number(v.target, {step: '0.01', min: '1.01', oninput: e => v.target = e.target.value})), K.field('Дедлайн, часов (0 — без лимита)', K.number(v.deadline, {min: 0, max: 720, oninput: e => v.deadline = e.target.value}))), K.field('Отсчёт после цели, секунд', K.number(v.confirm, {min: 0, max: 3600, oninput: e => v.confirm = e.target.value}), 'Когда кто-то ловит цель, запускается финальный отсчёт (по умолчанию 90 секунд). Побьёт его более высокий X — отсчёт начнётся заново. Если никто не перебил, событие заканчивается и приз получает лидер. 0 — выиграть сразу. Если задан дедлайн и цели никто не достиг, событие закроется без награды.'));
        paintRewards();
      };
      paintChips();
      const chk = (label, key) => K.check(label, !!v[key], {onchange: e => v[key] = e.target.checked});
      const status = h('div');
      const form = K.form([K.field('Режимы', chips, 'Режим, выключенный в «Управлении играми», останется в списке с пометкой «скоро» — ставки в нём не принимаются.'), K.field('Чем заканчивается', seg), fin,
        K.field('Минимальная ставка, TON', K.number(v.min_bet, {step: '0.01', min: 0, oninput: e => v.min_bet = e.target.value}), 'Раунды со ставкой меньше этой суммы в X-Hunt не засчитываются.'), h('div', {class: 'group-title', text: 'Награды'}), rbox,
        h('div', {class: 'group-title', text: 'Уведомления'}), chk('Уведомить всех при старте (всплывающее окно в приложении)', 'ns'), chk('Уведомить всех при окончании (итоги и победители)', 'ne'), chk('Дублировать в Telegram (старт и итоги всем игрокам)', 'tg'), h('small', {class: 'muted', text: 'Победитель всегда получает личное окно в приложении и сообщение в Telegram. Промокод виден только ему.'}), chk('Засчитывать администраторов (для теста)', 'adm'), status], async () => {
        if (!A.modes.size) throw new Error('Выберите хотя бы один режим.');
        try { await api('/api/admin/xhunt/start', {modes: [...A.modes], all_modes: A.all, finish_by: A.finish, hours: v.hours, minutes: v.minutes, target_x: v.target, deadline_hours: v.deadline, confirm_seconds: v.confirm, min_bet: v.min_bet || 0, rewards: A.rewards.slice(0, A.finish === 'target' ? 1 : 3), telegram: !!v.tg, notify_start: !!v.ns, notify_end: !!v.ne, include_admins: !!v.adm}); A.cfg = false; K.ok('X-Hunt запущен'); await load(); }
        catch (e) { status.replaceChildren(K.notice(e.message, 'bad')); throw e; }
      }, 'Запустить X-Hunt');
      paintFinish(); return K.card('Запустить X-Hunt', null, form);
    }

    function paint(d) {
      data = d; box.replaceChildren();
      if (d.active && d.event) {
        const ev = d.event;
        box.append(K.card('Сейчас идёт', null, K.badge('LIVE', 'good'), ...[ev.finish_by === 'target' ? 'Цель: x' + fx(ev.target_x) + (ev.end_at ? ' · дедлайн через ' + clock(Math.max(0, ev.seconds_left)) : ' · без дедлайна') : 'Осталось: ' + clock(Math.max(0, ev.seconds_left)), 'Режимы: ' + (ev.all_modes ? 'все' : (ev.modes || []).map(m => m.label).join(', ')), 'Мин. ставка: ' + (ev.min_bet > 0 ? fx(ev.min_bet) + ' TON' : 'любая'), 'Игроков в зачёте: ' + (ev.players || 0), ev.leader ? `Лидер: ${ev.leader.name || 'Игрок'} · x${fx(ev.leader.x)} (${ev.leader.mode_label})` : 'Лидера пока нет'].map(t => h('div', {class: 'muted', text: t})),
          h('div', {class: 'group-title', text: 'Награды'}), h('div', {class: 'list'}, (ev.rewards || []).map((r, i) => prizeRow(r, (ev.rewards || []).length > 1 ? i + 1 : 0))),
          h('div', {class: 'row'}, K.btn(ev.finish_by === 'target' ? 'Завершить сейчас' : 'Завершить и выдать награды', async () => { if (!await K.confirm(ev.finish_by === 'target' ? 'Завершить X-Hunt? Награду получит тот, кто уже поймал цель (если такой есть).' : 'Завершить X-Hunt досрочно и выдать награды текущим лидерам?', 'Завершить')) return; await stop(true); }, 'primary'),
            K.btn('Отменить без наград', async () => { if (!await K.confirm('Отменить X-Hunt без выдачи наград?', 'Отменить', true)) return; await stop(false); }, 'danger'))));
      } else box.append(formCard(d));
      box.append(K.card('Прошлые события', null, h('div', {class: 'list'}, (d.history || []).length ? d.history.map(e => { const w = e.winners || []; return h('div', {class: 'list-item'}, h('strong', {text: 'X-Hunt #' + e.id}), h('small', {text: (e.finish_by === 'target' ? 'цель x' + fx(e.target_x) : 'по времени') + ' · ' + (e.reason === 'cancelled' ? 'отменён' : w.length ? `победитель: ${w[0].name || 'Игрок'} (x${fx(w[0].x)})` : 'без победителя')})); }) : [K.empty('Пока ничего не было.')])));
      clearTimeout(timer); if (d.active && alive) timer = setTimeout(() => load().catch(() => {}), 10000);
    }
    async function load() { paint(await api('/api/admin/xhunt')); }
    root.append(K.head('X-Hunt', 'Событие «охота за иксами»: игроки ловят максимальный множитель в выбранных играх и получают награды.', 'События'), box);
    await load();
    return () => { alive = false; clearTimeout(timer); };
  }});
})();
