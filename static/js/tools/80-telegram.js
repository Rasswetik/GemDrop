/* Telegram: broadcast, channel post, bot /start settings. */
(() => {
  const K = window.AdminKit, {h, api} = K;
  const safe = fn => { try { return fn(); } catch (_) { return null; } };

  /* ---------- Рассылка ---------- */
  K.register({id: 'broadcastAdminPage', title: 'Рассылка', async render(root) {
    let mode = 'filters', files = [], source = null, timer = 0, alive = true;
    const rows = []; const draft = safe(() => JSON.parse(localStorage.getItem('bcDraft') || 'null'));
    const text = K.textarea(draft?.text || '', {rows: 8, maxLength: 4000, placeholder: 'Текст сообщения. Поддерживается Telegram HTML и premium emoji.'});
    if (Array.isArray(draft?.buttons)) rows.push(...draft.buttons.slice(0, 12).map(b => Array.isArray(b) ? b : [b]));
    const save = () => safe(() => localStorage.setItem('bcDraft', JSON.stringify({text: text.value, buttons: rows})));
    text.addEventListener('input', save);
    const [mine, giftEmojis] = await Promise.all([K.tg.emojis().catch(() => []), api('/api/admin/gift-emojis').then(d => (d.items || []).filter(x => x.emoji_id)).catch(() => [])]);
    const emojiBox = h('div');
    const paintEmoji = () => emojiBox.replaceChildren(
      h('div', {class: 'group-title', text: 'Emoji подарков'}),
      K.tg.emojiGrid(giftEmojis.map(x => ({id: x.emoji_id, emoji: x.emoji || '🎁'})), text, save),
      h('div', {class: 'group-title', text: 'Мои premium emoji'}), K.tg.emojiGrid(mine, text, save));
    paintEmoji();
    const myId = K.input({placeholder: 'ID emoji, можно несколько через пробел'}), addEmoji = K.btn('Добавить', async () => {
      const ids = myId.value.split(/[\s,;]+/).filter(Boolean); if (!ids.length) throw new Error('Введите ID emoji');
      const added = []; for (const id of ids) added.push((await api('/api/admin/emojis', {id})).item);
      myId.value = ''; mine.splice(0, mine.length, ...await K.tg.emojis()); paintEmoji(); if (added.length === 1) { K.tg.insert(text, K.tg.token(added[0].id, added[0].emoji)); save(); } K.ok('Добавлено emoji: ' + added.length);
    }, 'small');

    /* фото */
    const photos = h('div', {class: 'row', style: {flexWrap: 'wrap'}}), picker = h('input', {type: 'file', accept: 'image/*', multiple: true});
    const paintPhotos = () => { photos.replaceChildren(...files.map((f, i) => h('div', {style: {position: 'relative'}}, h('img', {class: 'thumb', src: URL.createObjectURL(f), alt: '', style: {width: '72px', height: '72px'}}), h('button', {type: 'button', class: 'small danger', text: '×', 'aria-label': 'Убрать фото', onclick: () => { files.splice(i, 1); paintPhotos(); }})))); };
    picker.onchange = e => { for (const f of e.target.files) { if (files.length >= 10) { K.toast('Максимум 10 фото', 'bad'); break; } if (!f.type.startsWith('image/') || f.size > 8 * 1024 * 1024) { K.toast('Пропущен файл: ' + f.name, 'bad'); continue; } files.push(f); } e.target.value = ''; paintPhotos(); };

    /* кнопки */
    const buttons = K.tg.buttonEditor(rows, {types: ['web_app', 'url', 'copy', 'callback'], emojis: mine, single: true, onchange: save, empty: 'Кнопок нет'});

    /* пост из канала */
    const srcBox = h('div'), postList = h('div', {class: 'list'});
    const paintSource = () => {
      srcBox.replaceChildren(); if (!source) return;
      srcBox.append(K.notice(`Пост из канала · ${source.chat_title}: ${source.preview || 'Без текста'}${source.message_ids.length > 1 ? ` · альбом ${source.message_ids.length} шт. (кнопки отключены)` : ''}`, 'info'),
        h('div', {class: 'row'}, K.btn('Показать мне, как придёт', async () => { await api('/api/admin/broadcast/channel-post/preview', {chat_id: source.chat_id, message_ids: source.message_ids}); K.ok('Пост отправлен вам в Telegram'); }, 'small'), h('button', {type: 'button', class: 'small danger', text: 'Убрать пост', onclick: () => { source = null; paintSource(); }})));
    };
    const pick = it => { source = {chat_id: it.chat_id, message_ids: it.message_ids, chat_title: it.chat_title || 'Канал', preview: (it.text || '').replace(/\s+/g, ' ').slice(0, 120)}; postList.replaceChildren(); paintSource(); };
    const KIND = {text: 'Текст', photo: 'Фото', video: 'Видео', animation: 'GIF', document: 'Файл', audio: 'Аудио', voice: 'Голос', video_note: 'Кружок', sticker: 'Стикер', poll: 'Опрос'};
    const link = K.input({placeholder: 'Ссылка на пост, например https://t.me/channel/123'});
    const postsBtn = K.btn('Выбрать пост из канала', async () => {
      const d = await api('/api/admin/broadcast/channel-posts'); postList.replaceChildren(...(d.items?.length ? d.items.map(it => h('button', {type: 'button', class: 'list-item', onclick: () => pick(it)}, h('div', {class: 'grow'}, h('strong', {text: it.chat_title || 'Канал'}), h('small', {text: `${KIND[it.kind] || it.kind}${(it.message_ids || []).length > 1 ? ' ×' + it.message_ids.length : ''}${it.premium ? ' · ✨ premium emoji' : ''}${it.date ? ' · ' + K.time(it.date) : ''}`}), h('small', {text: it.text || 'Без текста'})))) : [K.empty(d.hint || 'Постов пока нет. Вставьте ссылку на пост или перешлите пост боту командой /copypost.')]));
    }, 'small');
    const findBtn = K.btn('Найти по ссылке', async () => { const l = link.value.trim(); if (!l) throw new Error('Вставьте ссылку на пост'); const d = await api('/api/admin/broadcast/channel-post/resolve', {link: l}); link.value = ''; pick(d.item); K.ok(d.preview_sent ? 'Пост выбран. Копия отправлена вам в Telegram' : 'Пост выбран'); }, 'small');

    /* аудитория */
    const f = {bal: K.number('', {min: 0, step: '.01', placeholder: '0'}), turn: K.number('', {min: 0, step: '.01', placeholder: '0'}), dep: K.number('', {min: 0, step: '.01', placeholder: '0'}), days: K.number('', {min: 0, step: 1, placeholder: '0'}), excl: K.input({placeholder: 'ID пользователя', inputMode: 'numeric'})};
    const uSearch = K.input({placeholder: 'ID, имя или @username', 'aria-label': 'Получатель'}), uResults = h('div', {class: 'list'}); let uId = '';
    const filterBox = h('div', {class: 'form-grid'}, K.field('Мин. баланс, TON', f.bal), K.field('Мин. оборот, TON', f.turn), K.field('Мин. пополнений, TON', f.dep), K.field('Аккаунту не менее, дней', f.days), K.field('Исключить пользователя (ID)', f.excl));
    const userBox = h('div', {class: 'stack'}, K.field('Получатель', uSearch), uResults);
    const filters = () => mode === 'user' ? (() => { const raw = uSearch.value.trim(); return {user_id: uId || (/^\d{5,}$/.test(raw) ? raw : '') || 0, recipient: raw, exclude_user_id: 0}; })() : {min_balance: f.bal.value.trim(), min_turnover: f.turn.value.trim(), min_deposit: f.dep.value.trim(), min_account_days: f.days.value.trim(), exclude_user_id: f.excl.value.trim(), user_id: 0, recipient: ''};
    uSearch.oninput = K.debounce(async () => {
      uId = ''; const q = uSearch.value.trim(); if (!q) { uResults.replaceChildren(); return; }
      try { const d = await api('/api/admin/users?q=' + encodeURIComponent(q), undefined); uResults.replaceChildren(...(d.users || []).map(u => h('button', {type: 'button', class: 'list-item', onclick: () => { uId = u.id; uSearch.value = `${u.name || 'Игрок'} · ${u.id}`; uResults.replaceChildren(); preview(); }}, h('div', {class: 'grow'}, h('strong', {text: u.name || 'Игрок'}), h('small', {text: (u.username ? '@' + u.username + ' · ' : '') + 'ID ' + u.id})), K.badge('Выбрать')))); } catch (_) {}
    }, 250);
    const previewBox = h('div');
    async function preview() {
      const fl = filters(); if (mode === 'user' && !fl.user_id && !fl.recipient) { previewBox.replaceChildren(K.notice('Введите ID, имя или @username получателя.')); return; }
      try { const d = await api('/api/admin/broadcast/preview', {filters: fl}); previewBox.replaceChildren(K.notice(`Получателей: ${d.count}${d.users?.length ? ' · ' + d.users.slice(0, 6).map(x => `${x.name || ''}${x.username ? ' @' + x.username : ''} (${x.id})`).join(', ') : ''}`, 'info')); } catch (e) { previewBox.replaceChildren(K.notice(e.message, 'bad')); }
    }
    const seg = h('div', {class: 'segmented'}), setMode = m => { mode = m; filterBox.hidden = m === 'user'; userBox.hidden = m !== 'user'; seg.replaceChildren(...[['filters', 'По фильтрам'], ['user', 'Одному игроку']].map(([v, t]) => h('button', {type: 'button', text: t, 'aria-pressed': String(v === m), onclick: () => { setMode(v); preview(); }}))); };
    setMode('filters');

    /* отправка и статус */
    const status = h('div', {class: 'list'});
    async function send(test) {
      if (!source && !text.value.trim() && !files.length) throw new Error('Добавьте текст или фото');
      if (!test) { const fl = filters(); if (mode === 'user' && !fl.user_id && !fl.recipient) throw new Error('Введите получателя'); if (!await K.confirm(source ? (mode === 'user' ? 'Отправить пост выбранному пользователю?' : 'Разослать пост из канала выбранной аудитории?') : (mode === 'user' ? 'Отправить сообщение выбранному пользователю?' : 'Отправить рассылку выбранной аудитории?'), 'Отправить', true)) return; }
      const fd = new FormData();
      fd.append('text', source ? '' : text.value); fd.append('buttons', JSON.stringify(source && source.message_ids.length > 1 ? [] : rows)); fd.append('filters', JSON.stringify(filters()));
      if (source) fd.append('source', JSON.stringify({chat_id: source.chat_id, message_ids: source.message_ids, preview: source.preview})); else files.forEach(x => fd.append('photos', x));
      if (test) fd.append('test', '1');
      const d = await api('/api/admin/broadcast/send', fd); K.ok(test ? 'Тест отправляется вам' : 'Рассылка запущена: ' + d.queued);
      if (!test && !source) { files = []; paintPhotos(); } poll();
    }
    const ST = {running: 'Идёт', done: 'Готово', cancelled: 'Отменена'};
    async function loadStatus() {
      const d = await api('/api/admin/broadcast/status'); let run = false; status.replaceChildren();
      if (!d.items.length) status.append(K.empty('Пока нет рассылок'));
      for (const j of d.items) {
        if (j.state === 'running') run = true; const done = j.sent + j.failed + j.blocked, pct = j.total ? Math.round(done * 100 / j.total) : 0;
        status.append(h('div', {class: 'list-item', style: {flexDirection: 'column', alignItems: 'stretch'}},
          h('div', {class: 'row'}, h('strong', {class: 'grow', text: `#${j.id} · ${j.preview || (j.photos ? 'Фото' : 'Сообщение')}`}), K.badge((ST[j.state] || j.state) + (j.state === 'running' ? ' · ' + pct + '%' : ''), j.state === 'running' ? 'warn' : j.state === 'done' ? 'good' : '')),
          h('progress', {max: 100, value: pct, style: {width: '100%'}}),
          h('small', {text: `Всего ${j.total} · доставлено ${j.sent} · не доставлено ${j.failed} · бот заблокирован ${j.blocked}${j.pending ? ' · в очереди ' + j.pending : ''}${j.photos ? ' · фото ' + j.photos : ''}`}),
          ...(j.errors || []).map(er => h('small', {class: 'muted', text: `ID ${er.user_id} · ${er.state === 'blocked' ? 'бот заблокирован' : 'ошибка'} · ${er.error}`})),
          j.state === 'running' ? K.btn('Остановить', async () => { await api(`/api/admin/broadcast/${j.id}/cancel`, {}); poll(); }, 'small danger') : null));
      }
      return run;
    }
    function poll() { clearTimeout(timer); loadStatus().then(run => { if (run && alive && document.body.contains(root)) timer = setTimeout(poll, 2500); }).catch(e => status.replaceChildren(K.notice(e.message, 'bad'))); }
    paintSource(); poll(); preview();
    root.append(K.head('Рассылка', 'Сообщение игрокам в Telegram от имени бота: по фильтрам аудитории или одному игроку.', 'Telegram'),
      K.card('Сообщение', null, K.field('Текст', text), K.tg.toolbar(text), emojiBox, h('div', {class: 'row'}, myId, addEmoji)),
      K.card('Фото', 'До 10 фото, каждое до 8 МБ. Подпись — текст выше (если он короче 1024 символов).', picker, photos),
      K.card('Пост из канала', 'Можно разослать готовый пост вместо текста и фото — форматирование и premium emoji сохранятся.', h('div', {class: 'row'}, postsBtn), K.field('Или ссылка на пост', link), findBtn, srcBox, postList),
      K.card('Кнопки под сообщением', null, buttons.node),
      K.card('Кому отправить', null, seg, filterBox, userBox, previewBox, h('div', {class: 'row'}, K.btn('Обновить число получателей', preview), K.btn('Тест мне', () => send(true)), K.btn('Отправить', () => send(false), 'primary'))),
      K.card('Статус рассылок', null, K.btn('Обновить', async () => poll(), 'small'), status));
    return () => { alive = false; clearTimeout(timer); };
  }});

  /* ---------- Пост в канал ---------- */
  K.register({id: 'postAdminPage', title: 'Пост в канал', async render(root) {
    const [settings, draftRes, emojis] = await Promise.all([api('/api/admin/post/settings').catch(e => ({error: e.message})), api('/api/admin/post/draft').catch(() => ({})), K.tg.emojis().catch(() => [])]);
    const d0 = draftRes.draft || {}; const rows = structuredClone(d0.buttons || []);
    const chan = K.input({value: settings.chat_id || '', placeholder: '-100… · @channel · https://t.me/channel'}), chanState = h('div');
    const paintChan = d => chanState.replaceChildren(K.notice(d.error || (d.configured || d.chat_id ? `✓ ${d.title || d.chat_id}${d.username ? ' · @' + d.username : ''}${d.chat_type ? ' · ' + d.chat_type : ''}${d.join_url ? ' · ссылка готова' : ''}${d.chat_type === 'channel' ? ' · Для premium emoji в канале боту нужно дополнительное имя из Fragment.' : ''}` : 'Чат/канал ещё не настроен.'), d.error ? 'bad' : d.configured ? 'good' : 'warn'));
    paintChan(settings);
    const chanForm = K.form([K.field('ID, @username или t.me-ссылка', chan, 'Можно вставить ID, @username, ссылку на публичный чат или ссылку на сообщение t.me/c/…. Для группы проще: добавьте бота администратором и отправьте в группе команду, указанную в документации бота.')], async () => { const d = await api('/api/admin/post/settings', {chat_id: chan.value}); paintChan(d); K.ok('Чат/канал сохранён'); });
    const text = K.textarea(d0.text || '', {rows: 10, placeholder: 'Текст · Telegram HTML'}), image = K.textarea(d0.image || '', {rows: 3, placeholder: 'https://… или file_id, по одному на строку'}), files = h('input', {type: 'file', accept: 'image/*', multiple: true});
    const silent = K.check('Без звука', !!d0.silent), protect = K.check('Запретить пересылку и сохранение', !!d0.protect);
    const draftState = h('small', {class: 'muted', text: d0.text || d0.image ? 'Черновик загружен' : 'Новый черновик'});
    let timer = null, rev = 0, writing = Promise.resolve();
    const cur = () => ({text: text.value, buttons: structuredClone(rows), image: image.value, silent: silent.querySelector('input').checked, protect: protect.querySelector('input').checked});
    const saveDraft = () => { clearTimeout(timer); const draft = cur(), r = rev; writing = writing.catch(() => {}).then(async () => { await api('/api/admin/post/draft', draft); if (r === rev) draftState.textContent = 'Сохранено · ' + new Date().toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit'}); }); return writing; };
    const schedule = () => { rev++; draftState.textContent = 'Есть изменения…'; clearTimeout(timer); timer = setTimeout(() => saveDraft().catch(e => { draftState.textContent = 'Не сохранено: ' + e.message; }), 750); };
    [text, image, silent, protect].forEach(n => { n.addEventListener('input', schedule); n.addEventListener('change', schedule); });
    const buttons = K.tg.buttonEditor(rows, {types: ['url', 'copy', 'callback'], emojis, onchange: schedule});
    const emojiId = K.input({placeholder: 'ID premium emoji'}), emojiGrid = h('div');
    const paintEmojis = () => emojiGrid.replaceChildren(K.tg.emojiGrid(emojis, text, schedule));
    paintEmojis();
    const status = h('div');
    const publish = K.btn('Опубликовать', async () => {
      status.replaceChildren(); await saveDraft();
      const fd = new FormData(); fd.append('text', text.value); fd.append('image', image.value.trim()); fd.append('buttons', JSON.stringify(rows)); fd.append('silent', silent.querySelector('input').checked ? '1' : '0'); fd.append('protect', protect.querySelector('input').checked ? '1' : '0');
      const fl = [...(files.files || [])], urls = image.value.split(/\r?\n/).filter(x => x.trim()).length; if (fl.length + urls > 10) throw new Error('Можно добавить не более 10 изображений'); fl.forEach(x => fd.append('photos', x));
      try { const d = await api('/api/admin/post/publish', fd); status.replaceChildren(K.notice('Пост опубликован' + (d.message_id ? ` · message_id ${d.message_id}` : '') + (d.premium_emoji_verified ? ' · ✓ Premium emoji подтверждены Telegram' : '') + ((d.warnings || []).length ? '\n' + d.warnings.join('\n') : ''), (d.warnings || []).length ? 'warn' : 'good')); K.ok('Пост опубликован'); }
      catch (e) { status.replaceChildren(K.notice(e.message, 'bad')); throw e; }
    }, 'primary');
    root.append(K.head('Пост в канал', 'Публикация сообщения с форматированием, premium emoji, фото и кнопками в привязанный чат или канал.', 'Telegram'),
      K.card('Telegram чат / канал', null, chanForm, chanState),
      K.card('Новый пост', null, K.field('Текст · Telegram HTML', text), K.tg.toolbar(text), draftState, h('div', {class: 'row'}, K.btn('Сохранить черновик', async () => { await saveDraft(); K.ok('Черновик сохранён'); }, 'small'))),
      K.card('Сохранённые эмодзи', 'Нажмите на эмодзи, чтобы вставить в текст.', emojiGrid, h('div', {class: 'row'}, emojiId, K.btn('Добавить и вставить', async () => { const it = (await api('/api/admin/emojis', {id: emojiId.value.trim()})).item; emojis.splice(0, emojis.length, ...await K.tg.emojis()); paintEmojis(); K.tg.insert(text, K.tg.token(it.id, it.emoji)); emojiId.value = ''; schedule(); }, 'small'))),
      K.card('Изображения', 'Загруженные с устройства фото нужно выбрать заново после перезагрузки страницы.', K.field('URL или Telegram file_id, по одному на строку', image), K.field('Или загрузить до 10 изображений', files), silent, protect),
      K.card('Кнопки', null, buttons.node), publish, status);
  }});

  /* ---------- Настройки бота ---------- */
  K.register({id: 'botSettingsPage', title: 'Настройки бота', async render(root) {
    const [d, emojis] = await Promise.all([api('/api/admin/bot/settings'), K.tg.emojis().catch(() => [])]);
    const rows = Array.isArray(d.buttons) ? structuredClone(d.buttons) : [[{text: 'Играть', type: 'web_app', value: '', style: 'primary', icon_custom_emoji_id: ''}]];
    const welcome = K.textarea(d.welcome_text || '', {rows: 8, maxLength: 16000, placeholder: 'Оставьте пустым для стандартного приветствия'});
    const sel = K.tg.emojiSelect(emojis, '', () => {});
    sel.options[0].text = 'Сохранённые premium emoji';
    const buttons = K.tg.buttonEditor(rows, {types: ['web_app', 'url', 'copy', 'callback'], emojis, empty: 'Нет пользовательских кнопок. /start всё равно получит безопасную кнопку «Играть».'});
    const form = K.form([K.field('Приветствие после /start', welcome, 'HTML и premium emoji поддерживаются. {referral_percent} подставляет актуальный процент рефералки.'), K.tg.toolbar(welcome),
      h('div', {class: 'row'}, sel, h('button', {type: 'button', text: 'Вставить emoji', onclick: () => { const it = emojis.find(x => String(x.id) === sel.value); if (!it) return K.toast('Сначала выберите сохранённый emoji', 'bad'); K.tg.insert(welcome, K.tg.token(it.id, it.emoji)); }})),
      h('div', {class: 'group-title', text: 'Кнопки /start'}), buttons.node], async () => { await api('/api/admin/bot/settings', {welcome_text: welcome.value, buttons: rows}); K.ok('Настройки /start сохранены. Они применятся при следующем /start.'); });
    root.append(K.head('Настройки бота', 'Приветствие и кнопки после /start.', 'Telegram · GemDrop'), K.card(null, null, form),
      K.card('Инструменты администратора', 'Через /start администратор может определить ID premium emoji или импортировать сообщение в черновик публикации.', d.emoji_notice ? K.notice(d.emoji_notice) : null, K.link('Тексты уведомлений и premium emoji →', '#tool=noticeTemplatesPage')));
  }});
})();
