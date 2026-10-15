/* Music medals: profile player (long track), preview and PvP-win jingle (short track). */
(function () {
  var KEYS = {kashtan: 'Миша Каштан', jealous: 'Jealous', warhol: 'Andy Warhol'};
  var ok = {}, cur = null, mineP = null;
  function url(k, short) { return '/static/music/' + k + (short ? '_short' : '') + '.mp3'; }
  function exists(k, short) {
    var u = url(k, short); if (ok[u] !== undefined) return ok[u];
    return ok[u] = fetch(u, {method: 'HEAD', credentials: 'same-origin'}).then(function (r) { return r.ok && /audio|octet/.test(r.headers.get('content-type') || 'audio'); }).catch(function () { return false; });
  }
  function stop() { if (cur) { try { cur.pause(); } catch (e) {} cur.onended = null; cur = null; } }
  function playShort(k, vol) {
    return exists(k, true).then(function (has) {
      if (!has) return false;
      stop(); var a = new Audio(url(k, true)); a.volume = vol == null ? .8 : vol; cur = a;
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
  function win() { myMusic().then(function (k) { if (k) playShort(k, .85); }); }
  window.GDMusicRefresh = function () { mineP = null; };

  /* ---- profile player ---- */
  var st = document.createElement('style');
  st.textContent = '.mpl{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:4px 12px;align-items:center;margin:12px 0 4px;padding:12px 14px;border-radius:18px;background:linear-gradient(135deg,rgba(71,176,245,.14),rgba(160,102,255,.12));border:1px solid rgba(71,176,245,.3)}' +
    '.mpl-art{grid-row:1/3;width:52px;height:52px;display:grid;place-items:center}.mpl-art .gdm{width:52px;height:auto}' +
    '.mpl-t{min-width:0;display:flex;flex-direction:column}.mpl-t b{font-size:14px;line-height:1.2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.mpl-t small{font-size:11.5px;color:var(--muted,#96a0a8)}' +
    '.mpl-pp{width:42px;height:42px;border-radius:50%;border:0;background:linear-gradient(110deg,#9ae9e3,#47b0f5);color:#131524;cursor:pointer;display:grid;place-items:center;padding:0}.mpl-pp svg{width:18px;height:18px;fill:#131524}' +
    '.mpl-bar{grid-column:2/4;display:flex;align-items:center;gap:8px;font-size:11px;color:var(--muted,#96a0a8)}.mpl-bar input[type=range]{flex:1;min-width:0;height:4px;accent-color:#47b0f5;cursor:pointer}' +
    '.mpl-eq{display:inline-flex;gap:2px;align-items:flex-end;height:11px;margin-right:6px}.mpl-eq i{width:2.5px;background:#47b0f5;border-radius:2px;height:4px}.mpl.on .mpl-eq i{animation:mplEq .9s ease-in-out infinite}.mpl-eq i:nth-child(2){animation-delay:.2s!important}.mpl-eq i:nth-child(3){animation-delay:.4s!important}@keyframes mplEq{0%,100%{height:3px}50%{height:11px}}';
  document.head.appendChild(st);
  var P = {a: null};
  function fmt(s) { s = Math.max(0, Math.floor(s || 0)); return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2); }
  function removePlayer() { var o = document.getElementById('ppPlayer'); if (o) o.remove(); if (P.a) { try { P.a.pause(); } catch (e) {} P.a = null; } }
  var PLAY = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>', PAUSE = '<svg viewBox="0 0 24 24"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>';
  window.GDMusicProfile = function (d) {
    removePlayer(); stop();
    var chip = document.getElementById('ppMedals'); if (!chip) return;
    var key = ((d && d.medals) || []).map(function (m) { return m.key; }).filter(function (k) { return KEYS[k]; })[0];
    if (!key) return;
    exists(key, false).then(function (has) {
      if (!has || !document.getElementById('ppMedals')) return;
      removePlayer();
      var el = document.createElement('div'); el.id = 'ppPlayer'; el.className = 'mpl';
      el.innerHTML = '<div class="mpl-art">' + (window.GDMedals ? GDMedals.svg(key, 52, true) : '') + '</div><div class="mpl-t"><b>' + KEYS[key] + '</b><small><span class="mpl-eq"><i></i><i></i><i></i></span>Трек медали</small></div><button type="button" class="mpl-pp" aria-label="Играть">' + PLAY + '</button>' +
        '<div class="mpl-bar"><span class="mpl-c">0:00</span><input type="range" min="0" max="1000" value="0" aria-label="Перемотка"><span class="mpl-d">0:00</span></div>';
      chip.parentNode.insertBefore(el, chip.nextSibling);
      var a = new Audio(url(key, false)); a.preload = 'auto'; P.a = a;
      var btn = el.querySelector('.mpl-pp'), rng = el.querySelector('input'), c = el.querySelector('.mpl-c'), du = el.querySelector('.mpl-d'), seeking = false;
      function ui() { var pl = !a.paused; el.classList.toggle('on', pl); btn.innerHTML = pl ? PAUSE : PLAY; }
      a.addEventListener('play', ui); a.addEventListener('pause', ui); a.addEventListener('ended', function () { ui(); rng.value = 0; });
      a.addEventListener('loadedmetadata', function () { du.textContent = fmt(a.duration); });
      a.addEventListener('timeupdate', function () { if (!seeking && a.duration) rng.value = a.currentTime / a.duration * 1000; c.textContent = fmt(a.currentTime); });
      rng.addEventListener('input', function () { seeking = true; if (a.duration) { a.currentTime = rng.value / 1000 * a.duration; c.textContent = fmt(a.currentTime); } });
      rng.addEventListener('change', function () { seeking = false; });
      btn.onclick = function () { if (a.paused) a.play().catch(function () {}); else a.pause(); };
      a.volume = .8; a.play().catch(function () {});       // starts by itself; the browser may need a tap
    });
  };
  var mo = document.getElementById('publicProfileModal');
  if (mo) new MutationObserver(function () { if (mo.classList.contains('hidden')) removePlayer(); }).observe(mo, {attributes: true, attributeFilter: ['class']});
  window.GDMusic = {KEYS: KEYS, exists: exists, playShort: playShort, stop: stop, win: win};
})();
