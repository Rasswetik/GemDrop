/* Медали: каталог, создание из PNG, скрытие, выдача игрокам и тексты уведомлений. */
(() => {
  const K = window.AdminKit, {h, api} = K;
  const art = (key, size) => { const d = h('div', {style: {width: size + 6 + 'px', flex: 'none'}}); d.innerHTML = window.GDMedals.svg(key, size); return d; };

  function pickMedal(current, pick) {
    const back = h('div', {style: {position: 'fixed', inset: 0, zIndex: 9999, background: 'rgba(6,4,14,.72)', display: 'grid', placeItems: 'center', padding: '16px'}});
    const close = () => back.remove();
    const grid = h('div', {style: {display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(120px,1fr))', gap: '12px'}});
    const list = window.GDMedals.LIST.filter(m => !m.hidden);
    [...new Set(list.map(m => m.set))].forEach(set => {
      grid.append(h('div', {style: {gridColumn: '1/-1', fontWeight: 800, marginTop: '6px'}, text: set}));
      list.filter(m => m.set === set).forEach(m => {
        const tile = h('button', {type: 'button', style: {display: 'grid', justifyItems: 'center', gap: '6px', padding: '12px 8px', borderRadius: '16px', border: m.key === current ? '2px solid #a066ff' : '1px solid rgba(255,255,255,.12)', background: '#1b1e27', color: '#fff', cursor: 'pointer'}, onclick: () => { pick(m.key); close(); }});
        const a = h('div'); a.innerHTML = window.GDMedals.svg(m.key, 84);
        tile.append(a, h('b', {text: m.name, style: {fontSize: '12px', textAlign: 'center'}}));
        grid.append(tile);
      });
    });
    back.append(h('div', {style: {width: 'min(100%,560px)', maxHeight: '86vh', overflow: 'auto', padding: '18px', borderRadius: '22px', background: '#14161d', border: '1px solid rgba(255,255,255,.1)', color: '#fff'}},
      h('div', {style: {display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px'}}, h('strong', {text: 'Выберите медаль', style: {fontSize: '18px'}}), K.btn('Закрыть', close, 'small')), grid));
    back.addEventListener('click', e => { if (e.target === back) close(); });
    document.body.append(back);
  }
  window.AdminMedalPicker = pickMedal;

  K.register({id: 'medalsAdminPage', title: 'Медали', async render(root) {
    const box = h('div', {class: 'stack'});
    root.append(K.head('Медали', 'Каталог медалей: создавайте свои из картинки, переименовывайте, скрывайте и выдавайте игрокам.', 'Игроки'), box);

    async function paint() {
      const d = await api('/api/admin/medals/all', undefined, 'GET'); await window.GDMedals.load();
      box.replaceChildren();

      const list = h('div', {class: 'list'});
      d.medals.forEach(m => {
        const nm = h('input', {type: 'text', value: m.name, maxLength: 40, style: {width: '100%'}});
        const tag = (m.builtin ? 'Встроенная' : (m.manual_only ? 'Загруженная · только ручная выдача' : 'Загруженная')) + (m.hidden ? ' · СКРЫТА' : '');
        list.append(h('div', {class: 'list-item', style: {opacity: m.hidden ? .55 : 1}}, art(m.key, 44), h('div', {class: 'grow'}, nm, h('small', {class: 'muted', text: tag})),
          K.btn('Сохранить', async () => { await api('/api/admin/medals/rename', {key: m.key, name: nm.value}); K.ok('Название сохранено'); await paint(); }),
          K.btn(m.hidden ? 'Показать' : 'Скрыть', async () => { await api('/api/admin/medals/hide', {key: m.key, hidden: !m.hidden}); K.ok(m.hidden ? 'Медаль снова доступна' : 'Медаль скрыта'); await paint(); }),
          m.builtin ? null : K.btn('Удалить', async () => { if (!confirm('Удалить медаль «' + m.name + '»?')) return; await api('/api/admin/medals/' + m.key + '/delete', {}); K.ok('Медаль удалена'); await paint(); }, 'danger')));
      });
      box.append(K.card('Все медали', 'Скрытая медаль не предлагается при выдаче и в наградах сезона; у тех, кто её уже получил, остаётся.', list));

      const nn = h('input', {type: 'text', maxLength: 40, placeholder: 'Название новой медали', style: {width: '100%'}}), fi = h('input', {type: 'file', accept: 'image/png,image/jpeg,image/webp'}), mo = K.check('Только ручная выдача (не показывать в наградах сезона)', true);
      box.append(K.card('Создать медаль', 'Загрузите картинку PNG, JPG или WEBP (лучше квадрат до 2 МБ): она вставится в золотую рамку.', K.form([K.field('Название', nn), K.field('Картинка', fi), mo], async () => {
        const f = fi.files[0]; if (!f) throw new Error('Выберите картинку.');
        const image = await new Promise((ok, no) => { const r = new FileReader(); r.onload = () => ok(r.result); r.onerror = no; r.readAsDataURL(f); });
        await api('/api/admin/medals/create', {name: nn.value, image, manual_only: mo.querySelector('input').checked}); K.ok('Медаль создана'); await paint();
      }, 'Создать медаль')));

      const uid = h('input', {type: 'number', placeholder: 'ID игрока', inputMode: 'numeric', style: {width: '100%'}}), ttl = h('input', {type: 'text', maxLength: 60, placeholder: 'Подпись (например: За верность проекту)', style: {width: '100%'}});
      let pick = ''; const prev = h('div', {style: {width: '64px'}}), pname = h('strong', {text: 'Медаль не выбрана'}), got = h('div', {class: 'list'});
      async function show() {
        got.replaceChildren(); const id = parseInt(uid.value, 10); if (!id) return;
        try {
          const r = await api('/api/admin/medals/user/' + id, undefined, 'GET');
          got.append(h('small', {class: 'muted', text: 'Медали игрока ' + (r.user.name || '') + (r.user.username ? ' @' + r.user.username : '') + (r.medals.length ? ':' : ' — пока нет.')}));
          r.medals.forEach(m => got.append(h('div', {class: 'list-item'}, art(m.key, 40), h('div', {class: 'grow'}, h('strong', {text: m.name}), h('small', {text: m.title + (m.place ? ' · ' + m.place + ' место' : '')})), K.btn('Забрать', async () => { await api('/api/admin/medals/' + m.id + '/revoke', {}); K.ok('Медаль забрана'); await show(); }, 'danger'))));
        } catch (e) { got.append(h('small', {class: 'bad', text: e.message})); }
      }
      uid.addEventListener('change', show);
      box.append(K.card('Выдача медали', 'Игрок получит уведомление и окно с кнопкой «Получить». Выдать и забрать можно также в карточке игрока, вкладка «Медали».', K.form([K.field('ID игрока', uid), K.field('Подпись', ttl),
        h('div', {class: 'row', style: {alignItems: 'center', gap: '12px'}}, prev, h('div', {class: 'grow'}, pname), K.btn('Выбрать медаль', () => pickMedal(pick, v => { pick = v; prev.innerHTML = window.GDMedals.svg(v, 60); pname.textContent = window.GDMedals.name(v); })))],
        async () => { if (!pick) throw new Error('Выберите медаль.'); await api('/api/admin/medals/grant', {user_id: uid.value, medal: pick, title: ttl.value}); K.ok('Медаль выдана'); await show(); }, 'Выдать медаль'), got));

      const n1 = h('textarea', {rows: 3, style: {width: '100%'}}), n2 = h('textarea', {rows: 2, style: {width: '100%'}}); n1.value = d.notify; n2.value = d.season;
      box.append(K.card('Тексты уведомлений', null, K.form([K.field('Уведомление при ручной выдаче', n1, 'Подстановки: {medal} — название, {title} — подпись. Разрешены теги <b> и <i>.'), K.field('Строка про медаль в итогах сезона', n2, 'Подстановка: {medal}.')],
        async () => { await api('/api/admin/medals/texts', {notify: n1.value, season: n2.value}); K.ok('Тексты сохранены'); }, 'Сохранить тексты')));
    }
    await paint();
  }});
})();
