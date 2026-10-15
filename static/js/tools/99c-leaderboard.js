/* Лидерборд: недельный топ по обороту, награды за места, срок и досрочное завершение. */
(() => {
  const K = window.AdminKit, {h, api} = K;
  const SRC = [['catalog', 'Обычный подарок'], ['model', 'Модель подарка'], ['fragment', 'Telegram / Fragment (ссылка на NFT)']];
  const utc = iso => { if (!iso) return ''; const d = new Date(String(iso).replace(' ', 'T') + (/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? '' : 'Z')); return isNaN(d) ? '' : d.toISOString().slice(0, 16); };
  const when = iso => { const v = utc(iso); return v ? v.replace('T', ' ') + ' UTC' : '—'; };
  const reasons = {ended: 'по сроку', early: 'досрочно, награды выданы', reset: 'сброс без наград'};

  K.register({id: 'leaderboardAdminPage', title: 'Лидерборд', async render(root) {
    const box = h('div', {class: 'stack'}); let places = 3;

    function prizeCard(g, remove) {
      return h('div', {class: 'list-item'}, h('img', {class: 'thumb', src: g.image_url || '/static/img/gift.png', alt: '', referrerPolicy: 'no-referrer', style: {width: '44px', height: '44px', objectFit: g.source_type === 'fragment' ? 'cover' : 'contain', borderRadius: '10px'}, onerror(e) { e.target.onerror = null; e.target.src = '/static/img/gift.png'; }}),
        h('div', {class: 'grow'}, h('strong', {text: g.name}), h('small', {text: (g.source_type === 'fragment' ? 'Fragment' : g.model ? 'Модель' : 'Подарок') + ' · ' + K.money(g.price_ton) + ' TON'})),
        remove ? K.btn('Убрать', remove, 'danger') : null);
    }

    function placeEditor(place, list, d) {
      const save = async prizes => { await api('/api/admin/leaderboard/rewards/' + place, {prizes}); K.ok('Награды места ' + place + ' сохранены'); await load(); };
      const wrap = h('div', {class: 'list-item', style: {flexDirection: 'column', alignItems: 'stretch', gap: '10px'}});
      const kind = K.select(SRC, 'catalog'), body = h('div', {class: 'stack'});
      let picker = null, url = null, price = null, preview = h('div');
      const paint = () => {
        body.replaceChildren(); picker = url = price = null; preview.replaceChildren();
        if (kind.value === 'fragment') {
          url = K.input({type: 'url', placeholder: 'https://t.me/nft/PlushPepe-123'}); price = K.number('', {min: 0, step: '0.01', placeholder: 'Цена, TON (если не определилась)'});
          body.append(K.field('Ссылка на NFT', url), K.field('Цена, TON', price), K.btn('Показать превью', async () => {
            const r = await api('/api/admin/giveaways/fragment-preview', {url: url.value, price_ton: price.value});
            preview.replaceChildren(prizeCard({...r.gift, source_type: 'fragment', model: r.gift.model}));
          }), preview);
        } else { picker = K.giftPicker('', {label: 'Подарок', models: kind.value === 'model'}); body.append(picker.node); }
      };
      kind.onchange = paint; paint();
      const add = K.btn('Добавить награду', async () => {
        const item = {_new: true, source_type: kind.value === 'fragment' ? 'fragment' : 'catalog'};
        if (kind.value === 'fragment') { item.fragment_url = url.value; item.price_ton = price.value; }
        else { item.gift_id = picker.value; if (kind.value === 'model' && !String(item.gift_id).includes('@m:')) throw new Error('Выберите модель подарка.'); }
        await save([...list, item]);
      }, 'primary');
      wrap.append(h('strong', {text: place + ' место'}), list.length ? h('div', {class: 'list'}, list.map((g, i) => prizeCard(g, () => save(list.filter((_, j) => j !== i))))) : h('small', {class: 'muted', text: 'Награда не задана.' + (place <= 3 ? ' Пока награды нет — панель этого места на странице скрыта.' : '')}), K.field('Тип награды', kind), body, h('div', {class: 'row'}, add));
      return wrap;
    }

    function paint(d) {
      box.replaceChildren();
      const p = d.period, cfg = d.config;
      const end = K.input({type: 'datetime-local', value: utc(p && p.ends_at)});
      const state = p ? [K.badge('LIVE', 'good'), h('div', {class: 'muted', text: `Начался: ${when(p.starts_at)} · игроков в зачёте: ${p.participants} · оборот: ${K.money(p.turnover)} TON`}),
        K.field('Окончание периода (UTC)', end), h('div', {class: 'row'},
          K.btn('Сохранить срок', async () => { if (!end.value) throw new Error('Укажите дату окончания.'); await api('/api/admin/leaderboard/period', {ends_at: end.value + ':00Z'}); K.ok('Срок обновлён'); await load(); }, 'primary'),
          K.btn('+24 часа', async () => { await api('/api/admin/leaderboard/period', {extend_hours: 24}); K.ok('Продлено на сутки'); await load(); }),
          K.btn('+7 дней', async () => { await api('/api/admin/leaderboard/period', {extend_hours: 168}); K.ok('Продлено на неделю'); await load(); })),
        h('div', {class: 'row'},
          K.btn('Завершить досрочно и выдать награды', async () => { if (!await K.confirm('Завершить сезон сейчас и выдать награды текущим лидерам?', 'Завершить')) return; await api('/api/admin/leaderboard/finish', {award: true}); K.ok('Период завершён'); await load(); }, 'primary'),
          K.btn('Сбросить без наград', async () => { if (!await K.confirm('Обнулить таблицу без выдачи наград?', 'Сбросить', true)) return; await api('/api/admin/leaderboard/finish', {award: false}); K.ok('Таблица сброшена'); await load(); }, 'danger'))]
        : (() => { const sh = K.number(cfg.period_hours, {min: 1, max: 2160}); return [K.badge('Сезон не начат'), h('div', {class: 'muted', text: 'Пока сезон не запущен, игроки видят пустую таблицу на 100 мест без призов и оборота. Оборот начнёт считаться с момента запуска.'}),
          K.field('Длительность сезона, часов', sh, '168 часов = неделя'),
          h('div', {class: 'row'}, K.btn('Запустить сезон', async () => { await api('/api/admin/leaderboard/start', {hours: sh.value}); K.ok('Сезон запущен'); await load(); }, 'primary'))]; })();
      box.append(K.card('Текущий период', null, ...state));

      const en = K.check('Показывать игрокам кнопку «Лидеры»', cfg.enabled), rep = K.check('После окончания сезона сразу запускать следующий (по умолчанию — нет)', cfg.auto_repeat), hrs = K.number(cfg.period_hours, {min: 1, max: 2160});
      box.append(K.card('Настройки', null, K.form([en, rep, K.field('Длительность нового периода, часов', hrs, '168 часов = неделя. Действует для следующих периодов; у текущего срок меняется выше.')], async () => {
        await api('/api/admin/leaderboard/config', {enabled: en.querySelector('input').checked, auto_repeat: rep.querySelector('input').checked, period_hours: hrs.value}); K.ok('Сохранено'); await load();
      })));

      places = Math.max(places, ...Object.keys(d.rewards).map(Number), 3);
      const editors = [...Array(places).keys()].map(i => placeEditor(i + 1, d.rewards[String(i + 1)] || [], d));
      box.append(K.card('Награды за места', 'За одно место можно задать несколько наград. Места 1–3 получают отдельные панели, с 4-го награда показывается рядом с ником. Выдаются при завершении периода в инвентарь победителей.', h('div', {class: 'list'}, editors),
        places < d.max_places ? h('div', {class: 'row'}, K.btn('Добавить ещё место', async () => { places++; paint(d); })) : null));

      box.append(K.card('Топ сейчас', null, h('div', {class: 'list'}, d.top.length ? d.top.map(r => h('div', {class: 'list-item'}, K.badge('#' + r.place), h('div', {class: 'grow'}, h('strong', {text: r.name || 'Игрок'}), h('small', {text: (r.username ? '@' + r.username + ' · ' : '') + 'ID ' + r.user_id})), h('strong', {text: K.money(r.turnover) + ' TON'}))) : [K.empty('Пока никто не играл в этом периоде.')])));
      box.append(K.card('Прошлые периоды', null, h('div', {class: 'list'}, d.history.length ? d.history.map(x => h('div', {class: 'list-item'}, h('strong', {text: '#' + x.id}), h('small', {text: `${when(x.starts_at)} → ${when(x.finished_at || x.ends_at)} · ${reasons[x.reason] || x.reason || ''}`}))) : [K.empty('Пока нет завершённых периодов.')])));
      box.append(K.card('Выданные награды', null, h('div', {class: 'list'}, d.awards.length ? d.awards.map(a => h('div', {class: 'list-item'}, K.badge(a.place + ' место'), h('div', {class: 'grow'}, h('strong', {text: a.gift_name}), h('small', {text: `ID ${a.user_id} · период #${a.period_id} · ${K.money(a.price_ton)} TON`})))) : [K.empty('Награды ещё не выдавались.')])));
    }
    async function load() { paint(await api('/api/admin/leaderboard', undefined, 'GET')); }
    root.append(K.head('Лидерборд', 'Недельный топ по обороту: период, награды за места и досрочное завершение.', 'Игроки'), box);
    await load();
  }});
})();
