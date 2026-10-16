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
  st.textContent = '.gdfr{overflow:visible!important;border-radius:50%!important;position:relative!important}.gdfr>img:not(.gdf-svg){border-radius:50%}.gdf-svg{position:absolute;left:-65.5%;top:-65.5%;width:231%;height:231%;pointer-events:none;z-index:4;max-width:none!important;border-radius:0!important;object-fit:contain!important}' +
    '@keyframes gdfPulse{0%,100%{opacity:.75}50%{opacity:1}}@keyframes gdfSpin{to{transform:rotate(360deg)}}.gdf-pulse{animation:gdfPulse 1.8s ease-in-out infinite}.gdf-spin{transform-origin:0 0;animation:gdfSpin 14s linear infinite}' +
    '.gdc{position:fixed;inset:0;z-index:2147483100;display:grid;place-items:center;padding:16px;background:rgba(5,4,12,.72);overscroll-behavior:contain}.gdc-c{position:relative;width:min(100%,420px);max-height:calc(100dvh - 32px);overflow:auto;border-radius:24px;padding:20px 16px 18px;background:var(--card,#14162a);border:1px solid var(--gd-line,#22253d);color:var(--text,#f4f5f7)}' +
    '.gdc-c h2{margin:0 36px 4px 4px;font-size:22px}.gdc-x{position:absolute;top:14px;right:14px;width:32px;height:32px;border-radius:50%;border:0;background:rgba(255,255,255,.08);color:inherit;font-size:18px;cursor:pointer;padding:0}' +
    '.gdc-tabs{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:12px 0 14px}.gdc-tabs button{height:42px;border-radius:14px;border:1px solid var(--gd-line,#22253d);background:rgba(255,255,255,.04);color:inherit;font-weight:800;font-size:14px;cursor:pointer}.gdc-tabs button.on{background:linear-gradient(135deg,#8be9f0,#47b0f5);color:#08121c;border-color:transparent}' +
    '.gdc-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.gdc-card{display:grid;justify-items:center;gap:6px;padding:16px 8px 12px;border-radius:18px;background:rgba(255,255,255,.04);border:1px solid var(--gd-line,#22253d);text-align:center;cursor:pointer;color:inherit;font:inherit}' +
    '.gdc-card.on{border-color:#47b0f5;box-shadow:0 0 0 2px rgba(71,176,245,.25) inset}.gdc-card.lk{opacity:.55;cursor:default}.gdc-card b{font-size:14px}.gdc-card small{font-size:12px;color:var(--muted,#96a0a8)}' +
    '.gdc-av{position:relative;width:72px;height:72px;margin:18px 0 20px;border-radius:50%;background:#242841;display:grid;place-items:center;font-size:28px;font-weight:800;color:#47b0f5}.gdc-av img:not(.gdf-svg){width:100%;height:100%;object-fit:cover;border-radius:50%}' +
    '.gdc-p{margin:6px 2px 0;font-size:13px;line-height:1.4;color:var(--muted,#96a0a8)}.gdc-big{width:100%;height:46px;margin-top:12px;border-radius:14px;border:0;background:linear-gradient(135deg,#8be9f0,#47b0f5);color:#08121c;font-weight:900;font-size:15px;cursor:pointer}' +
    '.gdc-lk{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}.gdc-lk span{font-size:12px;padding:4px 9px;border-radius:999px;background:rgba(255,255,255,.06);color:var(--muted,#96a0a8)}';
  document.head.appendChild(st);

  /* every medal has its own 3D frame: motif ring around the avatar, rendered once by the WebGL renderer into a transparent PNG */
  var SPEC = {
    jealous: {p: 'black', ring: '#23262f', r: 'plain', d: ['thorns', 'gems'], gem: '#ff2a3a'},
    warhol: {p: 'blue', r: 'beaded', cols: [[1, .1, .55], [1, .86, .05], [.05, .8, .95], [1, .45, .05]], h: 'star', hs: 1.25, d: ['sparkles']},
    poets: {p: 'red', d: ['wreath', 'bow'], leaf: '#ffcf5a', h: 'heart', hs: .7},
    vklube: {p: 'black', r: 'neon', d: ['rays', 'sparkles'], ring: '#2a1536'},
    kashtan: {p: 'gold', r: 'chain', h: 'crown', hs: 1.05, pend: 'diamond', d: ['gems']},
    goshan: {p: 'red', r: 'rope', h: 'play', hs: 1.15, d: ['rays', 'bow_r']},
    kess: {p: 'silver', r: 'faceted', d: ['wings'], h: 'star', hs: 1.15},
    nickolay: {p: 'blue', r: 'rope', h: 'gift', hs: 1.1, d: ['bow_r', 'sparkles']},
    kuzmatagin: {p: 'bronze', r: 'rope', h: 'cup', hs: 1.15, s: 'cup', d: ['gems'], gem: '#ffb15a'},
    hw1: {p: 'gold', h: 'pumpkin', hs: 1.1, s: 'pumpkin', d: ['wreath_g']},
    hw2: {p: 'silver', h: 'ghost', hs: 1.1, s: 'ghost', d: ['sparkles']},
    hw3: {p: 'bronze', h: 'bat', hs: 1.15, d: ['wings_d'], r: 'faceted'},
    hw4: {p: 'violet', h: 'hat', hs: 1.0, d: ['sparkles', 'gems'], gem: '#ffcf5a'},
    hw5: {p: 'teal', h: 'skull', hs: 1.1, d: ['flames']},
    vip: {p: 'violet', h: 'crown', hs: 1.3, d: ['rays', 'gems'], gem: '#ff4a6a'},
    gem: {p: 'cyan', r: 'faceted', h: 'diamond', hs: 1.2, s: 'diamond', d: ['sparkles']},
    champ: {p: 'gold', h: 'trophy', hs: 1.2, d: ['wreath', 'bow']},
    pioneer: {p: 'orange', r: 'rope', h: 'flag', hs: 1.25, d: ['rays']},
    insider: {p: 'cyan', r: 'faceted', h: 'key', hs: 1.25, d: ['sparkles']},
    friend: {p: 'pink', h: 'heart', hs: 1.2, s: 'heart', d: ['bow_r', 'sparkles']},
    guard: {p: 'silver', r: 'faceted', h: 'shield', hs: 1.2, d: ['wings']},
    collector: {p: 'red', r: 'beaded', h: 'gift', hs: 1.1, s: 'gift', d: ['gems']},
    c1: {p: 'gold', h: 'star', hs: 1.3, d: ['rays']},
    c2: {p: 'silver', h: 'star', hs: 1.3, d: ['wreath']},
    c3: {p: 'bronze', r: 'beaded', h: 'star', hs: 1.3},
    ton: {p: 'blue', r: 'faceted', h: 'diamond', hs: 1.25, d: ['sparkles', 'rays']},
    lucky: {p: 'green', h: 'clover', hs: 1.2, d: ['wreath_g'], r: 'rope'},
    fire: {p: 'orange', d: ['flames_o'], r: 'plain'},
    bolt: {p: 'gold', h: 'bolt', hs: 1.2, d: ['rays']},
    rocket: {p: 'blue', r: 'faceted', h: 'rocket', hs: 1.2, d: ['sparkles']},
    gambler: {p: 'red', r: 'beaded', h: 'dice', hs: 1.2, d: ['gems']},
    miner: {p: 'green', r: 'rope', h: 'pick', hs: 1.15, d: ['gems'], gem: '#66e6a0'},
    cards: {p: 'obsidian', r: 'faceted', h: 'cards', hs: 1.15, d: ['sparkles']},
    night: {p: 'night', r: 'double', h: 'moon', hs: 1.2, d: ['sparkles'], gem: '#cfd8ff'}
  };
  function hexRgb(h) { return [parseInt(h.substr(1, 2), 16) / 255, parseInt(h.substr(3, 2), 16) / 255, parseInt(h.substr(5, 2), 16) / 255]; }
  function specOf(key) {
    var sp = SPEC[key]; if (!sp) { var m = window.GDMedals && GDMedals.by && GDMedals.by(key); sp = {p: (m && m.pal) || 'gold', r: 'beaded', h: 'star', hs: 1.2}; }
    var pal = (window.GDMedals && GDMedals.PAL && (GDMedals.PAL[sp.p] || GDMedals.PAL.gold)) || ['#fff3b0', '#ffc933', '#a86a05', '#ffe58a'];
    var o = {}; for (var q in sp) o[q] = sp[q]; o.pal = pal; if (sp.cols && sp.cols[0] && sp.cols[0].length) o.cols = sp.cols; return o;
  }
  function has(k) { return true; }
  var CACHE = {}, QUEUE = [], BUSY = false;
  function snap(key) {
    if (CACHE[key]) return CACHE[key];
    return CACHE[key] = new Promise(function (res) { QUEUE.push([key, res]); pump(); });
  }
  function pump() {
    if (BUSY || !QUEUE.length) return;
    var rd = window.GDMedals && GDMedals.ready; if (rd && !pump.ok) { BUSY = true; rd.then(function () { pump.ok = 1; BUSY = false; pump(); }, function () { pump.ok = 1; BUSY = false; pump(); }); return; }
    BUSY = true; var job = QUEUE.shift(), done = false;
    function fin(u) { if (done) return; done = true; if (!u) { BUSY = false; job[1](''); delete CACHE[job[0]]; setTimeout(pump, 20); return; }
      var go = function (v) { BUSY = false; job[1](v); setTimeout(pump, 20); };
      try { fetch(u).then(function (r) { return r.blob(); }).then(function (b) { go(URL.createObjectURL(b)); }).catch(function () { go(u); }); } catch (e) { go(u); } }
    try {
      if (!window.GDMedal3D) return fin('');
      var SH = {warhol: {t: 'bead', n: 18, cols: [[1, .1, .55], [1, .86, .05], [.05, .8, .95], [1, .45, .05]]}, poets: {t: 'scallop', n: 14}, vklube: {t: 'double'}, goshan: {t: 'rope'}, kess: {t: 'facet', n: 8}, nickolay: {t: 'scallop', n: 10}, kuzmatagin: {t: 'wave', n: 16}, hw1: {t: 'scallop', n: 8}, hw2: {t: 'wave', n: 12}, hw3: {t: 'points', n: 12}, hw4: {t: 'bead', n: 16}, hw5: {t: 'gear', n: 16}, vip: {t: 'points', n: 9, h: .011}, gem: {t: 'facet', n: 8}, champ: {t: 'rope'}, pioneer: {t: 'gear', n: 12}, insider: {t: 'facet', n: 6}, friend: {t: 'scallop', n: 10}, guard: {t: 'gear', n: 20}, collector: {t: 'bead', n: 20}, c1: {t: 'double'}, c2: {t: 'rope'}, c3: {t: 'wave', n: 14}, ton: {t: 'facet', n: 10}, lucky: {t: 'scallop', n: 4}, fire: {t: 'points', n: 14, h: .012}, bolt: {t: 'points', n: 6, h: .012}, rocket: {t: 'gear', n: 8}, gambler: {t: 'bead', n: 12}, miner: {t: 'gear', n: 10}, cards: {t: 'facet', n: 4}, night: {t: 'double'}};
      var mm = window.GDMedals && GDMedals.by && GDMedals.by(job[0]) || {}, dc = window.GDMedalDeco || {};
      GDMedal3D.mount(document.createElement('div'), {frame: 1, hollow: 1, deco: dc[job[0]], shape: SH[job[0]], pal: job[0] === 'goshan' ? null : ((window.GDMedals && GDMedals.PAL && GDMedals.PAL[mm.pal || 'gold']) || null), w: 512, h: 512, snap: function (u) { fin(u && u.length > 3000 ? u : ''); }, onfail: function () { fin(''); }});
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
  function tick() {}
  /* frames only go on round avatars (leaders, recent wins, transfers, games); square profile avatars stay as they are */
  function round(el, key) {
    if (!el || !key) return; var tries = 0;
    (function chk() {
      if (!el.isConnected) { if (++tries < 40) setTimeout(chk, 120); return; }
      if (el.id === 'bigAvatar' || el.id === 'publicProfileAvatar' || el.closest('#publicProfileModal,.profile-hero')) return;
      var cs = getComputedStyle(el), w = el.getBoundingClientRect().width, r = parseFloat(cs.borderTopLeftRadius) || 0;
      if (w < 26 || !(cs.borderTopLeftRadius.indexOf('%') > 0 ? r >= 45 : r >= w * .45)) return;
      apply(el, key);
    })();
  }
  
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
  window.GDFrames = {round: round, svg: svg, apply: apply, open: openCustomize, paint: paint, gained: gained, has: has, load: load, tick: tick, snap: snap, state: function () { return S; }};
})();
