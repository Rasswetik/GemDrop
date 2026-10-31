const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../templates/index.html'),'utf8');
const part=(a,b)=>html.slice(html.indexOf(a),html.indexOf(b,html.indexOf(a)));
function setup(){
 const elements=new Map(),requests=[],timers=[];let now=10000,copied='',logins=0;
 const classes=()=>{let set=new Set();return{add:x=>set.add(x),remove:x=>set.delete(x),contains:x=>set.has(x),toggle:(x,v)=>v?set.add(x):set.delete(x)}};
 const $=id=>{if(!elements.has(id))elements.set(id,{classList:classes(),dataset:{},textContent:'',disabled:false,parentElement:{classList:classes()}});return elements.get(id)};
 const ctx=vm.createContext({$,document:{body:{classList:classes()},documentElement:{classList:classes()},hidden:false},window:{matchMedia:()=>({matches:false})},URLSearchParams,location:{search:''},browserIdentity:()=> 'fixture-browser',Date:{now:()=>now},Math,Number,String,JSON,
  api:url=>new Promise((resolve,reject)=>requests.push({url,resolve,reject})),
  setTimeout:fn=>(timers.push(fn),timers.length),clearTimeout(){},setInterval:fn=>(timers.push(fn),timers.length),clearInterval(){},
  webAuthTimer:0,webAuthExpiryTimer:0,webAuthExpiresAt:0,
  navigator:{clipboard:{writeText:async value=>{copied=value}}},completeLogin:async()=>{logins++}});
 vm.runInContext(part('let webAuthGeneration=0;','async function handleLaunchAction'),ctx);
 vm.runInContext(part('async function pollWebAuth(','async function boot()'),ctx);
 return{ctx,$,requests,advance:ms=>now+=ms,get copied(){return copied},get logins(){return logins}};
}
const reply=(command='/auth DEMO')=>({command,bot_username:'fixture_bot',expires_in:300});
test('creating a code disables copy until ready and shows the waiting state',async()=>{
 const s=setup(),p=s.ctx.beginWebAuth();assert.equal(s.$('auth').dataset.authState,'loading');assert.equal(s.$('copyWebCommand').disabled,true);
 s.requests[0].resolve(reply());await p;assert.equal(s.$('auth').dataset.authState,'waiting');assert.equal(s.$('copyWebCommand').disabled,false);assert.equal(s.$('webAuthCommand').textContent,'/auth DEMO');assert.equal(s.$('webBotLabel').textContent,'Открыть @fixture_bot');
});
test('copy feedback appears only after the command reaches the clipboard',async()=>{
 const s=setup(),p=s.ctx.beginWebAuth();s.requests[0].resolve(reply());await p;await s.ctx.copyWebAuthCommand();assert.equal(s.copied,'/auth DEMO');assert.equal(s.$('auth').dataset.authState,'copied');assert.match(s.$('copyWebCommand').textContent,/Скопировано/);
});
test('expiry disables copy and hides the old bot action; renewing restores a fresh code',async()=>{
 const s=setup(),p=s.ctx.beginWebAuth();s.requests[0].resolve(reply());await p;s.advance(301000);s.ctx.webAuthCountdown();assert.equal(s.$('auth').dataset.authState,'expired');assert.equal(s.$('copyWebCommand').disabled,true);assert.equal(s.$('webBotLink').classList.contains('hidden'),true);
 const again=s.ctx.beginWebAuth();s.requests[1].resolve(reply('/auth NEW'));await again;assert.equal(s.$('copyWebCommand').disabled,false);assert.equal(s.$('webAuthCommand').textContent,'/auth NEW');
});
test('a slow old start response cannot replace a renewed command',async()=>{
 const s=setup(),old=s.ctx.beginWebAuth(),fresh=s.ctx.beginWebAuth();s.requests[1].resolve(reply('/auth NEW'));await fresh;s.requests[0].resolve(reply('/auth OLD'));await old;assert.equal(s.$('webAuthCommand').textContent,'/auth NEW');
});
test('an approval response from an older code cannot switch the renewed screen',async()=>{
 const s=setup(),start=s.ctx.beginWebAuth();s.requests[0].resolve(reply());await start;const poll=s.ctx.pollWebAuth(),fresh=s.ctx.beginWebAuth();s.requests[2].resolve(reply('/auth NEW'));await fresh;s.requests[1].resolve({status:'approved'});await poll;assert.equal(s.logins,0);assert.equal(s.$('auth').dataset.authState,'waiting');
});
test('a failed start leaves renewal available and gives an explicit error state',async()=>{
 const s=setup(),p=s.ctx.beginWebAuth();s.requests[0].reject(new Error('Нет соединения'));await p;assert.equal(s.$('auth').dataset.authState,'error');assert.equal(s.$('webAuthRenew').disabled,false);assert.equal(s.$('copyWebCommand').disabled,true);assert.match(s.$('webAuthStatus').textContent,/Нет соединения/);
});
test('a failed profile load after approval reveals the login screen again',async()=>{
 const s=setup(),p=s.ctx.beginWebAuth();s.requests[0].resolve(reply());await p;s.ctx.completeLogin=async()=>{throw new Error('Профиль временно недоступен')};const poll=s.ctx.pollWebAuth();s.requests[1].resolve({status:'approved'});await poll;assert.equal(s.$('loader').classList.contains('hidden'),true);assert.equal(s.$('auth').classList.contains('hidden'),false);assert.equal(s.$('auth').dataset.authState,'error');
});
