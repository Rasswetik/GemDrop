/* Premium emoji bindings: gifts and pumpkin. */
(() => {
  const K = window.AdminKit, {h, api} = K;
  K.register({id: 'giftEmojiAdminPage', title: 'Эмодзи подарков', async render(root) {
    const pk = await api('/api/admin/pumpkin-emoji'), pkId = K.input({value: pk.emoji_id || '', inputMode: 'numeric', placeholder: 'Telegram custom emoji ID тыквы'}), pkState = h('small', {class: 'muted'}), pkThumb = h('div', {style: {width: '48px', height: '48px', display: 'grid', placeItems: 'center', fontSize: '26px'}});
    const paintPk = d => { pkState.textContent = d.emoji_id ? `Сейчас: ${d.emoji || ''} · ${d.emoji_id}` : 'ID не задан — используется обычная 🎃'; pkThumb.replaceChildren(); if (d.emoji_id) pkThumb.append(h('img', {alt: '', src: K.emojiThumb(encodeURIComponent(d.emoji_id)), style: {width: '100%', height: '100%', objectFit: 'contain'}, onerror() { pkThumb.textContent = d.emoji || '🎃'; }})); else pkThumb.textContent = '🎃'; };
    paintPk(pk);
    const pkForm = K.form([h('div', {class: 'row'}, pkThumb, h('div', {class: 'grow'}, K.field('ID премиум-эмодзи тыквы', pkId)))], async () => { await api('/api/admin/pumpkin-emoji', {emoji_id: pkId.value.trim()}); K.ok(pkId.value.trim() ? 'Эмодзи тыквы сохранён' : 'Эмодзи тыквы очищен'); paintPk(await api('/api/admin/pumpkin-emoji')); });

    const search = K.input({type: 'search', placeholder: 'Поиск подарка', 'aria-label': 'Поиск'}), summary = h('div', {class: 'row'}), list = h('div', {class: 'list'});
    let items = [], onlyEmpty = false;
    const only = K.check('только без emoji', false, {onchange: e => { onlyEmpty = e.target.checked; paint(); }});
    function paint() {
      const bound = items.filter(x => x.emoji_id).length; summary.replaceChildren(h('span', {html: ''}, `Привязано: ${bound} из ${items.length}`), only);
      list.replaceChildren(); const shown = items.filter(x => !onlyEmpty || !x.emoji_id);
      if (!shown.length) list.append(K.empty('Подарки не найдены'));
      for (const x of shown) {
        const input = K.input({inputMode: 'numeric', placeholder: 'Telegram custom emoji ID', value: x.emoji_id || '', 'aria-label': 'Emoji ID для ' + (x.name || 'подарка')}), sm = h('small');
        const upd = () => sm.textContent = (x.emoji ? 'Сейчас: ' + x.emoji + ' · ' : '') + (x.emoji_id || 'ID не задан'); upd();
        list.append(h('div', {class: 'list-item', style: {flexWrap: 'wrap'}}, K.gift(x, 44), h('div', {class: 'grow'}, h('strong', {text: x.name || 'Подарок'}), sm), input,
          K.btn('Сохранить', async () => { const r = await api('/api/admin/gift-emojis', {gift_id: x.gift_id, emoji_id: input.value.trim()}); x.emoji_id = r.item.emoji_id || ''; x.emoji = r.item.emoji || ''; upd(); paint0(); K.ok(x.emoji_id ? 'Эмодзи привязан' : 'Привязка очищена'); }, 'small')));
      }
    }
    const paint0 = () => { const bound = items.filter(x => x.emoji_id).length; summary.firstChild.textContent = `Привязано: ${bound} из ${items.length}`; };
    const load = async q => { items = (await api('/api/admin/gift-emojis' + (q ? '?q=' + encodeURIComponent(q) : ''))).items || []; paint(); };
    search.oninput = K.debounce(() => load(search.value.trim()).catch(K.fail), 250);
    await load('');
    root.append(K.head('Эмодзи', 'Premium emoji для подарков и тыквы Хэллоуина — используются во Freebet и сообщениях бота.', 'Telegram'),
      K.card('Эмодзи тыквы (Хэллоуин)', 'Premium emoji тыквы для сообщений бота: «Вам получено N тыкв» при активации фрибета в тыквах и других уведомлений ивента. Пока ID не задан, используется обычная 🎃.', pkForm, pkState),
      K.card('Эмодзи подарков', 'Для каждого подарка указывается его собственный custom emoji ID. Привязка используется во Freebet и сообщениях о победах в розыгрышах.', search, summary, list));
  }});
})();
