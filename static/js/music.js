/* Music medals: profile player (long track), preview and PvP-win jingle (short track). */
(function () {
  var KEYS = {}, READY = fetch('/api/music/list', {credentials: 'same-origin'}).then(function (r) { return r.json(); }).then(function (d) { (d.tracks || []).forEach(function (t) { KEYS[t.key] = t.name; ok[url(t.key, false)] = Promise.resolve(!!t.long); ok[url(t.key, true)] = Promise.resolve(!!t.short); }); }).catch(function () {});
  var ok = {}, cur = null, mineP = null, LS = 'gd_medal_sound';
  function enabled() { try { return localStorage.getItem(LS) !== '0'; } catch (e) { return true; } }
  function setEnabled(v) { try { localStorage.setItem(LS, v ? '1' : '0'); } catch (e) {} if (!v) { stop(); stopProfile(); } }
  function url(k, short) { return '/static/music/' + k + (short ? '_short' : '') + '.mp3'; }
  function exists(k, short) {
    var u = url(k, short); if (ok[u] !== undefined) return ok[u];
    return ok[u] = fetch(u, {method: 'HEAD', credentials: 'same-origin'}).then(function (r) { return r.ok && /audio|octet/.test(r.headers.get('content-type') || 'audio'); }).catch(function () { return false; });
  }
  /* pick the file for a role; falls back to the other version. cut=true when a long track must be trimmed to a short excerpt */
  function pick(k, short) {
    return exists(k, short).then(function (has) {
      if (has) return {url: url(k, short), cut: false};
      return exists(k, !short).then(function (h2) { return h2 ? {url: url(k, !short), cut: short} : null; });
    });
  }
  function excerpt(a, secs) {
    var t0 = 0, out = false;
    a.addEventListener('loadedmetadata', function () { var d = a.duration || 0; t0 = d > secs + 4 ? Math.max(0, Math.min(d * .3, d - secs - 2)) : 0; try { a.currentTime = t0; } catch (e) {} });
    a.addEventListener('timeupdate', function () { if (!out && a.currentTime - t0 > secs - 1.6) { out = true; ramp(a, 0, 1500, function () { try { a.pause(); } catch (e) {} }); } });
  }
  var I = {a: null, key: ''};
  function stop() { try { islandKill(); } catch (e) {} try { pausePL(); } catch (e) {} if (cur) { try { cur.pause(); } catch (e) {} cur.onended = null; cur = null; } }
  function playShort(k, vol) {
    if (!enabled()) return Promise.resolve(false);
    return pick(k, true).then(function (pk) {
      if (!pk) return false;
      stop(); stopProfile(); var a = new Audio(pk.url); a.volume = vol == null ? .8 : vol; cur = a; if (pk.cut) excerpt(a, 12);
      return a.play().then(function () { return true; }).catch(function () { return false; });
    });
  }
  function myMusic() {
    if (mineP) return mineP;
    return mineP = fetch('/api/medals', {credentials: 'same-origin'}).then(function (r) { return r.json(); }).then(function (d) {
      var sh = (d.medals || []).filter(function (m) { return m.shown && KEYS[m.key]; });
      var main = sh.filter(function (m) { return m.key === d.main; })[0];
      return (main || sh[0] || {}).key || '';
    }).catch(function () { return ''; });
  }
  /* quiet jingle: fades in, fades out before the end */
  var fade = {raf: 0};
  function ramp(a, to, ms, done) {
    cancelAnimationFrame(fade.raf); var from = a.volume, t0 = performance.now();
    (function step(now) { var q = Math.min(1, (now - t0) / ms); a.volume = Math.max(0, Math.min(1, from + (to - from) * q * q * (3 - 2 * q))); if (q < 1) fade.raf = requestAnimationFrame(step); else if (done) done(); })(t0);
  }
  function fadeOut(ms) { var a = cur; if (!a) return; ramp(a, 0, ms || 900, function () { if (cur === a) stop(); else try { a.pause(); } catch (e) {} }); }
  function winFaded(k) {
    pick(k, true).then(function (pk) {
      if (!pk || !enabled()) return;
      stop(); stopProfile(); var a = new Audio(pk.url), MAX = .32; a.volume = 0; cur = a; var out = false; if (pk.cut) { out = true; excerpt(a, 11); }
      a.addEventListener('timeupdate', function () { if (!out && a.duration && a.currentTime > a.duration - 1.8) { out = true; ramp(a, 0, 1500); } });
      a.addEventListener('ended', function () { if (cur === a) cur = null; });
      a.play().then(function () { ramp(a, MAX, 1600); }).catch(function () {});
    });
  }
  function win() { myMusic().then(function (k) { if (k) winFaded(k); }); }

  /* background track in highlight moments (Crash: quiet from 10x, loud on cash-out, gone on crash) */
  var H = {a: null};
  function hypeKill(ms) {
    var a = H.a; H.a = null; if (!a) return;
    ramp(a, 0, ms || 700, function () { try { a.pause(); } catch (e) {} });
  }
  function hypeStart() {
    if (!enabled() || H.a) return;
    myMusic().then(function (k) {
      if (!k || H.a) return;
      pick(k, false).then(function (pk) {
        if (!pk || H.a) return;
        stop(); stopProfile();
        var a = new Audio(pk.url); a.loop = !!pk.cut; a.volume = 0; H.a = a;
        a.play().then(function () { ramp(a, .1, 2200); }).catch(function () { if (H.a === a) H.a = null; });
        a.addEventListener('timeupdate', function () { if (H.loud && !H.out && a.duration && !a.loop && a.currentTime > a.duration - 2) { H.out = true; ramp(a, 0, 1800); } });
      });
    });
  }
  function hypeWin() { var a = H.a; if (!a) return; H.loud = true; H.out = false; ramp(a, .75, 700); }
  function hypeEnd(ms) { H.loud = false; H.out = false; hypeKill(ms); }
  window.GDMusicRefresh = function () { mineP = null; };

  /* ---- profile player ---- */
  var st = document.createElement('style');
  st.textContent = '.mpl-sep{display:flex;align-items:center;gap:10px;margin:16px 0 10px;font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted,#96a0a8)}.mpl-sep:before,.mpl-sep:after{content:"";flex:1;height:1px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.16),transparent)}' +
    '.mpl{position:relative;overflow:hidden;display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:10px 14px;align-items:center;margin:0 0 6px;padding:16px;border-radius:22px;background:radial-gradient(120% 140% at 0 0,rgba(71,176,245,.22),transparent 60%),linear-gradient(160deg,rgba(33,38,60,.95),rgba(20,23,38,.95));border:1px solid rgba(255,255,255,.1);box-shadow:0 10px 30px rgba(0,0,0,.35),inset 0 1px 0 rgba(255,255,255,.08)}' +
    '.mpl-art{grid-row:1;width:64px;height:64px;display:grid;place-items:center;position:relative}.mpl-art:before{content:"";position:absolute;inset:-6px;border-radius:50%;background:radial-gradient(circle,rgba(71,176,245,.45),transparent 70%);opacity:.5;transition:opacity .4s}.mpl.on .mpl-art:before{opacity:1}.mpl-art .gdm{width:64px;height:auto;position:relative}' +
    '.mpl-t{min-width:0;display:flex;flex-direction:column;gap:3px}.mpl-t b{font-size:16px;line-height:1.2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.mpl-t small{font-size:12px;color:var(--muted,#96a0a8);display:flex;align-items:center}' +
    '.mpl-pp{width:50px;height:50px;border-radius:50%;border:0;background:linear-gradient(135deg,#9ae9e3,#47b0f5);cursor:pointer;display:grid;place-items:center;padding:0;box-shadow:0 6px 18px rgba(71,176,245,.45);transition:transform .15s}.mpl-pp:active{transform:scale(.92)}.mpl-pp svg{width:20px;height:20px;fill:#0f1326}' +
    '.mpl-ctl{display:flex;align-items:center;gap:8px}.mpl-nx{width:36px;height:36px;border-radius:50%;border:1px solid rgba(255,255,255,.14);background:rgba(255,255,255,.06);cursor:pointer;display:grid;place-items:center;padding:0}.mpl-nx:active{transform:scale(.92)}.mpl-nx svg{width:16px;height:16px;fill:#dfe6ee}' +
    '.mpl-bar{grid-column:1/4;display:flex;align-items:center;gap:10px;font-size:11px;font-variant-numeric:tabular-nums;color:var(--muted,#96a0a8)}.mpl-bar span{min-width:30px}.mpl-bar span:last-child{text-align:right}' +
    '.mpl-bar input[type=range]{--p:0%;flex:1;min-width:0;height:5px;border-radius:5px;-webkit-appearance:none;appearance:none;outline:0;cursor:pointer;background:linear-gradient(90deg,#9ae9e3,#47b0f5) 0/var(--p) 100% no-repeat,rgba(255,255,255,.14)}' +
    '.mpl-bar input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:#fff;box-shadow:0 0 0 3px rgba(71,176,245,.4)}.mpl-bar input[type=range]::-moz-range-thumb{width:14px;height:14px;border:0;border-radius:50%;background:#fff;box-shadow:0 0 0 3px rgba(71,176,245,.4)}' +
    '.mpl-eq{display:inline-flex;gap:2px;align-items:flex-end;height:11px;margin-right:6px}.mpl-eq i{width:2.5px;background:#47b0f5;border-radius:2px;height:4px}.mpl.on .mpl-eq i{animation:mplEq .9s ease-in-out infinite}.mpl-eq i:nth-child(2){animation-delay:.2s!important}.mpl-eq i:nth-child(3){animation-delay:.4s!important}@keyframes mplEq{0%,100%{height:3px}50%{height:11px}}';
  document.head.appendChild(st);
  var P = {a: null};
  function fmt(s) { s = Math.max(0, Math.floor(s || 0)); return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2); }
  function stopProfile() { if (P.a) { try { P.a.pause(); } catch (e) {} } }
  function removePlayer() { var o = document.getElementById('ppPlayer'); if (o) o.remove(); if (P.a) { try { P.a.pause(); } catch (e) {} P.a = null; } }
  function fadePlayer() {
    var o = document.getElementById('ppPlayer'), a = P.a; P.tk = (P.tk || 0) + 1; if (o) o.remove(); P.a = null;
    if (!a || a.paused) return;
    if (a.src && !a.ended) { I.a = a; I.key = P.key || ''; islandTick(true); return; }   // keep playing: the mini player takes over
    var v0 = a.volume, t0 = performance.now();
    (function step(now) { var q = Math.min(1, (now - t0) / 700); a.volume = Math.max(0, v0 * (1 - q)); if (q < 1) requestAnimationFrame(step); else try { a.pause(); } catch (e) {} })(t0);
  }
  var PREV = '<svg viewBox="0 0 24 24"><path d="M6 5h2v14H6zM20 5v14L9 12z"/></svg>', NEXT = '<svg viewBox="0 0 24 24"><path d="M16 5h2v14h-2zM4 5v14l11-7z"/></svg>';
  var PLAY = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>', PAUSE = '<svg viewBox="0 0 24 24"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>';
  window.GDMusicProfile = function (d) {
    removePlayer(); stop(); islandKill(); var tk = P.tk = (P.tk || 0) + 1; READY.then(function () { if (P.tk === tk) buildProfile(d, tk); });
  };
  function buildProfile(d, tk, idx, auto) {
    removePlayer();
    var chip = document.getElementById('ppMedals'); if (!chip) return;
    var keys = [], seenK = {};
    ((d && d.medals) || []).forEach(function (m) { if (KEYS[m.key] && !seenK[m.key]) { seenK[m.key] = 1; keys.push(m.key); } });
    if (!keys.length) return;
    idx = ((idx || 0) % keys.length + keys.length) % keys.length;
    var key = keys[idx], multi = keys.length > 1; P.key = key;
    pick(key, false).then(function (pk) { var has = !!pk;
      if (!has || P.tk !== tk || !document.getElementById('ppMedals')) return;
      removePlayer();
      var el = document.createElement('div'); el.id = 'ppPlayer'; el.className = 'mplw';
      el.innerHTML = '<div class="mpl-sep">Трек медали</div><div class="mpl"><div class="mpl-art">' + (window.GDMedals ? GDMedals.svg(key, 64, true) : '') + '</div><div class="mpl-t"><b>' + KEYS[key].replace(/</g, '&lt;') + '</b><small><span class="mpl-eq"><i></i><i></i><i></i></span>Играет сейчас</small></div><div class="mpl-ctl">' + (multi ? '<button type="button" class="mpl-nx" data-d="-1" aria-label="Предыдущий трек">' + PREV + '</button>' : '') + '<button type="button" class="mpl-pp" aria-label="Играть">' + PLAY + '</button>' + (multi ? '<button type="button" class="mpl-nx" data-d="1" aria-label="Следующий трек">' + NEXT + '</button>' : '') + '</div>' +
        '<div class="mpl-bar"><span class="mpl-c">0:00</span><input type="range" min="0" max="1000" value="0" aria-label="Перемотка"><span class="mpl-d">0:00</span></div></div>';
      chip.parentNode.insertBefore(el, chip.nextSibling);
      var a = new Audio(pk.url); a.preload = 'auto'; P.a = a;
      var box = el.querySelector('.mpl'), btn = el.querySelector('.mpl-pp'), rng = el.querySelector('input'), c = el.querySelector('.mpl-c'), du = el.querySelector('.mpl-d'), seeking = false;
      function ui() { var pl = !a.paused; box.classList.toggle('on', pl); btn.innerHTML = pl ? PAUSE : PLAY; }
      a.addEventListener('play', ui); a.addEventListener('pause', ui); a.addEventListener('ended', function () { ui(); rng.value = 0; rng.style.setProperty('--p', '0%'); });
      a.addEventListener('loadedmetadata', function () { du.textContent = fmt(a.duration); });
      a.addEventListener('timeupdate', function () { if (!seeking && a.duration) rng.value = a.currentTime / a.duration * 1000; rng.style.setProperty('--p', rng.value / 10 + '%'); c.textContent = fmt(a.currentTime); });
      rng.addEventListener('input', function () { seeking = true; if (a.duration) { a.currentTime = rng.value / 1000 * a.duration; rng.style.setProperty('--p', rng.value / 10 + '%'); c.textContent = fmt(a.currentTime); } });
      rng.addEventListener('change', function () { seeking = false; });
      btn.onclick = function () { if (a.paused) { stop(); a.play().catch(function () {}); } else a.pause(); };
      el.querySelectorAll('.mpl-nx').forEach(function (nb) {
        nb.onclick = function () { var was = !a.paused; buildProfile(d, tk, idx + Number(nb.dataset.d), was); };
      });
      a.volume = .8; if (auto === undefined ? enabled() : auto) { stop(); a.play().catch(function () {}); }       // starts by itself; the browser may need a tap
    });
  }
  var mo = document.getElementById('publicProfileModal');
  if (mo) new MutationObserver(function () { if (mo.classList.contains('hidden')) fadePlayer(); }).observe(mo, {attributes: true, attributeFilter: ['class']});
  function mountRow() {
    if (document.getElementById('pfMedalSound')) return true;
    var anchor = document.getElementById('pfHaptics'); if (!anchor) return false;
    var row = document.createElement('label'); row.className = 'perf-row'; row.id = 'pfMedalSound';
    row.innerHTML = '<span class="perf-copy"><b>Звук медалей</b><small>Музыка медалей в профилях и при победах</small></span><input class="perf-switch-input" type="checkbox" aria-label="Звук медалей"><i class="perf-switch" aria-hidden="true"></i>';
    var cb = row.querySelector('input'); cb.checked = enabled();
    cb.onchange = function () { setEnabled(cb.checked); };
    anchor.parentNode.insertBefore(row, anchor.nextSibling); return true;
  }
  if (!mountRow()) document.addEventListener('DOMContentLoaded', mountRow);
  setTimeout(mountRow, 800); setTimeout(mountRow, 2500);
  /* ---- medal player: own medals as a background playlist (sheet from the profile) ---- */
  var PL = {a: null, list: [], i: 0, vol: .7, open: false, loaded: false};
  try { var sv = parseFloat(localStorage.getItem('gd_pl_vol')); if (sv >= 0 && sv <= 1) PL.vol = sv; } catch (e) {}
  function pausePL() { if (PL.a && !PL.a.paused) { try { PL.a.pause(); } catch (e) {} } plUi(); }
  function plCss() {
    if (document.getElementById('plCss')) return;
    var c = document.createElement('style'); c.id = 'plCss';
    c.textContent = '.pls{position:fixed;inset:0;z-index:2147483200;display:flex;align-items:flex-end;justify-content:center;background:rgba(5,8,16,.62);backdrop-filter:blur(4px)}.pls.hidden{display:none}' +
      '.pls-c{width:min(100%,520px);max-height:88dvh;overflow:auto;padding:14px 16px calc(env(safe-area-inset-bottom,0px) + 18px);border-radius:26px 26px 0 0;background:linear-gradient(180deg,#1b2036,#10142a);border:1px solid rgba(255,255,255,.1);box-shadow:0 -12px 40px rgba(0,0,0,.5);animation:plUp .3s cubic-bezier(.2,.9,.3,1)}@keyframes plUp{from{transform:translateY(40px);opacity:0}to{transform:none;opacity:1}}' +
      '.pls-grab{width:42px;height:4px;border-radius:4px;background:rgba(255,255,255,.2);margin:0 auto 12px}.pls-h{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}.pls-h b{font-size:17px}.pls-x{width:34px;height:34px;border-radius:50%;border:0;background:rgba(255,255,255,.08);color:#fff;font-size:20px;cursor:pointer}' +
      '.pls-now{display:grid;justify-items:center;gap:6px;padding:8px 0 4px}.pls-art{width:132px;height:162px;display:grid;place-items:center;position:relative}.pls-art:before{content:"";position:absolute;inset:-14px;border-radius:50%;background:radial-gradient(circle,rgba(71,176,245,.5),transparent 70%);opacity:.45;transition:opacity .4s}.pls.on .pls-art:before{opacity:1}.pls-art .gdm{width:132px;height:auto;position:relative}' +
      '.pls-t{font-size:19px;font-weight:700;text-align:center;color:#fff}.pls-h b{color:#fff}.pls-s{font-size:12px;color:#96a0a8}' +
      '.pls-bar{display:flex;align-items:center;gap:10px;margin:12px 0 4px;font-size:11px;font-variant-numeric:tabular-nums;color:#96a0a8}.pls-bar span{min-width:32px}.pls-bar span:last-child{text-align:right}' +
      '.pls input[type=range]{--p:0%;flex:1;min-width:0;height:5px;border-radius:5px;-webkit-appearance:none;appearance:none;outline:0;cursor:pointer;background:linear-gradient(90deg,#9ae9e3,#47b0f5) 0/var(--p) 100% no-repeat,rgba(255,255,255,.14)}.pls input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:15px;height:15px;border-radius:50%;background:#fff;box-shadow:0 0 0 3px rgba(71,176,245,.4)}.pls input[type=range]::-moz-range-thumb{width:15px;height:15px;border:0;border-radius:50%;background:#fff}' +
      '.pls-ctl{display:flex;align-items:center;justify-content:center;gap:16px;margin:8px 0}.pls-ctl button{width:46px;height:46px;border-radius:50%;border:1px solid rgba(255,255,255,.14);background:rgba(255,255,255,.06);cursor:pointer;display:grid;place-items:center;padding:0}.pls-ctl button svg{width:19px;height:19px;fill:#e6edf5}.pls-ctl .pls-pp{width:64px;height:64px;border:0;background:linear-gradient(135deg,#9ae9e3,#47b0f5);box-shadow:0 8px 22px rgba(71,176,245,.45)}.pls-ctl .pls-pp svg{width:26px;height:26px;fill:#0f1326}.pls-ctl button.act{border-color:#47b0f5;background:rgba(71,176,245,.2)}' +
      '.pls-vol{display:flex;align-items:center;gap:10px;margin:6px 4px 12px;color:#96a0a8}.pls-vol svg{width:18px;height:18px;fill:#96a0a8;flex:none}' +
      '.pls-list{display:grid;gap:6px}.pls-it{display:flex;align-items:center;gap:12px;padding:8px 10px;border-radius:14px;background:rgba(255,255,255,.04);border:1px solid transparent;cursor:pointer;text-align:left;color:#e8edf8;font:inherit;width:100%}.pls-it.cur{border-color:rgba(71,176,245,.6);background:rgba(71,176,245,.12)}.pls-it .gdm{width:38px;height:auto}.pls-it span{flex:1;min-width:0;font-size:14px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.pls-it i{font-style:normal;font-size:11px;color:#47b0f5}' +
      '.pls-empty{padding:26px 10px;text-align:center;color:#96a0a8;font-size:13px;line-height:1.5}';
    document.head.appendChild(c);
  }
  var SPK = '<svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 4V5L7 9zm13.5 3a4.5 4.5 0 0 0-2.5-4v8a4.5 4.5 0 0 0 2.5-4z"/></svg>', SHUF = '<svg viewBox="0 0 24 24"><path d="M10.6 9.2 5.4 4 4 5.4l5.2 5.2zM14.5 4l2.05 2.05L4 18.6 5.4 20 17.95 7.45 20 9.5V4zm.35 10.8-1.4 1.4 3.1 3.1L14.5 20H20v-5.5l-2.05 2.05z"/></svg>';
  function plUi() {
    var m = document.getElementById('plSheet'); if (!m) return;
    var pl = !!(PL.a && !PL.a.paused); m.classList.toggle('on', pl);
    var pp = m.querySelector('.pls-pp'); if (pp) pp.innerHTML = pl ? PAUSE : PLAY;
  }
  function plRender() {
    var m = document.getElementById('plSheet'); if (!m) return;
    var body = m.querySelector('.pls-body');
    if (!PL.list.length) { body.innerHTML = '<div class="pls-empty">У вас пока нет медалей с музыкой.<br>Музыкальные медали появятся здесь, и вы сможете слушать их на фоне.</div>'; return; }
    var k = PL.list[PL.i], esc = function (t) { return String(t).replace(/</g, '&lt;'); };
    body.innerHTML = '<div class="pls-now"><div class="pls-art">' + (window.GDMedals ? GDMedals.svg(k, 132, true) : '') + '</div><div class="pls-t">' + esc(KEYS[k]) + '</div><div class="pls-s">' + (PL.i + 1) + ' из ' + PL.list.length + ' · трек медали</div></div>' +
      '<div class="pls-bar"><span class="pls-tc">0:00</span><input class="pls-seek" type="range" min="0" max="1000" value="0" aria-label="Перемотка"><span class="pls-td">0:00</span></div>' +
      '<div class="pls-ctl"><button type="button" class="pls-sh' + (PL.shuffle ? ' act' : '') + '" aria-label="Перемешать">' + SHUF + '</button><button type="button" class="pls-pv" aria-label="Назад">' + PREV + '</button><button type="button" class="pls-pp" aria-label="Играть">' + PLAY + '</button><button type="button" class="pls-nx" aria-label="Дальше">' + NEXT + '</button><span style="width:46px"></span></div>' +
      '<div class="pls-vol">' + SPK + '<input class="pls-vl" type="range" min="0" max="100" value="' + Math.round(PL.vol * 100) + '" aria-label="Громкость"></div>' +
      '<div class="pls-list">' + PL.list.map(function (x, j) { return '<button type="button" class="pls-it' + (j === PL.i ? ' cur' : '') + '" data-j="' + j + '">' + (window.GDMedals ? GDMedals.svg(x, 38, true) : '') + '<span>' + esc(KEYS[x]) + '</span>' + (j === PL.i ? '<i>играет</i>' : '') + '</button>'; }).join('') + '</div>';
    var seek = body.querySelector('.pls-seek'), vl = body.querySelector('.pls-vl');
    vl.style.setProperty('--p', vl.value + '%');
    vl.oninput = function () { PL.vol = vl.value / 100; vl.style.setProperty('--p', vl.value + '%'); plSetVol(); try { localStorage.setItem('gd_pl_vol', String(PL.vol)); } catch (e) {} };
    body.querySelector('.pls-pp').onclick = function () { if (PL.a && !PL.a.paused) PL.a.pause(); else plPlay(PL.i); };
    body.querySelector('.pls-nx').onclick = function () { plStep(1); };
    body.querySelector('.pls-pv').onclick = function () { plStep(-1); };
    body.querySelector('.pls-sh').onclick = function () { PL.shuffle = !PL.shuffle; this.classList.toggle('act', PL.shuffle); };
    body.querySelectorAll('.pls-it').forEach(function (b) { b.onclick = function () { plPlay(Number(b.dataset.j)); }; });
    var a = PL.a;
    if (a) { seek.value = a.duration ? a.currentTime / a.duration * 1000 : 0; seek.style.setProperty('--p', seek.value / 10 + '%'); body.querySelector('.pls-tc').textContent = fmt(a.currentTime); body.querySelector('.pls-td').textContent = fmt(a.duration); }
    seek.oninput = function () { if (PL.a && PL.a.duration) { PL.a.currentTime = seek.value / 1000 * PL.a.duration; seek.style.setProperty('--p', seek.value / 10 + '%'); } };
    plUi();
  }
  /* iOS Safari/Telegram ignores audio.volume, so the player volume goes through a Web Audio gain node */
  function plGain(a) {
    try {
      var AC = window.AudioContext || window.webkitAudioContext; if (!AC) throw 0;
      if (!PL.ctx) { PL.ctx = new AC(); PL.gain = PL.ctx.createGain(); PL.gain.connect(PL.ctx.destination); }
      if (PL.ctx.state === 'suspended' && PL.ctx.resume) PL.ctx.resume();
      PL.ctx.createMediaElementSource(a).connect(PL.gain);
      PL.gain.gain.value = PL.vol; a.volume = 1; PL.web = true;
    } catch (e) { PL.web = false; a.volume = PL.vol; }
  }
  function plSetVol() { if (PL.web && PL.gain) PL.gain.gain.value = PL.vol; else if (PL.a) PL.a.volume = PL.vol; }
  function plStep(d) {
    var n = PL.list.length; if (!n) return;
    var j = PL.shuffle && n > 1 ? (PL.i + 1 + Math.floor(Math.random() * (n - 1))) % n : ((PL.i + d) % n + n) % n;
    plPlay(j);
  }
  function plPlay(j) {
    var k = PL.list[j]; if (!k) return;
    pick(k, false).then(function (pk) {
      if (!pk) return;
      stop(); stopProfile(); H.a && hypeEnd(300);
      if (PL.a) { try { PL.a.pause(); } catch (e) {} PL.a = null; }
      PL.i = j; PL.dismissed = false; PL.started = true; var a = new Audio(pk.url); a.loop = false; PL.a = a; plGain(a);
      a.addEventListener('play', plUi); a.addEventListener('pause', plUi);
      a.addEventListener('ended', function () { if (PL.a === a) plStep(1); });
      a.addEventListener('timeupdate', function () {
        var m = document.getElementById('plSheet'); if (!m || !PL.open || PL.a !== a) return;
        var seek = m.querySelector('.pls-seek'); if (!seek || !a.duration) return;
        seek.value = a.currentTime / a.duration * 1000; seek.style.setProperty('--p', seek.value / 10 + '%');
        m.querySelector('.pls-tc').textContent = fmt(a.currentTime); m.querySelector('.pls-td').textContent = fmt(a.duration);
      });
      a.play().catch(function () {});
      plRender();
    });
  }
  function plLoad() {
    return fetch('/api/medals', {credentials: 'same-origin'}).then(function (r) { return r.json(); }).then(function (d) {
      var seen = {}, list = [];
      (d.medals || []).forEach(function (m) { if (KEYS[m.key] && !seen[m.key]) { seen[m.key] = 1; list.push(m.key); } });
      var cur = PL.list[PL.i]; PL.list = list; var ix = list.indexOf(cur); PL.i = ix >= 0 ? ix : 0;
    }).catch(function () {});
  }
  function plOpen() {
    plCss();
    var m = document.getElementById('plSheet');
    if (!m) {
      m = document.createElement('div'); m.id = 'plSheet'; m.className = 'pls hidden';
      m.innerHTML = '<div class="pls-c"><div class="pls-grab"></div><div class="pls-h"><b>Плеер медалей</b><button type="button" class="pls-x" aria-label="Закрыть">×</button></div><div class="pls-body"></div></div>';
      m.addEventListener('click', function (e) { if (e.target === m || e.target.classList.contains('pls-x')) { PL.open = false; m.classList.add('hidden'); } });
      document.body.appendChild(m);
    }
    m.classList.remove('hidden'); PL.open = true;
    READY.then(plLoad).then(plRender);
  }
  function mountPlayerRow() {
    if (document.getElementById('pfMedalPlayer')) return true;
    var anchor = document.getElementById('pfSettingsRow'); if (!anchor) return false;
    var row = document.createElement('div'); row.className = 'pf-row pf-row-btn'; row.id = 'pfMedalPlayer';
    row.innerHTML = '<span class="pf-row-t"><b>Плеер медалей</b><small>Слушайте треки своих медалей на фоне</small></span><button type="button" class="profile-settings pf-open" aria-label="Открыть плеер медалей" style="display:grid;place-items:center"><svg viewBox="0 0 24 24" fill="currentColor" stroke="none" style="width:22px;height:22px"><path d="M8 5v14l11-7z"/></svg></button>';
    row.querySelector('button').onclick = plOpen; row.onclick = function (e) { if (!e.target.closest('button')) plOpen(); };
    anchor.parentNode.insertBefore(row, anchor); return true;
  }
  if (!mountPlayerRow()) document.addEventListener('DOMContentLoaded', mountPlayerRow);
  setTimeout(mountPlayerRow, 900); setTimeout(mountPlayerRow, 2600);

  /* ---- mini player ("island"): shows what plays when the profile / player sheet is closed ---- */
  var IS = {el: null, key: null, kind: '', open: false, t: 0};
  function islandCss() {
    if (document.getElementById('gdIslandCss')) return;
    var c = document.createElement('style'); c.id = 'gdIslandCss';
    c.textContent = '.gdi{position:fixed;left:50%;top:calc(env(safe-area-inset-top,0px) + 8px);z-index:2147483100;transform:translateX(-50%) scale(.5) translateY(-18px);opacity:0;pointer-events:none;background:#05060a;color:#fff;border-radius:24px;box-shadow:0 8px 28px rgba(0,0,0,.55),0 0 0 1px rgba(255,255,255,.1);display:flex;flex-direction:column;overflow:hidden;width:176px;transition:width .38s cubic-bezier(.3,1.25,.5,1),transform .38s cubic-bezier(.3,1.25,.5,1),opacity .25s;font-family:system-ui,sans-serif;-webkit-tap-highlight-color:transparent}' +
      '.gdi.on{transform:translateX(-50%) scale(1) translateY(0);opacity:1;pointer-events:auto}.gdi.big{width:min(340px,calc(100vw - 24px))}' +
      '.gdi-r{display:flex;align-items:center;gap:9px;padding:5px 6px 5px 5px;min-height:42px}.gdi-art{flex:none;width:32px;height:32px;display:grid;place-items:center;border-radius:50%;background:radial-gradient(circle at 35% 30%,#2a2d44,#0d0e18);overflow:hidden}.gdi-art .gdm{width:30px;height:auto;margin-top:2px}' +
      '.gdi-t{flex:1;min-width:0;display:flex;flex-direction:column;gap:1px}.gdi-t b{font-size:12.5px;line-height:1.1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.gdi-t small{font-size:10px;color:#8e97b3;line-height:1.1;white-space:nowrap}' +
      '.gdi-eq{flex:none;display:inline-flex;align-items:flex-end;gap:2px;height:14px;margin-right:2px}.gdi-eq i{width:3px;height:4px;border-radius:2px;background:#47b0f5;animation:gdiEq .9s ease-in-out infinite}.gdi-eq i:nth-child(2){animation-delay:.18s;background:#a066ff}.gdi-eq i:nth-child(3){animation-delay:.36s;background:#ff7aaa}.gdi-eq i:nth-child(4){animation-delay:.54s}@keyframes gdiEq{0%,100%{height:3px}50%{height:14px}}.gdi:not(.play) .gdi-eq i{animation:none;height:3px}' +
      '.gdi button{flex:none;border:0;background:rgba(255,255,255,.09);color:#fff;width:30px;height:30px;border-radius:50%;display:grid;place-items:center;padding:0;cursor:pointer}.gdi button:active{background:rgba(255,255,255,.2)}.gdi button svg{width:15px;height:15px;fill:currentColor}' +
      '.gdi-x,.gdi-pv,.gdi-nx{display:none!important}.gdi.big .gdi-x,.gdi.big .gdi-pv,.gdi.big .gdi-nx{display:grid!important}.gdi.solo .gdi-pv,.gdi.solo .gdi-nx{display:none!important}' +
      '.gdi-bar{height:3px;margin:0 12px 7px;border-radius:2px;background:rgba(255,255,255,.12);overflow:hidden;display:none}.gdi.big .gdi-bar{display:block}.gdi-bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,#47b0f5,#a066ff);border-radius:2px}' +
      '@media(prefers-reduced-motion:reduce){.gdi,.gdi-eq i{transition:none;animation:none}}';
    document.head.appendChild(c);
  }
  function islandSrc() {
    if (PL.a && PL.a.src && !PL.dismissed && PL.started && !PL.open) return {kind: 'pl', a: PL.a, key: PL.list[PL.i], multi: PL.list.length > 1};
    if (I.a && I.a.src) return {kind: 'p', a: I.a, key: I.key, multi: false};
    return null;
  }
  function islandKill() { if (I.a) { try { I.a.pause(); } catch (e) {} I.a = null; } islandTick(true); }
  function islandClose() {
    var sr = islandSrc(); if (!sr) return;
    if (sr.kind === 'pl') { PL.dismissed = true; try { PL.a.pause(); } catch (e) {} }
    else { var a = I.a; I.a = null; if (a) { var v0 = a.volume, t0 = performance.now(); (function step(now) { var q = Math.min(1, (now - t0) / 500); a.volume = Math.max(0, v0 * (1 - q)); if (q < 1) requestAnimationFrame(step); else try { a.pause(); } catch (e) {} })(t0); } }
    IS.open = false; islandTick(true);
  }
  function islandTick(force) {
    var sr = islandSrc();
    if (sr && sr.a.ended && sr.kind === 'p') { I.a = null; sr = null; }
    if (!sr) { if (IS.el) { IS.el.classList.remove('on', 'big'); } IS.key = null; return; }
    islandCss();
    if (!IS.el) {
      var el = document.createElement('div'); el.className = 'gdi'; el.setAttribute('role', 'region'); el.setAttribute('aria-label', 'Мини-плеер');
      el.innerHTML = '<div class="gdi-r"><span class="gdi-art"></span><span class="gdi-t"><b></b><small></small></span><span class="gdi-eq"><i></i><i></i><i></i><i></i></span><button type="button" class="gdi-pv" aria-label="Предыдущий">' + PREV + '</button><button type="button" class="gdi-pp" aria-label="Пауза">' + PAUSE + '</button><button type="button" class="gdi-nx" aria-label="Следующий">' + NEXT + '</button><button type="button" class="gdi-x" aria-label="Закрыть"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" fill="none"/></svg></button></div><div class="gdi-bar"><i></i></div>';
      el.addEventListener('click', function (e) {
        var b = e.target.closest('button'), s2 = islandSrc();
        if (b) {
          e.stopPropagation(); if (!s2) return;
          if (b.classList.contains('gdi-pp')) { if (s2.a.paused) { s2.a.play().catch(function () {}); } else s2.a.pause(); }
          else if (b.classList.contains('gdi-nx')) plStep(1); else if (b.classList.contains('gdi-pv')) plStep(-1); else if (b.classList.contains('gdi-x')) islandClose();
          islandTick(true); return;
        }
        IS.open = !IS.open; el.classList.toggle('big', IS.open); clearTimeout(IS.t); if (IS.open) IS.t = setTimeout(function () { IS.open = false; el.classList.remove('big'); }, 6000);
      });
      document.body.appendChild(el); IS.el = el;
    }
    var el2 = IS.el, key = sr.key, title = (KEYS[key] || '').replace(/</g, '&lt;');
    if (IS.key !== key || force === 'redraw') {
      IS.key = key; el2.querySelector('.gdi-art').innerHTML = window.GDMedals ? GDMedals.svg(key, 30, true) : '';
      el2.querySelector('b').textContent = KEYS[key] || 'Трек медали';
    }
    var pl = !sr.a.paused;
    el2.classList.add('on'); el2.classList.toggle('play', pl); el2.classList.toggle('solo', !sr.multi);
    el2.querySelector('small').textContent = pl ? 'Играет' : 'Пауза';
    var pp = el2.querySelector('.gdi-pp'); pp.innerHTML = pl ? PAUSE : PLAY;
    var bar = el2.querySelector('.gdi-bar i'); if (bar && sr.a.duration) bar.style.width = (sr.a.currentTime / sr.a.duration * 100).toFixed(1) + '%';
  }
  setInterval(function () { try { islandTick(); } catch (e) {} }, 300);
  window.GDMusic = {dbg: function () { return {I: I, PL: PL}; }, island: islandTick, fadeOut: fadeOut, enabled: enabled, setEnabled: setEnabled, KEYS: KEYS, exists: exists, playShort: playShort, stop: stop, win: win, winKey: function (k) { READY.then(function () { if (KEYS[k]) winFaded(k); }); }, hypeStart: hypeStart, hypeWin: hypeWin, hypeEnd: hypeEnd};
})();
