/* Reward tasks. */
(() => {
  const K = window.AdminKit, {h, api} = K;
  const METRICS = [['upgrade_play', 'Сыграть в апгрейд'], ['upgrade_win', 'Победить в апгрейде'], ['upgrade_gift', 'Выиграть подарок в апгрейде'], ['mines_play', 'Сыграть в Mines'], ['mines_win', 'Выиграть в Mines'], ['deposit', 'Пополнить баланс'], ['deposit_5', 'Пополнить баланс от 5 TON'], ['referral', 'Пригласить друзей'], ['promo', 'Активировать промокод'], ['level', 'Достичь уровня'], ['link', 'Открыть ссылку'], ['subscribe', 'Вступить в чат / подписаться на канал']];
  const OPS = [['any', 'Любой'], ['lt', 'меньше'], ['lte', 'не больше'], ['gt', 'больше'], ['gte', 'не меньше']];
  const CATS = {once: 'Одноразовое', daily: 'Ежедневное', limited: 'На время'};
  const when = ts => !ts ? 'ещё не запускалась' : 'последняя генерация: ' + K.time(ts);

  K.register({id: 'rewardTasksAdminPage', title: 'Задания и билеты', async render(root) {
    const [auto, channels] = await Promise.all([api('/api/admin/reward-tasks/autogen'), api('/api/admin/external-channels').then(d => d.items || []).catch(() => [])]);
    let editing = null;

    /* автогенерация */
    const on = K.switchRow('Автогенерация заданий', 'Бот сам создаёт новый набор заданий через заданный период.', !!auto.enabled), cnt = K.number(auto.count || 4, {min: 1, max: 20, step: 1}), iv = K.number(auto.interval_hours || 16, {min: 1, step: 1});
    const status = h('small', {class: 'muted', text: when(auto.last_generated_at) + (auto.enabled ? ' · работает' : ' · выключено')});
    const autoBody = (generateNow) => ({enabled: true, count: cnt.value, interval_hours: iv.value, generate_now: false});
    const autoForm = K.form([on, h('div', {class: 'form-grid'}, K.field('Количество заданий', cnt), K.field('Обновлять каждые, часов', iv))], async () => {
      const d = await api('/api/admin/reward-tasks/autogen', {enabled: on.input.checked, count: cnt.value, interval_hours: iv.value, generate_now: false});
      status.textContent = when(d.last_generated_at) + (d.enabled ? ' · работает' : ' · выключено'); K.ok('Автогенерация сохранена');
    });
    const genBtn = K.btn('Сгенерировать сейчас', async () => {
      if (!on.input.checked) { await api('/api/admin/reward-tasks/autogen', autoBody()); on.input.checked = true; }
      const d = await api('/api/admin/reward-tasks/autogen/generate', {}); status.textContent = when(d.last_generated_at) + ' · работает'; await reload(); K.ok('Новый набор заданий создан');
    });

    /* форма */
    const formBox = h('div'), list = h('div', {class: 'list'}), count = h('span', {class: 'muted'});
    function paintForm() {
      const t = editing || {};
      const metric = K.select(METRICS, t.metric === 'upgrade_low' ? 'upgrade_win' : (t.metric || 'upgrade_play')), op = K.select(OPS, t.chance_operator || 'any'), chance = K.number(t.chance_percent ?? 25, {min: 0, max: 100, step: '.1'});
      const min = K.input({value: t.metric === 'level' ? (t.min_value || '') : (t.min_value ? t.min_value / 100 : ''), inputMode: 'decimal'}), minLabel = h('span'), link = K.input({type: 'url', value: t.metric === 'link' ? (t.link_url || '') : '', placeholder: 'https://…'});
      const chan = K.select([['', 'Основной чат/канал публикаций'], ...channels.map(x => [String(x.id), x.title || (x.username ? '@' + x.username : x.chat_id)])], t.external_channel_id ? String(t.external_channel_id) : '');
      const goal = K.number(t.goal ?? 1, {min: 1, step: 1}), tickets = K.number(t.tickets ?? 10, {min: 0, step: 1}), cat = K.select(Object.entries(CATS), t.category || 'once'), hours = K.number('', {min: 1, step: 1, placeholder: editing ? 'Без изменения' : ''});
      const desc = K.input({value: t.description || '', maxLength: 300}), title = K.input({value: t.auto_title ? '' : (t.title || ''), maxLength: 140});
      const gMin = h('label', null, minLabel, min), gLink = K.field('Ссылка (https://…)', link), gChan = K.field('Чат / канал для проверки подписки', chan, 'Сторонние каналы добавляются в «Каналы для заданий».'), gGoal = K.field('Количество', goal), gChance = h('div', {class: 'form-grid'}, K.field('Шанс', op), K.field('Процент', chance)), gHours = K.field('Срок, часы', hours);
      const names = Object.fromEntries(METRICS);
      const sync = () => {
        const m = metric.value, up = m.startsWith('upgrade'), valued = ['mines_play', 'mines_win', 'upgrade_play', 'upgrade_win', 'upgrade_gift', 'deposit'].includes(m), special = ['level', 'link', 'subscribe'].includes(m);
        gMin.hidden = !(valued || m === 'level'); minLabel.textContent = m === 'level' ? 'Нужный уровень' : 'Мин. сумма, TON'; gLink.hidden = m !== 'link'; gChan.hidden = m !== 'subscribe'; gGoal.hidden = special; gChance.hidden = !up; chance.closest('label').hidden = !up || op.value === 'any'; gHours.hidden = cat.value !== 'limited';
        let s = `${names[m]} ${goal.value || 1} раз`; if (up && op.value !== 'any') s += ` с шансом ${op.selectedOptions[0].text.toLowerCase()} ${chance.value}%`; title.placeholder = s;
      };
      [metric, op, chance, cat, goal].forEach(c => c.addEventListener('input', sync)); sync();
      const form = K.form([K.field('Действие', metric), gChance, gMin, gLink, gChan, gGoal, h('div', {class: 'form-grid'}, K.field('Награда, билеты', tickets), K.field('Период', cat)), gHours, K.field('Описание — необязательно', desc), K.field('Название — необязательно', title)], async () => {
        const p = {title: title.value, category: cat.value, metric: metric.value, goal: goal.value, tickets: tickets.value, chance_operator: op.value, chance_percent: chance.value, min_value: min.value.trim().replace(',', '.'), link_url: link.value.trim(), external_channel_id: chan.value, description: desc.value.trim()};
        if (hours.value) p.duration_hours = hours.value;
        await api('/api/admin/reward-tasks' + (editing ? '/' + editing.id : ''), p, editing ? 'PUT' : 'POST'); editing = null; K.ok('Задание сохранено'); paintForm(); await reload();
      }, editing ? 'Сохранить' : 'Добавить');
      formBox.replaceChildren(K.card(editing ? 'Редактирование' : 'Новое задание', null, form, editing ? K.btn('Отмена', async () => { editing = null; paintForm(); }) : null));
    }
    function paintList(items) {
      count.textContent = items.length; list.replaceChildren();
      if (!items.length) list.append(K.empty('Заданий пока нет'));
      for (const t of items) list.append(h('div', {class: 'list-item', style: {flexWrap: 'wrap', opacity: t.active ? '1' : '.55'}},
        h('div', {class: 'grow'}, h('strong', {text: t.title}), h('small', {text: `${t.tickets} билетов · ${t.active ? '' : 'выключено · '}${CATS[t.category] || ''}${t.demo ? ' · Пример' : ''}${t.autogenerated ? ' · Авто' : ''}${t.metric === 'subscribe' && t.channel_title ? ' · ' + t.channel_title : ''}`})),
        h('div', {class: 'row'}, h('button', {type: 'button', class: 'small', text: 'Изменить', onclick: () => { editing = t; paintForm(); formBox.scrollIntoView({block: 'start'}); }}),
          K.btn(t.active ? 'Выключить' : 'Включить', async () => { await api(`/api/admin/reward-tasks/${t.id}/toggle`, {}); await reload(); }, 'small'),
          K.btn('Удалить', async () => { if (!await K.confirm(`Удалить задание «${t.title}»? Если за него уже выдавали билеты, оно будет только выключено.`, 'Удалить', true)) return; await K.del('/api/admin/reward-tasks/' + t.id); await reload(); }, 'small danger'))));
    }
    async function reload() { paintList((await api('/api/admin/reward-tasks')).items || []); }
    paintForm(); await reload();
    root.append(K.head('Задания и билеты', 'Задания за билеты розыгрышей: создавайте вручную или включите автогенерацию.', 'Награды'), K.card('Автогенерация', null, autoForm, h('div', {class: 'row'}, genBtn), status), formBox, K.card('Задания', null, count, list));
  }});
})();
