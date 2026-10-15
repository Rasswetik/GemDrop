/* ===== Plinko ===== */
var PK=(function(){
const $=id=>document.getElementById(id);
const NS='http://www.w3.org/2000/svg';
const ROWS=12,DX=28,CX=200,Y0=34,DY=22.5,BALL_R=6.5,SLOT_Y=Y0+ROWS*DY+4;
const st={cfg:null,risk:'medium',turbo:false,busy:false,active:false,poll:0,loading:false,hist:[],wins:[],built:false,raf:0,slotEls:[],pinEls:[]};
const reduced=()=>window.matchMedia&&matchMedia('(prefers-reduced-motion:reduce)').matches;
const lite=()=>document.documentElement.classList.contains('perf-lite');
const fmt=n=>Number(n||0).toFixed(2);
const RISK_TITLE={low:'Низкий',medium:'Средний',high:'Высокий'};
function minBet(){return Number(st.cfg?.min_bet)||0.1}
function maxBet(){return Number(st.cfg?.max_bet)||300}
function betValue(){let v=parseFloat(String($('pkBet').value).replace(',','.'));return Number.isFinite(v)?v:0}
function setBet(v){$('pkBet').value=(Math.floor(v*100+1e-6)/100).toFixed(2)}
function clampBet(v){return Math.min(maxBet(),Math.max(minBet(),v))}
function mults(){return st.cfg?.multipliers?.[st.risk]||[]}
function el(tag,attrs,parent){const e=document.createElementNS(NS,tag);for(const k in attrs)e.setAttribute(k,attrs[k]);if(parent)parent.appendChild(e);return e}
function pinX(i,r){return CX+(2*r-i)*DX/2}
function pinY(i){return Y0+i*DY}
/* slot colour from multiplier: dull blue for <1, warm towards the big ones */
function slotColor(m){
 if(m>=100)return['#ff4a64','#7a1226'];
 if(m>=20)return['#ff7a3a','#7a3010'];
 if(m>=5)return['#ffb547','#6b4a10'];
 if(m>=2)return['#ffd84a','#5a4a0c'];
 if(m>=1)return['#5cc8ff','#13405e'];
 return['#6f86c9','#222c52'];
}
function fmtMult(m){return(m>=100?Math.round(m):m>=10?m.toFixed(1).replace(/\.0$/,''):m.toFixed(2).replace(/0$/,'').replace(/\.0$/,'.0'))+'x'}
function build(){
 if(st.built)return;st.built=true;
 const pins=$('pkPins'),slots=$('pkSlots');
 for(let i=0;i<ROWS;i++)for(let r=0;r<=i;r++){st.pinEls[i+'_'+r]=el('circle',{cx:pinX(i,r),cy:pinY(i),r:2.9,class:'pk-pin'},pins)}
 for(let k=0;k<=ROWS;k++){
  const x=pinX(ROWS,k),g=el('g',{class:'pk-slot','data-k':k,transform:'translate('+x+' '+SLOT_Y+')'},slots);
  el('rect',{x:-12.5,y:0,width:25,height:25,rx:7,class:'pk-slot-bg'},g);
  const t=el('text',{x:0,y:16.5,'text-anchor':'middle',class:'pk-slot-t'},g);t.textContent='';
  st.slotEls[k]=g;
 }
}
function paintSlots(){
 const m=mults();
 st.slotEls.forEach((g,k)=>{
  const v=m[k];if(v==null)return;const [c,d]=slotColor(v);
  const rect=g.firstChild,txt=g.lastChild;
  rect.setAttribute('fill',d);rect.setAttribute('stroke',c);
  txt.textContent=fmtMult(v).replace('x','');txt.setAttribute('fill','#fff');
  txt.style.fontSize=v>=100?'7.5px':v>=10?'8.5px':'9.5px';
 });
}
function paintRisk(){
 document.querySelectorAll('#pkRisk button').forEach(b=>{const on=b.dataset.risk===st.risk;b.classList.toggle('on',on);b.setAttribute('aria-pressed',on?'true':'false')});
 paintSlots();paintHint();
}
function paintHint(){
 const m=mults();if(!m.length)return;
 $('pkMax').textContent='до '+fmtMult(Math.max(...m));
}
function setCenter(label,big,cls){
 $('pkCLabel').textContent=label;$('pkCBig').textContent=big;$('pkStage').dataset.state=cls||'idle';
}
function paintIdle(){
 if(st.busy)return;
 $('pkStage').dataset.state='idle';
 $('pkResult').classList.add('hidden');
}
function pinHit(i,r){
 const p=st.pinEls[i+'_'+r];if(!p)return;
 p.classList.remove('hit');void p.getBoundingClientRect();p.classList.add('hit');
}
function dropBall(result,turbo){
 return new Promise(resolve=>{
  const ball=$('pkBall'),stage=$('pkStage');
  const fast=turbo||reduced()||lite(),stepMs=reduced()?30:turbo?95:fast?130:215;
  const path=[];for(let i=0;i<ROWS;i++)path.push((result.path>>(ROWS-1-i))&1);
  let r=0,i=0;
  let x0=pinX(0,0),y0=pinY(0)-9;
  ball.setAttribute('opacity','1');
  ball.setAttribute('transform','translate('+x0+' '+(Y0-26)+')');
  stage.dataset.state='drop';
  // first fall to the top pin
  const segs=[];let cx=x0,cy=Y0-26;
  segs.push({x0:cx,y0:cy,x1:x0,y1:y0,hit:null,dur:stepMs*.8});
  let px=x0,py=y0;
  for(let n=0;n<ROWS;n++){
   const nr=r+path[n],nx=n+1<ROWS?pinX(n+1,nr):pinX(ROWS,nr),ny=n+1<ROWS?pinY(n+1)-9:SLOT_Y+10;
   segs.push({x0:px,y0:py,x1:nx,y1:ny,hit:[n,r],dur:stepMs,last:n===ROWS-1});
   r=nr;px=nx;py=ny;
  }
  let k=0,t0=performance.now();
  const frame=now=>{
   const s=segs[k];let t=Math.min(1,(now-t0)/s.dur);
   const x=s.x0+(s.x1-s.x0)*t;
   const arc=s.hit?-9*Math.sin(Math.PI*t)*(1-.35*t):0;
   const y=s.y0+(s.y1-s.y0)*t*t+arc;
   ball.setAttribute('transform','translate('+x.toFixed(2)+' '+y.toFixed(2)+')');
   if(t>=1){
    if(s.hit)pinHit(s.hit[0],s.hit[1]);
    k++;t0=now;
    if(k>=segs.length){st.raf=0;resolve();return}
   }
   st.raf=requestAnimationFrame(frame);
  };
  st.raf=requestAnimationFrame(frame);
 });
}
function landSlot(result){
 const g=st.slotEls[result.slot];if(!g)return;
 g.classList.remove('land');void g.getBoundingClientRect();g.classList.add('land');
 setTimeout(()=>g.classList.remove('land'),1400);
 $('pkBall').setAttribute('opacity','0');
}
function showResult(r){
 const box=$('pkResult');box.classList.remove('hidden');
 const profit=r.payout-r.bet;
 $('pkResMult').textContent='x'+r.multiplier.toFixed(2);
 $('pkResTxt').textContent=(profit>=0?'+':'-')+fmt(Math.abs(profit))+' TON';
 box.dataset.k=profit>0?'win':profit===0?'even':'lose';
 $('pkStage').dataset.state=profit>0?'win':'lose';
 try{tg?.HapticFeedback?.notificationOccurred?.(profit>0?'success':'warning')}catch(e){}
}
function renderHist(){
 const box=$('pkHist');
 if(!st.hist.length){box.innerHTML='<span class="pk-hist-empty">Пока нет бросков</span>';$('pkHistCount').textContent='';return}
 box.replaceChildren(...st.hist.slice(0,16).map(h=>{const p=document.createElement('span');p.className='pk-pill'+(h.payout>h.bet?' won':h.payout<h.bet?' lost':'');p.textContent='x'+h.multiplier.toFixed(2);p.title='Ставка '+fmt(h.bet)+' · выплата '+fmt(h.payout)+' TON';return p}));
 const w=st.hist.filter(h=>h.payout>h.bet).length;$('pkHistCount').textContent=w+' из '+st.hist.length+' в плюс';
}
function winKey(w){return w.id!=null?'w'+w.id:[w.name,w.profit,w.created_at].join('|')}
function makeWin(w,k,anim){
 const e=document.createElement('div');e.className='pk-win'+(anim?' pk-win-new':'');e.dataset.k=k;if(w.user_id){e.style.cursor='pointer';e.onclick=()=>openPublicProfile(w.user_id)}
 const av=document.createElement('span');av.className='av';avatar(av,{name:w.name,photo_url:w.photo_url});
 const tx=document.createElement('div'),b=document.createElement('b'),s=document.createElement('small');
 b.textContent='+'+fmt(w.profit)+' TON';s.textContent='X'+w.multiplier.toFixed(2)+' • '+(RISK_TITLE[w.risk]||'').toUpperCase();
 tx.append(b,s);e.append(av,tx);return e;
}
function renderWins(){
 const box=$('pkLive');
 if(!st.wins.length){if(!box.querySelector('.pk-win-empty'))box.innerHTML='<div class="pk-win pk-win-empty">Ждём первые победы</div>';return}
 box.querySelector('.pk-win-empty')?.remove();
 const have=new Map();[...box.children].forEach(e=>{if(e.dataset.k)have.set(e.dataset.k,e)});
 const keys=st.wins.map(winKey),keep=new Set(keys),first=have.size===0;
 have.forEach((e,k)=>{if(!keep.has(k))e.remove()});
 let cursor=box.firstElementChild,added=0;
 keys.forEach((k,i)=>{
  const e=have.get(k);
  if(e){if(e===cursor)cursor=cursor.nextElementSibling;else box.insertBefore(e,cursor)}
  else{const n=makeWin(st.wins[i],k,!first);box.insertBefore(n,cursor);added+=n.offsetWidth+8}
 });
 if(added&&box.scrollLeft>2)box.scrollLeft+=added;
}
function applyState(d){
 if(d.config)st.cfg=d.config;
 if(Array.isArray(d.wins)){st.wins=d.wins;renderWins()}
 if(Array.isArray(d.history)&&!st.busy){st.hist=d.history;renderHist()}
 if(d.user&&!st.busy)setUser(d.user);
}
async function load(){
 if(st.loading)return;st.loading=true;
 try{const d=await api('/api/plinko/state',{timeoutMs:6000});applyState(d);paintRisk()}
 catch(e){if(st.active&&!st.cfg){toast(e.message||'Plinko недоступна');if(/недоступн|403/i.test(e.message||''))show('gamesPage')}}
 finally{st.loading=false}
}
function schedulePoll(){clearTimeout(st.poll);if(!st.active)return;st.poll=setTimeout(async()=>{if(st.active&&!document.hidden&&!st.busy)await load();schedulePoll()},7000)}
function setBusy(b){
 st.busy=b;
 ['pkPlay','pkHalf','pkDouble','pkAllIn','pkBet'].forEach(id=>{const e=$(id);if(e)e.disabled=b});
 document.querySelectorAll('#pkRisk button').forEach(x=>x.disabled=b);
 $('pkPlay').textContent=b?'Падает…':'Бросить шар';
}
async function play(){
 if(st.busy)return;
 const bet=betValue();
 if(!(bet>=minBet()-1e-9)||bet>maxBet()+1e-9){toast('Ставка от '+fmt(minBet())+' до '+fmt(maxBet())+' TON');return}
 if(gameSpendBalance()+1e-9<bet){toast('Недостаточно средств на активном балансе');return}
 setBet(bet);setBusy(true);$('pkResult').classList.add('hidden');
 const before=gameSpendBalance();
 try{
  const d=await api('/api/plinko/play',{method:'POST',body:JSON.stringify({bet:bet.toFixed(2),risk:st.risk,client_seed:(typeof fairClientSeed==='function'?fairClientSeed():undefined)}),timeoutMs:9000});
  try{$('balance').textContent=money(before-bet)}catch(e){}
  window.gdFairPlinko=d.fairness||null;try{fairnessRefresh()}catch(e){}
  await dropBall(d.result,st.turbo);
  landSlot(d.result);
  setUser(d.user);
  st.hist.unshift(d.result);st.hist=st.hist.slice(0,20);renderHist();
  showResult(d.result);
  if(d.result.prize){crShowWin({prize:d.result.prize,remainder:d.result.remainder,payout:d.result.payout,bonus_origin:d.result.bonus_origin});loadInventory()}
  if(d.new_level&&typeof showLevelNotice==='function')showLevelNotice(d.new_level);
  api('/api/plinko/state',{timeoutMs:6000}).then(s=>{if(Array.isArray(s.wins)){st.wins=s.wins;renderWins()}}).catch(()=>{});
 }catch(e){
  try{$('balance').textContent=money(before)}catch(_){}
  $('pkBall').setAttribute('opacity','0');paintIdle();
  toast(e.message||'Не удалось сыграть');
 }finally{setBusy(false)}
}
function bind(){
 if(bind.done)return;bind.done=true;build();
 document.querySelectorAll('#pkRisk button').forEach(b=>b.onclick=()=>{if(st.busy)return;st.risk=b.dataset.risk;try{localStorage.setItem('pk_risk',st.risk)}catch(e){}paintRisk();try{tg?.HapticFeedback?.selectionChanged?.()}catch(e){}});
 $('pkBet').addEventListener('input',e=>{let v=e.target.value.replace(',','.').replace(/[^0-9.]/g,''),i=v.indexOf('.');if(i>=0)v=v.slice(0,i+1)+v.slice(i+1).replace(/\./g,'').slice(0,2);e.target.value=v});
 $('pkBet').addEventListener('blur',()=>setBet(clampBet(betValue()||minBet())));
 $('pkBet').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();e.target.blur()}});
 $('pkHalf').onclick=()=>setBet(clampBet(betValue()/2));
 $('pkDouble').onclick=()=>setBet(clampBet(betValue()*2));
 $('pkAllIn').onclick=()=>{const bal=Math.floor(gameSpendBalance()*100+1e-6)/100;setBet(Math.max(minBet(),Math.min(bal,maxBet())))};
 $('pkTurbo').onclick=()=>{st.turbo=!st.turbo;$('pkTurbo').setAttribute('aria-pressed',st.turbo?'true':'false');try{localStorage.setItem('pk_turbo',st.turbo?'1':'0')}catch(e){}};
 $('pkPlay').onclick=play;
 try{if(localStorage.getItem('pk_turbo')==='1'){st.turbo=true;$('pkTurbo').setAttribute('aria-pressed','true')}const r=localStorage.getItem('pk_risk');if(r&&['low','medium','high'].includes(r))st.risk=r}catch(e){}
}
function enter(){bind();st.active=true;paintRisk();load();schedulePoll()}
function leave(){st.active=false;clearTimeout(st.poll)}
return {enter,leave,state:st};
})();
function plinkoEnter(){PK.enter()}
function plinkoLeave(){PK.leave()}
