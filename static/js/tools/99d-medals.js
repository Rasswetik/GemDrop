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
  const RARS = [['common','Обычная'],['rare','Редкая'],['epic','Эпическая'],['legendary','Легендарная']], PALS = [['gold','Золото'],['silver','Серебро'],['bronze','Бронза'],['black','Чёрная'],['obsidian','Обсидиан'],['violet','Фиолетовая'],['blue','Синяя'],['cyan','Голубая'],['green','Зелёная'],['red','Красная'],['pink','Розовая'],['orange','Оранжевая'],['night','Ночная'],['teal','Бирюзовая']];
  const sel = (opts, val) => { const e = h('select'); opts.forEach(([v, l]) => e.add(new Option(l, v))); e.value = val; return e; };


  const rarName = Object.fromEntries(RARS);
  K.register({id: 'medalsAdminPage', title: 'Медали', async render(root) {
    root.append(K.head('Медали', 'Каталог, коллекции и награды, выдача игрокам, промокоды и тексты.', 'Игроки'));
    const load = () => api('/api/admin/medals/all', undefined, 'GET').then(async d => { await window.GDMedals.load(); return d; });
    const chip = (text, color) => h('span', {text, style: {display: 'inline-block', padding: '2px 8px', borderRadius: '999px', fontSize: '11.5px', fontWeight: 700, background: color || 'rgba(120,130,160,.18)'}});
    const tabs = K.tabs([
      {id: 'cat', label: 'Медали', render: async body => { const d = await load(); const box = h('div', {class: 'stack wide'}); body.replaceChildren(box); const paint = () => tabs.select('cat'); catalog(box, d, paint); }},
      {id: 'col', label: 'Коллекции и награды', render: async body => { const d = await load(); const box = h('div', {class: 'stack wide'}); body.replaceChildren(box); collections(box, d, () => tabs.select('col')); }},
      {id: 'give', label: 'Выдача', render: async body => { const d = await load(); const box = h('div', {class: 'stack'}); body.replaceChildren(box); give(box, d, () => tabs.select('give')); }},
      {id: 'codes', label: 'Промокоды', render: async body => { const d = await load(); const box = h('div', {class: 'stack'}); body.replaceChildren(box); promo(box, d, () => tabs.select('codes')); }},
      {id: 'txt', label: 'Тексты', render: async body => { const d = await load(); const box = h('div', {class: 'stack'}); body.replaceChildren(box); texts(box, d); }},
    ]);
    root.append(tabs);

    function catalog(box, d, paint) {
      const cols = [...new Set(d.medals.map(m => m.event).filter(Boolean))];
      const dl = h('datalist', {id: 'mdCols'}, cols.map(c => h('option', {value: c})));
      const editor = h('div'), grid = h('div', {style: {display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(150px,1fr))', gap: '12px'}});
      const edit = m => {
        const nm = h('input', {type: 'text', value: m.name, maxLength: 40, style: {width: '100%'}}), rs = sel(RARS, m.rarity || 'common'), ev = h('input', {type: 'text', value: m.event || '', maxLength: 30, list: 'mdCols', placeholder: 'Без коллекции', style: {width: '100%'}});
        editor.replaceChildren(K.card('Медаль: ' + m.name, (m.builtin ? 'Встроенная' : m.manual_only ? 'Загруженная · только ручная выдача' : 'Загруженная') + (m.hidden ? ' · скрыта' : ''), h('div', {class: 'row', style: {alignItems: 'center', gap: '18px', flexWrap: 'wrap'}}, art(m.key, 110),
          h('div', {class: 'stack', style: {flex: 1, minWidth: '240px'}}, K.field('Название', nm), K.field('Редкость', rs), K.field('Коллекция', ev, 'Выберите из списка или впишите новую. Пусто — медаль не входит в событийную коллекцию.'),
            h('div', {class: 'row', style: {gap: '8px', flexWrap: 'wrap'}},
              K.btn('Сохранить', async () => { await api('/api/admin/medals/rename', {key: m.key, name: nm.value}); await api('/api/admin/medals/rarity', {key: m.key, rarity: rs.value}); await api('/api/admin/medals/event', {key: m.key, event: ev.value}); K.ok('Сохранено'); paint(); }, 'primary'),
              K.btn(m.hidden ? 'Показать' : 'Скрыть', async () => { await api('/api/admin/medals/hide', {key: m.key, hidden: !m.hidden}); K.ok(m.hidden ? 'Медаль снова доступна' : 'Медаль скрыта'); paint(); }),
              m.builtin ? null : K.btn('Удалить', async () => { if (!confirm('Удалить медаль «' + m.name + '»?')) return; await api('/api/admin/medals/' + m.key + '/delete', {}); K.ok('Медаль удалена'); paint(); }, 'danger'))))));
        editor.scrollIntoView({behavior: 'smooth', block: 'nearest'});
      };
      d.medals.forEach(m => {
        const t = h('button', {type: 'button', style: {display: 'grid', justifyItems: 'center', gap: '6px', padding: '12px 8px', borderRadius: '14px', border: '1px solid var(--line)', background: 'var(--surface)', cursor: 'pointer', opacity: m.hidden ? .5 : 1, color: 'inherit', textAlign: 'center'}, onclick: () => edit(m)});
        t.append(art(m.key, 64), h('strong', {text: m.name, style: {fontSize: '13px'}}), h('div', {style: {display: 'flex', gap: '4px', flexWrap: 'wrap', justifyContent: 'center'}}, chip(rarName[m.rarity] || 'Обычная'), m.event ? chip(m.event, 'rgba(255,180,84,.2)') : null, m.manual_only ? chip('ручная') : null));
        grid.append(t);
      });
      box.append(dl, K.head ? h('div', {class: 'row', style: {justifyContent: 'space-between', alignItems: 'center'}}, h('strong', {text: 'Всего медалей: ' + d.medals.length}), K.btn('＋ Создать медаль', () => { createCard.hidden = !createCard.hidden; }, 'small')) : null);
      const createCard = h('div'); createCard.hidden = true; box.append(createCard, editor, grid);
      createForm(createCard, cols, paint);
    }

    function createForm(host, cols, paint) {
      const cev = h('input', {type: 'text', maxLength: 30, placeholder: 'Событие — необязательно (коллекционная медаль)', style: {width: '100%'}}), nn = h('input', {type: 'text', maxLength: 40, placeholder: 'Название новой медали', style: {width: '100%'}}), fi = h('input', {type: 'file', accept: 'image/png,image/jpeg,image/webp'}), mo = K.check('Только ручная выдача (не показывать в наградах сезона)', true), cr = sel(RARS, 'common'), cp = sel(PALS, 'gold');
      host.append(K.card('Создать медаль', 'Загрузите картинку PNG, JPG или WEBP (лучше квадрат до 2 МБ): она вставится в золотую рамку.', K.form([K.field('Название', nn), K.field('Событие', cev, 'Если указать, медаль попадёт в коллекцию этого события — у неё свой прогресс и награда.'), K.field('Картинка', fi), K.field('Редкость', cr, 'Легендарные медали переливаются.'), K.field('Цвет рамки', cp), mo], async () => {
        const f = fi.files[0]; if (!f) throw new Error('Выберите картинку.');
        const image = await new Promise((ok, no) => { const r = new FileReader(); r.onload = () => ok(r.result); r.onerror = no; r.readAsDataURL(f); });
        await api('/api/admin/medals/create', {name: nn.value, image, rarity: cr.value, pal: cp.value, manual_only: mo.querySelector('input').checked, event: cev.value}); K.ok('Медаль создана'); paint();
      }, 'Создать медаль')));
    }

    function collections(box, d, paint) {
      const byScope = sc => sc.scope === '*' ? d.medals.filter(m => !m.hidden) : d.medals.filter(m => m.event === sc.scope);
      // new collection
      const colName = h('input', {type: 'text', maxLength: 30, placeholder: 'Название, например: Новый год', style: {width: '100%'}}), colPicked = new Set(), colPk = h('div', {style: {display: 'flex', flexWrap: 'wrap', gap: '8px'}});
      d.medals.forEach(m => { const t = h('button', {type: 'button', title: m.name, style: {border: '2px solid transparent', borderRadius: '12px', background: 'var(--surface-2)', padding: '4px', cursor: 'pointer'}}); t.append(art(m.key, 40)); t.onclick = () => { if (colPicked.has(m.key)) { colPicked.delete(m.key); t.style.borderColor = 'transparent'; } else { colPicked.add(m.key); t.style.borderColor = '#47b0f5'; } }; colPk.append(t); });
      const newCard = K.card('Новая коллекция', 'Введите название и отметьте медали, которые в неё входят.', K.form([K.field('Название', colName), K.field('Медали', colPk)],
        async () => { const n = colName.value.trim(); if (!n || !colPicked.size) throw new Error('Введите название и выберите медали.'); for (const k of colPicked) await api('/api/admin/medals/event', {key: k, event: n}); K.ok('Коллекция создана'); paint(); }, 'Создать коллекцию'));
      newCard.hidden = true;
      box.append(h('div', {class: 'row', style: {justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '8px'}}, h('div', null, h('strong', {text: 'Коллекции и награды'}), h('div', {class: 'muted', style: {fontSize: '13px', maxWidth: '620px'}, text: 'Игрок один раз забирает награду, когда соберёт нужный процент коллекции. Пустая награда отключает её.'})), K.btn('＋ Новая коллекция', () => { newCard.hidden = !newCard.hidden; }, 'small')), newCard);
      (d.scopes || []).forEach(sc => {
        const c = sc.cfg || {}; let rm = c.medal || '', gsel = c.gift ? 'keep' : '', gpick = null;
        const pc = h('input', {type: 'number', min: 1, max: 100, value: c.percent || 100, style: {width: '100%'}}), tk = h('input', {type: 'number', min: 0, value: c.tickets || 0, style: {width: '100%'}}), tn = h('input', {type: 'text', inputMode: 'decimal', value: c.ton || 0, style: {width: '100%'}});
        const mp = h('div', {style: {width: '44px', flex: 'none'}}), mn = h('span', {style: {flex: 1}}), gn = h('span', {style: {flex: 1}}), gbox = h('div', {class: 'stack wide'});
        const setM = () => { mp.innerHTML = rm ? window.GDMedals.svg(rm, 40, true) : ''; mn.textContent = rm ? window.GDMedals.name(rm) : 'не выбрана'; };
        const setG = txt => { gn.textContent = txt; };
        setM(); setG(c.gift ? c.gift.name : 'не выбран');
        const strip = h('div', {style: {display: 'flex', gap: '6px', flexWrap: 'wrap'}}); byScope(sc).slice(0, 24).forEach(m => strip.append(art(m.key, 34)));
        const rowOf = (label, ...kids) => h('div', {class: 'row', style: {alignItems: 'center', gap: '10px', padding: '10px 12px', borderRadius: '12px', background: 'var(--surface-2)'}}, h('strong', {text: label, style: {width: '70px', flex: 'none'}}), ...kids);
        box.append(K.card(sc.label, sc.total + ' медалей', strip,
          h('div', {class: 'form-grid', style: {marginTop: '12px'}}, K.field('Собрать, % коллекции', pc), K.field('Билеты', tk), K.field('TON', tn)),
          h('div', {class: 'stack wide', style: {gap: '8px', marginTop: '10px'}},
            rowOf('Медаль', mp, mn, K.btn('Выбрать', () => pickMedal(rm, v => { rm = v; setM(); }), 'small'), K.btn('Убрать', () => { rm = ''; setM(); }, 'small')),
            rowOf('Подарок', gn, K.btn('Выбрать (с моделью)', () => { gpick = K.giftPicker('', {label: 'Подарок', models: true}); gbox.replaceChildren(gpick.node, K.btn('Готово', () => { if (!String(gpick.value).includes('@m:')) { K.toast('Выберите модель подарка.', 'bad'); return; } gsel = gpick.value; setG(gpick.gift ? gpick.gift.name : gsel); gbox.replaceChildren(); }, 'small')); }, 'small'), K.btn('Убрать', () => { gsel = ''; setG('не выбран'); }, 'small')), gbox),
          h('div', {class: 'row', style: {gap: '8px', marginTop: '12px'}}, K.btn('Сохранить награду', async () => { await api('/api/admin/medals/collection', {scope: sc.scope, percent: pc.value, tickets: tk.value, ton: tn.value, medal: rm, gift: gsel}); K.ok('Награда сохранена'); paint(); }, 'primary'),
            sc.scope === '*' ? null : K.btn('Удалить коллекцию', async () => { if (!confirm('Убрать коллекцию «' + sc.label + '»? Медали останутся, но выйдут из неё.')) return; for (const m of byScope(sc)) await api('/api/admin/medals/event', {key: m.key, event: ''}); await api('/api/admin/medals/collection', {scope: sc.scope, percent: 100, tickets: 0, ton: 0, medal: '', gift: ''}); K.ok('Коллекция убрана'); paint(); }, 'danger'))));
      });
    }

    function give(box, d, paint) {
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

    }

    function promo(box, d, paint) {
      const cc = h('input', {type: 'text', maxLength: 32, placeholder: 'ПРОМОКОД или FREEBET-КОД', style: {width: '100%', textTransform: 'uppercase'}}), ct = h('input', {type: 'text', maxLength: 60, placeholder: 'Подпись (необязательно)', style: {width: '100%'}});
      let cpick = ''; const cprev = h('div', {style: {width: '64px'}}), cname = h('strong', {text: 'Медаль не выбрана'}); const clist = h('div', {class: 'list'});
      (d.codes || []).forEach(c => { const a = art(c.medal, 40); clist.append(h('div', {class: 'list-item'}, a, h('div', {class: 'grow'}, h('strong', {text: c.code}), h('small', {text: window.GDMedals.name(c.medal) + (c.title ? ' · ' + c.title : '')})), K.btn('Убрать', async () => { await api('/api/admin/medals/codes', {code: c.code, remove: true}); K.ok('Привязка убрана'); paint(); }, 'danger'))); });
      box.append(K.card('Медаль за промокод или фрибет', 'Когда игрок активирует этот код, он получит медаль: уведомление и окно «Получить». Работает для обычных промокодов и фрибетов.', K.form([K.field('Код', cc), K.field('Подпись', ct),
        h('div', {class: 'row', style: {alignItems: 'center', gap: '12px'}}, cprev, h('div', {class: 'grow'}, cname), K.btn('Выбрать медаль', () => pickMedal(cpick, v => { cpick = v; cprev.innerHTML = window.GDMedals.svg(v, 60); cname.textContent = window.GDMedals.name(v); })))],
        async () => { if (!cpick) throw new Error('Выберите медаль.'); await api('/api/admin/medals/codes', {code: cc.value, medal: cpick, title: ct.value}); K.ok('Привязано'); paint(); }, 'Привязать медаль'), clist));

    }

    function texts(box, d) {
      const n1 = h('textarea', {rows: 3, style: {width: '100%'}}), n2 = h('textarea', {rows: 2, style: {width: '100%'}}); n1.value = d.notify; n2.value = d.season;
      box.append(K.card('Тексты уведомлений', null, K.form([K.field('Уведомление при ручной выдаче', n1, 'Подстановки: {medal} — название, {title} — подпись. Разрешены теги <b> и <i>.'), K.field('Строка про медаль в итогах сезона', n2, 'Подстановка: {medal}.')],
        async () => { await api('/api/admin/medals/texts', {notify: n1.value, season: n2.value}); K.ok('Тексты сохранены'); }, 'Сохранить тексты')));
    }
  }});
})();
