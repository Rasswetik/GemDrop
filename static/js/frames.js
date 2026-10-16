/* Profile frames: music medals give an avatar frame. Customisation screen = medals + frames. */
(function () {
  'use strict';
  var KEYS = ['jealous', 'warhol', 'poets', 'vklube', 'kashtan'], N = 0, S = {selected: '', owned: [], frames: [], ok: false};
  var NAMES = {jealous: 'Шипы', warhol: 'Поп-арт', poets: 'Лавр поэта', vklube: 'Неон клуба', kashtan: 'Золотая цепь'};
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]; }); }
  function api(url, body) {
    return fetch(url, {method: body ? 'POST' : 'GET', credentials: 'same-origin', headers: body ? {'Content-Type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined})
      .then(function (r) { return r.json().then(function (j) { if (!r.ok || j.ok === false) throw new Error(j.error || 'Ошибка'); return j; }); });
  }
  var st = document.createElement('style');
  st.textContent = '.gdfr{overflow:visible!important;border-radius:50%!important;position:relative!important}.gdfr>img:not(.gdf-svg){border-radius:50%}.gdf-svg{position:absolute;left:-59.5%;top:-59.5%;width:219%;height:219%;pointer-events:none;z-index:4;max-width:none!important;border-radius:0!important;object-fit:contain!important}' +
    '@keyframes gdfPulse{0%,100%{opacity:.75}50%{opacity:1}}@keyframes gdfSpin{to{transform:rotate(360deg)}}.gdf-pulse{animation:gdfPulse 1.8s ease-in-out infinite}.gdf-spin{transform-origin:0 0;animation:gdfSpin 14s linear infinite}' +
    '.gdc{position:fixed;inset:0;z-index:2147483100;display:grid;place-items:center;padding:16px;background:rgba(5,4,12,.72);overscroll-behavior:contain}.gdc-c{position:relative;width:min(100%,420px);max-height:calc(100dvh - 32px);overflow:auto;border-radius:24px;padding:20px 16px 18px;background:var(--card,#14162a);border:1px solid var(--gd-line,#22253d);color:var(--text,#f4f5f7)}' +
    '.gdc-c h2{margin:0 36px 4px 4px;font-size:22px}.gdc-x{position:absolute;top:14px;right:14px;width:32px;height:32px;border-radius:50%;border:0;background:rgba(255,255,255,.08);color:inherit;font-size:18px;cursor:pointer;padding:0}' +
    '.gdc-tabs{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:12px 0 14px}.gdc-tabs button{height:42px;border-radius:14px;border:1px solid var(--gd-line,#22253d);background:rgba(255,255,255,.04);color:inherit;font-weight:800;font-size:14px;cursor:pointer}.gdc-tabs button.on{background:linear-gradient(135deg,#8be9f0,#47b0f5);color:#08121c;border-color:transparent}' +
    '.gdc-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.gdc-card{display:grid;justify-items:center;gap:6px;padding:16px 8px 12px;border-radius:18px;background:rgba(255,255,255,.04);border:1px solid var(--gd-line,#22253d);text-align:center;cursor:pointer;color:inherit;font:inherit}' +
    '.gdc-card.on{border-color:#47b0f5;box-shadow:0 0 0 2px rgba(71,176,245,.25) inset}.gdc-card.lk{opacity:.55;cursor:default}.gdc-card b{font-size:14px}.gdc-card small{font-size:12px;color:var(--muted,#96a0a8)}' +
    '.gdc-av{position:relative;width:72px;height:72px;margin:18px 0 20px;border-radius:50%;background:#242841;display:grid;place-items:center;font-size:28px;font-weight:800;color:#47b0f5}.gdc-av img{width:100%;height:100%;object-fit:cover;border-radius:50%}' +
    '.gdc-p{margin:6px 2px 0;font-size:13px;line-height:1.4;color:var(--muted,#96a0a8)}.gdc-big{width:100%;height:46px;margin-top:12px;border-radius:14px;border:0;background:linear-gradient(135deg,#8be9f0,#47b0f5);color:#08121c;font-weight:900;font-size:15px;cursor:pointer}' +
    '.gdc-lk{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}.gdc-lk span{font-size:12px;padding:4px 9px;border-radius:999px;background:rgba(255,255,255,.06);color:var(--muted,#96a0a8)}';
  document.head.appendChild(st);

  /* every medal has its own 3D frame: motif ring around the avatar, rendered once by the WebGL renderer into a transparent PNG */
  var SPEC = {
    jealous: {m: 'spikes', n: 20, p: 'black', ring: '#2b2e3b'}, warhol: {m: 'studs', n: 16, p: 'blue'}, poets: {m: 'leaves', n: 14, p: 'red', leaf: '#ffcf5a'}, vklube: {m: 'neon', n: 4, p: 'black', ring: '#1a1030'}, kashtan: {m: 'links', n: 22, p: 'gold'},
    goshan: {m: 'play', n: 8, p: 'red'}, kess: {m: 'star', n: 10, p: 'silver'}, nickolay: {m: 'gift', n: 8, p: 'blue'}, kuzmatagin: {m: 'cup', n: 8, p: 'bronze'},
    hw1: {m: 'pumpkin', n: 8, p: 'gold'}, hw2: {m: 'ghost', n: 8, p: 'silver'}, hw3: {m: 'bat', n: 8, p: 'bronze'}, hw4: {m: 'hat', n: 8, p: 'violet'}, hw5: {m: 'skull', n: 8, p: 'teal'},
    vip: {m: 'crown', n: 8, p: 'violet'}, gem: {m: 'diamond', n: 8, p: 'cyan'}, champ: {m: 'trophy', n: 8, p: 'gold'}, pioneer: {m: 'flag', n: 8, p: 'orange'}, insider: {m: 'key', n: 8, p: 'cyan'}, friend: {m: 'heart', n: 8, p: 'pink'}, guard: {m: 'shield', n: 8, p: 'silver'}, collector: {m: 'gift', n: 8, p: 'red'},
    c1: {m: 'studs', n: 16, p: 'gold', mono: 1}, c2: {m: 'studs', n: 16, p: 'silver', mono: 1}, c3: {m: 'studs', n: 16, p: 'bronze', mono: 1},
    ton: {m: 'diamond', n: 8, p: 'blue'}, lucky: {m: 'leaves', n: 14, p: 'green', leaf: '#7bd651'}, fire: {m: 'spikes', n: 18, p: 'orange'}, bolt: {m: 'star', n: 10, p: 'gold'}, rocket: {m: 'star', n: 10, p: 'blue'},
    gambler: {m: 'dice', n: 8, p: 'red'}, miner: {m: 'studs', n: 16, p: 'green', mono: 1}, cards: {m: 'star', n: 10, p: 'obsidian'}, night: {m: 'star', n: 10, p: 'night'}
  };
  function hexRgb(h) { return [parseInt(h.substr(1, 2), 16) / 255, parseInt(h.substr(3, 2), 16) / 255, parseInt(h.substr(5, 2), 16) / 255]; }
  function specOf(key) {
    var sp = SPEC[key]; if (!sp) { var m = window.GDMedals && GDMedals.by && GDMedals.by(key); sp = {m: 'studs', n: 16, p: (m && m.pal) || 'gold', mono: 1}; }
    var pal = (window.GDMedals && GDMedals.PAL && (GDMedals.PAL[sp.p] || GDMedals.PAL.gold)) || ['#fff3b0', '#ffc933', '#a86a05', '#ffe58a'];
    var o = {m: sp.m, n: sp.n, pal: pal, ring: sp.ring, leaf: sp.leaf}; if (sp.mono) o.cols = [hexRgb(pal[3]), hexRgb(pal[1])]; return o;
  }
  function has(k) { return true; }
  var CACHE = {}, QUEUE = [], BUSY = false;
  function snap(key) {
    if (CACHE[key]) return CACHE[key];
    return CACHE[key] = new Promise(function (res) { QUEUE.push([key, res]); pump(); });
  }
  function pump() {
    if (BUSY || !QUEUE.length) return; BUSY = true; var job = QUEUE.shift(), done = false;
    function fin(u) { if (done) return; done = true; BUSY = false; job[1](u || ''); if (!u) delete CACHE[job[0]]; setTimeout(pump, 20); }
    try {
      if (!window.GDMedal3D) return fin('');
      GDMedal3D.mount(document.createElement('div'), {frame: specOf(job[0]), w: 512, h: 512, snap: function (u) { fin(u && u.length > 3000 ? u : ''); }, onfail: function () { fin(''); }});
      setTimeout(function () { fin(''); }, 9000);
    } catch (e) { fin(''); }
  }
  function apply(el, key) {
    if (!el) return; var cur = el.getAttribute('data-fr') || '';
    if ((key || '') === cur && (key ? el.querySelector(':scope > .gdf-svg') : true)) return;
    var old = el.querySelector(':scope > .gdf-svg'); if (old) old.remove();
    el.setAttribute('data-fr', key || '');
    if (!key) { el.classList.remove('gdfr'); return; }
    el.classList.add('gdfr');
    snap(key).then(function (u) {
      if (!u || el.getAttribute('data-fr') !== key || el.querySelector(':scope > .gdf-svg')) return;
      el.insertAdjacentHTML('beforeend', '<img class="gdf-svg" alt="" src="' + u + '">');
    });
  }
  function svg(key) { return CACHE[key] && CACHE[key].u ? '' : ''; }
  function load() { return api('/api/frames').then(function (d) { S = {selected: d.selected || '', owned: d.owned || [], frames: d.frames || [], ok: true}; return S; }); }
  function visible(el) { return el && el.offsetParent !== null; }
  function tick() {
    var own = document.getElementById('bigAvatar');
    if (own && visible(own) && S.ok) apply(own, S.selected);
    var pub = document.getElementById('publicProfileAvatar');
    if (pub && visible(pub)) { var d = window.PP_D || (typeof PP_D !== 'undefined' ? PP_D : null); apply(pub, d && d.frame ? d.frame : ''); }
  }
  setInterval(tick, 700);
  var started = false; function boot() { if (started) return; started = true; load().then(tick).catch(function () { started = false; }); }
  document.addEventListener('click', boot, true); setTimeout(boot, 2500);

  function ownImg() { var i = document.querySelector('#bigAvatar img'); return i ? i.src : ''; }
  function ownLetter() { var a = document.getElementById('bigAvatar'); return a && !a.querySelector('img') ? (a.textContent || '').trim().slice(0, 1) : ''; }
  function avHtml(key, extra) { var im = ownImg(); return '<div class="gdc-av" data-pv="' + (key || '') + '"' + (extra || '') + '>' + (im ? '<img alt="" src="' + esc(im) + '">' : esc(ownLetter() || '👤')) + '</div>'; }
  function overlay(inner) {
    var el = document.createElement('div'); el.className = 'gdc'; el.setAttribute('role', 'dialog'); el.innerHTML = '<div class="gdc-c"><button type="button" class="gdc-x" aria-label="Закрыть">×</button>' + inner + '</div>';
    el.addEventListener('click', function (e) { if (e.target === el || e.target.classList.contains('gdc-x')) el.remove(); });
    document.body.appendChild(el); return el;
  }
  function paintPreviews(root) { Array.prototype.forEach.call(root.querySelectorAll('.gdc-av[data-pv]'), function (e) { if (e.dataset.pv) apply(e, e.dataset.pv); }); }
  function nameOf(k) { var m = window.GDMedals && GDMedals.by && GDMedals.by(k); return (m && m.name) || k; }
  /* Frames tab (rendered inside the medal "Кастомизация" window) */
  function paint(body) {
    function draw() {
      var list = (S.frames && S.frames.length) ? S.frames : S.owned.map(function (k) { return {key: k, name: nameOf(k), owned: true}; });
      var own = list.filter(function (f) { return f.owned; }), lk = list.filter(function (f) { return !f.owned; });
      var h = '<div class="gdc-grid"><button type="button" class="gdc-card' + (!S.selected ? ' on' : '') + '" data-k="">' + avHtml('') + '<b>Без рамки</b><small>' + (!S.selected ? 'Надета' : 'Выбрать') + '</small></button>';
      own.forEach(function (f) { h += '<button type="button" class="gdc-card' + (S.selected === f.key ? ' on' : '') + '" data-k="' + esc(f.key) + '">' + avHtml(f.key) + '<b>' + esc(f.name || nameOf(f.key)) + '</b><small>' + (S.selected === f.key ? 'Надета' : 'Выбрать') + '</small></button>'; });
      h += '</div>';
      if (lk.length) { h += '<p class="gdc-p" style="margin-top:14px"><b>Ещё не получены</b> — у каждой медали своя 3D-рамка, она открывается вместе с медалью:</p><div class="gdc-lk">' + lk.map(function (f) { return '<span>' + esc(f.name || nameOf(f.key)) + '</span>'; }).join('') + '</div>'; }
      body.innerHTML = h; paintPreviews(body);
    }
    draw(); load().then(draw).catch(function () {});
    if (!body._fr) { body._fr = 1; body.addEventListener('click', function (e) {
      var c = e.target.closest('.gdc-card[data-k]'); if (!c) return; var k = c.dataset.k;
      api('/api/frames/select', {key: k}).then(function (d) { S.selected = d.selected || ''; draw(); tick(); }).catch(function (er) { if (window.toast) toast(er.message); });
    }); }
  }
  function openCustomize(start) { if (window.GDMedalsOpen) window.GDMedalsOpen(start === 'f' ? 'f' : 'm'); }
  function gained(key) {
    load().catch(function () {}).then(function () {
      var el = overlay('<h2 style="text-align:center;margin:0 0 6px">Вы получили рамку</h2><div style="display:grid;place-items:center">' + avHtml(key) + '</div><p class="gdc-p" style="text-align:center"><b style="color:inherit">' + esc(nameOf(key)) + '</b> — 3D-рамка вокруг вашего аватара в профиле.</p><button type="button" class="gdc-big" id="gdcOn">Надеть</button><button type="button" class="gdc-big" id="gdcLater" style="background:rgba(255,255,255,.08);color:inherit;margin-top:8px">Позже</button>');
      paintPreviews(el);
      el.querySelector('#gdcLater').onclick = function () { el.remove(); };
      el.querySelector('#gdcOn').onclick = function () { api('/api/frames/select', {key: key}).then(function (d) { S.selected = d.selected; tick(); el.remove(); }).catch(function (er) { if (window.toast) toast(er.message); }); };
    });
  }
  window.GDFrames = {svg: svg, apply: apply, open: openCustomize, paint: paint, gained: gained, has: has, load: load, tick: tick, snap: snap, state: function () { return S; }};
})();
