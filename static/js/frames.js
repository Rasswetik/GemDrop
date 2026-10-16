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
  st.textContent = '.gdfr{overflow:visible!important;border-radius:50%!important;position:relative!important}.gdfr>img{border-radius:50%}.gdf-svg{position:absolute;left:-33.3%;top:-33.3%;width:166.7%;height:166.7%;pointer-events:none;z-index:4;overflow:visible}' +
    '@keyframes gdfPulse{0%,100%{opacity:.75}50%{opacity:1}}@keyframes gdfSpin{to{transform:rotate(360deg)}}.gdf-pulse{animation:gdfPulse 1.8s ease-in-out infinite}.gdf-spin{transform-origin:0 0;animation:gdfSpin 14s linear infinite}' +
    '.gdc{position:fixed;inset:0;z-index:2147483100;display:grid;place-items:center;padding:16px;background:rgba(5,4,12,.72);overscroll-behavior:contain}.gdc-c{position:relative;width:min(100%,420px);max-height:calc(100dvh - 32px);overflow:auto;border-radius:24px;padding:20px 16px 18px;background:var(--card,#14162a);border:1px solid var(--gd-line,#22253d);color:var(--text,#f4f5f7)}' +
    '.gdc-c h2{margin:0 36px 4px 4px;font-size:22px}.gdc-x{position:absolute;top:14px;right:14px;width:32px;height:32px;border-radius:50%;border:0;background:rgba(255,255,255,.08);color:inherit;font-size:18px;cursor:pointer;padding:0}' +
    '.gdc-tabs{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:12px 0 14px}.gdc-tabs button{height:42px;border-radius:14px;border:1px solid var(--gd-line,#22253d);background:rgba(255,255,255,.04);color:inherit;font-weight:800;font-size:14px;cursor:pointer}.gdc-tabs button.on{background:linear-gradient(135deg,#8be9f0,#47b0f5);color:#08121c;border-color:transparent}' +
    '.gdc-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.gdc-card{display:grid;justify-items:center;gap:6px;padding:16px 8px 12px;border-radius:18px;background:rgba(255,255,255,.04);border:1px solid var(--gd-line,#22253d);text-align:center;cursor:pointer;color:inherit;font:inherit}' +
    '.gdc-card.on{border-color:#47b0f5;box-shadow:0 0 0 2px rgba(71,176,245,.25) inset}.gdc-card.lk{opacity:.55;cursor:default}.gdc-card b{font-size:14px}.gdc-card small{font-size:12px;color:var(--muted,#96a0a8)}' +
    '.gdc-av{position:relative;width:72px;height:72px;margin:18px 0 20px;border-radius:50%;background:#242841;display:grid;place-items:center;font-size:28px;font-weight:800;color:#47b0f5}.gdc-av img{width:100%;height:100%;object-fit:cover;border-radius:50%}' +
    '.gdc-p{margin:6px 2px 0;font-size:13px;line-height:1.4;color:var(--muted,#96a0a8)}.gdc-big{width:100%;height:46px;margin-top:12px;border-radius:14px;border:0;background:linear-gradient(135deg,#8be9f0,#47b0f5);color:#08121c;font-weight:900;font-size:15px;cursor:pointer}' +
    '.gdc-eq{display:flex;gap:8px;justify-content:center;margin:6px 0 4px;min-height:60px}';
  document.head.appendChild(st);

  function uid() { return 'gf' + (++N); }
  function spikes(id) {
    var o = '<defs><linearGradient id="' + id + 'a" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#4a4f66"/><stop offset=".5" stop-color="#14151f"/><stop offset="1" stop-color="#2b2e3b"/></linearGradient><linearGradient id="' + id + 'b" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f3f6ff"/><stop offset="1" stop-color="#7a82a0"/></linearGradient></defs>', i;
    for (i = 0; i < 20; i++) { var a = i / 20 * 360; o += '<path transform="rotate(' + a + ')" d="M-4 -42 L0 -57 L4 -42Z" fill="url(#' + id + 'b)" stroke="#2b2e3b" stroke-width=".6"/>'; }
    return o + '<circle r="40" fill="none" stroke="url(#' + id + 'a)" stroke-width="8"/><circle r="36.2" fill="none" stroke="#9aa0c0" stroke-opacity=".6" stroke-width=".8"/><circle r="43.6" fill="none" stroke="#fff" stroke-opacity=".25" stroke-width=".8"/>';
  }
  function pop(id) {
    var C = ['#ff4fb0', '#ffd60a', '#2ad4ff', '#ff7a1a'], o = '<defs><linearGradient id="' + id + 'a" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#8cc2ff"/><stop offset="1" stop-color="#17408f"/></linearGradient></defs><circle r="40" fill="none" stroke="url(#' + id + 'a)" stroke-width="8"/><circle r="43.6" fill="none" stroke="#fff" stroke-opacity=".35" stroke-width=".8"/>', i;
    for (i = 0; i < 16; i++) { var a = i / 16 * Math.PI * 2; o += '<circle cx="' + (Math.cos(a) * 49).toFixed(1) + '" cy="' + (Math.sin(a) * 49).toFixed(1) + '" r="5" fill="' + C[i % 4] + '" stroke="#fff" stroke-width="1"/><circle cx="' + (Math.cos(a) * 49 - 1.5).toFixed(1) + '" cy="' + (Math.sin(a) * 49 - 1.6).toFixed(1) + '" r="1.5" fill="#fff" opacity=".7"/>'; }
    return o;
  }
  function laurel(id) {
    var o = '<defs><linearGradient id="' + id + 'a" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ff6b7d"/><stop offset="1" stop-color="#7a0f22"/></linearGradient><linearGradient id="' + id + 'b" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#fff2b0"/><stop offset="1" stop-color="#c8860e"/></linearGradient></defs><circle r="40" fill="none" stroke="url(#' + id + 'a)" stroke-width="8"/><circle r="43.8" fill="none" stroke="url(#' + id + 'b)" stroke-width="1.6"/>', i;
    for (i = 0; i < 14; i++) { var a = i / 14 * 360 + 6; o += '<ellipse transform="rotate(' + a + ') translate(0 -48.5) rotate(' + (i % 2 ? 24 : -24) + ')" rx="2.5" ry="6" fill="url(#' + id + 'b)" stroke="#8a5a06" stroke-width=".4"/>'; }
    return o + '<path d="M0 -53 l2 4 4.5 .6 -3.3 3.1 .8 4.5 -4 -2.2 -4 2.2 .8 -4.5 -3.3 -3.1 4.5 -.6z" fill="url(#' + id + 'b)"/>';
  }
  function neon(id) {
    return '<defs><linearGradient id="' + id + 'a" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ff3ea5"/><stop offset=".5" stop-color="#8a5cff"/><stop offset="1" stop-color="#2ad4ff"/></linearGradient><filter id="' + id + 'g" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="2.6"/></filter></defs>' +
      '<g class="gdf-pulse"><circle r="42" fill="none" stroke="url(#' + id + 'a)" stroke-width="8" filter="url(#' + id + 'g)" opacity=".9"/></g><circle r="41" fill="none" stroke="url(#' + id + 'a)" stroke-width="4.2"/><circle r="41" fill="none" stroke="#fff" stroke-opacity=".75" stroke-width="1"/>' +
      '<g class="gdf-spin">' + [0, 1, 2, 3].map(function (k) { return '<path transform="rotate(' + k * 90 + ') translate(0 -50)" d="M0 -5 Q0 0 5 0 Q0 0 0 5 Q0 0 -5 0 Q0 0 0 -5Z" fill="#fff"/>'; }).join('') + '</g>';
  }
  function chain(id) {
    var o = '<defs><linearGradient id="' + id + 'a" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#fff3b0"/><stop offset=".5" stop-color="#ffc933"/><stop offset="1" stop-color="#a86a05"/></linearGradient></defs><circle r="40" fill="none" stroke="url(#' + id + 'a)" stroke-width="7"/>', i;
    for (i = 0; i < 22; i++) { var a = i / 22 * 360; o += '<ellipse transform="rotate(' + a + ') translate(0 -47)" rx="' + (i % 2 ? 2 : 3.6) + '" ry="' + (i % 2 ? 4.6 : 3.2) + '" fill="none" stroke="url(#' + id + 'a)" stroke-width="2.2"/>'; }
    return o + '<circle r="43.5" fill="none" stroke="#fff" stroke-opacity=".3" stroke-width=".8"/>';
  }
  var ART = {jealous: spikes, warhol: pop, poets: laurel, vklube: neon, kashtan: chain};
  function svg(key) { var f = ART[key]; if (!f) return ''; var id = uid(); return '<svg class="gdf-svg" viewBox="-60 -60 120 120" aria-hidden="true">' + f(id) + '</svg>'; }
  function apply(el, key) {
    if (!el) return; var cur = el.getAttribute('data-fr') || '';
    if ((key || '') === cur && (key ? el.querySelector('.gdf-svg') : true)) return;
    var old = el.querySelector(':scope > .gdf-svg'); if (old) old.remove();
    el.setAttribute('data-fr', key || '');
    if (!key || !ART[key]) { el.classList.remove('gdfr'); return; }
    el.classList.add('gdfr'); el.insertAdjacentHTML('beforeend', svg(key));
  }
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
  function avatarHtml(key) { var im = ownImg(); return '<div class="gdc-av' + (key ? ' gdfr' : '') + '" data-fr="' + (key || '') + '">' + (im ? '<img alt="" src="' + esc(im) + '">' : esc(ownLetter() || '👤')) + (key ? svg(key) : '') + '</div>'; }
  function overlay(inner) {
    var el = document.createElement('div'); el.className = 'gdc'; el.setAttribute('role', 'dialog'); el.innerHTML = '<div class="gdc-c"><button type="button" class="gdc-x" aria-label="Закрыть">×</button>' + inner + '</div>';
    el.addEventListener('click', function (e) { if (e.target === el || e.target.classList.contains('gdc-x')) el.remove(); });
    document.body.appendChild(el); return el;
  }
  function openCustomize(start) {
    var el = overlay('<h2>Кастомизация</h2><p class="gdc-p">Медали и рамки профиля</p><div class="gdc-tabs"><button type="button" data-t="m">Медали</button><button type="button" data-t="f">Рамки</button></div><div id="gdcBody"></div>');
    var body = el.querySelector('#gdcBody');
    function paintMedals() {
      body.innerHTML = '<p class="gdc-p">Наденьте до 3 медалей — их увидят рядом с вашим ником. Награды за коллекции тоже здесь.</p><button type="button" class="gdc-big" id="gdcMed">Открыть коллекцию медалей</button>';
      body.querySelector('#gdcMed').onclick = function () { el.remove(); if (window.GDMedalsOpen) window.GDMedalsOpen(); };
    }
    function paintFrames() {
      var h = '<div class="gdc-grid"><button type="button" class="gdc-card' + (!S.selected ? ' on' : '') + '" data-k=""><div class="gdc-av" style="margin:18px 0 20px">' + (ownImg() ? '<img alt="" src="' + esc(ownImg()) + '">' : esc(ownLetter() || '👤')) + '</div><b>Без рамки</b><small>' + (!S.selected ? 'Надета' : 'Выбрать') + '</small></button>';
      (S.frames.length ? S.frames : KEYS.map(function (k) { return {key: k, owned: S.owned.indexOf(k) >= 0}; })).forEach(function (f) {
        if (!ART[f.key]) return; var own = S.owned.indexOf(f.key) >= 0;
        h += '<button type="button" class="gdc-card' + (S.selected === f.key ? ' on' : '') + (own ? '' : ' lk') + '" data-k="' + f.key + '" ' + (own ? '' : 'disabled') + '>' + avatarHtml(f.key) + '<b>' + esc(NAMES[f.key] || f.name) + '</b><small>' + (own ? (S.selected === f.key ? 'Надета' : 'Выбрать') : 'Нужна медаль «' + esc(f.name || f.key) + '»') + '</small></button>';
      });
      body.innerHTML = h + '</div><p class="gdc-p">Рамку дают музыкальные медали. Получите медаль — и рамка появится здесь.</p>';
    }
    body.addEventListener('click', function (e) {
      var c = e.target.closest('.gdc-card[data-k]'); if (!c || c.disabled) return; var k = c.dataset.k;
      api('/api/frames/select', {key: k}).then(function (d) { S.selected = d.selected || ''; paintFrames(); tick(); }).catch(function (er) { if (window.toast) toast(er.message); });
    });
    function tab(t) { Array.prototype.forEach.call(el.querySelectorAll('.gdc-tabs button'), function (b) { b.classList.toggle('on', b.dataset.t === t); }); if (t === 'm') paintMedals(); else { paintFrames(); load().then(paintFrames).catch(function () {}); } }
    el.querySelector('.gdc-tabs').onclick = function (e) { var b = e.target.closest('button'); if (b) tab(b.dataset.t); };
    tab(start === 'f' ? 'f' : 'm');
    return el;
  }
  function gained(key) {
    if (!ART[key]) return;
    load().catch(function () {}).then(function () {
      var el = overlay('<h2 style="text-align:center;margin:0 0 6px">Вы получили рамку</h2><div style="display:grid;place-items:center">' + avatarHtml(key) + '</div><p class="gdc-p" style="text-align:center"><b style="color:inherit">' + esc(NAMES[key] || key) + '</b> — рамка вокруг вашего аватара в профиле.</p><button type="button" class="gdc-big" id="gdcOn">Надеть</button><button type="button" class="gdc-big" id="gdcLater" style="background:rgba(255,255,255,.08);color:inherit;margin-top:8px">Позже</button>');
      el.querySelector('#gdcLater').onclick = function () { el.remove(); };
      el.querySelector('#gdcOn').onclick = function () { api('/api/frames/select', {key: key}).then(function (d) { S.selected = d.selected; tick(); el.remove(); }).catch(function (er) { if (window.toast) toast(er.message); }); };
    });
  }
  window.GDFrames = {svg: svg, apply: apply, open: openCustomize, gained: gained, has: function (k) { return !!ART[k]; }, load: load, tick: tick, state: function () { return S; }};
})();
