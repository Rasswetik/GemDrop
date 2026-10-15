/* Security and Telegram channels: antifraud, external channels. */
(() => {
  const K = window.AdminKit, {h, api} = K;
  const uname = u => u.name || (u.username ? '@' + u.username : '') || 'ID ' + u.id;

  /* ---------- Мульти-аккаунты и баны ---------- */
  K.register({id: 'antifraudPage', title: 'Мульти-аккаунты и баны', async render(root) {
    const out = h('div', {class: 'stack'});
    const load = async () => {
      const d = await api('/api/admin/antifraud');
      const enabled = K.switchRow('Блокировать вторые аккаунты', 'Самые старые аккаунты на IP остаются, более новые блокируются. Администраторы и креаторы не блокируются. Учитываются заходы за последние 30 дней.', d.settings.enabled);
      const limit = K.number(d.settings.ip_limit, {min: 1, max: 50, step: 1, inputMode: 'numeric'});
      const you = d.you ? K.notice(`Ваш IP для сервера: ${d.you.ip || 'не определён'}${d.you.source ? ' (источник: ' + d.you.source + ')' : ''}. CF-Connecting-IP: ${d.you.cf || '—'}; X-Forwarded-For: ${d.you.forwarded || '—'}; соединение: ${d.you.remote_addr || '—'}`) : null;
      const form = K.form([enabled, K.field('Допустимо аккаунтов с одного IP', limit, 'Общие IP (мобильные операторы, общежития, офисы) дают ложные срабатывания — держите лимит повыше, а ошибочно заблокированных разблокируйте: такие аккаунты больше не блокируются автоматически.')], async () => {
        await api('/api/admin/antifraud', {enabled: enabled.input.checked, ip_limit: limit.value}); K.ok('Настройки сохранены.'); await load();
      });
      const actions = h('div', {class: 'row'},
        K.btn('Проверить сейчас', async () => { const r = await api('/api/admin/antifraud/scan', {}); K.ok(r.banned ? 'Заблокировано аккаунтов: ' + r.banned : 'Новых нарушителей нет'); await load(); }),
        K.btn('Исправить IP и снять ошибочные баны', async () => {
          if (!await K.confirm('Удалить из базы адреса прокси и служебные IP и снять автоблокировки, которые на них держались? Ручные баны не затрагиваются.', 'Исправить')) return;
          const r = await api('/api/admin/antifraud/cleanup', {}); K.ok(`Удалено записей IP: ${r.removed}; разблокировано: ${r.unbanned}`); await load();
        }));
      const groups = h('div', {class: 'list'});
      if (!d.groups.length) groups.append(K.empty('IP с несколькими аккаунтами не найдены'));
      for (const g of d.groups) groups.append(h('div', {class: 'list-item', style: {flexWrap: 'wrap'}},
        h('div', {class: 'grow'}, h('strong', {text: g.ip}), h('small', {text: g.count + ' акк.'})),
        h('div', {class: 'row'}, g.users.map(u => h('button', {type: 'button', class: 'small', text: uname(u) + (u.banned ? (u.kind === 'multi' ? ' · бан авто' : ' · бан вручную') : u.exempt ? ' · админ/креатор' : ''), onclick: () => K.openUser(u.id)})))));
      const banned = h('div', {class: 'list'});
      if (!d.banned.length) banned.append(K.empty('Заблокированных нет'));
      for (const u of d.banned) banned.append(h('div', {class: 'list-item'},
        h('div', {class: 'grow'}, h('button', {type: 'button', text: uname(u), onclick: () => K.openUser(u.id)}), h('small', {text: (u.kind === 'multi' ? 'авто' : 'вручную') + (u.reason ? ' · ' + u.reason : '')})),
        K.btn('Разблокировать', async () => { await api(`/api/admin/users/${u.id}/ban`, {banned: false}); K.ok('Разблокирован'); await load(); }, 'small')));
      out.replaceChildren(K.card('Автоблокировка', null, form, actions, you),
        K.card('IP с несколькими аккаунтами (' + d.groups.length + ')', null, groups),
        K.card('Заблокированные (' + d.banned.length + ')', null, banned));
    };
    root.append(K.head('Мульти-аккаунты и баны', 'Аккаунты с одного IP, автоблокировка вторых аккаунтов и список заблокированных.', 'Безопасность'), out);
    await load();
  }});

  /* ---------- Сторонние каналы ---------- */
  K.register({id: 'externalChannelsPage', title: 'Сторонние каналы', async render(root) {
    const label = x => x.title || (x.username ? '@' + x.username : x.chat_id);
    const input = K.input({placeholder: '-1001234567890 · @channel · https://t.me/channel'}), status = h('div'), list = h('div', {class: 'list'});
    const paint = items => {
      list.replaceChildren();
      if (!items.length) list.append(K.empty('Сторонних каналов пока нет'));
      for (const x of items) list.append(h('div', {class: 'list-item'},
        h('div', {class: 'grow'}, h('strong', {text: x.title || x.chat_id}), h('small', {text: [x.username ? '@' + x.username : '', x.chat_type || '', x.bot_status ? 'бот: ' + x.bot_status : ''].filter(Boolean).join(' · ')})),
        x.join_url ? K.link('Открыть', x.join_url, 'button small') : null,
        K.btn('Удалить', async () => { if (!await K.confirm(`Удалить привязку «${label(x)}»?`, 'Удалить', true)) return; paint((await K.del('/api/admin/external-channels/' + encodeURIComponent(x.id))).items || []); K.ok('Привязка удалена'); }, 'small danger')));
    };
    const add = K.form([K.field('ID, @username или t.me-ссылка', input, 'Бот должен быть администратором. Для приватного чата/канала дайте боту право создавать пригласительную ссылку.')], async () => {
      const v = input.value.trim(); if (!v) throw new Error('Введите ID, @username или t.me-ссылку');
      try { const d = await api('/api/admin/external-channels', {chat_id: v}); input.value = ''; status.replaceChildren(K.notice('✓ Бот — администратор · ' + label(d.item), 'good')); paint(d.items || []); K.ok('Чат/канал привязан'); }
      catch (e) { status.replaceChildren(K.notice(e.message, 'bad')); throw e; }
    }, 'Проверить и привязать');
    paint((await api('/api/admin/external-channels')).items || []);
    root.append(K.head('Сторонние каналы', 'Каналы и чаты для заданий на подписку.', 'Telegram · Подписки'), K.card('Привязать чат или канал', null, add, status), K.card('Привязанные', null, list));
  }});
})();
