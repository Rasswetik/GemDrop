/* Giveaways. */
(() => {
  const K = window.AdminKit, {h, api} = K;
  const label = p => { const n = p.name || 'Подарок'; return p.fragment_number && !n.includes('#' + p.fragment_number) ? `${n} #${p.fragment_number}` : n; };
  const slots = x => (x.prizes || []).flatMap(p => Array(Math.max(1, Number(p.quantity || 1))).fill(p));
  const left = (iso, status = 'active') => { if (status !== 'active') return 'Завершён'; const ms = new Date(iso).getTime() - Date.now(); if (!(ms > 0)) return 'завершается'; let s = Math.floor(ms / 1000); const d = Math.floor(s / 86400); s %= 86400; const hh = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return d ? `${d}д ${hh}ч` : hh ? `${hh}ч ${String(m).padStart(2, '0')}м` : `${m}м`; };
  const thumb = p => h('img', {class: 'thumb', src: p.image_url || '/static/img/gift.svg', alt: '', loading: 'lazy', style: {width: '44px', height: '44px'}, onerror(e) { e.target.onerror = null; e.target.src = '/static/img/gift.svg'; }});

  K.register({id: 'giveawaysAdminPage', title: 'Розыгрыши', async render(root) {
    const gifts = await K.catalog();
    let prizes = [], source = 'catalog';
    const title = K.input({maxLength: 120, placeholder: 'Название розыгрыша'}), descr = K.textarea('', {rows: 2, maxLength: 500}), hours = K.number(24, {min: 0, step: 1}), minutes = K.number(0, {min: 0, step: 1}), winners = K.number(0, {readOnly: true}), repeat = K.check('Один игрок может выиграть несколько призов', false);
    const picker = K.giftPicker('', {label: 'Подарок из каталога'}), fragUrl = K.input({type: 'url', placeholder: 'https://t.me/nft/…'}), fragAnim = K.input({placeholder: 'Ссылка на видео'}), fragPrice = K.number('', {min: 0, step: '0.01', placeholder: 'Определится автоматически'});
    const prizeBox = h('div', {class: 'list'}), prizeCount = h('span', {class: 'muted'}), existing = h('div', {class: 'list'}), existingCount = h('span', {class: 'muted'});
    const total = () => prizes.reduce((s, p) => s + Math.max(1, Number(p.quantity || 1)), 0);
    const sync = () => { winners.value = total(); prizeCount.textContent = total() + ' мест'; };
    function paintPrizes() {
      sync(); prizeBox.replaceChildren();
      if (!prizes.length) { prizeBox.append(K.empty('Добавьте хотя бы один подарок')); return; }
      prizes.forEach((p, i) => {
        const qty = K.number(Math.max(1, Number(p.quantity || 1)), {min: 1, max: 100, step: 1, 'aria-label': 'Количество', style: {width: '80px'}, onchange: e => { p.quantity = Math.max(1, Math.min(100, Math.floor(Number(e.target.value) || 1))); e.target.value = p.quantity; sync(); }});
        const det = [p.source_type === 'fragment' ? 'Fragment #' + (p.fragment_number || '') : 'Portal', p.price_ton ? K.money(p.price_ton) + ' TON' : '', p.model, p.backdrop].filter(Boolean).join(' · ');
        const move = d => () => { [prizes[i], prizes[i + d]] = [prizes[i + d], prizes[i]]; paintPrizes(); };
        prizeBox.append(h('div', {class: 'list-item', style: {flexWrap: 'wrap'}}, thumb(p), h('div', {class: 'grow'}, h('strong', {text: label(p)}), h('small', {text: det}),
          p.source_type === 'fragment' ? K.field('Своя цена, TON', K.input({inputMode: 'decimal', placeholder: K.money(p.price_ton), value: p.manual_price ?? '', oninput: e => p.manual_price = e.target.value.trim() || null})) : null),
          h('div', {class: 'row'}, qty, h('button', {type: 'button', class: 'small', text: '↑', disabled: i === 0, 'aria-label': 'Выше', onclick: move(-1)}), h('button', {type: 'button', class: 'small', text: '↓', disabled: i === prizes.length - 1, 'aria-label': 'Ниже', onclick: move(1)}),
            h('button', {type: 'button', class: 'small danger', text: 'Убрать', onclick: () => { prizes.splice(i, 1); paintPrizes(); }}))));
      });
    }
    const srcSeg = h('div', {class: 'segmented'});
    const catBox = h('div', {class: 'stack'}, K.field('Подарок из каталога', picker.node), K.btn('Добавить подарок', async () => {
      const g = gifts.find(x => String(x.id) === String(picker.value)); if (!g) throw new Error('Выберите подарок');
      prizes.push({source_type: 'catalog', gift_id: String(g.id), name: g.name, image_url: g.image_url, price_ton: g.price_ton, quantity: 1}); paintPrizes();
    }));
    const preview = h('div');
    const fragBox = h('div', {class: 'stack'}, K.field('Ссылка на конкретный NFT', fragUrl), K.field('Видео · необязательно', fragAnim), K.field('Своя цена, TON · необязательно', fragPrice), K.btn('Проверить и добавить', async () => {
      const url = fragUrl.value.trim(); if (!url) throw new Error('Вставьте ссылку Fragment');
      const g = (await api('/api/admin/giveaways/fragment-preview', {url, price_ton: fragPrice.value, animation_url: fragAnim.value.trim()})).gift;
      preview.replaceChildren(K.notice(`${g.name || 'Подарок'} · #${g.fragment_number || ''}${g.model ? ' · ' + g.model : ''}${g.backdrop ? ' · ' + g.backdrop : ''}${g.price_ton ? ' · ' + K.money(g.price_ton) + ' TON' : ''}`, 'good'));
      prizes.push({source_type: 'fragment', fragment_url: g.fragment_url, name: g.name, image_url: g.image_url, fragment_number: g.fragment_number, gift_id: g.gift_id, price_ton: g.price_ton, manual_price: fragPrice.value.trim() || null, model: g.model, backdrop: g.backdrop, animation_url: g.animation_url, quantity: 1});
      paintPrizes(); fragUrl.value = ''; fragAnim.value = ''; fragPrice.value = ''; K.ok('Точный подарок Fragment добавлен');
    }), preview);
    const setSource = s => { source = s; catBox.hidden = s !== 'catalog'; fragBox.hidden = s !== 'fragment'; srcSeg.replaceChildren(...[['catalog', 'Из каталога'], ['fragment', 'Ссылка Fragment']].map(([v, t]) => h('button', {type: 'button', text: t, 'aria-pressed': String(v === s), onclick: () => setSource(v)}))); };
    setSource('catalog'); paintPrizes();

    const form = K.form([K.field('Название розыгрыша', title), K.field('Описание · необязательно', descr), h('div', {class: 'form-grid'}, K.field('Часов', hours), K.field('Минут', minutes), K.field('Призовых мест', winners)), repeat,
      h('div', {class: 'group-title', text: 'Призы'}), srcSeg, catBox, fragBox, prizeCount, prizeBox], async () => {
      const n = total(); if (!n) throw new Error('Добавьте хотя бы один подарок'); if (n > 100) throw new Error('Максимум 100 призовых мест');
      const dur = Math.max(0, Math.floor(Number(hours.value || 0))) * 60 + Math.max(0, Math.floor(Number(minutes.value || 0))); if (dur < 1) throw new Error('Укажите длительность хотя бы 1 минуту');
      await api('/api/admin/giveaways', {title: title.value, description: descr.value, duration_minutes: dur, allow_repeat_winners: repeat.querySelector('input').checked, prizes: prizes.map(p => p.source_type === 'fragment' ? {source_type: 'fragment', fragment_url: p.fragment_url, animation_url: p.animation_url, price_ton: p.manual_price, quantity: p.quantity} : {source_type: 'catalog', gift_id: p.gift_id, quantity: p.quantity})});
      title.value = ''; descr.value = ''; prizes = []; paintPrizes(); K.ok('Розыгрыш создан'); await reload();
    }, 'Создать розыгрыш');

    function edit(card, x) {
      if (card.querySelector('.giveaway-edit-form')) return;
      const t = K.input({value: x.title, maxLength: 120}), d = K.textarea(x.description || '', {rows: 2, maxLength: 500}), end = K.input({type: 'datetime-local', value: K.localInput(new Date(x.ends_at).getTime())});
      const f = K.form([K.field('Название', t), K.field('Описание', d), K.field('Окончание', end)], async () => { await api(`/api/admin/giveaways/${x.id}`, {title: t.value, description: d.value, ends_at: new Date(end.value).toISOString()}, 'PUT'); K.ok('Розыгрыш сохранён'); await reload(); });
      f.classList.add('giveaway-edit-form'); card.append(f);
    }
    function paintExisting(items) {
      existingCount.textContent = items.length; existing.replaceChildren();
      if (!items.length) { existing.append(K.empty('Розыгрышей пока нет')); return; }
      for (const x of items) {
        const card = h('div', {class: 'list-item', style: {flexDirection: 'column', alignItems: 'stretch'}});
        card.append(h('div', {class: 'row'}, h('div', {class: 'grow'}, h('strong', {text: x.title}), h('small', {text: `${x.participants} участников · ${K.num(x.pool || 0)} билетов · ${x.prize_count || x.winner_count} мест`})), K.badge(x.status === 'active' ? left(x.ends_at) : 'Завершён', x.status === 'active' ? 'good' : '')),
          h('div', {class: 'row', style: {flexWrap: 'wrap'}}, slots(x).slice(0, 8).map(p => { const i = thumb(p); i.title = label(p); return i; })));
        const acts = h('div', {class: 'row', style: {flexWrap: 'wrap'}});
        if (x.status === 'active') {
          acts.append(h('button', {type: 'button', class: 'small', text: 'Изменить', onclick: () => edit(card, x)}),
            K.btn('Обновить подарки', async () => { await api(`/api/admin/giveaways/${x.id}/refresh-prizes`, {}); K.ok('Подарки обновлены'); await reload(); }, 'small'),
            K.btn('Завершить досрочно', async () => { if (!await K.confirm('Завершить розыгрыш сейчас? Победители будут выбраны сразу, все призовые места разыграются.', 'Завершить', true)) return; await api(`/api/admin/giveaways/${x.id}/finish`, {}); K.ok('Розыгрыш завершён, победители выбраны'); await reload(); }, 'small'),
            K.btn('Отменить и вернуть билеты', async () => { if (!await K.confirm('Отменить розыгрыш и вернуть участникам все билеты?', 'Отменить', true)) return; await K.del('/api/admin/giveaways/' + x.id); await reload(); }, 'small danger'));
          card.append(acts);
          for (const p of x.prizes || []) {
            if (p.source_type !== 'fragment') continue;
            const inp = K.input({inputMode: 'decimal', value: K.money(p.price_ton)});
            card.append(K.form([K.field(p.name + ' · цена, TON', inp)], async () => { await api(`/api/admin/giveaways/${x.id}/prizes/${p.id}/price`, {price_ton: inp.value}); K.ok('Цена сохранена'); await reload(); }, 'Сохранить цену'));
          }
        } else { acts.append(K.btn('Удалить старый розыгрыш', async () => { await api(`/api/admin/giveaways/${x.id}/archive`, {}); await reload(); }, 'small')); card.append(acts); }
        existing.append(card);
      }
    }
    async function reload() { paintExisting((await api('/api/admin/giveaways')).items || []); }
    await reload();
    root.append(K.head('Розыгрыши', 'Создавайте розыгрыши подарков за билеты; победители выбираются автоматически по окончании.', 'Награды'), K.card('Новый розыгрыш', null, form),
      K.card('Созданные розыгрыши', null, existingCount, K.btn('Убрать завершённые из списка', async () => { await api('/api/admin/giveaways/clear-completed', {}); await reload(); }, 'small'), existing));
  }});
})();
