/* New transfer flow: recipient -> amount -> confirm. Replaces the old openTransfer() window; reuses its receipt, API and globals. */
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (v) { return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); };
  var CSS = '.trx{position:fixed;inset:0;z-index:2450;display:flex;justify-content:center;background:#000;color:#fff;overflow:auto;-webkit-overflow-scrolling:touch}.trx.hidden{display:none}' +
    '.trx-in{position:relative;width:min(100%,480px);min-height:100%;display:flex;flex-direction:column;padding:calc(14px + env(safe-area-inset-top)) 18px calc(18px + env(safe-area-inset-bottom));box-sizing:border-box;background:radial-gradient(120% 260px at 50% 0,rgba(16,86,170,.55),rgba(0,0,0,0) 70%)}' +
    '.trx-top{display:flex;align-items:center;justify-content:space-between;height:40px;margin-bottom:6px}.trx-x,.trx-back{width:38px;height:38px;border-radius:50%;border:0;background:rgba(255,255,255,.09);color:#fff;font-size:20px;line-height:1;cursor:pointer;display:grid;place-items:center;padding:0}.trx-back svg{width:20px;height:20px;fill:none;stroke:#fff;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round}' +
    '.trx-bar{display:flex;gap:4px;flex:1;max-width:120px;margin:0 auto}.trx-bar i{flex:1;height:3px;border-radius:3px;background:rgba(255,255,255,.18)}.trx-bar i.on{background:#fff}.trx-sp{width:38px}' +
    '.trx-body{flex:1;display:flex;flex-direction:column;align-items:center;text-align:center;padding-top:clamp(40px,12vh,110px)}.trx-ico{width:34px;height:34px;margin-bottom:12px;fill:none;stroke:#fff;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}' +
    '.trx h2{margin:0;font-size:30px;font-weight:560;letter-spacing:-.02em}.trx-sub{margin:10px 0 24px;max-width:280px;color:#a7adb5;font-size:15px;line-height:1.35}' +
    '.trx-field{position:relative;width:100%;display:flex;align-items:center;gap:10px;height:50px;padding:0 16px;border-radius:25px;background:#151515;border:1.5px solid #333;box-sizing:border-box;transition:border-color .18s}.trx-field:focus-within{border-color:#1f7bff}.trx-field.ok{border-color:#2fd163}.trx-field.bad{border-color:#ff453a}' +
    '.trx-field input{flex:1;min-width:0;height:100%;border:0;outline:0;background:none;color:#fff;font:inherit;font-size:17px}.trx-field input::placeholder{color:#6d737a}.trx-av{width:28px;height:28px;border-radius:50%;overflow:hidden;background:#2a2a2a;display:grid;place-items:center;font-size:13px;font-weight:700;flex:none}.trx-av img{width:100%;height:100%;object-fit:cover}.trx-av:empty{display:none}' +
    '.trx-spin{width:18px;height:18px;border-radius:50%;border:2.5px solid #444;border-top-color:#bbb;animation:trxSpin .8s linear infinite;flex:none}@keyframes trxSpin{to{transform:rotate(360deg)}}' +
    '.trx-st{min-height:40px;margin-top:10px;font-size:13px;line-height:1.35}.trx-st.ok{color:#2fd163}.trx-st.bad{color:#ff453a}' +
    '.trx-chip{display:inline-flex;align-items:center;gap:8px;height:38px;padding:0 14px 0 6px;border-radius:19px;background:#1a1a1a;font-size:15px;font-weight:650}' +
    '.trx-seg{display:grid;grid-template-columns:1fr 1fr;width:100%;margin:14px 0 18px;padding:4px;border-radius:20px;background:#161616;border:1px solid #2a2a2a;box-sizing:border-box}.trx-seg button{display:flex;align-items:center;justify-content:center;gap:7px;height:38px;border:0;border-radius:16px;background:none;color:#8d939b;font:inherit;font-size:15px;font-weight:700;cursor:pointer}.trx-seg button img{width:18px;height:18px}.trx-seg button.on{background:#3a3a3d;color:#fff}' +
    '.trx-av2{font-size:13px;color:#8d939b}.trx-amt{display:block;width:100%;margin:6px 0 4px;border:0;outline:0;background:none;color:#fff;text-align:center;font:inherit;font-size:64px;font-weight:600;letter-spacing:-.03em}.trx-amt::placeholder{color:#3b3b3d}.trx-amt.big{font-size:52px}' +
    '.trx-mini{display:flex;justify-content:center;gap:8px;margin:8px 0 12px}.trx-mini button{height:34px;padding:0 14px;border-radius:17px;border:1px solid #2c2c2c;background:#151515;color:#fff;font:inherit;font-size:14px;font-weight:650;cursor:pointer}' +
    '.trx-fee{min-height:20px;color:#8d939b;font-size:13px}.trx-rev{width:100%;margin-top:8px;border-radius:20px;background:#121212;border:1px solid #262626;overflow:hidden;text-align:left}.trx-rev div{display:flex;justify-content:space-between;gap:12px;padding:13px 16px;font-size:15px}.trx-rev div+div{border-top:1px solid #222}.trx-rev span{color:#8d939b}.trx-rev b{font-weight:650}' +
    '.trx-go{width:100%;height:54px;margin-top:18px;border:0;border-radius:20px;background:#fff;color:#000;font:inherit;font-size:16px;font-weight:650;cursor:pointer;flex:none;transition:opacity .15s,transform .15s}.trx-go:active{transform:scale(.985)}.trx-go:disabled{background:#8f8f8f;color:#111;cursor:default}.trx-go.sec{background:#1d1d1d;color:#fff;margin-top:10px}' +
    '.trx-two{display:grid;grid-template-columns:1fr 1.4fr;gap:10px;margin-top:18px;width:100%}.trx-two .trx-go{margin-top:0}' +
    '.trx-lock{margin:18px 0;padding:14px;border-radius:16px;background:#1a1a1a;color:#c9ced6;font-size:14px}';
  var st = document.createElement('style'); st.textContent = CSS; document.head.appendChild(st);

  var S = {step: 'a', to: null, src: 'main', timer: 0, seq: 0, pending: null};
  var root;
  function build() {
    if (root) return root;
    root = document.createElement('div'); root.className = 'trx hidden'; root.setAttribute('role', 'dialog'); root.setAttribute('aria-modal', 'true');
    root.innerHTML = '<div class="trx-in"><div class="trx-top"><button type="button" class="trx-x" id="trxX" aria-label="Закрыть">×</button><div class="trx-bar" id="trxBar"><i class="on"></i><i></i><i></i></div><span class="trx-sp"></span></div><div class="trx-body" id="trxBody"></div></div>';
    document.body.appendChild(root);
    root.querySelector('#trxX').onclick = close;
    return root;
  }
  function close() { if (root) root.classList.add('hidden'); S.step = 'a'; }
  function bar(n) { var i = root.querySelectorAll('#trxBar i'); for (var k = 0; k < i.length; k++) i[k].classList.toggle('on', k < n); }
  function avInto(el, u) { el.textContent = u && u.name ? u.name.charAt(0).toUpperCase() : ''; if (u && u.photo_url) { var im = new Image(); im.src = u.photo_url; im.alt = ''; im.onerror = function () { im.remove(); }; el.replaceChildren(im); } }
  function bal() { return Number(S.src === 'bonus' ? (user && user.bonus_balance) : (user && user.balance)) || 0; }
  function cents(v) { v = String(v || '').trim().replace(',', '.'); return /^\d+(?:\.\d{1,2})?$/.test(v) ? Math.round(Number(v) * 100) : NaN; }
  function feeOf(c) { return Math.round(c * Number((typeof transferStatus !== 'undefined' && transferStatus && transferStatus.fee_percent) || 0) / 100); }

  /* ---- step A: recipient ---- */
  function stepA() {
    S.step = 'a'; bar(1);
    var b = $('trxBody');
    b.innerHTML = '<svg class="trx-ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4.5 20c.8-3.6 3.8-5.5 7.5-5.5s6.700 1.900 7.500 5.500"/></svg><h2>Кому перевести?</h2><p class="trx-sub">Переводы доступны только игрокам GemDrop</p>' +
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
        S.pending = null; setUser(d.user); close(); S.to = null; showTransferReceipt(d, false);
      }).catch(function (e) { toast(e.message); btn.disabled = false; });
    };
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
