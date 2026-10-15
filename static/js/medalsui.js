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
  document.head.appendChild(st);

  function api(url, body) {
    return fetch(url, {method: body ? 'POST' : 'GET', credentials: 'same-origin', headers: body ? {'Content-Type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined})
      .then(function (r) { return r.json().then(function (j) { if (!r.ok || j.ok === false) throw new Error(j.error || 'Ошибка'); return j; }); });
  }
  function modal() {
    var m = $('mdlModal');
    if (m) return m;
    m = document.createElement('div'); m.id = 'mdlModal'; m.className = 'mdl-modal hidden'; m.setAttribute('role', 'dialog'); m.setAttribute('aria-modal', 'true');
    m.innerHTML = '<div class="mdl-card"><div class="mdl-top"><h2>Мои медали</h2><button type="button" class="mdl-x" aria-label="Закрыть">×</button></div><p class="mdl-sub" id="mdlSub"></p><div class="mdl-list" id="mdlList"></div></div>';
    m.addEventListener('click', function (e) { if (e.target === m || e.target.classList.contains('mdl-x')) m.classList.add('hidden'); });
    document.body.appendChild(m); return m;
  }
  function open() {
    var m = modal(); m.classList.remove('hidden');
    var list = $('mdlList'); list.innerHTML = '<div class="mdl-empty">Загрузка…</div>';
    api('/api/medals').then(function (d) {
      $('mdlSub').textContent = 'Медали выдаются за призовые места в сезонах и особые заслуги. Отметьте до ' + d.max_shown + ' — они появятся в вашем профиле.';
      if (!d.medals.length) { list.innerHTML = '<div class="mdl-empty">Пока нет медалей. Займите призовое место в лидерборде — и она появится здесь.</div>'; return; }
      list.innerHTML = d.medals.map(function (x) {
        return '<div class="mdl-item"><div>' + GDMedals.svg(x.key, 56) + '</div><div><b>' + esc(x.name) + '</b><small>' + esc(x.title || 'Сезон') + (x.place ? ' · ' + x.place + ' место' : '') + '</small></div>' +
          '<label class="mdl-sw">В профиле<input type="checkbox" data-id="' + x.id + '"' + (x.shown ? ' checked' : '') + '></label></div>';
      }).join('');
      list.querySelectorAll('input').forEach(function (i) {
        i.onchange = function () {
          api('/api/medals/' + i.dataset.id + '/shown', {shown: i.checked}).catch(function (e) { i.checked = !i.checked; if (window.toast) toast(e.message); });
        };
      });
    }).catch(function (e) { list.innerHTML = '<div class="mdl-empty">' + esc(e.message) + '</div>'; });
  }
  function mountRow() {
    if ($('pfMedals')) return true;
    var anchor = $('pfHaptics'); if (!anchor) return false;
    var row = document.createElement('div'); row.className = 'pf-row pf-row-btn'; row.id = 'pfMedals';
    row.innerHTML = '<span class="pf-row-t"><b>Мои медали</b><small>Награды за места в сезонах</small></span><button type="button" class="profile-settings pf-open" aria-label="Мои медали" style="display:grid;place-items:center">' + GDMedals.svg('hw1', 30, true) + '</button>';
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
      el.innerHTML = '<div class="mgt-card"><span class="mgt-k">Вам выдана медаль</span><div>' + GDMedals.svg(m.key, 170) + '</div><h3>' + esc(m.name) + '</h3><p>' + esc(m.title || 'Особая награда') + (m.place ? ' · ' + m.place + ' место' : '') + '</p><button type="button" class="mgt-btn">Получить</button></div>';
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
