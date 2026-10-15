/* Player side of the season medals: «Мои медали» in the profile settings and medals on the public profile. */
(function () {
  if (!window.GDMedals) return;
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (v) { return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); };
  var st = document.createElement('style');
  st.textContent = '.mdl-modal{position:fixed;inset:0;z-index:2600;display:flex;align-items:flex-end;justify-content:center;background:rgba(5,4,12,.66);backdrop-filter:blur(6px)}.mdl-modal.hidden{display:none}' +
    '.mdl-card{width:min(100%,520px);max-height:88dvh;overflow:auto;padding:18px 16px calc(18px + env(safe-area-inset-bottom));border-radius:26px 26px 0 0;background:#14161d;border:1px solid rgba(255,255,255,.08);color:#f2f4f8}' +
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
  st.textContent += '.mdl-card{width:min(100%,560px);height:min(88dvh,820px);display:flex;flex-direction:column;overflow:hidden;padding-bottom:0}.mdl-body{overflow:auto;flex:1;margin:0 -16px;padding:0 16px calc(18px + env(safe-area-inset-bottom));-webkit-overflow-scrolling:touch}' +
    '.mdl-prog{display:grid;gap:6px;margin:2px 0 10px}.mdl-prog div{height:8px;border-radius:99px;background:rgba(255,255,255,.08);overflow:hidden}.mdl-prog i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,#7a3fe0,#b27dff,#6ac8ff);transition:width .5s}.mdl-prog span{font-size:12.5px;color:#9aa6b6}.mdl-prog b{color:#fff}' +
    '.mdl-slots{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px}.mdl-slot{display:grid;justify-items:center;align-content:center;gap:2px;min-height:112px;padding:8px 4px;border-radius:18px;border:1.5px dashed rgba(255,255,255,.16);background:rgba(255,255,255,.03);font-size:11px;color:#6f7b8c;text-align:center}.mdl-slot.on{border-style:solid;border-color:rgba(178,125,255,.45);background:rgba(160,102,255,.09);color:#e8dcff}.mdl-slot em{font-style:normal;font-size:22px;opacity:.5}' +
    '.mdl-search{width:100%;box-sizing:border-box;height:42px;margin-bottom:6px;padding:0 14px;border-radius:14px;border:1px solid rgba(255,255,255,.1);background:#1b1e27;color:#fff;font:inherit;font-size:14px;outline:0}.mdl-search:focus{border-color:#a066ff}' +
    '.mdl-filt{display:flex;gap:6px;overflow:auto;padding:4px 0 8px;margin-bottom:2px}.mdl-filt button{flex:none;height:30px;padding:0 12px;border-radius:99px;border:1px solid rgba(255,255,255,.1);background:transparent;color:#9aa6b6;font:inherit;font-size:12.5px;font-weight:700;cursor:pointer}.mdl-filt button.on{background:#a066ff;border-color:#a066ff;color:#fff}' +
    '.mdl-h{display:flex;align-items:center;justify-content:space-between;margin:14px 0 8px;font-size:13px;font-weight:800;letter-spacing:.04em;text-transform:uppercase}.mdl-h small{font-weight:600;color:#8d99a8;text-transform:none;letter-spacing:0}' +
    '.mdl-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.mdl-c{position:relative;display:grid;justify-items:center;align-content:start;gap:3px;padding:10px 6px 10px;border-radius:18px;background:#1b1e27;border:1px solid rgba(255,255,255,.06);text-align:center;min-width:0}.mdl-c.on{border-color:rgba(178,125,255,.55);box-shadow:0 0 0 1px rgba(178,125,255,.25) inset}' +
    '.mdl-c b{font-size:12.5px;line-height:1.2;max-width:100%;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}.mdl-c small{font-size:10.5px;color:#8d99a8;line-height:1.2}' +
    '.mdl-eq{margin-top:4px;width:100%;height:30px;border:0;border-radius:11px;background:linear-gradient(180deg,#b27dff,#7a3fe0);color:#fff;font:inherit;font-size:12.5px;font-weight:800;cursor:pointer}.mdl-eq.off{background:rgba(255,255,255,.1);color:#d5dbe6}.mdl-eq:active{transform:scale(.97)}' +
    '.mdl-lock{position:relative;display:grid;place-items:center}.mdl-lock .gdm{filter:grayscale(1) brightness(.22) contrast(1.2);opacity:.55}.mdl-lock .gdm *{animation:none!important}.mdl-lock span{position:absolute;inset:0;display:grid;place-items:center;font-size:26px;font-weight:900;color:rgba(255,255,255,.7);text-shadow:0 2px 8px rgba(0,0,0,.7)}.mdl-c.lk{background:#15171e;opacity:.92}.mdl-c.lk b{color:#6f7b8c}' +
    '.mdl-cnt{position:absolute;top:6px;right:8px;font-size:10.5px;font-weight:800;color:#c9a3ff}';
  var RARS = ['legendary', 'epic', 'rare', 'common'], RPL = {common: 'Обычные', rare: 'Редкие', epic: 'Эпические', legendary: 'Легендарные'};
  var state = {q: '', f: 'all', own: {}, max: 3, shown: {}};
  function modal() {
    var m = $('mdlModal');
    if (m) return m;
    m = document.createElement('div'); m.id = 'mdlModal'; m.className = 'mdl-modal hidden'; m.setAttribute('role', 'dialog'); m.setAttribute('aria-modal', 'true');
    m.innerHTML = '<div class="mdl-card"><div class="mdl-top"><h2>Коллекция медалей</h2><button type="button" class="mdl-x" aria-label="Закрыть">×</button></div><div class="mdl-body">' +
      '<div class="mdl-prog" id="mdlProg"></div><p class="mdl-sub" id="mdlSub"></p><div class="mdl-slots" id="mdlSlots"></div>' +
      '<input class="mdl-search" id="mdlQ" type="search" placeholder="Поиск медали" autocomplete="off"><div class="mdl-filt" id="mdlF"></div><div id="mdlList"></div></div></div>';
    m.addEventListener('click', function (e) { if (e.target === m || e.target.classList.contains('mdl-x')) m.classList.add('hidden'); });
    m.querySelector('#mdlQ').addEventListener('input', function () { state.q = this.value.trim().toLowerCase(); paint(); });
    document.body.appendChild(m); return m;
  }
  function collection() {
    return (GDMedals.LIST || []).filter(function (m) { return !m.hidden || state.own[m.key]; });
  }
  function medalCard(m) {
    var o = state.own[m.key], r = m.rarity || 'common';
    if (!o) return '<div class="mdl-c lk" data-k="' + m.key + '"><div class="mdl-lock">' + GDMedals.svg(m.key, 62, true) + '<span>?</span></div><b>???</b><small>Не найдена</small></div>';
    var on = !!state.shown[m.key];
    return '<div class="mdl-c' + (on ? ' on' : '') + '" data-k="' + m.key + '">' + (o.n > 1 ? '<i class="mdl-cnt">×' + o.n + '</i>' : '') + GDMedals.svg(m.key, 62) + '<b>' + esc(m.name) + '</b>' + rarTag(r) +
      '<button type="button" class="mdl-eq' + (on ? ' off' : '') + '" data-k="' + m.key + '">' + (on ? 'Снять' : 'Надеть') + '</button></div>';
  }
  function paint() {
    var all = collection(), got = all.filter(function (m) { return state.own[m.key]; }).length, shownN = Object.keys(state.shown).length;
    $('mdlProg').innerHTML = '<span>Собрано <b>' + got + '</b> из <b>' + all.length + '</b></span><div><i style="width:' + (all.length ? Math.round(got * 100 / all.length) : 0) + '%"></i></div>';
    $('mdlSub').textContent = 'Наденьте до ' + state.max + ' медалей — они будут видны в вашем профиле.';
    var slots = '', keys = Object.keys(state.shown), i;
    for (i = 0; i < state.max; i++) slots += keys[i] ? '<div class="mdl-slot on">' + GDMedals.svg(keys[i], 56) + '<span>' + esc(GDMedals.name(keys[i])) + '</span></div>' : '<div class="mdl-slot"><em>＋</em><span>Свободно</span></div>';
    $('mdlSlots').innerHTML = slots;
    $('mdlF').innerHTML = [['all', 'Все'], ['own', 'Мои'], ['lock', 'Не найденные']].map(function (f) { return '<button type="button" data-f="' + f[0] + '" class="' + (state.f === f[0] ? 'on' : '') + '">' + f[1] + '</button>'; }).join('');
    var q = state.q, html = '';
    RARS.forEach(function (r) {
      var items = all.filter(function (m) {
        if ((m.rarity || 'common') !== r) return false;
        var o = !!state.own[m.key];
        if (state.f === 'own' && !o) return false;
        if (state.f === 'lock' && o) return false;
        if (q) { if (!o) return false; if (m.name.toLowerCase().indexOf(q) < 0) return false; }   // not found medals stay secret
        return true;
      });
      if (!items.length) return;
      var have = items.filter(function (m) { return state.own[m.key]; }).length;
      html += '<div class="mdl-h">' + rarTag(r).replace(/>[^<]*</, '>' + RPL[r] + '<') + '<small>' + have + ' / ' + items.length + '</small></div><div class="mdl-grid">' + items.map(medalCard).join('') + '</div>';
    });
    $('mdlList').innerHTML = html || '<div class="mdl-empty">' + (q ? 'Среди найденных медалей ничего не нашлось.' : 'Здесь пока пусто.') + '</div>';
  }
  function load() {
    return Promise.all([api('/api/medals'), GDMedals.ready]).then(function (r) {
      var d = r[0]; state.max = d.max_shown || 3; state.own = {}; state.shown = {};
      d.medals.forEach(function (x) { var o = state.own[x.key] || (state.own[x.key] = {n: 0}); o.n++; if (x.shown) state.shown[x.key] = 1; });
    });
  }
  function open() {
    var m = modal(); m.classList.remove('hidden');
    var list = $('mdlList'); list.innerHTML = '<div class="mdl-empty">Загрузка…</div>';
    if (!m.dataset.bound) {
      m.dataset.bound = '1';
      m.addEventListener('click', function (e) {
        var f = e.target.closest('[data-f]'); if (f) { state.f = f.dataset.f; paint(); return; }
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
  var ready = window.GDMedals.ready || Promise.resolve();
  setTimeout(function () { ready.then(pending); }, 2500);
  setInterval(function () { if (!document.hidden) pending(); }, 45000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) setTimeout(pending, 800); });
  if (!mountRow()) document.addEventListener('DOMContentLoaded', mountRow);
  setTimeout(mountRow, 800);
})();
