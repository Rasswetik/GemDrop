/* Notice templates (bot messages) with premium emoji. */
(() => {
  const K = window.AdminKit, {h, api} = K;
  const TG_EM = /<tg-emoji\s+emoji-id="(\d+)">([^<]*)<\/tg-emoji>|\[emoji:(\d+):([^\]]*)\]/g;
  const ALLOWED = new Set(['B', 'STRONG', 'I', 'EM', 'U', 'S', 'STRIKE', 'DEL', 'CODE', 'PRE', 'A', 'TG-SPOILER', 'BR', 'BLOCKQUOTE', 'SPAN']);
  /* Server preview is Telegram-HTML; render it through a whitelist so nothing active can run in the admin origin. */
  function safeInto(node, html) {
    const tpl = document.createElement('template'); tpl.innerHTML = String(html || '').replace(TG_EM, (m, a, b, c, d) => `<img data-em="${a || c}" alt="${(b || d || '⭐').replace(/[<>"&]/g, '')}">`);
    const clean = el => {
      for (const ch of [...el.childNodes]) {
        if (ch.nodeType === 3) continue;
        if (ch.nodeType !== 1) { ch.remove(); continue; }
        if (ch.tagName === 'IMG' && ch.dataset.em && /^\d+$/.test(ch.dataset.em)) { const im = document.createElement('img'); im.className = 'nt-em'; im.src = K.emojiThumb(ch.dataset.em); im.alt = ch.alt; im.style.cssText = 'width:1.2em;height:1.2em;vertical-align:-.2em'; im.onerror = () => im.replaceWith(document.createTextNode(im.alt)); ch.replaceWith(im); continue; }
        if (!ALLOWED.has(ch.tagName)) { ch.replaceWith(document.createTextNode(ch.textContent)); continue; }
        for (const a of [...ch.attributes]) { if (!(ch.tagName === 'A' && a.name === 'href' && /^(https?:|tg:)/i.test(a.value))) ch.removeAttribute(a.name); }
        if (ch.tagName === 'A') { ch.target = '_blank'; ch.rel = 'noopener'; }
        clean(ch);
      }
    };
    clean(tpl.content); node.replaceChildren(tpl.content);
  }

  K.register({id: 'noticeTemplatesPage', title: 'Тексты уведомлений', async render(root) {
    let data = await api('/api/admin/notice-templates'), lib = null;
    const loadLib = async force => { if (lib && !force) return lib; try { lib = await api('/api/admin/emoji-library'); } catch (e) { lib = {items: [], gifts: [], starter_total: 0, starter_loaded: 0}; K.fail(e); } return lib; };
    await loadLib();
    const search = K.input({type: 'search', placeholder: 'Поиск уведомления', 'aria-label': 'Поиск'}), info = h('div', {class: 'muted'}), list = h('div', {class: 'stack'});
    const paintInfo = () => info.textContent = `Библиотека emoji: ${lib.items.length} · стартовый набор: ${lib.starter_loaded}/${lib.starter_total} · emoji подарков: ${lib.gifts.length}${data ? ' · изменено текстов: ' + data.custom_count : ''}${lib.notice ? ' · ' + lib.notice : ''}`;
    paintInfo();

    function card(it) {
      const ta = K.textarea(it.text, {rows: 5, maxLength: 3500, spellcheck: false}), pv = h('div', {class: 'notice'}), reset = h('button', {type: 'button', class: 'small', text: 'Вернуть стандартный', disabled: !it.custom}), badge = h('span');
      const paintBadge = () => badge.replaceChildren(...(it.custom ? [K.badge('изменено', 'good')] : []));
      paintBadge(); safeInto(pv, it.preview);
      let t = 0; const refresh = () => { clearTimeout(t); t = setTimeout(async () => { try { const d = await api('/api/admin/notice-templates/preview', {key: it.key, text: ta.value}); pv.className = 'notice' + (d.ok ? '' : ' bad'); d.ok ? safeInto(pv, d.html) : (pv.textContent = d.error || 'Ошибка в разметке'); } catch (e) { pv.className = 'notice bad'; pv.textContent = e.message; } }, 280); };
      ta.addEventListener('input', refresh);
      const ins = tok => { K.tg.insert(ta, tok); };
      const chips = h('div', {class: 'row', style: {flexWrap: 'wrap'}}, it.vars.map(v => h('button', {type: 'button', class: 'small', title: v.desc + (v.html ? ' (готовый HTML)' : ''), text: '{' + v.name + '}', onclick: () => ins('{' + v.name + '}')})));
      const picker = h('div');
      chips.append(h('button', {type: 'button', class: 'small', text: '✨ premium emoji', onclick: async () => {
        if (picker.childElementCount) { picker.replaceChildren(); return; }
        const l = await loadLib(), q = K.input({type: 'search', placeholder: 'Поиск emoji'}), body = h('div');
        const cell = (id, glyph, label) => h('button', {type: 'button', title: label || glyph, dataset: {l: String(label || '').toLowerCase()}, onclick: () => ins(K.tg.token(id, glyph))}, h('span', {text: glyph || '⭐', style: {fontSize: '20px'}}), h('img', {src: K.emojiThumb(id), alt: '', style: {width: '26px', height: '26px'}, onerror(e) { e.target.remove(); }}));
        const draw = () => { const s = q.value.trim().toLowerCase(); body.replaceChildren(); for (const [title, cells] of [['Подарки', l.gifts.map(x => cell(x.id, x.emoji, x.label))], ['Библиотека', l.items.map(x => cell(x.id, x.emoji, x.label || x.emoji))]]) { const f = cells.filter(c => !s || c.dataset.l.includes(s)); if (f.length) body.append(h('div', {class: 'group-title', text: title}), h('div', {class: 'catalog-grid'}, f)); } if (!body.children.length) body.append(K.empty(l.items.length || l.gifts.length ? 'Ничего не найдено' : 'Библиотека пуста. Нажмите «+ Стартовый набор emoji» вверху страницы или привяжите emoji к подаркам.')); };
        q.oninput = draw; draw(); picker.replaceChildren(q, body);
      }}));
      const save = K.btn('Сохранить', async () => {
        const d = await api('/api/admin/notice-templates', {key: it.key, text: ta.value}); Object.assign(it, d.item); data.custom_count = data.groups.reduce((n, g) => n + g.items.filter(x => x.custom).length, 0);
        reset.disabled = !it.custom; paintBadge(); ta.value = it.text; pv.className = 'notice'; safeInto(pv, it.preview); paintInfo(); K.ok('Сохранено');
      }, 'primary small');
      reset.onclick = () => K.action(reset, async () => { if (!await K.confirm('Вернуть стандартный текст?', 'Вернуть')) return; await api('/api/admin/notice-templates/reset', {key: it.key}); await reloadAll(); K.ok('Стандартный текст возвращён'); });
      const test = K.btn('Отправить тест мне', async () => { const d = await api('/api/admin/notice-templates/test', {key: it.key, text: ta.value}); K.ok(d.emoji_requested ? `Отправлено. Premium emoji: ${d.emoji_delivered} из ${d.emoji_requested}` : 'Отправлено вам в Telegram'); }, 'small');
      return h('div', {class: 'list-item', style: {flexDirection: 'column', alignItems: 'stretch'}}, h('div', {class: 'row'}, h('div', {class: 'grow'}, h('strong', {text: it.title}), h('small', {text: it.when})), badge), ta, chips, picker, pv, h('div', {class: 'row'}, save, reset, test));
    }
    function paint() {
      const q = search.value.trim().toLowerCase(), open = new Set([...list.querySelectorAll('details[open]')].map(d => d.dataset.g)); list.replaceChildren();
      for (const g of data.groups) {
        const items = g.items.filter(x => !q || (x.title + ' ' + x.when + ' ' + x.key + ' ' + x.text).toLowerCase().includes(q)); if (!items.length) continue;
        const d = h('details', {dataset: {g: g.name}}, h('summary', {text: `${g.name} · ${items.length}`}), h('div', {class: 'stack', style: {marginTop: '10px'}}, items.map(card))); if (q || open.has(g.name)) d.open = true; list.append(d);
      }
      if (!list.children.length) list.append(K.empty('Ничего не найдено'));
    }
    async function reloadAll() { data = await api('/api/admin/notice-templates'); await loadLib(true); paintInfo(); paint(); }
    search.addEventListener('input', paint); paint();
    root.append(K.head('Тексты уведомлений', 'Любое сообщение бота можно переписать и украсить premium emoji.', 'Telegram · Уведомления', K.link('← Настройки бота', '#tool=botSettingsPage')),
      K.card(null, null, search, h('div', {class: 'row'},
        K.btn('+ Стартовый набор emoji', async () => { const d = await api('/api/admin/emojis/starter', {}); await loadLib(true); paintInfo(); K.ok(`Добавлено emoji: ${d.added} из ${d.total}${d.missing?.length ? '. Не найдены в Telegram: ' + d.missing.map(x => x.label).join(', ') : ''}`); }, 'small'),
        K.btn('+ Набор по ссылке', async () => { const link = window.prompt('Ссылка на набор premium emoji, например https://t.me/addemoji/НАЗВАНИЕ'); if (!link) return; const d = await api('/api/admin/emojis/pack', {link}); await loadLib(true); paintInfo(); K.ok(`Набор «${d.title}»: добавлено ${d.added}`); }, 'small'),
        K.btn('Сбросить все', async () => { if (!await K.confirm('Вернуть стандартные тексты для ВСЕХ уведомлений?', 'Сбросить', true)) return; await api('/api/admin/notice-templates/reset', {key: '*'}); await reloadAll(); K.ok('Все тексты сброшены'); }, 'small danger')), info), list);
  }});
})();
