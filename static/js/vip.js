/* VIP statuses: seven tiers by turnover, gem icons, the VIP window, badge buttons next to avatars. */
(function(){
  var PAL={
    jade:['#e2fbe9','#79cf9b','#2c7650','#b4f0cb'],
    iron:['#f1f4f8','#a3acb7','#5a626d','#d5dbe3'],
    bronze:['#ffd2a8','#c47a42','#6d3b1c','#f0a36b'],
    silver:['#ffffff','#cfd5de','#7d8795','#eef1f6'],
    gold:['#fff4b8','#eab43c','#8c5f10','#ffd75e'],
    platinum:['#f1feff','#93d8ee','#3a7f9b','#c9f1fb'],
    diamond:['#d9bfff','#8147e6','#2b1069','#b189ff'],
    obsidian:['#6a6d78','#1d1e23','#050506','#9aa0ad']
  };
  var KEYS=['jade','iron','bronze','silver','gold','platinum','diamond','obsidian'],uid=0,state=null,loadedAt=0,loading=null;
  function $(id){return document.getElementById(id)}
  /* rank emblems: a shield that gets more ornate with every tier (chevrons -> star -> wings -> crown -> aura) */
  var SPEC=[
    {},
    {chev:1},
    {chev:2},
    {chev:2,star:1},
    {star:1,wings:1,chev:1},
    {star:1,wings:1,chev:2,crown:0},
    {star:1,wings:1,crown:1,glow:1},
    {star:1,wings:1,crown:1,glow:1,spark:1}
  ];
  function gem(i,size){
    i=Math.max(0,Math.min(7,i));
    var k=KEYS[i],p=PAL[k],sp=SPEC[i],n=++uid,g='vg'+n,s=size||40,dark=k==='obsidian';
    var edge=dark?'#e8ebf2':'#ffffff',eo=dark?.8:.6,ink=dark?'#f2f4f8':p[0],ink2=dark?'#aeb4c2':p[3];
    var SH='M32 11 52 17V33C52 46 43 54 32 60 21 54 12 46 12 33V17Z';
    var o='<svg class="vip-gem" viewBox="-4 -2 72 68" width="'+s+'" height="'+s+'" aria-hidden="true"><defs>'+
      '<linearGradient id="'+g+'a" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="'+p[0]+'"/><stop offset=".5" stop-color="'+p[1]+'"/><stop offset="1" stop-color="'+p[2]+'"/></linearGradient>'+
      '<linearGradient id="'+g+'b" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="'+p[1]+'"/><stop offset="1" stop-color="'+p[2]+'"/></linearGradient>'+
      '<radialGradient id="'+g+'g"><stop offset="0" stop-color="'+p[3]+'" stop-opacity=".42"/><stop offset="1" stop-color="'+p[3]+'" stop-opacity="0"/></radialGradient></defs>';
    if(sp.glow)o+='<circle cx="32" cy="34" r="30" fill="url(#'+g+'g)"><animate attributeName="r" values="28;31;28" dur="3.6s" repeatCount="indefinite"/></circle>';
    if(sp.wings){var w='<g fill="url(#'+g+'b)" stroke="'+edge+'" stroke-opacity="'+eo+'" stroke-width=".8" stroke-linejoin="round"><path d="M13 22Q5 20 0 9Q10 10 15 19Z"/><path d="M13 29Q4 29 -2 19Q9 19 15 26Z"/><path d="M13 36Q5 38 -1 30Q8 28 15 33Z"/></g>';
      o+=w+'<g transform="translate(64 0) scale(-1 1)">'+w+'</g>'}
    o+='<g stroke="'+edge+'" stroke-opacity="'+eo+'" stroke-width="1" stroke-linejoin="round"><path d="'+SH+'" fill="url(#'+g+'a)"/>'+
      '<path d="M32 16 47 21V33C47 43 40 49 32 54 24 49 17 43 17 33V21Z" fill="'+(dark?'#0b0b0e':p[2])+'" fill-opacity="'+(dark?'.85':'.5')+'" stroke="'+edge+'" stroke-opacity=".28"/></g>'+
      '<path d="M32 11V60" stroke="#fff" stroke-opacity=".1"/><path d="M14 18 32 12.5V30Z" fill="#fff" fill-opacity=".16"/>';
    var ch=function(y,wd){return '<path d="M'+(32-wd)+' '+(y+wd*.8)+'L32 '+y+' '+(32+wd)+' '+(y+wd*.8)+'" fill="none" stroke="'+ink+'" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/>'};
    if(sp.star){o+='<path transform="translate(0 -7)" d="M32 21.5l3.100 6.300 6.900 1-5 4.900 1.200 6.900L32 37.300l-6.200 3.300 1.200-6.900-5-4.900 6.900-1z" fill="'+ink+'" stroke="'+ink2+'" stroke-width=".7" stroke-linejoin="round"/>';
      if(sp.chev>=1)o+=ch(sp.chev>=2?37:42,8);if(sp.chev>=2)o+=ch(44,8)}
    else if(sp.chev===1)o+=ch(32,10);
    else if(sp.chev===2)o+=ch(27,10)+ch(37,10);
    if(sp.crown!==undefined&&sp.crown)o+='<path d="M19 12 22 3 27.500 8.500 32 1 36.500 8.500 42 3 45 12Z" fill="'+(dark?'#d7dbe5':p[3])+'" stroke="'+edge+'" stroke-opacity=".7" stroke-width=".8" stroke-linejoin="round"/><circle cx="32" cy="5" r="1.4" fill="'+p[2]+'"/>';
    if(sp.spark)o+='<path d="M55 8v8M51 12h8" stroke="'+p[3]+'" stroke-width="2" stroke-linecap="round"><animate attributeName="opacity" values="1;.2;1" dur="2.6s" repeatCount="indefinite"/></path><circle cx="8" cy="12" r="1.8" fill="'+p[3]+'"><animate attributeName="opacity" values=".2;1;.2" dur="2.1s" repeatCount="indefinite"/></circle>';
    return o+'</svg>';
  }
  function fmt(n){return Number(n).toLocaleString('ru-RU',{minimumFractionDigits:0,maximumFractionDigits:2}).replace(/ /g,' ')}
  function tonIcon(){return '<img class="vip-ton" src="/static/img/ton.png" alt="">'}
  function paintBadges(){
    if(!state)return;var i=state.state.index,html=gem(i,26);
    document.querySelectorAll('.vip-badge-btn').forEach(function(b){b.innerHTML=html;b.title='VIP: '+state.tiers[i].name;b.classList.toggle('dot',(state.pending||0)>0)});
    var c=$('vipProfileCard');if(c){var st=state.state,t=state.tiers[i];
      c.querySelector('.vpc-ico').innerHTML=gem(i,44);
      c.querySelector('.vpc-name').textContent=t.name;
      c.querySelector('.vpc-val').textContent=st.next?fmt(st.turnover)+' / '+fmt(st.next.ton)+' TON':fmt(st.turnover)+' TON · MAX';
      c.querySelector('.vpc-bar i').style.width=st.progress+'%'}
  }
  function render(){
    if(!state)return;var st=state.state,cur=st.index,t=state.tiers[cur],nx=st.next;
    $('vipHeroGem').innerHTML=gem(cur,52);
    $('vipHeroName').textContent=t.name;
    $('vipHeroSub').textContent=st.reached?'Текущий VIP-статус':'Достигните уровня '+t.name;
    var target=nx?nx.ton:t.ton;
    $('vipVals').innerHTML='<b>'+fmt(st.turnover)+'</b> / '+fmt(target)+' TON';
    $('vipNext').innerHTML=nx?'До уровня <span>'+gem(cur+1,18)+nx.name+'</span>':'<span>Максимальный уровень</span>';
    $('vipBar').style.width=st.progress+'%';
    $('vipBar').dataset.tier=KEYS[cur];
    $('vipLeft').textContent=nx?'осталось '+fmt(nx.left)+' TON':'';
    var list=$('vipList');list.replaceChildren();
    state.tiers.forEach(function(x,i){
      var open=i===cur,el=document.createElement('div');
      el.className='vip-tier'+(x.unlocked?' on':' locked')+(i===cur?' cur':'')+(open?' open':'');
      var lock=x.unlocked?'':'<svg class="vt-lock" viewBox="0 0 24 24"><path d="M7 11V8a5 5 0 0 1 10 0v3M6 11h12v9H6z"/></svg>';
      var tag=x.claimed?'<span class="vt-got">Получено</span>':(i===cur?'<span class="vt-now">Текущий</span>':'');
      var rw=x.reward&&x.reward.gift?'<div class="vt-gift"><span class="vg-tile '+bgc(x.reward.gift.background)+'"><img src="'+esc(x.reward.gift.image_url)+'" alt=""></span><span><small>Ваш подарок</small><b>'+esc(x.reward.gift.name)+'</b></span><em>'+tonIcon()+fmt(x.reward.gift.price)+'</em></div>':'';
      el.innerHTML='<button type="button" class="vt-head"><span class="vt-gem">'+gem(i,46)+'</span><span class="vt-txt"><b>'+x.name+lock+'</b><small>'+tonIcon()+fmt(x.ton)+' TON</small></span>'+tag+'<svg class="vt-chev" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg></button><div class="vt-body"><div><ul>'+x.perks.map(function(p){return '<li><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="m7.5 12.5 3 3 6-6.5"/></svg>'+p+'</li>'}).join('')+'</ul>'+rw+'</div></div>';
      el.querySelector('.vt-head').onclick=function(){el.classList.toggle('open');try{window.haptic&&haptic('light')}catch(e){}};
      list.append(el);
    });
    paintBadges();paintPlaybonus();
  }
  function bgc(b){return b==='Black'?'bg-black':b==='Onyx Black'?'bg-onyx':''}
  function esc(t){return String(t==null?'':t).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
  function paintPlaybonus(){
    var p=state&&state.playbonus,c=$('vipPb');if(!p||!c)return;
    c.querySelector('.pb-val').innerHTML=tonIcon()+fmt(p.amount)+' TON';
    var pe=c.querySelector('.pb-pct');if(pe)pe.textContent='Ваш процент: '+p.percent+'%';
    var b=c.querySelector('.pb-claim');b.disabled=!(p.unlocked&&p.amount>0);
    b.textContent=p.unlocked?'Забрать на баланс':'Откроется на '+p.tier;
    c.classList.toggle('locked',!p.unlocked);
  }
  async function claimPb(btn){
    btn.disabled=true;
    try{
      var d=await api('/api/playbonus/claim',{method:'POST',body:'{}'});
      try{window.haptic&&haptic('success')}catch(e){}
      try{if(d.user)setUser(d.user)}catch(e){}
      try{toast('+'+fmt(d.amount)+' TON на основной баланс')}catch(e){}
      await load(true);
    }catch(e){try{toast(e.message||'Не удалось забрать PLAYBONUS')}catch(_){}paintPlaybonus()}
  }
  function pbInfo(){
    var p=(state&&state.playbonus)||{percent:1,tier:'Opal'};
    showInfo('<h3>PLAYBONUS</h3><p>С каждого проигрыша с основного баланса мы возвращаем '+p.percent+'% в PLAYBONUS. С ростом VIP-статуса процент увеличивается: от 1% на Opal до 5% на Onyx.</p><p>Накопленную сумму можно забрать на основной баланс — функция открывается со статуса <b>'+esc(p.tier)+'</b>. До этого PLAYBONUS продолжает копиться.</p>');
  }
  function showInfo(html){
    var o=document.createElement('div');o.className='vip-pop';
    o.innerHTML='<div class="vip-pop-card">'+html+'<button type="button" class="vip-pop-ok">Понятно</button></div>';
    o.onclick=function(e){if(e.target===o||e.target.classList.contains('vip-pop-ok'))o.remove()};
    document.body.append(o);
  }
  var showing=false;
  function showRewards(){
    if(showing||!state||!state.unseen||!state.unseen.length)return;
    showing=true;var q=state.unseen.slice();state.unseen=[];state.pending=0;paintBadges();
    api('/api/vip/seen',{method:'POST',body:'{}'}).catch(function(){});
    (function next(){
      var r=q.shift();if(!r){showing=false;return}
      var i=Math.max(0,KEYS.indexOf(r.tier));
      var o=document.createElement('div');o.className='vip-pop vip-reward';
      var g=r.gift?'<div class="vr-gift"><span class="vg-tile '+bgc(r.gift.background)+'"><img src="'+esc(r.gift.image_url)+'" alt=""></span><b>'+esc(r.gift.name)+'</b><small>'+tonIcon()+fmt(r.gift.price)+' TON</small></div>':'';
      var TK='<svg class="vr-tk" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 8a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v2a2 2 0 0 0 0 4v2a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-2a2 2 0 0 0 0-4Z" fill="#ffd75e" stroke="#a8741a" stroke-width="1.2"/><path d="M14 7v10" stroke="#a8741a" stroke-width="1.4" stroke-dasharray="2 2"/></svg>';
      var chips='<div class="vr-chips"><span>'+TK+'+'+fmt(r.tickets)+' билетов</span>'+(r.balance?'<span>'+tonIcon()+'+'+fmt(r.balance)+' TON на баланс</span>':'')+'</div>';
      var pb=r.playbonus_unlocked?'<div class="vr-pb">Открыт <b>PLAYBONUS</b> — кэшбэк с проигрышей теперь можно забирать на основной баланс</div>':'';
      o.innerHTML='<div class="vip-pop-card"><div class="vr-gem">'+gem(i,84)+'</div><small class="vr-k">Новый VIP-статус</small><h3>'+esc(r.name)+'</h3>'+g+chips+pb+'<button type="button" class="vip-pop-ok">Отлично</button></div>';
      o.onclick=function(e){if(e.target.classList.contains('vip-pop-ok')){o.remove();next()}};
      document.body.append(o);try{window.haptic&&haptic('success')}catch(e){}
    })();
  }
  function load(force){
    if(loading)return loading;
    if(!force&&state&&Date.now()-loadedAt<20000)return Promise.resolve(state);
    loading=api('/api/vip').then(function(d){state=d;loadedAt=Date.now();render();showRewards();return d}).catch(function(){}).then(function(r){loading=null;return r});
    return loading;
  }
  function open(){
    var m=$('vipModal');m.classList.remove('hidden');document.body.classList.add('vip-open');
    var u=(typeof user!=='undefined')?user:null;if(u){$('vipHeroAvatar').textContent=(u.name||'G').charAt(0).toUpperCase();$('vipHeroAvatar').style.backgroundImage=u.photo_url?'url("'+u.photo_url+'")':'';$('vipHeroAvatar').classList.toggle('img',!!u.photo_url);$('vipHeroNick').textContent=u.name||'Игрок'}
    m.querySelector('.vip-scroll').scrollTop=0;
    load(true);
  }
  function close(){$('vipModal').classList.add('hidden');document.body.classList.remove('vip-open')}
  window.openVip=open;window.vipRefresh=load;window.vipGem=gem;
  function boot(){
    $('vipClose').onclick=close;
    var pc=$('vipPb');if(pc){pc.querySelector('.pb-claim').onclick=function(){claimPb(this)};pc.querySelector('.pb-info').onclick=pbInfo}
    $('vipModal').addEventListener('click',function(e){if(e.target===$('vipModal'))close()});
    document.querySelectorAll('.vip-badge-btn,#vipProfileCard').forEach(function(b){b.onclick=function(e){e.stopPropagation();open()}});
    window.openLevelsModal=open;
    var ol=$('openLevels');if(ol)ol.onclick=open;
    var ln=$('levelNotice');if(ln){ln.onclick=open}
    var prev=window.setUser;
    if(typeof prev==='function'){window.setUser=setUser=function(){var r=prev.apply(this,arguments);try{load(Date.now()-loadedAt>4000)}catch(e){}return r}}
    setInterval(function(){if(!document.hidden&&typeof user!=='undefined'&&user)load()},60000);
    load();
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot);else boot();
})();
