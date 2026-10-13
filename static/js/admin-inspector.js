window.AdminInspector = {
  create(ctx) {
    const {api,el,table,pager,paymentRows,userCell,msg,time,money,metric,state,paintState,action,saveUser} = ctx;
    const $=id=>document.getElementById(id);
    const labels={mines:'Mines',crash:'Crash',upgrade:'Upgrade',arena:'Arena',hilo:'Hi-Lo',limbo:'Limbo',road:'Hamster Road',payments:'Пополнения',sales:'Продажи',withdrawals:'Выводы',referrals:'Рефералы',admin:'Администратор',events:'События'};
    let generation=0,tabRequest=0;
    const tabs=[...document.querySelectorAll('[data-user-tab]')];
    const controls=window.AdminUserControls.create({api,el,state,money,time,msg,action,saveUser,userCell,selectUserTab:selectTab,reloadUser:load});
    function placeholder(root,text='Загрузка…'){root.replaceChildren(el('div',text,'empty'));}
    function image(url,alt,model='',collection=''){
      const img=el('img');img.alt=alt;img.loading='lazy';let fallback=0;
      img.src=/^(https?:\/\/|\/static\/|\/api\/gift-model-image)/.test(url || '')?url:'/static/img/gift.png';
      img.onerror=()=>{if(!fallback++ && model && collection)img.src='/api/gift-model-image?name='+encodeURIComponent(collection)+'&model='+encodeURIComponent(model);else{img.onerror=null;img.src='/static/img/gift.png';}};
      return img;
    }
    function selectTab(tab,load=true){
      state.userTab=tab;
      tabs.forEach(b=>{const active=b.dataset.userTab===tab;b.setAttribute('aria-selected',String(active));b.tabIndex=active?0:-1;});
      document.querySelectorAll('[data-user-panel]').forEach(p=>p.hidden=p.dataset.userPanel!==tab);
      if(load)loadTab().catch(e=>msg(e.message));
    }
    tabs.forEach(b=>b.onclick=()=>selectTab(b.dataset.userTab));
    $('userTabs').onkeydown=e=>{
      const index=tabs.indexOf(document.activeElement);if(index<0)return;
      let next;if(e.key==='ArrowRight')next=(index+1)%tabs.length;else if(e.key==='ArrowLeft')next=(index-1+tabs.length)%tabs.length;else if(e.key==='Home')next=0;else if(e.key==='End')next=tabs.length-1;else return;
      e.preventDefault();tabs[next].focus();selectTab(tabs[next].dataset.userTab);
    };
    $('userAccessBadge').onclick=()=>{controls.selectControl('access');selectTab('controls');};
    $('copyUserId').onclick=()=>navigator.clipboard.writeText(String(state.userId)).then(()=>msg('Telegram ID скопирован.')).catch(()=>msg('ID '+state.userId));
    function renderHeader(d){
      const u=d.user,t=d.totals;state.user=u;state.userLoadedId=u.id;state.userOverview=d;
      $('userTitle').textContent=u.name || 'Игрок';$('copyUserId').textContent='ID: '+u.id;
      $('userRegistered').textContent=u.created_at?'Регистрация: '+time(u.created_at).split(',')[0]:'Дата регистрации не указана';
      const avatar=$('userAvatar');avatar.replaceChildren(document.createTextNode((u.name || 'G').slice(0,1).toUpperCase()));
      if(/^https?:\/\//.test(u.photo_url || '')){const img=el('img');img.src=u.photo_url;img.alt='';img.onerror=()=>img.remove();avatar.append(img);}
      for(const id of ['userTelegram','userTelegramOpen']){const a=$(id);a.hidden=!/^[a-zA-Z0-9_]{5,32}$/.test(u.username || '');a.removeAttribute('href');if(!a.hidden)a.href='https://t.me/'+u.username;}
      $('userTelegram').textContent=u.username?'@'+u.username:'';
      $('userFacts').replaceChildren(metric('Основной баланс',money(u.balance)+' TON','Бонус: '+money(u.bonus_balance)+' TON'),metric('Партнёрка',money(u.ref_balance)+' TON','Реферальный баланс'),metric('Всего пополнил',money(t.deposits)+' TON','Только подтверждённые платежи'),metric('Выведено NFT',money(t.withdrawn)+' TON','Завершённые передачи'),metric('Фактический RTP',t.rtp==null?'—':money(t.rtp)+'%',t.games+' завершённых обычных игр'));
      $('userFacts').lastChild.title='Возврат '+money(t.payout)+' TON / ставки '+money(t.stake)+' TON. Промо и бонусные ставки исключены.';
      for(const [key,id] of Object.entries({logs:'userLogCount',inventory:'userInventoryCount',payments:'userPaymentCount',withdrawals:'userWithdrawalCount',linked:'userLinkedCount'}))$(id).textContent='('+d.counts[key]+')';
      const s=d.withdrawal_state;paintState(s);
      const badge=$('userAccessBadge');badge.classList.toggle('bad',!s.allowed || !!u.banned);
      badge.textContent=u.banned?'Аккаунт заблокирован: '+(u.ban_reason || 'Без пояснения'):s.allowed?'Вывод доступен · условия выполнены':'Вывод ограничен · '+(s.wager_remaining?'осталось отыграть '+money(s.wager_remaining)+' TON':s.reasons[0]?.text || 'Проверьте условия');
      const a=$('accessForm').elements;a.enabled.checked=!!u.withdrawal_enabled;a.reason.value=u.withdrawal_block_reason || '';a.minimum.value=u.withdrawal_min_deposit_override==null?'':u.withdrawal_min_deposit_override;
      const b=$('banForm').elements;b.banned.checked=!!u.banned;b.reason.value=u.ban_reason || '';
      $('userControlFields').disabled=false;
    }
    async function load(id=state.userId){
      const version=++generation;const d=await api('/api/admin/users/'+id+'/overview');
      if(id!==state.userId || version!==generation)return;
      renderHeader(d);await loadTab();
    }
    async function open(id){
      state.userId=Number(id);controls.reset();state.user=null;state.userLoadedId=null;state.userCategory='all';
      for(const key of ['user_logs','user_inventory','user_payments','user_withdrawals','user_linked'])state.offsets[key]=0;
      $('inventoryFilters').reset();$('userPaymentFilters').reset();$('userTitle').textContent='Пользователь';$('copyUserId').textContent='ID: '+id;
      $('userRegistered').textContent='';$('userAvatar').replaceChildren();$('userTelegram').hidden=true;$('userTelegramOpen').hidden=true;$('userFacts').replaceChildren();
      $('userAccessBadge').textContent='Загрузка данных…';$('userControlFields').disabled=true;
      document.querySelectorAll('#userTabs span').forEach(n=>n.textContent='');selectTab('logs',false);renderFilters();placeholder($('userLogs'));
      if(!$('userDialog').open)$('userDialog').showModal();
      try{await load(state.userId);}catch(e){placeholder($('userLogs'),e.message);msg(e.message);}
    }
    function renderFilters(){const root=$('logFilters');root.replaceChildren();for(const [value,label] of [['all','Все'],...Object.entries(labels)]){const b=el('button',label);b.type='button';b.setAttribute('aria-pressed',String(value===state.userCategory));b.onclick=()=>{state.userCategory=value;state.offsets.user_logs=0;renderFilters();loadTab().catch(e=>msg(e.message));};root.append(b);}}
    renderFilters();
    function eventCard(item){
      const d=item.details || {},game=!!d.game,card=el('article',null,'activity-card'),top=el('div',null,'activity-top');
      top.append(el('span',labels[item.category] || item.category,'activity-kind'),el('span',time(item.date),'activity-date'));
      top.append(el('span',({lost:'Проигрыш',won:'Выигрыш',active:'В игре'}[d.state] || {loss:'Списание',win:'Зачисление',pending:'Ожидает'}[item.tone] || 'Событие'),'activity-outcome '+item.tone));card.append(top);
      const main=el('div',null,'activity-main'),copy=el('div',null,'activity-copy');copy.append(el('h3',item.title));
      if(game)copy.append(el('span','Ставка: '+money(d.stake)+' TON','stake-label'));
      (item.lines || []).forEach(line=>copy.append(el('p',line)));
      if(game && d.state!=='active'){
        const net=Number(d.payout)-Number(d.stake),footer=el('div',null,'activity-net '+(net>=0?'win':'loss'));
        footer.append(el('span',d.ordinary?'Итог:':'Игровой результат:'),el('strong',(net>=0?'+':'')+money(net)+' TON'));copy.append(footer);
      }else if(item.amount!=null && !game){const footer=el('div',null,'activity-net '+(item.amount<0?'loss':'win'));footer.append(el('span','Сумма:'),el('strong',(item.amount>0?'+':'')+money(item.amount)+' TON'));copy.append(footer);}
      main.append(copy);
      if(d.board){const wrap=el('div',null,'mini-board-wrap');wrap.append(el('small','ПОЛЕ 5×5 · '+d.board.count+' МИН'));const board=el('div',null,'mini-board');board.setAttribute('role','img');board.setAttribute('aria-label','Поле Mines, '+d.board.count+' мин');const opened=new Set(d.board.opened),mines=new Set(d.board.mines);for(let i=0;i<25;i++){const danger=mines.has(i),isOpen=opened.has(i),exploded=d.board.lost_cell===i;const cell=el('span',danger?'✹':isOpen?'✦':'•','mini-cell'+(danger?' mine':isOpen?' open':'')+(exploded?' exploded':''));cell.title='Клетка '+(i+1)+': '+(danger?'мина':isOpen?'открыта':'закрыта');board.append(cell);}wrap.append(board);main.append(wrap);}
      else if(item.image){const img=image(item.image,item.title);img.className='activity-picture';main.append(img);}
      card.append(main);return card;
    }
    async function loadTab(){
      const uid=state.userId;if(!uid)return;const tab=state.userTab || 'logs',version=++tabRequest;
      if(tab==='controls'){if(state.userLoadedId===uid)await controls.load();return;}
      const key='user_'+tab,offset=state.offsets[key] || 0;
      let url,root,pagerId;
      if(tab==='logs'){root=$('userLogs');pagerId='logPager';url=`/api/admin/users/${uid}/timeline?offset=${offset}&limit=${$('logLimit').value}&category=${state.userCategory || 'all'}`;}
      if(tab==='inventory'){root=$('userInventory');pagerId='inventoryPager';url=`/api/admin/users/${uid}/inventory-view?offset=${offset}&q=${encodeURIComponent($('inventoryFilters').elements.q.value)}`;}
      if(tab==='payments'){root=$('userPayments');pagerId='userPaymentPager';url=`/api/admin/payments?user_id=${uid}&offset=${offset}&method=${$('userPaymentFilters').elements.method.value}`;}
      if(tab==='withdrawals'){root=$('userWithdrawals');pagerId='userWithdrawalPager';url=`/api/admin/users/${uid}/withdrawals-view?offset=${offset}`;}
      if(tab==='linked'){root=$('userLinked');pagerId='linkedPager';url=`/api/admin/users/${uid}/linked?offset=${offset}`;}
      placeholder(root);
      const d=await api(url);if(uid!==state.userId || tab!==state.userTab || version!==tabRequest)return;
      if(tab==='logs'){root.replaceChildren(...d.items.map(eventCard));if(!d.items.length)placeholder(root,'По этому фильтру записей нет.');}
      if(tab==='inventory'){root.replaceChildren();for(const item of d.items){const card=el('div',null,'inventory-item'),collection=(item.name || '').split(' · ')[0];card.append(image(item.image_url,item.name,item.fragment_model,collection),el('h3',item.name),el('small',item.fragment_model?'Модель: '+item.fragment_model:'Коллекционный подарок'),el('strong',money(item.price_ton)+' TON'),el('small',item.source_label || item.price_source || item.source));const locked=item.promo_locked || (item.bonus_origin && !item.bonus_unlock_complete);card.append(el('span',locked?'Отыгрыш / ограничение':item.deposit_mirror?'Уже учтён на балансе':'В инвентаре','inventory-status'+(locked?' locked':'')));if(/^https:\/\/(t\.me|fragment\.com)\//.test(item.external_url || '')){const a=el('a','Открыть NFT ↗');a.href=item.external_url;a.target='_blank';a.rel='noopener';card.append(el('br'),a);}card.append(controls.removeInventory(item));root.append(card);}if(!d.items.length)placeholder(root,'Инвентарь пуст.');}
      if(tab==='payments')table(root,['Пользователь','Способ','Сумма','Подарок / назначение','Операция / время'],paymentRows(d.items));
      if(tab==='withdrawals')table(root,['Заявка','NFT','Стоимость','Статус','Время'],d.items.map(r=>{const name=el('div',r.gift_name);if(r.fragment_model)name.append(el('small',r.fragment_model));const status=el('div',{approved:'Передан',pending:'Ожидает',rejected:'Отклонён'}[r.status] || r.status);if(r.delivery_error)status.append(el('small',r.delivery_error,'bad'));return ['#'+r.id,name,money(r.price_ton)+' TON',status,time(r.processed_at || r.created_at)];}));
      if(tab==='linked')table(root,['Пользователь','Связь','Регистрация'],d.items.map(r=>[userCell(r),r.relations.map(x=>({referral:'Приглашён пользователем',referrer:'Пригласил пользователя',shared_ip:'Общий IP'}[x] || x)).join(' · '),time(r.created_at)]));
      pager(pagerId,d,key,loadTab);
    }
    $('logLimit').onchange=()=>{state.offsets.user_logs=0;loadTab().catch(e=>msg(e.message));};
    $('inventoryFilters').onsubmit=e=>{e.preventDefault();state.offsets.user_inventory=0;loadTab().catch(e=>msg(e.message));};
    $('userPaymentFilters').onsubmit=e=>{e.preventDefault();state.offsets.user_payments=0;loadTab().catch(e=>msg(e.message));};
    return {open,load};
  }
};
