/* Лидерборд: недельный топ по обороту, награды за места, срок и досрочное завершение. */
(() => {
  const K = window.AdminKit, {h, api} = K;
  const SRC = [['catalog', 'Обычный подарок'], ['model', 'Модель подарка'], ['fragment', 'Telegram / Fragment (ссылка на NFT)'], ['ton', 'TON (сразу на баланс)']];
  const utc = iso => { if (!iso) return ''; const d = new Date(String(iso).replace(' ', 'T') + (/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? '' : 'Z')); return isNaN(d) ? '' : d.toISOString().slice(0, 16); };
  const when = iso => { const v = utc(iso); return v ? v.replace('T', ' ') + ' UTC' : '—'; };
  const reasons = {ended: 'по сроку', early: 'досрочно, награды выданы', reset: 'сброс без наград'};

  K.register({id: 'leaderboardAdminPage', title: 'Лидерборд', async render(root) {
    const box = h('div', {class: 'stack'}); let places = 5;

    function prizeCard(g, remove) {
      return h('div', {class: 'list-item'}, h('img', {class: 'thumb', src: g.image_url || '/static/img/gift.png', alt: '', referrerPolicy: 'no-referrer', style: {width: '44px', height: '44px', objectFit: g.source_type === 'fragment' ? 'cover' : 'contain', borderRadius: '10px'}, onerror(e) { e.target.onerror = null; e.target.src = '/static/img/gift.png'; }}),
        h('div', {class: 'grow'}, h('strong', {text: g.name}), h('small', {text: (g.source_type === 'ton' ? 'На баланс' : g.source_type === 'fragment' ? 'Fragment' : g.model ? 'Модель' : 'Подарок') + ' · ' + K.money(g.price_ton) + ' TON'})),
        remove ? K.btn('Убрать', remove, 'danger') : null);
    }

    function medalPicker(place, d) {
      const cur = (d.medals || {})[String(place)] || '';
      const holder = h('div', {class: 'row', style: {alignItems: 'center', gap: '12px'}});
      const paintHolder = key => {
        holder.replaceChildren();
        const prev = h('div', {style: {width: '56px'}}); prev.innerHTML = key ? window.GDMedals.svg(key, 52) : '';
        holder.append(prev, h('div', {class: 'grow'}, h('strong', {text: key ? window.GDMedals.name(key) : 'Медаль не выбрана'}), h('small', {class: 'muted', text: 'Выдаётся навсегда вместе с призом при завершении сезона'})),
          K.btn(key ? 'Сменить медаль' : 'Выбрать медаль', () => chooseMedal(place, key, async v => { await api('/api/admin/leaderboard/medals', {place, medal: v}); K.ok(v ? 'Медаль сохранена' : 'Медаль убрана'); await load(); })),
          key ? K.btn('Убрать', async () => { await api('/api/admin/leaderboard/medals', {place, medal: ''}); K.ok('Медаль убрана'); await load(); }, 'small') : null);
      };
      paintHolder(cur); return holder;
    }
    function chooseMedal(place, current, pick) {
      const back = h('div', {style: {position: 'fixed', inset: 0, zIndex: 9999, background: 'rgba(6,4,14,.72)', display: 'grid', placeItems: 'center', padding: '16px'}});
      const close = () => back.remove();
      const grid = h('div', {style: {display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(120px,1fr))', gap: '12px'}});
      [...new Set(window.GDMedals.LIST.map(m => m.set))].forEach(set => {
        grid.append(h('div', {style: {gridColumn: '1/-1', fontWeight: 800, marginTop: '6px'}, text: set}));
        window.GDMedals.LIST.filter(m => m.set === set).forEach(m => {
          const tile = h('button', {type: 'button', style: {display: 'grid', justifyItems: 'center', gap: '6px', padding: '12px 8px', borderRadius: '16px', border: m.key === current ? '2px solid #a066ff' : '1px solid rgba(255,255,255,.12)', background: '#1b1e27', color: '#fff', cursor: 'pointer'}, onclick: async () => { try { await pick(m.key); close(); } catch (e) { K.err ? K.err(e) : alert(e.message); } }});
          const art = h('div'); art.innerHTML = window.GDMedals.svg(m.key, 84); tile.append(art, h('b', {text: m.name, style: {fontSize: '12px', textAlign: 'center'}}), h('small', {text: m.place ? 'как за ' + m.place + ' место' : 'особая', style: {color: '#8d99a8'}}));
          grid.append(tile);
        });
      });
      back.append(h('div', {style: {width: 'min(100%,560px)', maxHeight: '86vh', overflow: 'auto', padding: '18px', borderRadius: '22px', background: '#14161d', border: '1px solid rgba(255,255,255,.1)', color: '#fff'}},
        h('div', {style: {display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px'}}, h('strong', {text: 'Медаль за ' + place + ' место', style: {fontSize: '18px'}}), K.btn('Закрыть', close, 'small')), grid));
      back.addEventListener('click', e => { if (e.target === back) close(); });
      document.body.append(back);
    }

    function placeEditor(place, list, d) {
      const save = async prizes => { await api('/api/admin/leaderboard/rewards/' + place, {prizes}); K.ok('Награды места ' + place + ' сохранены'); await load(); };
      const wrap = h('div', {class: 'list-item', style: {flexDirection: 'column', alignItems: 'stretch', gap: '10px'}});
      const kind = K.select(SRC, 'catalog'), body = h('div', {class: 'stack'});
      let picker = null, url = null, price = null, tonAmt = null, preview = h('div');
      const paint = () => {
        body.replaceChildren(); picker = url = price = tonAmt = null; preview.replaceChildren();
        if (kind.value === 'ton') { tonAmt = K.number('', {min: 0.01, step: '0.01', placeholder: 'Например: 50'}); body.append(K.field('Сколько TON', tonAmt, 'Придёт на основной баланс победителя при завершении сезона.')); }
        else if (kind.value === 'fragment') {
          url = K.input({type: 'url', placeholder: 'https://t.me/nft/PlushPepe-123'}); price = K.number('', {min: 0, step: '0.01', placeholder: 'Цена, TON (если не определилась)'});
          body.append(K.field('Ссылка на NFT', url), K.field('Цена, TON', price), K.btn('Показать превью', async () => {
            const r = await api('/api/admin/giveaways/fragment-preview', {url: url.value, price_ton: price.value});
            preview.replaceChildren(prizeCard({...r.gift, source_type: 'fragment', model: r.gift.model}));
          }), preview);
        } else { picker = K.giftPicker('', {label: 'Подарок', models: kind.value === 'model'}); body.append(picker.node); }
      };
      kind.onchange = paint; paint();
      const add = K.btn('Добавить награду', async () => {
        const item = {_new: true, source_type: kind.value === 'fragment' ? 'fragment' : kind.value === 'ton' ? 'ton' : 'catalog'};
        if (kind.value === 'ton') { if (!tonAmt.value) throw new Error('Укажите сумму TON.'); item.price_ton = tonAmt.value; }
        else if (kind.value === 'fragment') { item.fragment_url = url.value; item.price_ton = price.value; }
        else { item.gift_id = picker.value; if (kind.value === 'model' && !String(item.gift_id).includes('@m:')) throw new Error('Выберите модель подарка.'); }
        await save([...list, item]);
      }, 'primary');
      wrap.append(h('strong', {text: place + ' место'}), medalPicker(place, d), list.length ? h('div', {class: 'list'}, list.map((g, i) => prizeCard(g, () => save(list.filter((_, j) => j !== i))))) : h('small', {class: 'muted', text: 'Награда не задана.' + (place <= 3 ? ' Пока награды нет — панель этого места на странице скрыта.' : '')}), K.field('Тип награды', kind), body, h('div', {class: 'row'}, add));
      return wrap;
    }

    function paint(d) {
      box.replaceChildren();
      const p = d.period, cfg = d.config;
      const end = K.input({type: 'datetime-local', value: utc(p && p.ends_at)}), ttl = K.input({type: 'text', maxlength: 40, value: (p && p.title) || '', placeholder: 'Например: Сезон 3 · Осень'});
      const state = p ? [K.badge('LIVE', 'good'), h('div', {class: 'muted', text: `Начался: ${when(p.starts_at)} · игроков в зачёте: ${p.participants} · оборот: ${K.money(p.turnover)} TON`}),
        K.field('Название сезона', ttl, 'Показывается игрокам и в уведомлении о призовом месте. Пусто — «Сезон N».'), K.btn('Сохранить название', async () => { await api('/api/admin/leaderboard/period', {title: ttl.value}); K.ok('Название сохранено'); await load(); }), K.field('Окончание периода (UTC)', end), h('div', {class: 'row'},
          K.btn('Сохранить срок', async () => { if (!end.value) throw new Error('Укажите дату окончания.'); await api('/api/admin/leaderboard/period', {ends_at: end.value + ':00Z'}); K.ok('Срок обновлён'); await load(); }, 'primary'),
          K.btn('+24 часа', async () => { await api('/api/admin/leaderboard/period', {extend_hours: 24}); K.ok('Продлено на сутки'); await load(); }),
          K.btn('+7 дней', async () => { await api('/api/admin/leaderboard/period', {extend_hours: 168}); K.ok('Продлено на неделю'); await load(); })),
        h('div', {class: 'row'},
          K.btn('Завершить досрочно и выдать награды', async () => { if (!await K.confirm('Завершить сезон сейчас и выдать награды текущим лидерам?', 'Завершить')) return; await api('/api/admin/leaderboard/finish', {award: true}); K.ok('Период завершён'); await load(); }, 'primary'),
          K.btn('Обнулить таблицу (сезон продолжается)', async () => { if (!await K.confirm('Обнулить оборот всех игроков в текущем сезоне? Сезон и награды останутся.', 'Обнулить', true)) return; const r = await api('/api/admin/leaderboard/reset-standings', {}); K.ok('Таблица обнулена: ' + r.removed + ' игроков'); await load(); }, 'danger'),
          K.btn('Сбросить без наград', async () => { if (!await K.confirm('Обнулить таблицу без выдачи наград?', 'Сбросить', true)) return; await api('/api/admin/leaderboard/finish', {award: false}); K.ok('Таблица сброшена'); await load(); }, 'danger'))]
        : (() => { const sh = K.number(cfg.period_hours, {min: 1, max: 2160}), nt = K.input({type: 'text', maxlength: 40, placeholder: 'Например: Сезон 3 · Осень'}); return [K.badge('Сезон не начат'), h('div', {class: 'muted', text: 'Пока сезон не запущен, игроки видят пустую таблицу на 100 мест без призов и оборота. Оборот начнёт считаться с момента запуска.'}),
          K.field('Название сезона (необязательно)', nt, 'Пусто — «Сезон N» по порядку.'), K.field('Длительность сезона, часов', sh, '168 часов = неделя'),
          h('div', {class: 'row'}, K.btn('Запустить сезон', async () => { await api('/api/admin/leaderboard/start', {hours: sh.value, title: nt.value}); K.ok('Сезон запущен'); await load(); }, 'primary'))]; })();
      box.append(K.card('Текущий период', null, ...state));

      const en = K.check('Показывать игрокам кнопку «Лидеры»', cfg.enabled), rep = K.check('После окончания сезона сразу запускать следующий (по умолчанию — нет)', cfg.auto_repeat), hrs = K.number(cfg.period_hours, {min: 1, max: 2160});
      box.append(K.card('Настройки', null, K.form([en, rep, K.field('Длительность нового периода, часов', hrs, '168 часов = неделя. Действует для следующих периодов; у текущего срок меняется выше.')], async () => {
        await api('/api/admin/leaderboard/config', {enabled: en.querySelector('input').checked, auto_repeat: rep.querySelector('input').checked, period_hours: hrs.value}); K.ok('Сохранено'); await load();
      })));

      places = Math.max(places, ...Object.keys(d.rewards).map(Number), ...Object.keys(d.medals || {}).map(Number), 5);
      const editors = [...Array(places).keys()].map(i => placeEditor(i + 1, d.rewards[String(i + 1)] || [], d));
      box.append(K.card('Награды за места', 'За одно место можно задать несколько наград. Места 1–3 получают отдельные панели, с 4-го награда показывается рядом с ником. Выдаются при завершении периода в инвентарь победителей.', h('div', {class: 'list'}, editors),
        places < d.max_places ? h('div', {class: 'row'}, K.btn('Добавить ещё место', async () => { places++; paint(d); })) : null));

      box.append(grantCard());
      box.append(K.card('Топ сейчас', null, h('div', {class: 'list'}, d.top.length ? d.top.map(r => h('div', {class: 'list-item'}, K.badge('#' + r.place), h('div', {class: 'grow'}, h('strong', {text: r.name || 'Игрок'}), h('small', {text: (r.username ? '@' + r.username + ' · ' : '') + 'ID ' + r.user_id})), h('strong', {text: K.money(r.turnover) + ' TON'}))) : [K.empty('Пока никто не играл в этом периоде.')])));
      box.append(K.card('Прошлые периоды', null, h('div', {class: 'list'}, d.history.length ? d.history.map(x => h('div', {class: 'list-item'}, h('strong', {text: x.title || ('#' + x.id)}), h('small', {text: `${when(x.starts_at)} → ${when(x.finished_at || x.ends_at)} · ${reasons[x.reason] || x.reason || ''}`}))) : [K.empty('Пока нет завершённых периодов.')])));
      box.append(K.card('Выданные награды', null, h('div', {class: 'list'}, d.awards.length ? d.awards.map(a => h('div', {class: 'list-item'}, K.badge(a.place + ' место'), h('div', {class: 'grow'}, h('strong', {text: a.gift_name}), h('small', {text: `ID ${a.user_id} · период #${a.period_id} · ${K.money(a.price_ton)} TON`})))) : [K.empty('Награды ещё не выдавались.')])));
    }
    function grantCard() {
      const uid = h('input', {type: 'number', placeholder: 'ID игрока', inputMode: 'numeric', style: {width: '100%'}}), ttl = h('input', {type: 'text', maxLength: 60, placeholder: 'Подпись (например: За верность проекту)', style: {width: '100%'}});
      let pick = '';
      const prev = h('div', {style: {width: '64px'}}), pname = h('strong', {text: 'Медаль не выбрана'});
      const list = h('div', {class: 'list'});
      const choose = K.btn('Выбрать медаль', () => chooseMedal(0, pick, async v => { pick = v; prev.innerHTML = window.GDMedals.svg(v, 60); pname.textContent = window.GDMedals.name(v); }));
      async function show() {
        list.replaceChildren(); const id = parseInt(uid.value, 10); if (!id) return;
        try {
          const d = await api('/api/admin/medals/user/' + id, undefined, 'GET');
          list.append(h('small', {class: 'muted', text: 'Медали игрока ' + (d.user.name || '') + (d.user.username ? ' @' + d.user.username : '') + ':'}));
          if (!d.medals.length) list.append(h('small', {class: 'muted', text: 'Пока нет медалей.'}));
          d.medals.forEach(m => { const art = h('div', {style: {width: '44px'}}); art.innerHTML = window.GDMedals.svg(m.key, 40); list.append(h('div', {class: 'list-item'}, art, h('div', {class: 'grow'}, h('strong', {text: m.name}), h('small', {text: m.title + (m.place ? ' · ' + m.place + ' место' : '')})), K.btn('Забрать', async () => { await api('/api/admin/medals/' + m.id + '/revoke', {}); K.ok('Медаль забрана'); await show(); }, 'danger'))); });
        } catch (e) { list.append(h('small', {class: 'bad', text: e.message})); }
      }
      uid.addEventListener('change', show);
      return K.card('Выдача медалей', 'Выдайте любую медаль игроку вручную. Он получит уведомление, медаль появится в «Мои медали» и в профиле.', K.form([K.field('ID игрока', uid), K.field('Подпись', ttl), h('div', {class: 'row', style: {alignItems: 'center', gap: '12px'}}, prev, h('div', {class: 'grow'}, pname), choose)], async () => {
        if (!pick) throw new Error('Выберите медаль.');
        await api('/api/admin/medals/grant', {user_id: uid.value, medal: pick, title: ttl.value}); K.ok('Медаль выдана'); await show();
      }, 'Выдать медаль'), list);
    }
    async function load() { paint(await api('/api/admin/leaderboard', undefined, 'GET')); }
    root.append(K.head('Лидерборд', 'Недельный топ по обороту: период, награды за места и досрочное завершение.', 'Игроки'), box);
    await load();
  }});
})();
