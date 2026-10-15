/* ===== Plinko ===== */
var PK=(function(){
const $=id=>document.getElementById(id);
const NS='http://www.w3.org/2000/svg';
const ROWS=16,DX=23.5,CX=200,Y0=24,DY=19.5,SLOT_Y=Y0+ROWS*DY+5,SW=21;
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
/* slot colour from multiplier: vivid fill + readable label */
function slotColor(m){
 if(m>=100)return['#ff3b5c','#fff'];
 if(m>=25)return['#ff6a3d','#fff'];
 if(m>=8)return['#ffa532','#1a1204'];
 if(m>=2)return['#ffd23f','#1a1204'];
 if(m>=1)return['#4fc3f7','#04141f'];
 return['#5b6fb5','#fff'];
}
function fmtMult(m){
 if(m>=1000)return(m/1000).toFixed(m%1000?1:0).replace(/\.0$/,'')+'k';
 if(m>=100)return String(Math.round(m));
 if(m>=10)return m.toFixed(1).replace(/\.0$/,'');
 return m.toFixed(2).replace(/0$/,'').replace(/\.$/,'');
}
function build(){
 if(st.built)return;st.built=true;
 const pins=$('pkPins'),slots=$('pkSlots');
 for(let i=0;i<ROWS;i++)for(let r=0;r<=i;r++){st.pinEls[i+'_'+r]=el('circle',{cx:pinX(i,r),cy:pinY(i),r:2.3,class:'pk-pin'},pins)}
 for(let k=0;k<=ROWS;k++){
  const x=pinX(ROWS,k),g=el('g',{class:'pk-slot','data-k':k,transform:'translate('+x+' '+SLOT_Y+')'},slots);
  el('rect',{x:-SW/2,y:0,width:SW,height:24,rx:6,class:'pk-slot-bg'},g);
  const t=el('text',{x:0,y:15.5,'text-anchor':'middle',class:'pk-slot-t'},g);t.textContent='';
  st.slotEls[k]=g;
 }
}
function paintSlots(){
 const m=mults();
 st.slotEls.forEach((g,k)=>{
  const v=m[k];if(v==null)return;const [c,tc]=slotColor(v);
  const rect=g.firstChild,txt=g.lastChild;
  rect.setAttribute('fill',c);rect.setAttribute('stroke','rgba(255,255,255,.35)');
  txt.textContent=fmtMult(v);txt.setAttribute('fill',tc);
  txt.style.fontSize=String(fmtMult(v)).length>=4?'7px':String(fmtMult(v)).length===3?'8px':'9px';
 });
}
function paintRisk(){
 document.querySelectorAll('#pkRisk button').forEach(b=>{const on=b.dataset.risk===st.risk;b.classList.toggle('on',on);b.setAttribute('aria-pressed',on?'true':'false')});
 paintSlots();paintHint();
}
function paintHint(){
 const m=mults();if(!m.length)return;
 $('pkMax').textContent='от '+fmtMult(Math.min(...m))+' до '+fmtMult(Math.max(...m))+'x';
}
function setCenter(label,big,cls){
 $('pkCLabel').textContent=label;$('pkCBig').textContent=big;$('pkStage').dataset.state=cls||'idle';
}
function pinHit(i,r){
 const p=st.pinEls[i+'_'+r];if(!p)return;
 p.classList.remove('hit');void p.getBoundingClientRect();p.classList.add('hit');
}
function dropBall(result,turbo){
 return new Promise(resolve=>{
  const proto=$('pkBall'),ball=proto.cloneNode(true);ball.removeAttribute('id');proto.parentNode.appendChild(ball);
  const fast=turbo||reduced()||lite(),stepMs=reduced()?25:turbo?70:fast?95:150;
  const path=[];for(let i=0;i<ROWS;i++)path.push((result.path>>(ROWS-1-i))&1);
  let r=0,i=0;
  let x0=pinX(0,0),y0=pinY(0)-8;
  ball.setAttribute('opacity','1');
  ball.setAttribute('transform','translate('+x0+' '+(Y0-22)+')');
  
  // first fall to the top pin
  const segs=[];let cx=x0,cy=Y0-26;
  segs.push({x0:cx,y0:cy,x1:x0,y1:y0,hit:null,dur:stepMs*.8});
  let px=x0,py=y0;
  for(let n=0;n<ROWS;n++){
   const nr=r+path[n],nx=n+1<ROWS?pinX(n+1,nr):pinX(ROWS,nr),ny=n+1<ROWS?pinY(n+1)-8:SLOT_Y+9;
   segs.push({x0:px,y0:py,x1:nx,y1:ny,hit:[n,r],dur:stepMs,last:n===ROWS-1});
   r=nr;px=nx;py=ny;
  }
  let k=0,t0=performance.now();
  const frame=now=>{
   const s=segs[k];let t=Math.min(1,(now-t0)/s.dur);
   const x=s.x0+(s.x1-s.x0)*t;
   const arc=s.hit?-7*Math.sin(Math.PI*t)*(1-.35*t):0;
   const y=s.y0+(s.y1-s.y0)*t*t+arc;
   ball.setAttribute('transform','translate('+x.toFixed(2)+' '+y.toFixed(2)+')');
   if(t>=1){
    if(s.hit)pinHit(s.hit[0],s.hit[1]);
    k++;t0=now;
    if(k>=segs.length){ball.remove();resolve();return}
   }
   requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
 });
}
function landSlot(result){
 const g=st.slotEls[result.slot];if(!g)return;
 g.classList.remove('land');void g.getBoundingClientRect();g.classList.add('land');
 setTimeout(()=>g.classList.remove('land'),1400);
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
  if(d.user&&!st.inflight)setUser(d.user);
}
async function load(){
 if(st.loading)return;st.loading=true;
 try{const d=await api('/api/plinko/state',{timeoutMs:6000});applyState(d);paintRisk()}
 catch(e){if(st.active&&!st.cfg){toast(e.message||'Plinko недоступна');if(/недоступн|403/i.test(e.message||''))show('gamesPage')}}
 finally{st.loading=false}
}
function schedulePoll(){clearTimeout(st.poll);if(!st.active)return;st.poll=setTimeout(async()=>{if(st.active&&!document.hidden&&!st.inflight)await load();schedulePoll()},7000)}
function reserved(){return st.reserved||0}
async function play(){
 const now=performance.now();if(now-(st.last||0)<90)return;st.last=now;
 const bet=betValue();
 if(!(bet>=minBet()-1e-9)||bet>maxBet()+1e-9){toast('Ставка от '+fmt(minBet())+' до '+fmt(maxBet())+' TON');return}
 if(gameSpendBalance()-reserved()+1e-9<bet){toast('Недостаточно средств на активном балансе');return}
 setBet(bet);
 st.inflight=(st.inflight||0)+1;st.reserved=reserved()+bet;
 try{$('balance').textContent=money(gameSpendBalance()-reserved())}catch(e){}
 let landed=false;
 const settle=()=>{if(landed)return;landed=true;st.inflight--;st.reserved=Math.max(0,reserved()-bet)};
 try{
  const d=await api('/api/plinko/play',{method:'POST',body:JSON.stringify({bet:bet.toFixed(2),risk:st.risk,client_seed:(typeof fairClientSeed==='function'?fairClientSeed():undefined)}),timeoutMs:9000});
  window.gdFairPlinko=d.fairness||null;try{fairnessRefresh()}catch(e){}
  dropBall(d.result,st.turbo).then(()=>{
   landSlot(d.result);settle();
   if(!st.inflight)setUser(d.user);
   if(d.result.prize){crShowWin({prize:d.result.prize,remainder:d.result.remainder,payout:d.result.payout,bonus_origin:d.result.bonus_origin});loadInventory()}
   if(d.new_level&&typeof showLevelNotice==='function')showLevelNotice(d.new_level);
   try{tg?.HapticFeedback?.impactOccurred?.(d.result.multiplier>=2?'medium':'light')}catch(e){}
   api('/api/plinko/state',{timeoutMs:6000}).then(x=>{if(Array.isArray(x.wins)){st.wins=x.wins;renderWins()}}).catch(()=>{});
  });
 }catch(e){settle();try{$('balance').textContent=money(gameSpendBalance()-reserved())}catch(_){}toast(e.message||'Не удалось сыграть')}
}
function bind(){
 if(bind.done)return;bind.done=true;build();
 document.querySelectorAll('#pkRisk button').forEach(b=>b.onclick=()=>{st.risk=b.dataset.risk;try{localStorage.setItem('pk_risk',st.risk)}catch(e){}paintRisk();try{tg?.HapticFeedback?.selectionChanged?.()}catch(e){}});
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
