/* «Недавние выигрыши» strip on the games page */
(function(){
  var GAME_COL={'Mines':'#4fd1b0','Hi-Lo':'#4fd18b','Crash':'#62b8ff','Limbo':'#7cf6cd','Hamster Road':'#f0a35a','Plinko':'#47b0f5','Арена':'#f0a35a'};
  var seen='',timer=0;
  function $(id){return document.getElementById(id)}
  function fmt(n){return Number(n).toLocaleString('ru-RU',{minimumFractionDigits:0,maximumFractionDigits:2}).replace(/ /g,' ')}
  function item(w,dup){
    var a=document.createElement('button');a.type='button';a.className='rw-item'+(w.image?'':' ton');
    a.title=w.name+' · '+w.game+': '+(w.amount>0?'+'+fmt(w.amount)+' TON':(w.prize||'Подарок'));
    var im=new Image;im.alt=w.prize||'TON';im.loading='lazy';im.src=w.image||'/static/img/ton.png';
    im.onerror=function(){im.onerror=null;im.src='/static/img/ton.png'};
    a.append(im);
    if(dup){a.setAttribute('aria-hidden','true');a.tabIndex=-1}
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
      var tr=$('rwTrack');tr.replaceChildren();
      items.forEach(function(w){tr.append(item(w))});
      if(items.length>=6){items.forEach(function(w){tr.append(item(w,true))});tr.classList.add('loop');tr.style.setProperty('--rw-dur',Math.max(24,items.length*4)+'s')}else tr.classList.remove('loop');
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
