/* VIP statuses: seven tiers by turnover, gem icons, the VIP window, badge buttons next to avatars. */
(function(){
  var PAL={
    iron:['#f1f4f8','#a3acb7','#5a626d','#d5dbe3'],
    bronze:['#ffd2a8','#c47a42','#6d3b1c','#f0a36b'],
    silver:['#ffffff','#cfd5de','#7d8795','#eef1f6'],
    gold:['#fff4b8','#eab43c','#8c5f10','#ffd75e'],
    platinum:['#f1feff','#93d8ee','#3a7f9b','#c9f1fb'],
    diamond:['#d9bfff','#8147e6','#2b1069','#b189ff'],
    obsidian:['#6a6d78','#1d1e23','#050506','#9aa0ad']
  };
  var KEYS=['iron','bronze','silver','gold','platinum','diamond','obsidian'],uid=0,state=null,loadedAt=0,loading=null;
  function $(id){return document.getElementById(id)}
  function gem(i,size){
    var k=KEYS[Math.max(0,Math.min(6,i))],p=PAL[k],n=++uid,g='vg'+n,s=size||40,dark=k==='obsidian';
    var edge=dark?'#e8ebf2':'#ffffff',eo=dark?.75:.55;
    var crown=(k==='gold'||k==='platinum')?'<path d="M17 14l5-7 5 6 5-8 5 8 5-6 5 7z" fill="url(#'+g+'c)" stroke="'+edge+'" stroke-opacity=".6" stroke-width=".8" stroke-linejoin="round"/>':'';
    var glow=(k==='diamond'||k==='obsidian')?'<circle cx="32" cy="36" r="27" fill="url(#'+g+'g)"/>':'';
    var sp=(k==='platinum'||k==='diamond'||k==='obsidian')?'<path d="M52 10v8M48 14h8" stroke="'+p[3]+'" stroke-width="2" stroke-linecap="round"><animate attributeName="opacity" values="1;.2;1" dur="2.8s" repeatCount="indefinite"/></path><circle cx="10" cy="44" r="1.8" fill="'+p[3]+'"><animate attributeName="opacity" values=".2;1;.2" dur="2.2s" repeatCount="indefinite"/></circle>':'';
    var y0=crown?20:14;
    return '<svg class="vip-gem" viewBox="0 0 64 64" width="'+s+'" height="'+s+'" aria-hidden="true"><defs>'+
      '<linearGradient id="'+g+'a" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="'+p[0]+'"/><stop offset=".55" stop-color="'+p[1]+'"/><stop offset="1" stop-color="'+p[2]+'"/></linearGradient>'+
      '<linearGradient id="'+g+'b" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="'+p[1]+'"/><stop offset="1" stop-color="'+p[2]+'"/></linearGradient>'+
      '<linearGradient id="'+g+'c" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="'+p[0]+'"/><stop offset="1" stop-color="'+p[1]+'"/></linearGradient>'+
      '<radialGradient id="'+g+'g"><stop offset="0" stop-color="'+p[3]+'" stop-opacity=".38"/><stop offset="1" stop-color="'+p[3]+'" stop-opacity="0"/></radialGradient></defs>'+glow+
      '<g transform="translate(0 '+(crown?4:0)+')" stroke="'+edge+'" stroke-opacity="'+eo+'" stroke-width=".9" stroke-linejoin="round">'+
      '<path d="M13 22 22 11H42L51 22Z" fill="url(#'+g+'a)"/>'+
      '<path d="M13 22H51L32 54Z" fill="url(#'+g+'b)"/>'+
      '<path d="M22 11 26 22 32 11 38 22 42 11" fill="none"/>'+
      '<path d="M26 22 32 54 38 22" fill="none"/>'+
      '<path d="M13 22 26 22 32 54Z" fill="'+p[0]+'" fill-opacity=".22" stroke="none"/>'+
      '<path d="M22 11 32 11 26 22 13 22Z" fill="#fff" fill-opacity=".28" stroke="none"/></g>'+crown+sp+'</svg>';
  }
  function fmt(n){return Number(n).toLocaleString('ru-RU',{minimumFractionDigits:0,maximumFractionDigits:2}).replace(/ /g,' ')}
  function tonIcon(){return '<img class="vip-ton" src="/static/img/ton.png" alt="">'}
  function paintBadges(){
    if(!state)return;var i=state.state.index,html=gem(i,26);
    document.querySelectorAll('.vip-badge-btn').forEach(function(b){b.innerHTML=html;b.title='VIP: '+state.tiers[i].name});
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
    $('vipHeroSub').textContent=st.reached?'Текущий VIP-статус':'Достигните '+t.name;
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
      el.innerHTML='<button type="button" class="vt-head"><span class="vt-gem">'+gem(i,46)+'</span><span class="vt-txt"><b>'+x.name+lock+'</b><small>'+tonIcon()+fmt(x.ton)+' TON</small></span>'+(i===cur?'<span class="vt-now">Текущий</span>':'')+'<svg class="vt-chev" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg></button><div class="vt-body"><ul>'+x.perks.map(function(p){return '<li><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="m7.5 12.5 3 3 6-6.5"/></svg>'+p+'</li>'}).join('')+'</ul></div>';
      el.querySelector('.vt-head').onclick=function(){el.classList.toggle('open');try{window.haptic&&haptic('light')}catch(e){}};
      list.append(el);
    });
    paintBadges();
  }
  function load(force){
    if(loading)return loading;
    if(!force&&state&&Date.now()-loadedAt<20000)return Promise.resolve(state);
    loading=api('/api/vip').then(function(d){state=d;loadedAt=Date.now();render();return d}).catch(function(){}).then(function(r){loading=null;return r});
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
    $('vipModal').addEventListener('click',function(e){if(e.target===$('vipModal'))close()});
    document.querySelectorAll('.vip-badge-btn,#vipProfileCard').forEach(function(b){b.onclick=function(e){e.stopPropagation();open()}});
    window.openLevelsModal=open;
    var ol=$('openLevels');if(ol)ol.onclick=open;
    var ln=$('levelNotice');if(ln){ln.onclick=open}
    var prev=window.setUser;
    if(typeof prev==='function'){window.setUser=setUser=function(){var r=prev.apply(this,arguments);try{load()}catch(e){}return r}}
    setInterval(function(){if(!document.hidden&&typeof user!=='undefined'&&user)load()},60000);
    load();
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot);else boot();
})();
