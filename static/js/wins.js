/* «Недавние выигрыши» strip on the games page */
(function(){
  var GAME_COL={'Mines':'#4fd1b0','Hi-Lo':'#4fd18b','Crash':'#62b8ff','Limbo':'#7cf6cd','Hamster Road':'#f0a35a','Plinko':'#47b0f5','Арена':'#f0a35a'};
  var seen='',timer=0;
  function $(id){return document.getElementById(id)}
  function fmt(n){return Number(n).toLocaleString('ru-RU',{minimumFractionDigits:0,maximumFractionDigits:2}).replace(/ /g,' ')}
  function chip(w){
    var a=document.createElement('button');a.type='button';a.className='rw-chip';a.style.setProperty('--gc',GAME_COL[w.game]||'#6cc8ff');
    var av=document.createElement('span');av.className='rw-av';av.textContent=(w.name||'?').replace('@','').charAt(0).toUpperCase();
    if(w.photo_url){var im=new Image;im.alt='';im.src=w.photo_url;im.onload=function(){av.textContent='';av.append(im)}}
    var t=document.createElement('span');t.className='rw-t';
    var n=document.createElement('b');n.textContent=w.name;
    var g=document.createElement('small');g.textContent=w.game;
    t.append(n,g);
    var s=document.createElement('span');s.className='rw-sum';
    s.textContent=w.amount>0?'+'+fmt(w.amount)+' TON':(w.prize||'Подарок');
    if(!(w.amount>0))s.classList.add('gift');
    a.append(av,t,s);
    a.onclick=function(){try{window.haptic&&haptic('light');openPublicProfile(w.user_id)}catch(e){}};
    return a;
  }
  async function load(){
    var box=$('rwFeed');if(!box)return;
    var page=$('gamesPage');if(!page||page.classList.contains('hidden')||document.hidden)return;
    try{
      var d=await api('/api/recent-wins');var items=d.items||[];
      var key=items.map(function(x){return x.id}).join(',');
      box.classList.toggle('hidden',!items.length);
      if(key===seen)return;seen=key;
      var tr=$('rwTrack'),old=tr.scrollLeft;tr.replaceChildren();
      items.forEach(function(w){tr.append(chip(w))});
      tr.scrollLeft=old;
    }catch(e){}
  }
  function boot(){
    load();timer=setInterval(load,12000);
    var page=$('gamesPage');
    if(page&&window.MutationObserver)new MutationObserver(function(){if(!page.classList.contains('hidden'))load()}).observe(page,{attributes:true,attributeFilter:['class']});
  }
  window.loadRecentWinsStrip=load;
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot);else boot();
})();
