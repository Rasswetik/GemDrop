/* Profile inventory: total value of all gifts and a «sell everything» button. */
(function(){
  function $(id){return document.getElementById(id)}
  function fmt(n){return Number(n).toLocaleString('ru-RU',{minimumFractionDigits:0,maximumFractionDigits:2})}
  function sellable(){try{return (inventoryItems||[]).filter(function(x){return !x.promo_locked&&!x.deposit_mirror})}catch(e){return []}}
  window.paintInvTotal=function(){
    var b=$('invSellAll'),t=$('invTotal');if(!b||!t)return;
    var all=0,sell=0,list=[];try{list=inventoryItems||[]}catch(e){}
    list.forEach(function(x){all+=Number(x.price_ton||0)});
    var s=sellable();s.forEach(function(x){sell+=Number(x.price_ton||0)});
    t.classList.toggle('hidden',!list.length);b.classList.toggle('hidden',!list.length);
    t.innerHTML='<img class="is-ton" src="/static/img/ton.png" alt="">'+fmt(all);
    t.title='Стоимость всех подарков, TON';
    b.disabled=!s.length;b.title=s.length?'Продать '+s.length+' шт. за '+fmt(sell)+' TON':'Нечего продавать';
  };
  async function sellAll(){
    var s=sellable();if(!s.length)return;
    var sum=s.reduce(function(a,x){return a+Number(x.price_ton||0)},0);
    if(!confirm('Продать '+s.length+' '+(s.length===1?'подарок':'подарков')+' за '+fmt(sum)+' TON? Промо-подарки с отыгрышем останутся.'))return;
    var b=$('invSellAll');b.disabled=true;
    try{
      var d=await api('/api/inventory/sell-all',{method:'POST',body:'{}'});
      try{window.haptic&&haptic('success')}catch(e){}
      try{if(d.user)setUser(d.user)}catch(e){}
      try{toast('Продано: '+d.sold+' · +'+fmt(d.sold_for)+' TON')}catch(e){}
      await loadInventory();
    }catch(e){try{toast(e.message||'Не удалось продать')}catch(_){}}
    paintInvTotal();
  }
  function boot(){var b=$('invSellAll');if(b)b.onclick=sellAll}
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot);else boot();
})();
