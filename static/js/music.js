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
  function stop() { if (cur) { try { cur.pause(); } catch (e) {} cur.onended = null; cur = null; } }
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
    var v0 = a.volume, t0 = performance.now();
    (function step(now) { var q = Math.min(1, (now - t0) / 700); a.volume = Math.max(0, v0 * (1 - q)); if (q < 1) requestAnimationFrame(step); else try { a.pause(); } catch (e) {} })(t0);
  }
  var PREV = '<svg viewBox="0 0 24 24"><path d="M6 5h2v14H6zM20 5v14L9 12z"/></svg>', NEXT = '<svg viewBox="0 0 24 24"><path d="M16 5h2v14h-2zM4 5v14l11-7z"/></svg>';
  var PLAY = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>', PAUSE = '<svg viewBox="0 0 24 24"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>';
  window.GDMusicProfile = function (d) {
    removePlayer(); stop(); var tk = P.tk = (P.tk || 0) + 1; READY.then(function () { if (P.tk === tk) buildProfile(d, tk); });
  };
  function buildProfile(d, tk, idx, auto) {
    removePlayer();
    var chip = document.getElementById('ppMedals'); if (!chip) return;
    var keys = [], seenK = {};
    ((d && d.medals) || []).forEach(function (m) { if (KEYS[m.key] && !seenK[m.key]) { seenK[m.key] = 1; keys.push(m.key); } });
    if (!keys.length) return;
    idx = ((idx || 0) % keys.length + keys.length) % keys.length;
    var key = keys[idx], multi = keys.length > 1;
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
  window.GDMusic = {fadeOut: fadeOut, enabled: enabled, setEnabled: setEnabled, KEYS: KEYS, exists: exists, playShort: playShort, stop: stop, win: win, hypeStart: hypeStart, hypeWin: hypeWin, hypeEnd: hypeEnd};
})();
