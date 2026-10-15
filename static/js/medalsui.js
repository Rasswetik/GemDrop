/* Player side of the season medals: «Мои медали» in the profile settings and medals on the public profile. */
(function () {
  if (!window.GDMedals) return;
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (v) { return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); };
  var st = document.createElement('style');
  st.textContent = '.mdl-modal{position:fixed;inset:0;z-index:2600;display:grid;place-items:center;padding:20px;box-sizing:border-box;background:rgba(5,4,12,.66)}.mdl-modal.hidden{display:none}' +
    '' +
    '.mdl-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px}.mdl-top h2{margin:0;font-size:20px}.mdl-x{width:36px;height:36px;border-radius:50%;border:0;background:rgba(255,255,255,.08);color:#fff;font-size:20px;cursor:pointer}' +
    '.mdl-sub{margin:0 0 12px;color:#8d99a8;font-size:13px}.mdl-list{display:grid;gap:10px}' +
    '.mdl-item{display:grid;grid-template-columns:64px minmax(0,1fr) auto;gap:12px;align-items:center;padding:10px 12px;border-radius:18px;background:#1b1e27;border:1px solid rgba(255,255,255,.06)}' +
    '.mdl-item b{display:block;font-size:15px}.mdl-item small{display:block;margin-top:2px;color:#8d99a8;font-size:12px}' +
    '.mdl-sw{display:grid;gap:4px;justify-items:center;font-size:10px;color:#8d99a8}.mdl-sw input{width:40px;height:22px;accent-color:#a066ff}' +
    '.mdl-empty{padding:30px 10px;text-align:center;color:#8d99a8;font-size:14px}' +
    'html.hw .mdl-card{background:linear-gradient(180deg,#2a1450,#150b26);border-color:rgba(182,140,255,.25)}html.hw .mdl-item{background:#1d1033;border-color:rgba(182,140,255,.2)}' +
    '.pp-medals{display:flex;justify-content:center;flex-wrap:wrap;gap:8px;margin:8px 0 2px}.pp-medals:empty{display:none}.pp-medals span{display:inline-flex;cursor:default}';
  st.textContent += '.mgt-modal{position:fixed;inset:0;z-index:2800;display:grid;place-items:center;padding:20px;background:radial-gradient(ellipse at 50% 30%,rgba(60,40,120,.55),rgba(5,4,12,.88));backdrop-filter:blur(8px);animation:mgtIn .35s ease both}' +
    '.mgt-card{width:min(100%,360px);display:grid;justify-items:center;gap:10px;text-align:center;padding:26px 20px 20px;border-radius:30px;background:linear-gradient(180deg,#1b1e2e,#12141d);border:1px solid rgba(255,255,255,.1);box-shadow:0 30px 80px rgba(0,0,0,.6),0 0 60px rgba(160,102,255,.25);color:#f2f4f8;animation:mgtPop .5s cubic-bezier(.2,1.1,.3,1) both}' +
    '.mgt-k{font-size:11px;font-weight:800;letter-spacing:.16em;text-transform:uppercase;color:#c9a3ff}.mgt-card h3{margin:0;font-size:24px;line-height:1.15}.mgt-card p{margin:0;color:#9aa6b6;font-size:14px}' +
    '.mgt-btn{width:100%;min-height:54px;margin-top:8px;border:0;border-radius:18px;background:linear-gradient(180deg,#b27dff,#7a3fe0);color:#fff;font:inherit;font-size:16px;font-weight:800;cursor:pointer;box-shadow:0 10px 26px rgba(122,63,224,.4),inset 0 1px rgba(255,255,255,.3)}.mgt-btn:active{transform:scale(.98)}' +
    '@keyframes mgtIn{from{opacity:0}to{opacity:1}}@keyframes mgtPop{from{opacity:0;transform:translateY(24px) scale(.9)}to{opacity:1;transform:none}}';
  st.textContent += '.rar{font-size:11px;font-weight:800;letter-spacing:.06em;text-transform:uppercase}.rar-common{color:#9aa6b6}.rar-rare{color:#5aa8ff}.rar-epic{color:#c18bff}.rar-legendary{background:linear-gradient(90deg,#ff6ad5,#ffd36a,#6aff9e,#6ac8ff,#b06aff);-webkit-background-clip:text;background-clip:text;color:transparent}';
  var rarTag = function (r) { r = r || 'common'; return '<span class="rar rar-' + r + '">' + esc((GDMedals.RAR || {})[r] || '') + '</span>'; };
  document.head.appendChild(st);

  function api(url, body) {
    return fetch(url, {method: body ? 'POST' : 'GET', credentials: 'same-origin', headers: body ? {'Content-Type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined})
      .then(function (r) { return r.json().then(function (j) { if (!r.ok || j.ok === false) throw new Error(j.error || 'Ошибка'); return j; }); });
  }
  st.textContent += '.mdl-card{position:relative;width:min(100%,440px)!important;max-height:calc(100dvh - 40px);display:flex;flex-direction:column;padding:20px 16px 16px!important;text-align:left!important;overflow:hidden!important}' +
    '.mdl-card h2{margin:0 36px 2px 4px;font-size:22px}.mdl-card .modal-x{position:absolute;top:14px;right:14px;width:32px;height:32px;border-radius:50%;border:0;background:rgba(255,255,255,.08);color:#fff;font-size:18px;line-height:1;padding:0;cursor:pointer}' +
    '.mdl-meta{margin:0 4px 12px;font-size:13px;color:var(--muted,#96a0a8)}.mdl-meta b{color:var(--text,#f4f5f7);font-weight:700}' +
    '.mdl-body{overflow:auto;flex:1;margin:0 -16px;padding:0 16px 4px;overscroll-behavior:contain}' +
    '.mdl-slots{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px}.mdl-slot{display:grid;justify-items:center;align-content:center;gap:3px;min-height:104px;padding:8px 4px;border-radius:16px;border:1px dashed rgba(255,255,255,.14);background:rgba(255,255,255,.02);font-size:11px;color:var(--muted,#96a0a8);text-align:center}.mdl-slot.on{border:1px solid rgba(71,176,245,.45);background:rgba(71,176,245,.07);color:var(--text,#f4f5f7)}.mdl-slot em{font-style:normal;font-size:20px;opacity:.5}.mdl-slot span{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
    '.mdl-star img,.mdl-slot em.mn img{width:13px;height:13px;object-fit:contain;vertical-align:-2px;margin-right:3px}.mdl-star img{margin:0}.mdl-modal{overscroll-behavior:contain}html.mdl-ns,html.mdl-ns body{overflow:hidden!important}.mdl-sec{margin:18px 0 0;padding:12px 10px 10px;border-radius:18px;background:linear-gradient(180deg,rgba(255,180,84,.09),rgba(255,180,84,.02));border:1px solid rgba(255,180,84,.28)}.mdl-sec .mdl-h{margin:0 2px 10px}.mdl-sec .mdl-h .rar{color:#ffb454;font-size:13px}' +
    '.mdl-filt{display:flex;gap:6px;margin-bottom:4px}.mdl-filt button{flex:1;height:34px;padding:0 8px;border-radius:12px;border:1px solid var(--gd-line,#22253d);background:rgba(255,255,255,.045);color:var(--muted,#96a0a8);font:inherit;font-size:12.5px;font-weight:700;cursor:pointer}.mdl-filt button.on{background:linear-gradient(110deg,#9ae9e3,#47b0f5);border-color:transparent;color:#131524}' +
    '.mdl-h{display:flex;align-items:center;justify-content:space-between;margin:16px 2px 8px;font-size:12.5px;font-weight:800;letter-spacing:.05em;text-transform:uppercase}.mdl-h small{font-weight:600;color:var(--muted,#96a0a8);text-transform:none;letter-spacing:0}' +
    '.mdl-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.mdl-c{position:relative;display:grid;justify-items:center;align-content:start;gap:3px;padding:10px 6px;border-radius:16px;background:rgba(255,255,255,.045);border:1px solid var(--gd-line,#22253d);text-align:center;min-width:0}.mdl-c.on{border-color:rgba(71,176,245,.55)}' +
    '.mdl-c b{font-size:12.5px;line-height:1.2;max-width:100%;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}.mdl-c small{font-size:10.5px;color:var(--muted,#96a0a8);line-height:1.2}' +
    '.mdl-eq{margin-top:5px;width:100%;height:30px;border-radius:10px;border:1px solid rgba(242,245,249,.3);background:linear-gradient(110deg,#9ae9e3,#47b0f5);color:#131524;font:inherit;font-size:12.5px;font-weight:800;cursor:pointer}.mdl-eq.off{background:rgba(255,255,255,.06);border-color:var(--gd-line,#22253d);color:var(--text,#f4f5f7);font-weight:700}.mdl-eq:active{transform:scale(.97)}' +
    '.mdl-lock{position:relative;display:grid;place-items:center}.mdl-lock .gdm{filter:grayscale(1) brightness(.22) contrast(1.2);opacity:.55}.mdl-lock .gdm *{animation:none!important}.mdl-lock span{position:absolute;inset:0;display:grid;place-items:center;font-size:24px;font-weight:900;color:rgba(255,255,255,.7);text-shadow:0 2px 8px rgba(0,0,0,.7)}.mdl-c.lk{background:rgba(255,255,255,.02)}.mdl-c.lk b{color:var(--muted,#96a0a8)}' +
    '.mdl-rw{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:4px 10px;align-items:center;margin:0 0 10px;padding:11px 12px;border-radius:16px;background:rgba(71,176,245,.07);border:1px solid rgba(71,176,245,.28)}.mdl-rw b{font-size:13px}.mdl-rwp{grid-column:1;font-size:12.5px;color:#9ae9e3;font-weight:700}.mdl-rw small{grid-column:1;font-size:11.5px;color:var(--muted,#96a0a8);line-height:1.3}.mdl-rw .mdl-eq{grid-column:2;grid-row:1/4;width:auto;min-width:96px;margin:0;padding:0 12px}.mdl-rw .mdl-eq:disabled{opacity:.55}.mdl-star{position:absolute;top:5px;left:6px;font-size:13px;color:#ffd34d;text-shadow:0 1px 4px rgba(0,0,0,.7)}.mdl-slot.on{cursor:pointer}.mdl-slot em.mn{opacity:1;color:#ffd34d;font-size:11px;font-weight:800;margin-top:1px}.mdl-slot{position:relative}.mdl-cnt{position:absolute;top:6px;right:8px;font-size:10.5px;font-weight:800;color:#9ae9e3;font-style:normal}';
  var RARS = ['common', 'rare', 'epic', 'legendary'], RPL = {common: 'Обычные', rare: 'Редкие', epic: 'Эпические', legendary: 'Легендарные'};
  var state = {q: '', f: 'all', own: {}, max: 3, shown: {}, cols: [], events: {}, main: ''};
  function modal() {
    var m = $('mdlModal');
    if (m) return m;
    m = document.createElement('div'); m.id = 'mdlModal'; m.className = 'modal mdl-modal hidden'; m.setAttribute('role', 'dialog'); m.setAttribute('aria-modal', 'true');
    m.innerHTML = '<div class="modal-card mdl-card"><button type="button" class="modal-x" aria-label="Закрыть">×</button><h2>Коллекция медалей</h2><p class="mdl-meta" id="mdlMeta"></p><div class="mdl-body">' +
      '<div class="mdl-slots" id="mdlSlots"></div>' +
      '<div class="mdl-filt" id="mdlF"></div><div id="mdlList"></div></div></div>';
    m.addEventListener('click', function (e) { if (e.target === m || e.target.classList.contains('modal-x')) { m.classList.add('hidden'); document.documentElement.classList.remove('mdl-ns'); } });
    document.body.appendChild(m); return m;
  }
  function collection() {
    return (GDMedals.LIST || []).filter(function (m) { return !m.hidden || state.own[m.key]; });
  }
  function medalCard(m) {
    var o = state.own[m.key], r = m.rarity || 'common';
    if (!o) return '<div class="mdl-c lk" data-k="' + m.key + '"><div class="mdl-lock">' + GDMedals.svg(m.key, 62, true) + '<span>?</span></div><b>???</b><small>Не найдена</small></div>';
    var on = !!state.shown[m.key];
    return '<div class="mdl-c' + (on ? ' on' : '') + '" data-k="' + m.key + '">' + (on && state.main === m.key ? '<i class="mdl-star"><img src="/static/img/star.png" alt=""></i>' : '') + (o.n > 1 ? '<i class="mdl-cnt">×' + o.n + '</i>' : '') + GDMedals.svg(m.key, 62) + '<b>' + esc(m.name) + '</b>' + rarTag(r) +
      '<button type="button" class="mdl-eq' + (on ? ' off' : '') + '" data-k="' + m.key + '">' + (on ? 'Снять' : 'Надеть') + '</button></div>';
  }
  function paint() {
    var all = collection(), got = all.filter(function (m) { return state.own[m.key]; }).length, shownN = Object.keys(state.shown).length;
    $('mdlMeta').innerHTML = 'Собрано <b>' + got + ' из ' + all.length + '</b> · надето <b>' + shownN + ' из ' + state.max + '</b>';
    var slots = '', keys = Object.keys(state.shown), i, mk = state.shown[state.main] ? state.main : keys[keys.length - 1];
    keys.sort(function (a, b) { return (a === mk ? -1 : 0) - (b === mk ? -1 : 0); });
    for (i = 0; i < state.max; i++) slots += keys[i] ? '<div class="mdl-slot on" data-main="' + keys[i] + '">' + GDMedals.svg(keys[i], 52) + '<span>' + esc(GDMedals.name(keys[i])) + '</span>' + (keys[i] === mk ? '<em class="mn"><img src="/static/img/star.png" alt="">у ника</em>' : '<em class="mn" style="color:#8d99a8">сделать главной</em>') + '</div>' : '<div class="mdl-slot"><em>＋</em><span>Свободно</span></div>';
    $('mdlSlots').innerHTML = slots;
    $('mdlF').innerHTML = [['all', 'Все'], ['own', 'Мои'], ['lock', 'Не найденные']].map(function (f) { return '<button type="button" data-f="' + f[0] + '" class="' + (state.f === f[0] ? 'on' : '') + '">' + f[1] + '</button>'; }).join('');
    var q = state.q, html = '', evOf = state.events;
    function pass(m) {
      var o = !!state.own[m.key];
      if (state.f === 'own' && !o) return false;
      if (state.f === 'lock' && o) return false;
      if (q) { if (!o) return false; if (m.name.toLowerCase().indexOf(q) < 0) return false; }   // not found medals stay secret
      return true;
    }
    function reward(c) {
      if (!c || !c.reward) return '';
      var r = c.reward, parts = [];
      if (r.tickets) parts.push(r.tickets + ' билетов'); if (r.ton) parts.push(r.ton + ' TON'); if (r.medal) parts.push('медаль «' + esc(r.medal_name) + '»'); if (r.gift) parts.push(esc(r.gift.name));
      var btn = c.claimed ? '<button type="button" class="mdl-eq off" disabled>Получено</button>' : '<button type="button" class="mdl-eq mdl-claim" data-scope="' + esc(c.scope) + '"' + (c.ready ? '' : ' disabled') + '>' + (c.ready ? 'Забрать' : c.owned + ' / ' + c.need) + '</button>';
      return '<div class="mdl-rw"><b>' + (c.claimed ? 'Награда получена' : 'Награда за коллекцию') + '</b><span class="mdl-rwp">' + parts.join(' + ') + '</span><small>' + (c.claimed ? 'Спасибо, что собрали коллекцию!' : c.ready ? 'Условие выполнено — заберите награду.' : 'Нужно ' + c.need + ' из ' + c.total + (c.percent < 100 ? ' (' + c.percent + '%)' : '') + ' · у вас ' + c.owned) + '</small>' + btn + '</div>';
    }
    var colOf = {}; state.cols.forEach(function (c) { colOf[c.scope] = c; });
    if (colOf['*'] && colOf['*'].reward && state.f === 'all') html += reward(colOf['*']);
    RARS.forEach(function (r) {
      var items = all.filter(function (m) { return !evOf[m.key] && (m.rarity || 'common') === r && pass(m); });
      if (!items.length) return;
      var have = items.filter(function (m) { return state.own[m.key]; }).length;
      html += '<div class="mdl-h">' + rarTag(r).replace(/>[^<]*</, '>' + RPL[r] + '<') + '<small>' + have + ' / ' + items.length + '</small></div><div class="mdl-grid">' + items.map(medalCard).join('') + '</div>';
    });
    var evs = {}; all.forEach(function (m) { if (evOf[m.key]) (evs[evOf[m.key]] = evs[evOf[m.key]] || []).push(m); });
    Object.keys(evs).forEach(function (ev) {
      var items = evs[ev].filter(pass).sort(function (a, b) { return RARS.indexOf(a.rarity || 'common') - RARS.indexOf(b.rarity || 'common'); });
      if (!items.length) return;
      var have = items.filter(function (m) { return state.own[m.key]; }).length;
      html += '<div class="mdl-sec"><div class="mdl-h"><span class="rar">Коллекция · ' + esc(ev) + '</span><small>' + have + ' / ' + evs[ev].length + '</small></div>' + (state.f === 'all' ? reward(colOf[ev]) : '') + '<div class="mdl-grid">' + items.map(medalCard).join('') + '</div></div>';
    });
    $('mdlList').innerHTML = html || '<div class="mdl-empty">' + (q ? 'Среди найденных медалей ничего не нашлось.' : 'Здесь пока пусто.') + '</div>';
  }
  function load() {
    return Promise.all([api('/api/medals'), GDMedals.ready]).then(function (r) {
      var d = r[0]; state.max = d.max_shown || 3; state.own = {}; state.shown = {}; state.cols = d.collections || []; state.events = d.events || {}; state.main = d.main || '';
      d.medals.forEach(function (x) { var o = state.own[x.key] || (state.own[x.key] = {n: 0}); o.n++; if (x.shown) state.shown[x.key] = 1; });
    });
  }
  function open() {
    var m = modal(); m.classList.remove('hidden'); document.documentElement.classList.add('mdl-ns');
    var list = $('mdlList'); list.innerHTML = '<div class="mdl-empty">Загрузка…</div>';
    if (!m.dataset.bound) {
      m.dataset.bound = '1';
      m.addEventListener('click', function (e) {
        var f = e.target.closest('[data-f]'); if (f) { state.f = f.dataset.f; paint(); return; }
        var sl = e.target.closest('[data-main]');
        if (sl) { api('/api/medals/main', {key: sl.dataset.main}).then(function () { state.main = sl.dataset.main; paint(); if (window.GDMedalsMainChanged) GDMedalsMainChanged(state.main); }).catch(function (er) { if (window.toast) toast(er.message); }); return; }
        var cl = e.target.closest('.mdl-claim');
        if (cl) { cl.disabled = true; api('/api/medals/collection/claim', {scope: cl.dataset.scope}).then(function () { if (window.toast) toast('Награда получена'); return load(); }).then(paint).then(function () { try { if (typeof api === 'function' && typeof setUser === 'function') fetch('/api/me', {credentials: 'same-origin'}).then(function (r) { return r.json(); }).then(function (d) { if (d && d.user) setUser(d.user); }); } catch (x) {} }).catch(function (er) { cl.disabled = false; if (window.toast) toast(er.message); }); return; }
        var b = e.target.closest('.mdl-eq'); if (!b) return;
        var k = b.dataset.k, on = !state.shown[k]; b.disabled = true;
        api('/api/medals/equip', {key: k, on: on}).then(function () { if (on) state.shown[k] = 1; else delete state.shown[k]; paint(); })
          .catch(function (er) { b.disabled = false; if (window.toast) toast(er.message); });
      });
    }
    load().then(paint).catch(function (e) { list.innerHTML = '<div class="mdl-empty">' + esc(e.message) + '</div>'; });
  }
  function mountRow() {
    if ($('pfMedals')) return true;
    var anchor = $('pfHaptics'); if (!anchor) return false;
    var row = document.createElement('div'); row.className = 'pf-row pf-row-btn'; row.id = 'pfMedals';
    row.innerHTML = '<span class="pf-row-t"><b>Коллекция медалей</b><small>Собирайте и надевайте до 3</small></span><button type="button" class="profile-settings pf-open" aria-label="Мои медали" style="display:grid;place-items:center">' + GDMedals.svg('hw1', 30, true) + '</button>';
    row.querySelector('button').onclick = open; row.onclick = function (e) { if (e.target.closest('button')) return; open(); };
    anchor.parentNode.insertBefore(row, anchor); return true;
  }
  function paintPublic(d) {
    var chip = $('ppTopChip'); if (!chip) return;
    var box = $('ppMedals');
    if (!box) { box = document.createElement('div'); box.id = 'ppMedals'; box.className = 'pp-medals'; var host = chip.parentElement; host.parentNode.insertBefore(box, host.nextSibling); }
    box.innerHTML = ((d && d.medals) || []).map(function (m) { return '<span title="' + esc(m.name + ' · ' + m.title + (m.place ? ' · ' + m.place + ' место' : '')) + '">' + GDMedals.svg(m.key, 52) + '</span>'; }).join('');
  }
  var orig = window.openPublicProfile;
  if (typeof orig === 'function') {
    window.openPublicProfile = async function () {
      var r = await orig.apply(this, arguments);
      try { paintPublic(window.PP_D || (typeof PP_D !== 'undefined' ? PP_D : null)); } catch (e) {}
      return r;
    };
  }
  var showing = false;
  function pending() {
    if (showing) return;
    api('/api/medals/pending').then(function (d) {
      var m = d.medals && d.medals[0]; if (!m || showing) return;
      showing = true;
      var el = document.createElement('div'); el.className = 'mgt-modal'; el.setAttribute('role', 'dialog');
      el.innerHTML = '<div class="mgt-card"><span class="mgt-k">Вам выдана медаль</span><div>' + GDMedals.svg(m.key, 170) + '</div><h3>' + esc(m.name) + '</h3>' + rarTag(m.rarity) + '<p>' + esc(m.title || 'Особая награда') + (m.place ? ' · ' + m.place + ' место' : '') + '</p><button type="button" class="mgt-btn">Получить</button></div>';
      el.querySelector('button').onclick = function () {
        var b = this; b.disabled = true;
        api('/api/medals/' + m.id + '/claim', {}).catch(function () {}).then(function () { el.remove(); showing = false; setTimeout(pending, 300); });
      };
      document.body.appendChild(el);
    }).catch(function () {});
  }
  window.GDMedalsCheck = pending;
  var ready = window.GDMedals.ready || Promise.resolve();
  setTimeout(function () { ready.then(pending); }, 2500);
  setInterval(function () { if (!document.hidden) pending(); }, 45000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) setTimeout(pending, 800); });
  if (!mountRow()) document.addEventListener('DOMContentLoaded', mountRow);
  setTimeout(mountRow, 800);
})();
