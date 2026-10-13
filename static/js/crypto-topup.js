(() => {
  window.GEMDROP_ADMIN_PAGES=new Set(['adminPage','portalPage','usersPage','userPage','withdrawalsPage','transactionsPage','depositSettingsPage','promocodesPage','rtpPage','loaderSettingsPage','rollAdminPage','levelsAdminPage','userActivityPage','transferSettingsPage','winsAdminPage','postAdminPage','externalChannelsPage','freebetsAdminPage','botSettingsPage','sectionSettingsPage','giveawaysAdminPage','rewardTasksAdminPage','gameHistoryPage','gamesAdminPage','giftEmojiAdminPage','broadcastAdminPage','noticeTemplatesPage','creatorsAdminPage','antifraudPage','maintenancePage','halloweenPage','wheelAdminPage','xhuntAdminPage']);
  const ids=['chooseCryptoDeposit','cryptoTopupCreate','cryptoTopupVerify','cryptoDepositStatus'];
  if(ids.some(id=>!document.getElementById(id)))return;
  const get=id=>document.getElementById(id);
  let order=null,busy=false,enabled=false,testnet=false;
  try{order=JSON.parse(sessionStorage.getItem('gemdrop_crypto_order') || 'null')}catch(_){}
  function status(text){get('cryptoDepositStatus').textContent=text;}
  function remember(d){order=d;try{if(d)sessionStorage.setItem('gemdrop_crypto_order',JSON.stringify(d));else sessionStorage.removeItem('gemdrop_crypto_order')}catch(_){}render();}
  function render(){get('cryptoTopupCreate').disabled=busy || !!order;get('cryptoTopupVerify').disabled=busy;get('cryptoTopupVerify').classList.toggle('hidden',!order);const a=get('cryptoTopupLink');a.classList.toggle('hidden',!order);a.removeAttribute('href');if(order && /^https:\/\/(?:t\.me|pay\.crypt\.bot|testnet-pay\.crypt\.bot)\//.test(order.invoice_url || ''))a.href=order.invoice_url;if(order)get('cryptoTopupAmount').value=order.amount;get('cryptoTopupAmount').disabled=!!order;}
  async function load(){if(order&&String(order.user_id)!==String(user?.id))remember(null);try{const d=await api('/api/cryptobot/settings');enabled=d.enabled;testnet=d.testnet;get('chooseCryptoDeposit').hidden=!enabled&&!order;get('chooseCryptoDeposit').classList.toggle('hidden',!enabled&&!order);get('chooseCryptoDeposit').querySelector('small').textContent=testnet?'Тестовый счёт · без зачисления':'Оплата TON в Telegram';}catch(_){} }
  get('chooseCryptoDeposit').onclick=()=>{showDepositStep('crypto');render();status(order?'Счёт сохранён. Оплатите его и проверьте зачисление.':testnet?'Тестовая сеть: реальные средства не зачисляются.':'');};
  get('cryptoDepositBack').onclick=()=>showDepositStep('method');
  get('cryptoTopupCreate').onclick=async()=>{if(busy || !enabled || order)return;busy=true;render();try{const d=await api('/api/cryptobot/deposit/create',{method:'POST',body:JSON.stringify({amount:get('cryptoTopupAmount').value})});remember({...d,user_id:user?.id});status(d.testnet?'Создан тестовый счёт. Реальный баланс не изменится.':'Счёт готов. Оплатите его в CryptoBot.');}catch(e){status(e.message)}finally{busy=false;render();}};
  async function verify(){if(busy || !order)return;busy=true;render();try{const d=await api('/api/cryptobot/deposit/'+encodeURIComponent(order.order_id)+'/verify',{method:'POST',body:'{}'});if(['credited','test_paid'].includes(d.status)){if(d.user)setUser(d.user);remember(null);status(d.status==='credited'?'Пополнение подтверждено и зачислено.':'Тестовая оплата подтверждена. Реальный баланс не изменён.');toast(d.status==='credited'?'Пополнение CryptoBot зачислено':'Тестовая оплата подтверждена');}else if(d.status==='expired'){remember(null);status('Срок счёта истёк. Создайте новый.');}else status('Оплата пока не подтверждена. Повторите проверку после оплаты.');}catch(e){status(e.message)}finally{busy=false;render();}}
  get('cryptoTopupVerify').onclick=verify;
  window.addEventListener('focus',()=>{if(order && get('depositModal')?.dataset.method==='crypto')verify();});
  // Load only after login; the operator controls need no payment SDK.
  const observer=new MutationObserver(()=>{if(!get('content')?.classList.contains('hidden')){observer.disconnect();load();}});
  if(!get('content')?.classList.contains('hidden'))load();else observer.observe(get('content'),{attributes:true,attributeFilter:['class']});
  get('depositMini')?.addEventListener('click',load);
  render();
})();
