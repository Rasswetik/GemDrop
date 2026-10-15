/* Shared building blocks for the GemDrop admin shell and every native tool module. */
(() => {
  'use strict';
  const K = window.AdminKit = {tools: new Map(), views: [], offsets: {}};
  const $ = id => document.getElementById(id);
  K.$ = $;

  /* ---------- formatting ---------- */
  K.money = n => Number(n || 0).toLocaleString('ru-RU', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  K.num = n => Number(n || 0).toLocaleString('ru-RU');
  K.pct = (n, d = 1) => Number.isFinite(Number(n)) ? Number(n).toLocaleString('ru-RU', {minimumFractionDigits: d, maximumFractionDigits: d}) + '%' : '—';
  K.time = stamp => {
    if (!stamp && stamp !== 0) return '—';
    if (typeof stamp === 'number') { const d = new Date(stamp > 1e12 ? stamp : stamp * 1000); return Number.isNaN(d.getTime()) ? String(stamp) : d.toLocaleString('ru-RU'); }
    const text = String(stamp), d = new Date(text.replace(' ', 'T') + (/Z$|[+-]\d\d:\d\d$/.test(text) ? '' : 'Z'));
    return Number.isNaN(d.getTime()) ? text : d.toLocaleString('ru-RU');
  };
  K.ago = stamp => {
    const d = new Date(typeof stamp === 'number' ? (stamp > 1e12 ? stamp : stamp * 1000) : String(stamp).replace(' ', 'T') + (/Z$|[+-]\d\d:\d\d$/.test(String(stamp)) ? '' : 'Z'));
    if (Number.isNaN(d.getTime())) return '';
    const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
    return s < 60 ? 'только что' : s < 3600 ? Math.floor(s / 60) + ' мин назад' : s < 86400 ? Math.floor(s / 3600) + ' ч назад' : Math.floor(s / 86400) + ' дн назад';
  };
  K.signed = n => (Number(n) >= 0 ? '+' : '−') + K.money(Math.abs(Number(n) || 0));
  K.localInput = ts => { const d = new Date(Number(ts) > 1e12 ? Number(ts) : Number(ts) * 1000); if (!ts || Number.isNaN(d.getTime())) return ''; const p = n => String(n).padStart(2, '0'); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`; };

  /* ---------- DOM ---------- */
  K.el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  K.h = (tag, props, ...kids) => {
    const n = document.createElement(tag);
    if (props && (typeof props !== 'object' || props instanceof Node || Array.isArray(props))) { kids.unshift(props); props = null; }
    for (const [key, value] of Object.entries(props || {})) {
      if (value == null || value === false) continue;
      if (key === 'class') n.className = value;
      else if (key === 'text') n.textContent = value;
      else if (key === 'dataset') Object.assign(n.dataset, value);
      else if (key === 'style' && typeof value === 'object') Object.assign(n.style, value);
      else if (key.startsWith('on') && typeof value === 'function') n.addEventListener(key.slice(2).toLowerCase(), value);
      else if (key in n && key !== 'list' && key !== 'form' && typeof value !== 'object') { try { n[key] = value; } catch (_) { n.setAttribute(key, value); } }
      else n.setAttribute(key, value === true ? '' : value);
    }
    const add = list => { for (const kid of list) { if (kid == null || kid === false) continue; if (Array.isArray(kid)) add(kid); else n.append(kid instanceof Node ? kid : document.createTextNode(String(kid))); } };
    add(kids);
    return n;
  };
  K.clear = root => { root.replaceChildren(); return root; };

  /* ---------- notifications ---------- */
  K.toast = (text, kind) => {
    const open = [...document.querySelectorAll('dialog[open]')].at(-1);
    let target = $('message');
    if (open) { target = open.querySelector('.dialog-message'); if (!target) { target = K.el('div', null, 'dialog-message'); target.setAttribute('role', 'status'); open.append(target); } }
    target.textContent = text; target.hidden = false;
    target.classList.toggle('bad', kind === 'bad'); target.classList.toggle('good', kind === 'good');
    clearTimeout(K.toast.timer); K.toast.timer = setTimeout(() => target.hidden = true, kind === 'bad' ? 8000 : 4500);
  };
  K.fail = e => K.toast(e && e.message ? e.message : String(e), 'bad');
  K.ok = text => K.toast(text || 'Сохранено.', 'good');

  /* ---------- network ---------- */
  K.api = async (url, data, method = 'POST') => {
    const init = {credentials: 'same-origin', cache: 'no-store', headers: {'Content-Type': 'application/json'}};
    if (data === undefined) init.method = method === 'POST' ? 'GET' : method;
    else { init.method = method; init.body = data instanceof FormData ? data : JSON.stringify(data); if (data instanceof FormData) delete init.headers; }
    const r = await fetch(url, init);
    const d = await r.json().catch(() => ({error: 'Сервер не ответил. Обновите страницу.'}));
    if (!r.ok) throw new Error(d.error || d.message || 'Ошибка ' + r.status);
    return d;
  };
  K.post = (url, data) => K.api(url, data === undefined ? {} : data, 'POST');
  K.del = url => K.api(url, {}, 'DELETE');

  /* ---------- actions ---------- */
  K.action = async (button, fn) => {
    if (button) button.disabled = true;
    try { return await fn(); } catch (e) { K.fail(e); } finally { if (button) button.disabled = false; }
  };
  K.btn = (text, fn, cls = '') => { const b = K.h('button', {type: 'button', class: cls, text}); b.onclick = () => K.action(b, fn); return b; };
  K.link = (text, href, cls = 'button') => K.h('a', {class: cls, href, text});
  K.confirm = (text, okLabel = 'Подтвердить', danger = false) => new Promise(resolve => {
    let dlg = $('confirmDialog');
    if (!dlg) { dlg = K.h('dialog', {id: 'confirmDialog', class: 'dialog-small'}, K.h('p', {id: 'confirmText'}), K.h('div', {class: 'dialog-actions'}, K.h('button', {type: 'button', id: 'confirmNo', text: 'Отмена'}), K.h('button', {type: 'button', id: 'confirmYes', class: 'primary'}))); document.body.append(dlg); }
    $('confirmText').textContent = text; $('confirmYes').textContent = okLabel; $('confirmYes').className = danger ? 'danger' : 'primary';
    const done = v => { dlg.close(); resolve(v); };
    $('confirmYes').onclick = () => done(true); $('confirmNo').onclick = () => done(false); dlg.oncancel = () => resolve(false);
    dlg.showModal();
  });

  /* ---------- form widgets ---------- */
  K.field = (label, control, hint) => K.h('label', null, K.h('span', {text: label}), control, hint ? K.h('span', {class: 'hint', text: hint}) : null);
  K.input = (props = {}) => K.h('input', {type: 'text', autocomplete: 'off', ...props});
  K.number = (value, props = {}) => K.h('input', {type: 'number', step: 'any', value: value ?? '', ...props});
  K.select = (options, value, props = {}) => {
    const s = K.h('select', props);
    for (const o of options) { const [v, t] = Array.isArray(o) ? o : [o, o]; const opt = K.h('option', {value: v, text: t}); s.append(opt); }
    if (value != null) s.value = String(value);
    return s;
  };
  K.textarea = (value, props = {}) => K.h('textarea', {rows: 4, ...props}, value ?? '');
  K.check = (label, checked, props = {}) => K.h('label', {class: 'check'}, K.h('input', {type: 'checkbox', checked: !!checked, ...props}), K.h('span', {text: label}));
  K.switchRow = (label, description, checked, onChange) => {
    const input = K.h('input', {type: 'checkbox', checked: !!checked});
    if (onChange) input.addEventListener('change', () => onChange(input.checked, input));
    const row = K.h('label', {class: 'switch-row'}, K.h('span', null, label, description ? K.h('small', {text: description}) : null), input, K.h('i'));
    row.input = input; return row;
  };
  K.form = (children, onSubmit, submitLabel = 'Сохранить', cls = 'stack') => {
    const submit = K.h('button', {type: 'submit', class: 'primary', text: submitLabel});
    const form = K.h('form', {class: cls}, children, submit);
    form.addEventListener('submit', e => { e.preventDefault(); K.action(submit, () => onSubmit(form, new FormData(form))); });
    form.submitButton = submit; return form;
  };

  /* ---------- layout widgets ---------- */
  K.card = (title, description, ...kids) => K.h('article', null, title || description ? K.h('div', {class: 'card-head'}, K.h('div', null, title ? K.h('h2', {text: title}) : null, description ? K.h('p', {text: description}) : null)) : null, kids);
  K.metric = (label, value, detail = '', tone = '') => K.h('div', {class: 'metric ' + tone}, K.h('span', {text: label}), K.h('strong', {text: value}), K.h('small', {text: detail}));
  K.badge = (text, tone = '') => K.h('span', {class: 'badge ' + tone, text});
  K.notice = (text, tone = '') => K.h('div', {class: 'notice ' + tone, role: 'status'}, text);
  K.empty = text => K.h('div', {class: 'empty', text});
  K.kv = rows => K.h('dl', {class: 'kv'}, rows.filter(Boolean).flatMap(([k, v]) => [K.h('dt', {text: k}), K.h('dd', null, v instanceof Node ? v : String(v ?? '—'))]));
  K.tabs = (items, initial) => {
    const bar = K.h('div', {class: 'tabs', role: 'tablist'}), body = K.h('div'), root = K.h('div', null, bar, body);
    const select = id => {
      const item = items.find(t => t.id === id) || items[0];
      bar.querySelectorAll('button').forEach(b => { b.setAttribute('aria-selected', String(b.dataset.id === item.id)); });
      body.replaceChildren(K.empty('Загрузка…'));
      Promise.resolve().then(() => item.render(body)).catch(e => { body.replaceChildren(K.notice(e.message, 'bad')); });
    };
    for (const t of items) bar.append(K.h('button', {type: 'button', role: 'tab', dataset: {id: t.id}, text: t.label, onclick: () => select(t.id)}));
    root.select = select; select(initial || items[0].id); return root;
  };
  K.table = (root, headings, rows, opts = {}) => {
    root.replaceChildren(); root.classList.add('table-wrap'); root.classList.toggle('cards', opts.cards !== false);
    if (!rows.length) { root.append(K.empty(opts.empty || 'Записей нет.')); return; }
    const head = K.h('tr', null, headings.map((x, i) => K.h('th', {text: x, class: opts.numeric && opts.numeric.includes(i) ? 'number' : ''})));
    const body = K.h('tbody', null, rows.map(cells => K.h('tr', null, cells.map((value, i) => { const td = K.h('td', {dataset: {label: headings[i]}, class: opts.numeric && opts.numeric.includes(i) ? 'number' : ''}); td.append(value instanceof Node ? value : document.createTextNode(String(value ?? '—'))); return td; }))));
    root.append(K.h('table', null, K.h('thead', null, head), body));
  };
  K.simpleTable = (headings, rows, opts) => { const root = K.h('div'); K.table(root, headings, rows, opts); return root; };
  K.pager = (root, data, key, reload) => {
    const offset = K.offsets[key] || 0; root.replaceChildren();
    const prev = K.btn('←', async () => { K.offsets[key] = Math.max(0, offset - data.limit); await reload(); }); prev.disabled = offset === 0; prev.setAttribute('aria-label', 'Предыдущая страница');
    const next = K.btn('→', async () => { K.offsets[key] = offset + data.limit; await reload(); }); next.disabled = offset + data.limit >= data.total; next.setAttribute('aria-label', 'Следующая страница');
    root.append(prev, K.el('span', null, data.total ? `${offset + 1}–${Math.min(data.total, offset + data.limit)} из ${K.num(data.total)}` : '0 записей'), next);
  };
  /* Paged remote list: renders into `root`, returns {reload}. fetcher(offset,limit)->{items,total,limit}; renderRows(items)->{headings,rows} */
  K.pagedTable = (root, key, fetcher, build, opts = {}) => {
    const holder = K.h('div'), pg = K.h('div', {class: 'pager'}); root.append(holder, pg);
    K.offsets[key] = 0;
    const reload = async () => {
      const d = await fetcher(K.offsets[key] || 0, opts.limit || 50);
      const {headings, rows, numeric} = build(d.items, d);
      K.table(holder, headings, rows, {numeric, empty: opts.empty});
      K.pager(pg, {...d, limit: d.limit || opts.limit || 50}, key, reload);
    };
    reload().catch(e => holder.replaceChildren(K.notice(e.message, 'bad')));
    return {reload: () => { K.offsets[key] = 0; return reload(); }, refresh: reload};
  };
  K.gift = (g, size = 40) => K.h('img', {class: 'thumb', src: g?.image_url || '/static/img/gift.png', alt: g?.name || '', loading: 'lazy', style: {width: size + 'px', height: size + 'px'}, onerror(e) { e.target.onerror = null; e.target.src = '/static/img/gift.png'; }});

  /* ---------- gift catalog ---------- */
  let catalogPromise = null;
  K.catalog = (force = false) => { if (force || !catalogPromise) catalogPromise = K.api('/api/catalog').then(d => d.gifts || []).catch(e => { catalogPromise = null; throw e; }); return catalogPromise; };
  /* Searchable gift <select>. Returns {node, get value(), set value(v)}; selected id is kept even if filtered out. */

  /* ---------- gift model refinement: value "<id>" or "<id>@m:<model>" ---------- */
  const MSEP = '@m:', modelCache = new Map();
  K.baseGiftId = v => String(v == null ? '' : v).split(MSEP)[0];
  async function fetchModels(gid, tries = 0) {
    const hit = modelCache.get(gid); if (hit) return hit;
    const r = await fetch('/api/gifts/models?gift_id=' + encodeURIComponent(gid), {credentials: 'same-origin'}).then(x => x.ok ? x.json() : null).catch(() => null);
    const list = (r && r.models) || [];
    if (r && r.loading && !list.length && tries < 6) { await new Promise(z => setTimeout(z, 2200)); return fetchModels(gid, tries + 1); }
    modelCache.set(gid, list); return list;
  }
  /* Wraps a gift <select>: adds an optional model <select> below it and makes sel.value return "<id>@m:<model>". Returns the wrapper node. */
  K.modelize = (sel, initial) => {
    const desc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value'), st = {base: desc.get.call(sel), model: '', pending: '', loaded: null};
    const box = K.h('select', {title: 'Модель подарка', 'aria-label': 'Модель подарка', hidden: true});
    Object.defineProperty(sel, 'value', {configurable: true,
      get() { const b = desc.get.call(this); if (b !== st.base) { st.base = b; st.model = ''; st.pending = ''; } return st.model ? b + MSEP + st.model : b; },
      set(v) { v = String(v == null ? '' : v); const i = v.indexOf(MSEP), b = i >= 0 ? v.slice(0, i) : v; st.pending = i >= 0 ? v.slice(i + MSEP.length) : ''; desc.set.call(this, b); if (b !== st.base) { st.base = b; st.model = ''; } load(); }});
    async function load() {
      const b = desc.get.call(sel); if (!b) { box.hidden = true; return; }
      if (st.loaded === b) { if (st.pending) { st.model = st.pending; box.value = st.pending; st.pending = ''; } return; }
      st.loaded = b; box.hidden = true; const list = await fetchModels(b); if (st.loaded !== b) return;
      box.replaceChildren(new Option('Базовый подарок · без модели', ''), ...list.map(m => new Option(`${m.name} · ${Number(m.price_ton).toLocaleString('ru-RU', {maximumFractionDigits: 2})} TON`, m.name)));
      box.hidden = !list.length; box.value = st.pending || st.model || ''; if (box.value !== (st.pending || st.model || '')) box.value = ''; st.model = box.value; st.pending = '';
    }
    box.onchange = () => { st.model = box.value; sel.dispatchEvent(new Event('change', {bubbles: true})); };
    sel.addEventListener('change', () => load()); sel.addEventListener('modelsync', () => load());
    if (initial != null && initial !== '') sel.value = initial; else load();
    return K.h('div', {class: 'stack', style: {gap: '6px'}}, sel, box);
  };
  /* plain gift select with model refinement. Returns {node, select} */
  K.giftSelect = (gifts, value, onchange, props = {}) => {
    const sel = K.select(gifts.map(g => [String(g.id), `${g.name || 'Подарок'} · ${g.price_ton == null ? '?' : K.money(g.price_ton)} TON`]), K.baseGiftId(value), {'aria-label': 'Подарок', ...props});
    const node = K.modelize(sel, value || sel.value); if (onchange) sel.addEventListener('change', () => onchange(sel.value)); return {node, select: sel};
  };
  K.giftPicker = (initial, {label = 'Подарок', onchange, allowEmpty = false, models = false} = {}) => {
    let gifts = [], value = initial ?? '';
    const search = K.input({type: 'search', placeholder: 'Поиск по названию', 'aria-label': 'Поиск подарка'}), select = K.h('select', {'aria-label': label});
    const fill = () => {
      const base = K.baseGiftId(value), q = search.value.trim().toLocaleLowerCase('ru'), list = gifts.filter(g => !q || String(g.name || '').toLocaleLowerCase('ru').includes(q) || String(g.id) === base);
      select.replaceChildren(...(allowEmpty ? [K.h('option', {value: '', text: '— не выбрано —'})] : []), ...list.slice(0, 400).map(g => K.h('option', {value: g.id, text: `${g.name || 'Подарок'} · ${g.price_ton == null ? '?' : K.money(g.price_ton)} TON`})));
      if ([...select.options].some(o => o.value === base)) select.value = String(value);
      else if (!allowEmpty && select.options.length) { value = select.options[0].value; select.value = value; }
      select.dispatchEvent(new Event('modelsync'));
    };
    search.addEventListener('input', fill);
    select.addEventListener('change', () => { value = select.value; onchange && onchange(value); });
    K.catalog().then(g => { gifts = g; fill(); }).catch(e => select.replaceChildren(K.h('option', {text: 'Каталог недоступен: ' + e.message})));
    const node = K.h('div', {class: 'stack wide', style: {gap: '8px'}}, search, models ? K.modelize(select) : select);
    return {node, select, get value() { return select.value || value; }, set value(v) { value = v ?? ''; fill(); }, get gift() { return gifts.find(g => String(g.id) === K.baseGiftId(select.value || value)); }};
  };

  /* ---------- tools registry ---------- */
  /* def: {id, title, desc, group, keywords?, render(root, K) -> (cleanup)?} */
  K.register = def => { K.tools.set(def.id, def); };
  K.query = obj => { const p = new URLSearchParams(); for (const [k, v] of Object.entries(obj)) if (v !== '' && v != null) p.set(k, v); return p.toString(); };
  K.debounce = (fn, ms = 300) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
  K.copy = async text => { try { await navigator.clipboard.writeText(text); K.ok('Скопировано.'); } catch (_) { K.toast('Не удалось скопировать.', 'bad'); } };
  K.head = (title, description, eyebrow, ...actions) => K.h('div', {class: 'tool-head'}, K.h('div', null, eyebrow ? K.h('span', {class: 'eyebrow', text: eyebrow}) : null, K.h('h1', {text: title}), description ? K.h('p', {text: description}) : null), actions.length ? K.h('div', {class: 'row'}, actions) : null);
})();
