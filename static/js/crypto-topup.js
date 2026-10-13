(() => {
  window.GEMDROP_ADMIN_PAGES=new Set(['adminPage','portalPage','usersPage','userPage','withdrawalsPage','transactionsPage','depositSettingsPage','promocodesPage','rtpPage','loaderSettingsPage','rollAdminPage','levelsAdminPage','userActivityPage','transferSettingsPage','winsAdminPage','postAdminPage','externalChannelsPage','freebetsAdminPage','botSettingsPage','sectionSettingsPage','giveawaysAdminPage','rewardTasksAdminPage','gameHistoryPage','gamesAdminPage','giftEmojiAdminPage','broadcastAdminPage','noticeTemplatesPage','creatorsAdminPage','antifraudPage','maintenancePage','halloweenPage','wheelAdminPage','xhuntAdminPage']);
  const ids=['chooseCryptoDeposit','cryptoTopupCreate','cryptoTopupVerify','cryptoDepositStatus'];
  if(ids.some(id=>!document.getElementById(id)))return;
  const get=id=>document.getElementById(id),key='gemdrop_crypto_order';
  let order=null,busy=false,enabled=false,testnet=false,timer=null,failures=0,lastCheck=0;
  try{order=JSON.parse(sessionStorage.getItem(key) || 'null')}catch(_){}
  if(order && (!order.order_id || !order.invoice_url))order=null;
  const style=document.createElement('link');style.rel='stylesheet';style.href='/static/css/crypto-payments.css';document.head.append(style);
  function status(text,tone='waiting'){get('cryptoDepositStatus').textContent=text;get('cryptoDepositStatus').dataset.state=tone;get('cryptoDepositStatus').hidden=!text;}
  function remember(d){order=d;clearTimeout(timer);timer=null;failures=0;try{if(d)sessionStorage.setItem(key,JSON.stringify(d));else sessionStorage.removeItem(key);}catch(_){}render();}
  function render(){
    get('cryptoTopupCreate').disabled=busy || !enabled;get('cryptoTopupCreate').classList.toggle('hidden',!!order);
    get('cryptoTopupCreate').textContent=busy&&!order?'Создаём счёт…':'Создать счёт';
    get('cryptoTopupVerify').disabled=busy;get('cryptoTopupVerify').classList.toggle('hidden',!order);
    get('cryptoTopupVerify').textContent=busy&&order?'Проверяем оплату…':'Проверить сейчас';
    const a=get('cryptoTopupLink');a.classList.toggle('hidden',!order);
    if(order && /^https:\/\/(?:t\.me|pay\.crypt\.bot|testnet-pay\.crypt\.bot)\//.test(order.invoice_url || ''))a.href=order.invoice_url;else a.removeAttribute('href');
    if(order)get('cryptoTopupAmount').value=order.amount;get('cryptoTopupAmount').disabled=!!order;
  }
  function schedule(delay=3000){clearTimeout(timer);if(order)timer=setTimeout(()=>verify(false),delay);}
  async function verify(manual=false){
    clearTimeout(timer);timer=null;
    if(!order)return;
    if(busy || (!manual && document.hidden)){schedule(document.hidden?8000:1000);return;}
    if(!manual && Date.now()-lastCheck<2500){schedule(2500-(Date.now()-lastCheck));return;}
    const pending=order;
    if(String(pending.user_id)!==String(user?.id)){remember(null);return;}
    busy=true;lastCheck=Date.now();render();
    try{
      const d=await api('/api/cryptobot/deposit/'+encodeURIComponent(pending.order_id)+'/verify',{method:'POST',body:'{}'});
      if(!order || order.order_id!==pending.order_id || String(user?.id)!==String(pending.user_id))return;
      failures=0;
      if(['credited','test_paid'].includes(d.status)){
        if(d.user)setUser(d.user);remember(null);
        status(d.status==='credited'?'Оплата подтверждена. Пополнение зачислено на баланс.':'Тестовая оплата подтверждена. Реальный баланс не изменён.','paid');
        toast(d.status==='credited'?'Пополнение CryptoBot зачислено':'Тестовая оплата подтверждена');
      }else if(d.status==='expired'){
        remember(null);status('Срок счёта истёк. Можно создать новый.','expired');
      }else status('Ожидаем оплату. Зачисление проверяется автоматически.');
    }catch(e){
      if(order?.order_id===pending.order_id){failures++;status(manual?e.message:'Проверка временно недоступна. Повторим автоматически.','retry');}
    }finally{busy=false;render();if(order)schedule(Math.min(15000,3000*Math.max(1,failures)));}
  }
  async function load(){
    if(order && String(order.user_id)!==String(user?.id))remember(null);
    try{const d=await api('/api/cryptobot/settings');enabled=d.enabled;testnet=d.testnet;
      get('chooseCryptoDeposit').hidden=!enabled&&!order;get('chooseCryptoDeposit').classList.toggle('hidden',!enabled&&!order);
      get('chooseCryptoDeposit').querySelector('small').textContent=testnet?'Тестовый счёт · без зачисления':'Оплата TON в Telegram';
    }catch(_){}
    render();if(order)verify(false);
  }
  get('chooseCryptoDeposit').onclick=()=>{showDepositStep('crypto');render();status(order?'Ожидаем оплату. Проверяем зачисление автоматически.':testnet?'Тестовая сеть: реальные средства не зачисляются.':'');if(order)verify(false);};
  get('cryptoDepositBack').onclick=()=>showDepositStep('method');
  get('cryptoTopupCreate').onclick=async()=>{
    if(busy || !enabled || order)return;
    const uid=user?.id;busy=true;render();status('Создаём счёт в CryptoBot…','checking');
    try{
      const d=await api('/api/cryptobot/deposit/create',{method:'POST',body:JSON.stringify({amount:get('cryptoTopupAmount').value})});
      if(uid!==user?.id)return;
      remember({...d,user_id:uid});status(d.testnet?'Создан тестовый счёт. Проверяем оплату автоматически.':'Счёт готов. Оплатите его в CryptoBot — баланс обновится автоматически.');
    }catch(e){status(e.message,'error');}finally{busy=false;render();if(order)verify(false);}
  };
  get('cryptoTopupVerify').onclick=()=>verify(true);
  get('cryptoTopupLink').onclick=e=>{const url=get('cryptoTopupLink').href;if(url.startsWith('https://t.me/')&&window.Telegram?.WebApp?.openTelegramLink){e.preventDefault();window.Telegram.WebApp.openTelegramLink(url);}schedule(3000);};
  window.addEventListener('focus',()=>{if(order)verify(false);});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden && order)verify(false);});
  window.addEventListener('pagehide',()=>clearTimeout(timer));
  window.addEventListener('pageshow',()=>{if(order && user?.id)verify(false);});
  const observer=new MutationObserver(()=>{if(!get('content')?.classList.contains('hidden')){observer.disconnect();load();}});
  if(!get('content')?.classList.contains('hidden'))load();else observer.observe(get('content'),{attributes:true,attributeFilter:['class']});
  get('depositMini')?.addEventListener('click',load);
  render();
})();
