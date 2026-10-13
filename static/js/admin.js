(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const money = n => Number(n || 0).toLocaleString('ru-RU', {minimumFractionDigits:2, maximumFractionDigits:2});
  const methods = {ton:'TON Connect', cryptobot:'CryptoBot', stars:'Stars', nft:'NFT'};
  const state = {view:'dashboard', offsets:{}, userId:null, request:0, chart:null, procurement:null};
  const toolGroups = [
    ['Игры','gamesAdminPage','Включение режимов'],['RTP и ставки','rtpPage','Вероятности, лимиты ставок'],
    ['Пополнение и Relayer','depositSettingsPage','TON, Stars, получение NFT'],['Portal','portalPage','Каталог, модели, цены, Partner token'],
    ['Правила вывода','withdrawalsPage','Минимум пополнений и заявки'],['Переводы между игроками','transferSettingsPage','Комиссии и доступ по уровням'],['Разделы приложения','sectionSettingsPage','Доступность разделов'],
    ['Промокоды','promocodesPage','Шаблоны и управление'],['Фрибеты','freebetsAdminPage','Коды и награды'],
    ['Уровни','levelsAdminPage','Награды за оборот'],['Розыгрыши','giveawaysAdminPage','Призы и участники'],
    ['Задания','rewardTasksAdminPage','Условия и награды'],['Колесо','wheelAdminPage','Призы и условия'],
    ['История игр','gameHistoryPage','Ставки и результаты'],['Операции баланса','transactionsPage','Журнал всех транзакций'],
    ['Победители','winsAdminPage','Лента выигрышей'],['Тексты бота','noticeTemplatesPage','Все уведомления'],
    ['Настройки бота','botSettingsPage','Меню и приветствия'],['Рассылки','broadcastAdminPage','Сообщения и аудитория'],
    ['Публикации','postAdminPage','Подготовка постов'],['Внешние каналы','externalChannelsPage','Источники и подписки'],
    ['Антифрод','antifraudPage','Проверка пользователей'],['Авторы','creatorsAdminPage','Demo и управление авторами'],
    ['Технические работы','maintenancePage','Доступность приложения'],['Экран загрузки','loaderSettingsPage','Настройки загрузчика'],
    ['Emoji подарков','giftEmojiAdminPage','Иконки в сообщениях'],['Halloween','halloweenPage','Событие и награды'],
    ['X Hunt','xhuntAdminPage','Настройка события'],['Все прежние инструменты','adminPage','Полное меню управления']
  ];
  function el(tag, text, cls) { const n = document.createElement(tag); if(text != null)n.textContent=text; if(cls)n.className=cls; return n; }
  function msg(text) { $('message').textContent=text; $('message').hidden=false; clearTimeout(msg.timer); msg.timer=setTimeout(()=>$('message').hidden=true,6500); }
  async function api(url, data) {
    const r = await fetch(url, {credentials:'same-origin', cache:'no-store', headers:{'Content-Type':'application/json'}, ...(data === undefined ? {} : {method:'POST',body:JSON.stringify(data)})});
    const d = await r.json().catch(()=>({error:'Сервер не ответил. Обновите страницу.'}));
    if(!r.ok)throw new Error(d.error || d.message || 'Ошибка '+r.status);
    return d;
  }
  async function action(button, fn) { button.disabled=true; try { await fn(); } catch(e) { msg(e.message); } finally { button.disabled=false; } }
  function button(text, fn) { const b=el('button',text); b.type='button'; b.onclick=()=>action(b,fn); return b; }
  function time(stamp) { if(!stamp)return '—'; const d=new Date(String(stamp).replace(' ','T')+(String(stamp).match(/Z$|[+-]\d\d:\d\d$/)?'':'Z')); return Number.isNaN(d.getTime()) ? stamp : d.toLocaleString('ru-RU'); }
  function userCell(row) { const n=el('div'); const b=el('button',row.name || row.user_name || row.sender_name || 'Игрок'); b.type='button'; const id=Object.hasOwn(row,'user_id')?row.user_id:Object.hasOwn(row,'sender_user_id')?row.sender_user_id:row.id; b.onclick=()=>openUser(id); if(!id){b.disabled=true;b.textContent=row.sender_name || 'Отправитель не определён';} n.append(b,el('small',(row.username ? '@'+row.username+' · ' : '')+(id?'ID '+id:'ID неизвестен'))); return n; }
  function table(root, headings, rows) {
    root.replaceChildren();
    root.classList.toggle('cards',root.id!=='seriesTable');
    if(!rows.length){root.append(el('div','Записей нет.','empty'));return;}
    const t=el('table'), thead=el('thead'), tr=el('tr'); headings.forEach(x=>tr.append(el('th',x)));thead.append(tr);t.append(thead);
    const body=el('tbody');rows.forEach(cells=>{const r=el('tr');cells.forEach((value,i)=>{const td=el('td');td.dataset.label=headings[i];td.append(value instanceof Node ? value : document.createTextNode(String(value ?? '—')));r.append(td)});body.append(r)});t.append(body);root.append(t);
  }
  function pager(id, data, key, reload) {
    const root=$(id), offset=state.offsets[key] || 0;root.replaceChildren();
    const prev=button('←',async()=>{state.offsets[key]=Math.max(0,offset-data.limit);await reload()});prev.disabled=offset===0;prev.setAttribute('aria-label','Предыдущая страница');
    const next=button('→',async()=>{state.offsets[key]=offset+data.limit;await reload()});next.disabled=offset+data.limit>=data.total;next.setAttribute('aria-label','Следующая страница');
    root.append(prev,el('span',data.total ? `${offset+1}–${Math.min(data.total,offset+data.limit)} из ${data.total}`:'0 записей'),next);
  }
  function query(form,key) { const params=new URLSearchParams(new FormData($(form)));params.set('offset',state.offsets[key] || 0);params.set('limit',50);return params; }
  function metric(label,value,detail='') { const d=el('div',null,'metric');d.append(el('span',label),el('strong',value),el('small',detail));return d; }
  function closeMenu() {document.body.classList.remove('menu-open');$('menuShade').hidden=true;$('menuToggle').setAttribute('aria-expanded','false');}
  $('menuToggle').onclick=()=>{const open=!document.body.classList.contains('menu-open');document.body.classList.toggle('menu-open',open);$('menuShade').hidden=!open;$('menuToggle').setAttribute('aria-expanded',String(open));};
  $('menuShade').onclick=$('menuClose').onclick=closeMenu;
  document.addEventListener('keydown',e=>{if(e.key==='Escape')closeMenu();});
  $('logout').onclick=()=>action($('logout'),async()=>{await api('/api/logout',{});try{sessionStorage.setItem('gemdrop_manual_logout','1')}catch(_){}location.assign('/');});
  document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>b.closest('dialog').close());
  $('refresh').onclick=()=>action($('refresh'),()=>{if(state.view==='tool'){$('toolFrame').src=$('toolFrame').src;return;}return loadView();});
  function toolLink(title,page,description) {const a=el('a');a.href='#tool='+encodeURIComponent(page);a.append(el('strong',title),el('small',description));return a;}
  function renderTools() {const q=$('toolSearch').value.trim().toLocaleLowerCase('ru');$('toolList').replaceChildren(...toolGroups.filter(x=>x.join(' ').toLocaleLowerCase('ru').includes(q)).map(x=>toolLink(...x)));}
  $('toolSearch').oninput=renderTools;renderTools();
  function navigate() {
    const hash=location.hash.slice(1) || 'dashboard';
    const isTool=hash.startsWith('tool=');state.view=isTool?'tool':hash;
    if(!document.querySelector(`[data-view][id="${CSS.escape(state.view)}"]`))state.view='dashboard';
    document.querySelectorAll('[data-view]').forEach(n=>n.hidden=n.id!==state.view);
    document.querySelectorAll('#navigation a').forEach(n=>n.classList.toggle('active',n.hash==='#'+state.view || (isTool && n.hash==='#settings')));
    closeMenu();
    if(isTool){const page=decodeURIComponent(hash.slice(5));const entry=toolGroups.find(x=>x[1]===page);const safePage=entry || (page==='userPage'?['Пользователь','userPage']:null);if(!safePage){location.hash='settings';return;} $('toolTitle').textContent=safePage[0];let src='/admin/tools?page='+encodeURIComponent(page);const uid=new URLSearchParams(location.search).get('user');if(uid && /^\d+$/.test(uid))src+='&user='+uid;$('toolFrame').src=src;}
    loadView().catch(e=>msg(e.message));
  }
  window.addEventListener('hashchange',navigate);
  window.addEventListener('message',e=>{if(e.origin!==location.origin || e.source!==$('toolFrame').contentWindow || e.data?.type!=='admin-tool-page')return;const title=toolGroups.find(x=>x[1]===e.data.page);if(title)$('toolTitle').textContent=title[0];});
  async function loadView() {const loader={dashboard:loadDashboard,payments:loadPayments,nfts:loadNfts,withdrawals:loadWithdrawals,users:loadUsers,grants:loadGrants,cryptobot:loadCrypto}[state.view];if(loader)await loader();}
  const svgNS='http://www.w3.org/2000/svg';
  function svg(tag,attrs,text) {const n=document.createElementNS(svgNS,tag);Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,v));if(text!=null)n.textContent=text;return n;}
  function drawChart(id,series,counts=false) {
    const host=$(id),width=Math.max(260,host.clientWidth),height=host.clientHeight || 350,L=55,R=12,T=22,B=36,w=width-L-R,h=height-T-B;
    const canvas=svg('svg',{viewBox:`0 0 ${width} ${height}`});host.replaceChildren(canvas);
    const peak=Math.max(1,...series.map(r=>counts?Math.max(r.deposit_count,r.withdrawal_count):Math.max(r.deposits,r.withdrawals)));
    const max=counts?Math.max(4,Math.ceil(peak/4)*4):peak*1.12;
    const x=i=>L+(i+.5)*w/series.length,y=n=>T+h-n/max*h;
    for(let i=0;i<=4;i++){const yy=T+i*h/4;canvas.append(svg('line',{x1:L,x2:width-R,y1:yy,y2:yy,stroke:'#333'}),svg('text',{x:L-7,y:yy+4,'text-anchor':'end'},counts?Math.ceil(max*(1-i/4)):Number((max*(1-i/4)).toFixed(2)).toLocaleString('ru-RU')));}
    const slots=width<500?3:7;const step=Math.max(1,Math.ceil(series.length/slots));
    series.forEach((r,i)=>{if(i%step===0 || i===series.length-1)canvas.append(svg('text',{x:x(i),y:height-9,'text-anchor':'middle'},r.day.slice(8)+'.'+r.day.slice(5,7)));});
    if(!counts){const bw=Math.max(1,Math.min(30,w/series.length*.7));series.forEach((r,i)=>{const cash=Math.max(0,r.deposits-r.nft);canvas.append(svg('rect',{x:x(i)-bw/2,y:y(cash),width:bw,height:h*cash/max,fill:'#72bf9c'}),svg('rect',{x:x(i)-bw/2,y:y(r.deposits),width:bw,height:h*r.nft/max,fill:'#c7a96b'}));});}
    for(const [field,color] of (counts?[['deposit_count','#72bf9c'],['withdrawal_count','#86b5ed']]:[['withdrawals','#86b5ed']])){
      const path=series.map((r,i)=>(i?'L':'M')+x(i)+','+y(r[field])).join(' ');canvas.append(svg('path',{d:path,fill:'none',stroke:color,'stroke-width':2.5}));
      if(series.length===1)canvas.append(svg('circle',{cx:x(0),cy:y(series[0][field]),r:4,fill:color}));
    }
    const marker=svg('line',{x1:0,x2:0,y1:T,y2:T+h,stroke:'#eee','stroke-dasharray':'4 3',visibility:'hidden'});canvas.append(marker);
    function point(e){const rect=canvas.getBoundingClientRect();const xx=(e.clientX-rect.left)*width/rect.width;const i=Math.max(0,Math.min(series.length-1,Math.floor((xx-L)/w*series.length)));const r=series[i];if(!r)return;marker.setAttribute('x1',x(i));marker.setAttribute('x2',x(i));marker.setAttribute('visibility','visible');$('chartPoint').textContent=`${r.day} · Пополнения ${money(r.deposits)} TON (${r.deposit_count}), из них NFT ${money(r.nft)} TON · Выводы ${money(r.withdrawals)} TON (${r.withdrawal_count})`;}
    canvas.onpointermove=point;canvas.onclick=point;
  }
  let dashboardRevision=0;
  async function loadDashboard(){const revision=++dashboardRevision;const d=await api('/api/admin/dashboard?days='+$('chartDays').value);if(revision!==dashboardRevision)return;state.chart=d;const t=d.totals;$('metrics').replaceChildren(metric('Реальные пополнения',money(t.deposits)+' TON',t.deposit_count+' операций'),metric('Завершённые выводы NFT',money(t.withdrawals)+' TON',t.withdrawal_count+' подарков'),metric('Ожидают вывода',t.pending_count,money(t.pending_value)+' TON'),metric('Пользователи',t.users,'Основной баланс: '+money(t.balance)+' TON'));drawChart('moneyChart',d.series);drawChart('countChart',d.series,true);$('methods').replaceChildren(...d.methods.map(m=>{const row=el('div',null,'method-row');row.append(el('span',methods[m.method] || m.method),el('strong',money(m.total)+' TON · '+m.count));return row;}));if(!d.methods.length)$('methods').append(el('p','За период пополнений нет.','muted'));table($('seriesTable'),['Дата UTC','Пополнения TON','Из них NFT','Выводы TON'],d.series.map(r=>[r.day,money(r.deposits),money(r.nft),money(r.withdrawals)]));}
  $('chartDays').onchange=()=>loadDashboard().catch(e=>msg(e.message));
  new ResizeObserver(()=>{if(state.chart && state.view==='dashboard'){drawChart('moneyChart',state.chart.series);drawChart('countChart',state.chart.series,true);}}).observe($('moneyChart'));
  function paymentRows(items){return items.map(r=>{const nft=el('div',r.gift_name || methods[r.method] || r.method);if(r.model)nft.append(el('small',r.model));if(/^https:\/\/(t\.me|fragment\.com)\//.test(r.external_url || '')){const a=el('a','Открыть NFT');a.href=r.external_url;a.target='_blank';a.rel='noopener';nft.append(el('br'),a);}const ref=el('div',r.reference);ref.append(el('small',time(r.created_at)));return [userCell(r),methods[r.method] || r.method,money(r.amount)+' TON',nft,ref]});}
  let paymentRevision=0;
  async function loadPayments(){const rev=++paymentRevision,d=await api('/api/admin/payments?'+query('paymentFilters','payments'));if(rev!==paymentRevision)return;table($('paymentTable'),['Пользователь','Способ','Сумма','Подарок / назначение','Операция / время'],paymentRows(d.items));pager('paymentPager',d,'payments',loadPayments);}
  async function loadNfts(){const d=await api('/api/admin/nft-receipts?'+query('nftFilters','nfts'));table($('nftTable'),['Отправитель','NFT','Portal / принято','Результат','Получено'],d.items.map(r=>{const gift=el('div',r.gift_name+' #'+(r.fragment_number || '—'));if(/^https:\/\/t\.me\/nft\//.test(r.external_url)){const a=el('a','Открыть');a.href=r.external_url;a.target='_blank';a.rel='noopener';gift.append(el('br'),a);}const status={delivered:'Зачислен в инвентарь',credited:'Зачислен на баланс',unpriced:'Нет оценки',unmatched:'Не найден отправитель',baseline:'Был на аккаунте до запуска',not_nft:'Не NFT'}[r.status] || r.status;return [userCell(r),gift,money(r.portal_price)+' / '+money(r.floor_price)+' TON',status,time(r.created_at)]}));pager('nftPager',d,'nfts',loadNfts);}
  let userRevision=0;
  async function loadUsers(){const rev=++userRevision,d=await api('/api/admin/players?'+query('userFilters','users'));if(rev!==userRevision)return;table($('userTable'),['Пользователь','Баланс / бонус','Пополнения','Остаток отыгрыша','Вывод'],d.items.map(r=>[userCell(r),money(r.balance)+' / '+money(r.bonus_balance)+' TON',money(r.deposits)+' TON',money(r.wager_remaining)+' TON',r.banned?'Аккаунт заблокирован':r.withdrawal_blocked?'Ограничен':'Доступен']));pager('userPager',d,'users',loadUsers);}
  async function loadGrants(){const d=await api('/api/admin/grants?offset='+(state.offsets.grants || 0)+'&limit=50');table($('grantTable'),['Пользователь','Сумма операции','Операция / администратор','Время'],d.items.map(r=>[userCell(r),(r.amount>=0?'+':'')+money(r.amount)+' TON',({admin_deposit:'Начисление на основной',main_balance_add:'Добавление на основной',main_balance_set:'Установка основного',bonus_balance_add:'Добавление бонусов',bonus_balance_set:'Установка бонусов',referral_balance_add:'Добавление на реферальный',referral_balance_set:'Установка реферального'}[r.action] || r.action)+' · admin '+r.admin_id,time(r.created_at)]));pager('grantPager',d,'grants',loadGrants);}
  for(const [id,key,fn] of [['paymentFilters','payments',loadPayments],['nftFilters','nfts',loadNfts],['userFilters','users',loadUsers]])$(id).onsubmit=e=>{e.preventDefault();state.offsets[key]=0;fn().catch(e=>msg(e.message));};
  let searchTimer;
  $('userFilters').elements.q.oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{state.offsets.users=0;loadUsers().catch(e=>msg(e.message));},300);};
  function paintState(s){const root=$('withdrawalState');root.replaceChildren(el('strong',s.allowed?'Общий доступ к выводу открыт':'Вывод ограничен',s.allowed?'good':'bad'));s.reasons.forEach(r=>root.append(el('p',r.text,'bad')));root.append(el('p',`Учтено пополнений: ${money(s.deposited)} TON · Минимум: ${money(s.minimum)} TON`),el('p',`Отыгрыш: ${money(s.wager_progress)} / ${money(s.wager_required)} TON · Осталось: ${money(s.wager_remaining)} TON`));$('wagerProgress').max=Math.max(1,s.wager_required);$('wagerProgress').value=Math.min(s.wager_progress,s.wager_required);$('wagerForm').elements.amount.value=Number(s.wager_remaining).toFixed(2);$('unlockStars').hidden=!s.stars_until;}
  async function openUser(id){state.userId=id;$('userTitle').textContent='Пользователь ID '+id;$('userFacts').replaceChildren();$('withdrawalState').textContent='Загрузка…';if(!$('userDialog').open)$('userDialog').showModal();try{await loadUser(id);}catch(e){msg(e.message);}}
  async function loadUser(id=state.userId){const [d,p,b]=await Promise.all([api('/api/admin/users/'+id),api('/api/admin/payments?user_id='+id+'&limit=100'),api('/api/admin/users/'+id+'/ban')]);if(id!==state.userId)return;const u=d.user;state.user=u;$('userTitle').textContent=(u.name || 'Игрок')+' · ID '+id;$('userFacts').replaceChildren(metric('Основной баланс',money(u.balance)+' TON'),metric('Бонусный баланс',money(u.bonus_balance)+' TON'),metric('Инвентарь',d.items.length+' NFT'));paintState(u.withdrawal_state);const access=$('accessForm').elements;access.enabled.checked=u.withdrawal_enabled;access.reason.value=u.withdrawal_block_reason || '';access.minimum.value=u.withdrawal_min_deposit_override == null ? '' : u.withdrawal_min_deposit_override;$('banForm').elements.banned.checked=!!b.banned;$('banForm').elements.reason.value=b.reason || b.ban_reason || '';table($('userPayments'),['Пользователь','Способ','Сумма','Подарок','Время'],paymentRows(p.items));}
  async function saveUser(path,data){await api('/api/admin/users/'+state.userId+path,data);await loadUser();if(state.view==='users')await loadUsers();msg('Сохранено.');}
  $('wagerForm').onsubmit=e=>{e.preventDefault();const mode=e.submitter?.value || 'set_remaining';action(e.submitter,()=>saveUser('/wager',{mode,amount:$('wagerForm').elements.amount.value}));};
  $('clearWager').onclick=()=>action($('clearWager'),()=>saveUser('/wager',{mode:'clear'}));
  $('accessForm').onsubmit=e=>{e.preventDefault();const f=e.target.elements;action(e.submitter,()=>saveUser('/withdrawal-access',{enabled:f.enabled.checked,reason:f.reason.value,min_ton_connect_deposit_override:f.minimum.value===''?null:f.minimum.value}));};
  $('unlockStars').onclick=()=>action($('unlockStars'),()=>saveUser('/stars-withdrawal-unlock',{}));
  $('balanceForm').onsubmit=e=>{e.preventDefault();const f=e.target.elements;action(e.submitter,()=>saveUser('/balance',{kind:f.kind.value,mode:f.mode.value,amount:f.amount.value}));};
  $('banForm').onsubmit=e=>{e.preventDefault();const f=e.target.elements;action(e.submitter,()=>saveUser('/ban',{banned:f.banned.checked,reason:f.reason.value}));};
  $('userAdvanced').onclick=()=>{const id=state.userId;$('userDialog').close();history.replaceState(null,'','/admin?user='+id+'#tool=userPage');navigate();};
  async function loadWithdrawals(){const [d,settings]=await Promise.all([api('/api/admin/withdrawals?view='+$('withdrawalView').value),api('/api/admin/withdrawal-settings')]);$('minimumForm').elements.minimum.value=settings.min_ton_connect_deposit;const root=$('withdrawalList');root.replaceChildren();if(!d.items.length)root.append(el('div','Заявок нет.','empty'));for(const r of d.items){const card=el('article',null,'card');card.append(el('h2',`#${r.id} · ${r.gift_name} · ${money(r.price_ton)} TON`));const facts=el('div',null,'facts');facts.append(userCell(r),el('span','Статус: '+r.status),el('span',time(r.created_at)));card.append(facts);if(r.auto_status)card.append(el('p',`Доставка: ${r.auto_status}`,r.auto_error?'bad':'muted'));if(r.auto_error)card.append(el('p',r.auto_error,'bad'));if(r.status==='pending'){const actions=el('div',null,'actions');actions.append(button('Закупка: коллекция + модель',()=>openProcurement(r.id)),button('Повторить доставку',async()=>{await api('/api/admin/withdrawals/'+r.id+'/retry',{});msg('Доставка поставлена в очередь.');await loadWithdrawals();}),button('Подтвердить ручную передачу',async()=>{if(!confirm('NFT уже передан пользователю? Подтверждение завершит заявку.'))return;await api('/api/admin/withdrawals/'+r.id+'/approve',{});await loadWithdrawals();}),button('Отклонить и вернуть NFT',async()=>{if(!confirm('Отклонить заявку и вернуть NFT с комиссией пользователю?'))return;await api('/api/admin/withdrawals/'+r.id+'/reject',{});await loadWithdrawals();}));card.append(actions);}root.append(card);}}
  $('minimumForm').onsubmit=e=>{e.preventDefault();action(e.submitter,async()=>{await api('/api/admin/withdrawal-settings',{min_ton_connect_deposit:$('minimumForm').elements.minimum.value});msg('Общий минимум сохранён.');});};
  $('withdrawalView').onchange=()=>loadWithdrawals().catch(e=>msg(e.message));
  async function openProcurement(id){const d=await api('/api/admin/withdrawals/'+id+'/procurement');state.procurement=d;$('procurementDetails').replaceChildren(el('p','Коллекция: '+d.collection),el('p','Модель: '+(d.model || 'Любая модель этой коллекции')),el('p','Фон: '+(d.backdrop || 'Любой')),el('p','Relayer: @'+d.relay_username),el('p','Получатель: ID '+d.recipient_id));if(d.number)$('procurementDetails').append(el('p','Номер исходного NFT: '+d.number));$('procurementSteps').replaceChildren(...d.steps.map(x=>el('li',x)));$('portalOpen').href=d.portal_url;$('procurementDialog').showModal();}
  $('copyTraits').onclick=()=>action($('copyTraits'),async()=>{const d=state.procurement;await navigator.clipboard.writeText([d.collection,d.model,d.backdrop].filter(Boolean).join(' · '));msg('Коллекция и модель скопированы.');});
  $('retryDelivery').onclick=()=>action($('retryDelivery'),async()=>{await api('/api/admin/withdrawals/'+state.procurement.id+'/retry',{});$('procurementDialog').close();await loadWithdrawals();msg('Доставка поставлена в очередь.');});
  function paintCrypto(d){const f=$('cryptoForm').elements;f.enabled.checked=d.enabled;f.testnet.checked=d.testnet;f.token.value='';f.token.disabled=d.env_token;$('cryptoWebhook').value=d.webhook_url || '';$('copyWebhook').disabled=!d.webhook_url;$('cryptoState').textContent=d.configured ? `${d.app_name || 'Crypto Pay'} · приложение ${d.app_id || '—'} · ${d.testnet?'тестовая':'основная'} сеть · ${d.enabled?'пополнение включено':'пополнение скрыто'}` : 'Приложение ещё не подключено.';}
  async function loadCrypto(){paintCrypto(await api('/api/admin/cryptobot'));}
  $('cryptoForm').onsubmit=e=>{e.preventDefault();const f=e.target.elements;action(e.submitter,async()=>{const d=await api('/api/admin/cryptobot',{token:f.token.value,enabled:f.enabled.checked,testnet:f.testnet.checked});paintCrypto(d);msg('Приложение проверено, настройки сохранены.');});};
  $('copyWebhook').onclick=()=>action($('copyWebhook'),async()=>{await navigator.clipboard.writeText($('cryptoWebhook').value);msg('URL скопирован.');});
  api('/api/me').then(d=>{$('operator').textContent=d.user.name || 'ID '+d.user.id;navigate();}).catch(e=>msg(e.message));
})();
