/* New transfer flow: recipient -> amount -> confirm. Replaces the old openTransfer() window; reuses its receipt, API and globals. */
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (v) { return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); };
  var CSS = '.trx{z-index:2450;background:rgba(5,4,12,.66)}.trx.hidden{display:none}' +
    '.trx-card{position:relative;width:min(100%,420px)!important;min-height:420px;display:flex;flex-direction:column;padding:20px 20px 18px!important;text-align:left!important;box-sizing:border-box}' +
    '.trx-top{display:flex;align-items:center;gap:10px;margin-bottom:6px}.trx-bar{display:flex;gap:4px;flex:1}.trx-bar i{flex:1;height:3px;border-radius:3px;background:rgba(255,255,255,.14)}.trx-bar i.on{background:linear-gradient(110deg,#9ae9e3,#47b0f5)}' +
    '.trx-x{width:32px;height:32px;border-radius:50%;border:0;background:rgba(255,255,255,.08);color:inherit;font-size:18px;line-height:1;padding:0;cursor:pointer;flex:none}' +
    '.trx-body{flex:1;display:flex;flex-direction:column;align-items:stretch;padding-top:12px}' +
    '.trx h2{margin:0;font-size:22px;font-weight:800;letter-spacing:-.01em}.trx-sub{margin:6px 0 16px;color:var(--muted,#96a0a8);font-size:13.5px;line-height:1.4}' +
    '.trx-field{position:relative;display:flex;align-items:center;gap:10px;height:48px;padding:0 14px;border-radius:14px;background:rgba(255,255,255,.045);border:1px solid var(--gd-line,#22253d);box-sizing:border-box;transition:border-color .18s}.trx-field:focus-within{border-color:#47b0f5}.trx-field.ok{border-color:#2fd163}.trx-field.bad{border-color:#ff453a}' +
    '.trx .trx-field input,.trx .trx-field input:focus{flex:1;min-width:0;width:auto;height:100%;border:0!important;outline:0!important;box-shadow:none!important;background:none!important;padding:0!important;border-radius:0!important;color:inherit;font:inherit;font-size:16px}.trx-field input::placeholder{color:#6d737a}' +
    '.trx-av{width:28px;height:28px;border-radius:50%;overflow:hidden;background:rgba(255,255,255,.1);display:grid;place-items:center;font-size:13px;font-weight:700;flex:none}.trx-av img{width:100%;height:100%;object-fit:cover}.trx-av:empty{display:none}' +
    '.trx-spin{width:18px;height:18px;border-radius:50%;border:2.5px solid #444;border-top-color:#bbb;animation:trxSpin .8s linear infinite;flex:none}@keyframes trxSpin{to{transform:rotate(360deg)}}' +
    '.trx-st{min-height:38px;margin-top:8px;font-size:13px;line-height:1.35}.trx-st.ok{color:#2fd163}.trx-st.bad{color:#ff6b61}' +
    '.trx-chip{align-self:flex-start;display:inline-flex;align-items:center;gap:8px;height:34px;padding:0 12px 0 4px;border-radius:17px;background:rgba(255,255,255,.06);font-size:14px;font-weight:700}' +
    '.trx-seg{display:grid;grid-template-columns:1fr 1fr;margin:12px 0 14px;padding:3px;border-radius:14px;background:rgba(255,255,255,.045);border:1px solid var(--gd-line,#22253d)}.trx-seg button{display:flex;align-items:center;justify-content:center;gap:7px;height:36px;border:0;border-radius:11px;background:none;color:var(--muted,#96a0a8);font:inherit;font-size:14px;font-weight:700;cursor:pointer}.trx-seg button img{width:17px;height:17px}.trx-seg button.on{background:rgba(71,176,245,.16);color:var(--text,#f4f5f7)}' +
    '.trx-av2{text-align:center;font-size:13px;color:var(--muted,#96a0a8)}.trx .trx-amt{display:block;width:100%;margin:6px 0 2px;border:0!important;outline:0!important;box-shadow:none!important;background:none!important;color:inherit;text-align:center;font:inherit;font-size:48px;font-weight:800;letter-spacing:-.03em;padding:0!important}.trx-amt::placeholder{color:#3b3d4a}.trx-amt.big{font-size:38px}' +
    '.trx-mini{display:flex;justify-content:center;margin:6px 0 8px}.trx-mini button{height:30px;padding:0 14px;border-radius:15px;border:1px solid var(--gd-line,#22253d);background:rgba(255,255,255,.045);color:inherit;font:inherit;font-size:13px;font-weight:700;cursor:pointer}' +
    '.trx-fee{min-height:18px;text-align:center;color:var(--muted,#96a0a8);font-size:12.5px}.trx-rev{border-radius:16px;background:rgba(255,255,255,.04);border:1px solid var(--gd-line,#22253d);overflow:hidden}.trx-rev div{display:flex;justify-content:space-between;gap:12px;padding:11px 14px;font-size:14px}.trx-rev div+div{border-top:1px solid var(--gd-line,#22253d)}.trx-rev span{color:var(--muted,#96a0a8)}.trx-rev b{font-weight:700}' +
    '.trx-go{width:100%;height:48px;margin-top:14px;border:1px solid rgba(242,245,249,.3);border-radius:14px;background:linear-gradient(110deg,#9ae9e3,#47b0f5);color:#131524;font:inherit;font-size:15px;font-weight:800;cursor:pointer;flex:none;transition:opacity .15s,transform .15s}.trx-go:active{transform:scale(.985)}.trx-go:disabled{opacity:.45;cursor:default}.trx-go.sec{background:rgba(255,255,255,.06);border-color:var(--gd-line,#22253d);color:inherit;font-weight:700}' +
    '.trx-two{display:grid;grid-template-columns:1fr 1.4fr;gap:10px;margin-top:14px}.trx-two .trx-go{margin-top:0}' +
    '.trx-lock{margin:12px 0;padding:14px;border-radius:14px;background:rgba(255,255,255,.05);color:var(--muted,#c9ced6);font-size:14px}' +
    '.trx-done{align-items:center;text-align:center;justify-content:center}.trx-ok{width:104px;height:104px;margin:10px 0 14px}.trx-ok circle{fill:none;stroke:#2fd163;stroke-width:4;stroke-dasharray:302;stroke-dashoffset:302;transform:rotate(-90deg);transform-origin:50% 50%;animation:trxC .6s ease-out forwards}.trx-ok path{fill:none;stroke:#2fd163;stroke-width:5;stroke-linecap:round;stroke-linejoin:round;stroke-dasharray:60;stroke-dashoffset:60;animation:trxK .35s .55s ease-out forwards}.trx-okbg{animation:trxP .5s .9s ease-out}@keyframes trxC{to{stroke-dashoffset:0}}@keyframes trxK{to{stroke-dashoffset:0}}@keyframes trxP{0%{transform:scale(1)}40%{transform:scale(1.12)}100%{transform:scale(1)}}' +
    '.trx-done h2{opacity:0;animation:trxF .4s 1s forwards}.trx-done .trx-sub,.trx-done .trx-rev,.trx-done .trx-go{opacity:0;animation:trxF .4s 1.15s forwards}@keyframes trxF{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}';
  var st = document.createElement('style'); st.textContent = CSS; document.head.appendChild(st);

  var S = {step: 'a', to: null, src: 'main', timer: 0, seq: 0, pending: null};
  var root;
  function build() {
    if (root) return root;
    root = document.createElement('div'); root.className = 'modal trx hidden'; root.setAttribute('role', 'dialog'); root.setAttribute('aria-modal', 'true');
    root.innerHTML = '<div class="modal-card trx-card"><div class="trx-top"><div class="trx-bar" id="trxBar"><i class="on"></i><i></i><i></i></div><button type="button" class="trx-x" id="trxX" aria-label="Закрыть">×</button></div><div class="trx-body" id="trxBody"></div></div>';
    document.body.appendChild(root);
    root.querySelector('#trxX').onclick = close;
    root.addEventListener('click', function (e) { if (e.target === root) close(); });
    return root;
  }
  function close() { if (root) { root.classList.add('hidden'); var b = $('trxBody'); if (b) b.className = 'trx-body'; } S.step = 'a'; }
  function bar(n) { var i = root.querySelectorAll('#trxBar i'); for (var k = 0; k < i.length; k++) i[k].classList.toggle('on', k < n); }
  function avInto(el, u) { el.textContent = u && u.name ? u.name.charAt(0).toUpperCase() : ''; if (u && u.photo_url) { var im = new Image(); im.src = u.photo_url; im.alt = ''; im.onerror = function () { im.remove(); }; el.replaceChildren(im); } }
  function bal() { return Number(S.src === 'bonus' ? (user && user.bonus_balance) : (user && user.balance)) || 0; }
  function cents(v) { v = String(v || '').trim().replace(',', '.'); return /^\d+(?:\.\d{1,2})?$/.test(v) ? Math.round(Number(v) * 100) : NaN; }
  function feeOf(c) { return Math.round(c * Number((typeof transferStatus !== 'undefined' && transferStatus && transferStatus.fee_percent) || 0) / 100); }

  /* ---- step A: recipient ---- */
  function stepA() {
    S.step = 'a'; bar(1);
    var b = $('trxBody');
    b.innerHTML = '<h2>Кому перевести?</h2><p class="trx-sub">Переводы доступны только игрокам GemDrop</p>' +
      '<div class="trx-field" id="trxF"><span class="trx-av" id="trxAv"></span><input id="trxU" placeholder="Юзернейм Telegram" autocomplete="off" autocapitalize="off" spellcheck="false" enterkeyhint="go"><span id="trxSp" hidden class="trx-spin"></span></div><div class="trx-st" id="trxS"></div><div style="flex:1"></div>' +
      '<button type="button" class="trx-go" id="trxNext" disabled>Продолжить</button>';
    var inp = $('trxU'), f = $('trxF'), stt = $('trxS'), next = $('trxNext'), sp = $('trxSp');
    if (S.to) { inp.value = S.to.username || ''; avInto($('trxAv'), S.to); f.classList.add('ok'); stt.className = 'trx-st ok'; stt.textContent = '✓ Игрок GemDrop'; next.disabled = false; }
    inp.oninput = function () {
      S.to = null; transferResolved = null; $('trxAv').textContent = ''; f.className = 'trx-field'; stt.textContent = ''; next.disabled = true; next.textContent = 'Продолжить'; next.onclick = go;
      clearTimeout(S.timer); var v = inp.value.trim().replace(/^@/, '');
      if (!/^[A-Za-z0-9_]{5,32}$/.test(v)) { sp.hidden = true; return; }
      var seq = ++S.seq; sp.hidden = false;
      S.timer = setTimeout(function () {
        api('/api/transfers/recipient?username=' + encodeURIComponent(v)).then(function (d) {
          if (seq !== S.seq) return; sp.hidden = true; S.to = d.user; transferResolved = d.user; avInto($('trxAv'), d.user);
          f.className = 'trx-field ok'; stt.className = 'trx-st ok'; stt.textContent = '✓ Игрок GemDrop'; next.disabled = false;
        }).catch(function (e) {
          if (seq !== S.seq) return; sp.hidden = true; f.className = 'trx-field bad'; stt.className = 'trx-st bad';
          stt.innerHTML = 'Игрока нет в GemDrop<br>Проверьте юзернейм или пригласите'; next.disabled = false; next.textContent = 'Пригласить в GemDrop';
          next.onclick = function () { var sh = document.getElementById('shareReferral'); if (sh) sh.click(); };
        });
      }, 380);
    };
    function go() { if (S.to) stepB(); }
    next.onclick = go;
    inp.onkeydown = function (e) { if (e.key === 'Enter' && !next.disabled) next.click(); };
    setTimeout(function () { inp.focus(); }, 60);
  }

  /* ---- step B: amount ---- */
  function stepB() {
    S.step = 'b'; bar(2);
    var b = $('trxBody'), ts = (typeof transferStatus !== 'undefined' && transferStatus) || {};
    b.innerHTML = '<div class="trx-chip"><span class="trx-av" id="trxAv2"></span>@' + esc(S.to.username) + '</div>' +
      '<div class="trx-seg" id="trxSeg"><button type="button" data-s="main"><img src="/static/img/ton.png" alt="">Основной</button><button type="button" data-s="bonus"><img src="/static/img/ton2.png" alt="">Бонусный</button></div>' +
      '<div class="trx-av2" id="trxAvail"></div><input id="trxAmt" class="trx-amt" inputmode="decimal" placeholder="0.00" autocomplete="off"><div class="trx-mini"><button type="button" id="trxMax">Макс.</button></div><div class="trx-fee" id="trxFee"></div><div style="flex:1"></div>' +
      '<button type="button" class="trx-go" id="trxGo" disabled>Введите сумму</button>';
    avInto($('trxAv2'), S.to);
    var amt = $('trxAmt'), go = $('trxGo');
    function paint() {
      root.querySelectorAll('#trxSeg button').forEach(function (x) { x.classList.toggle('on', x.dataset.s === S.src); });
      $('trxAvail').textContent = 'Доступно: ' + money(bal()) + ' TON';
      var c = cents(amt.value), fee = Number.isFinite(c) ? feeOf(c) : 0;
      amt.classList.toggle('big', amt.value.length > 7);
      $('trxFee').textContent = ts.fee_percent ? ('Комиссия ' + ts.fee_percent + '%' + (c > 0 ? ' · ' + money(fee / 100) + ' TON' : '')) : 'Без комиссии';
      if (!amt.value) { go.disabled = true; go.textContent = 'Введите сумму'; }
      else if (!Number.isFinite(c) || c < 10) { go.disabled = true; go.textContent = 'Минимум 0.10 TON'; }
      else if ((c + fee) / 100 > bal()) { go.disabled = true; go.textContent = 'Недостаточно средств'; }
      else { go.disabled = false; go.textContent = 'Перевести ' + money(c / 100) + ' TON'; }
    }
    root.querySelectorAll('#trxSeg button').forEach(function (x) { x.onclick = function () { S.src = x.dataset.s; transferBalanceSource = S.src; S.pending = null; paint(); }; });
    transferBalanceSource = S.src;
    $('trxMax').onclick = function () { var f = Number(ts.fee_percent || 0) / 100, m = Math.floor(bal() * 100 / (1 + f)) / 100; amt.value = m > 0 ? String(m) : ''; paint(); };
    amt.oninput = function () { S.pending = null; amt.value = amt.value.replace(',', '.').replace(/[^0-9.]/g, '').replace(/(\..*)\./g, '$1'); paint(); };
    go.onclick = function () { if (!go.disabled) stepC(cents(amt.value)); };
    root._amt = amt;
    paint(); setTimeout(function () { amt.focus(); }, 60);
  }

  /* ---- step C: confirm ---- */
  function stepC(c) {
    S.step = 'c'; bar(3);
    var fee = feeOf(c), b = $('trxBody');
    b.innerHTML = '<h2 style="margin-top:6px">Проверьте перевод</h2><p class="trx-sub" style="margin-bottom:14px">Деньги придут получателю сразу</p>' +
      '<div class="trx-rev"><div><span>Получатель</span><b>@' + esc(S.to.username) + '</b></div><div><span>Баланс</span><b>' + (S.src === 'bonus' ? 'Бонусный' : 'Основной') + '</b></div><div><span>Сумма</span><b>' + money(c / 100) + ' TON</b></div><div><span>Комиссия</span><b>' + money(fee / 100) + ' TON</b></div><div><span>Спишется</span><b>' + money((c + fee) / 100) + ' TON</b></div></div>' +
      '<div style="flex:1"></div><div class="trx-two"><button type="button" class="trx-go sec" id="trxBack">Назад</button><button type="button" class="trx-go" id="trxSend">Подтвердить</button></div>';
    $('trxBack').onclick = stepB_keep;
    $('trxSend').onclick = function () {
      var btn = this; if (btn.disabled) return; btn.disabled = true;
      var id = S.pending || (crypto && crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2)).replace(/-/g, ''); S.pending = id;
      api('/api/transfers/send', {method: 'POST', body: JSON.stringify({username: S.to.username, amount: (c / 100).toFixed(2), request_id: id, balance_source: S.src})}).then(function (d) {
        S.pending = null; setUser(d.user); done(c, fee);
      }).catch(function (e) { toast(e.message); btn.disabled = false; });
    };
  }
  function done(c, fee) {
    S.step = 'd'; bar(3);
    var b = $('trxBody'), to = S.to; b.className = 'trx-body trx-done';
    b.innerHTML = '<svg class="trx-ok" viewBox="0 0 104 104" aria-hidden="true"><g class="trx-okbg"><circle cx="52" cy="52" r="48"/><path d="M30 54l16 16 29-33"/></g></svg><h2>Перевод выполнен</h2><p class="trx-sub">Деньги уже у получателя</p>' +
      '<div class="trx-rev" style="width:100%;text-align:left"><div><span>Получатель</span><b>@' + esc(to.username) + '</b></div><div><span>Сумма</span><b>' + money(c / 100) + ' TON</b></div><div><span>Списано</span><b>' + money((c + fee) / 100) + ' TON</b></div></div>' +
      '<button type="button" class="trx-go" id="trxDone">Готово</button>';
    $('trxDone').onclick = function () { b.className = 'trx-body'; close(); };
    S.to = null;
  }
  function stepB_keep() { var v = root._amt ? root._amt.value : ''; stepB(); if (v) { $('trxAmt').value = v; $('trxAmt').dispatchEvent(new Event('input')); } }

  window.openTransfer = async function () {
    try {
      transferStatus = await api('/api/transfers/status');
      build(); S.to = null; S.src = 'main'; S.pending = null; transferBalanceSource = 'main'; transferResolved = null;
      root.classList.remove('hidden');
      if (!transferStatus.enabled) {
        bar(1); $('trxBody').innerHTML = '<h2>Переводы отключены</h2><div class="trx-lock">Переводы для вашего уровня (' + esc(transferStatus.level) + ') сейчас отключены администратором.</div>';
        return;
      }
      stepA();
    } catch (e) { toast(e.message); }
  };
  var pb = document.getElementById('profileTransfer'); if (pb) pb.onclick = window.openTransfer;
})();
