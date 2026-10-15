/* Shared Telegram helpers: button editor, premium emoji library, insertion at cursor. */
(() => {
  const K = window.AdminKit, {h, api} = K;
  const TARGETS = [['', 'Главная / текущий GemDrop'], ['section:games', 'Игры'], ['section:mines', 'Мины'], ['section:upgrade', 'Апгрейды'], ['section:crash', 'Crash'], ['section:arena', 'Арена'], ['section:hilo', 'Hi-Lo'], ['section:limbo', 'Limbo'], ['section:road', 'Hamster Road'], ['section:giveaways', 'Розыгрыши'], ['section:profile', 'Профиль'], ['section:levels', 'Уровни'], ['section:bonuses', 'Бонусы'], ['section:deposit', 'Пополнение'], ['section:creator', 'Панель автора']];
  const TYPE_LABEL = {web_app: 'Открыть GemDrop / Mini App', url: 'Ссылка / браузер', copy: 'Скопировать текст', callback: 'Callback'};
  const STYLES = [['', 'Обычная'], ['primary', 'Синяя'], ['success', 'Зелёная'], ['danger', 'Красная']];
  K.emojiThumb = id => '/api/admin/emoji-thumb/' + id;

  K.tg = {
    token: (id, glyph) => `[emoji:${id}:${glyph || '⭐'}]`,
    insert(ta, text) {
      const a = ta.selectionStart ?? ta.value.length, b = ta.selectionEnd ?? a;
      ta.value = ta.value.slice(0, a) + text + ta.value.slice(b); ta.focus(); ta.selectionStart = ta.selectionEnd = a + text.length; ta.dispatchEvent(new Event('input', {bubbles: true}));
    },
    wrap(ta, tag) {
      const a = ta.selectionStart ?? 0, b = ta.selectionEnd ?? a, sel = ta.value.slice(a, b) || 'текст';
      ta.value = ta.value.slice(0, a) + `<${tag}>${sel}</${tag}>` + ta.value.slice(b); ta.focus(); ta.selectionStart = a + tag.length + 2; ta.selectionEnd = ta.selectionStart + sel.length; ta.dispatchEvent(new Event('input', {bubbles: true}));
    },
    /* toolbar with Telegram HTML wrappers */
    toolbar: ta => h('div', {class: 'row', style: {flexWrap: 'wrap'}}, [['b', 'Ж'], ['i', 'К'], ['u', 'Ч'], ['s', 'З'], ['tg-spoiler', 'Спойлер'], ['code', 'Код']].map(([tag, t]) => h('button', {type: 'button', class: 'small', text: t, title: `<${tag}>`, onclick: () => K.tg.wrap(ta, tag)}))),
    async emojis() { const d = await api('/api/admin/emojis'); return Array.isArray(d.items) ? d.items : []; },
    /* grid of saved emoji tiles that insert into textarea */
    emojiGrid(items, ta, onInsert) {
      const g = h('div', {class: 'catalog-grid'});
      if (!items.length) g.append(K.empty('Пришлите premium emoji боту командой /emoji или добавьте по ID.'));
      for (const it of items) g.append(h('button', {type: 'button', title: 'Вставить в текст', onclick: () => { K.tg.insert(ta, K.tg.token(it.id, it.emoji)); onInsert && onInsert(); }},
        h('span', {text: it.emoji || '◌', style: {fontSize: '22px'}}), h('img', {src: K.emojiThumb(it.id), alt: '', loading: 'lazy', style: {width: '28px', height: '28px'}, onerror(e) { e.target.remove(); }}), h('span', {text: '…' + String(it.id).slice(-6)})));
      return g;
    },
    emojiSelect(items, value, onchange) {
      const s = K.select([['', 'Без premium emoji'], ...items.map(x => [String(x.id), `${x.emoji || '◌'} · ${String(x.id).slice(-8)}`])], value || '', {'aria-label': 'Premium emoji кнопки', onchange: e => onchange(e.target.value)});
      if (value && !items.some(x => String(x.id) === String(value))) { s.append(new Option('ID · ' + String(value).slice(-10), value)); s.value = value; }
      return s;
    },
    /* rows editor: rows = array of arrays of {text,type,value,style,icon_custom_emoji_id}; opts.types, opts.emojis, opts.onchange, opts.newButton */
    buttonEditor(rows, opts = {}) {
      const types = opts.types || ['web_app', 'url', 'copy', 'callback'], emojis = opts.emojis || [], changed = () => opts.onchange && opts.onchange();
      const box = h('div', {class: 'stack'});
      const fresh = () => ({text: 'Кнопка', type: types.includes('url') ? 'url' : types[0], value: 'https://t.me/', style: '', icon_custom_emoji_id: ''});
      const valueEditor = btn => {
        const holder = h('span', {style: {display: 'contents'}});
        const paint = () => {
          holder.replaceChildren();
          if (btn.type === 'web_app') {
            const cur = String(btn.value || ''), list = TARGETS.slice();
            if (cur && !list.some(([v]) => v === cur)) list.push([cur, /^https:\/\//i.test(cur) ? 'Пользовательский HTTPS URL' : 'Текущее значение']);
            const s = K.select(list, cur, {'aria-label': 'Куда ведёт кнопка', onchange: e => { btn.value = e.target.value; changed(); }}); btn.value = s.value; holder.append(s);
          } else {
            if (String(btn.value || '').startsWith('section:')) btn.value = '';
            holder.append(K.input({value: btn.value || '', 'aria-label': 'Действие кнопки', placeholder: btn.type === 'url' ? 'https://... или tg://...' : btn.type === 'copy' ? 'Текст для копирования' : 'callback_data', oninput: e => { btn.value = e.target.value; changed(); }}));
          }
        };
        paint(); return {node: holder, paint};
      };
      const paint = () => {
        box.replaceChildren();
        rows.forEach((row, ri) => {
          const card = h('div', {class: 'list-item', style: {flexDirection: 'column', alignItems: 'stretch'}});
          card.append(h('div', {class: 'row'}, h('strong', {class: 'grow', text: `Ряд ${ri + 1}`}), h('button', {type: 'button', class: 'small danger', text: 'Удалить ряд', onclick: () => { rows.splice(ri, 1); paint(); changed(); }})));
          row.forEach((btn, bi) => {
            const ve = valueEditor(btn);
            const type = K.select(types.map(t => [t, TYPE_LABEL[t]]), btn.type || types[0], {'aria-label': 'Тип кнопки', onchange: e => { const old = btn.type; btn.type = e.target.value; if (btn.type === 'web_app' && old !== 'web_app') btn.value = ''; ve.paint(); changed(); }});
            card.append(h('div', {class: 'form-grid'}, K.input({value: btn.text || '', placeholder: 'Надпись', 'aria-label': 'Надпись кнопки', oninput: e => { btn.text = e.target.value; changed(); }}), type, ve.node,
              K.select(STYLES, btn.style || '', {'aria-label': 'Цвет кнопки', onchange: e => { btn.style = e.target.value; changed(); }}), K.tg.emojiSelect(emojis, btn.icon_custom_emoji_id, v => { btn.icon_custom_emoji_id = v; changed(); }),
              h('button', {type: 'button', class: 'small danger', text: '×', 'aria-label': 'Удалить кнопку', onclick: () => { row.splice(bi, 1); if (!row.length) rows.splice(ri, 1); paint(); changed(); }})));
          });
          if (!opts.single) card.append(h('div', null, h('button', {type: 'button', class: 'small', text: '+ Кнопка в этот ряд', onclick: () => { if (row.length >= 6) return K.toast('В одном ряду максимум 6 кнопок', 'bad'); row.push(fresh()); paint(); changed(); }})));
          box.append(card);
        });
        if (!rows.length) box.append(K.empty(opts.empty || 'Кнопок нет. Нажмите «+ Ряд».'));
      };
      paint();
      return {node: h('div', {class: 'stack'}, box, h('div', null, h('button', {type: 'button', class: 'small', text: '+ Ряд', onclick: () => { rows.push([fresh()]); paint(); changed(); }}))), repaint: paint};
    }
  };
})();
