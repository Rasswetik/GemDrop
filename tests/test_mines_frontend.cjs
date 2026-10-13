// Run with node --test tests/test_mines_frontend.cjs (no npm dependencies).
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../templates/index.html'),'utf8');
const code=html.slice(html.indexOf('let ladderSeq='),html.indexOf('function drawLadder()'));
function deferred(){let resolve,reject;const promise=new Promise((ok,fail)=>{resolve=ok;reject=fail});return{promise,resolve,reject}}
function ladder(){
  const requests=[],bet={value:'10'},draws=[];
  const ctx=vm.createContext({round:null,betGift:null,mines:3,levels:[],Date,Number,encodeURIComponent,
    $:()=>bet,api:url=>{let d=deferred();requests.push({...d,url});return d.promise},
    drawLadder:()=>draws.push(vm.runInContext('({levels,loading:ladderLoading})',ctx))});
  vm.runInContext(code,ctx);return{ctx,requests,bet,draws};
}
const levels=id=>[{step:1,amount:12,prize:{id}}];

test('a slow old stake response cannot replace the latest preview',async()=>{
  const {ctx,requests,bet}=ladder();
  const first=ctx.refreshLadder();bet.value='20';ctx.invalidateMinesLadder();const second=ctx.refreshLadder();
  requests[1].resolve({levels:levels('new')});await second;
  requests[0].resolve({levels:levels('old')});await first;
  assert.equal(ctx.levels[0].prize.id,'new');
});

test('a settled round adopts the next panel without another network request',async()=>{
  const {ctx,requests}=ladder();const pending=ctx.refreshLadder();
  ctx.applyNextMinesLadder({bet:10,mines:3,levels:levels('next-round')});
  assert.equal(requests.length,1);assert.equal(ctx.levels[0].prize.id,'next-round');
  requests[0].resolve({levels:levels('finished-round')});await pending;
  assert.equal(ctx.levels[0].prize.id,'next-round');
});

test('identical previews reuse requests and cache while changed stakes clear old gifts',async()=>{
  const {ctx,requests,bet,draws}=ladder();const first=ctx.refreshLadder(),duplicate=ctx.refreshLadder();
  assert.equal(requests.length,1);requests[0].resolve({levels:levels('cached')});await Promise.all([first,duplicate]);
  await ctx.refreshLadder();assert.equal(requests.length,1);assert.equal(ctx.levels[0].prize.id,'cached');
  bet.value='25';const next=ctx.refreshLadder();assert.equal(ctx.levels.length,0);assert.equal(draws.at(-1).loading,true);
  requests[1].resolve({levels:levels('25')});await next;assert.equal(ctx.levels[0].prize.id,'25');
});

test('a stake clamped after loss fetches its own preview and cannot reuse the finished round cache',async()=>{
  const {ctx,requests,bet}=ladder();const first=ctx.refreshLadder();requests[0].resolve({levels:levels('old')});await first;
  bet.value='5';ctx.applyNextMinesLadder({bet:10,mines:3,levels:levels('wrong-stake')});
  assert.equal(ctx.levels.length,0);assert.match(requests[1].url,/bet=5/);
  requests[1].resolve({levels:levels('clamped')});await new Promise(resolve=>setImmediate(resolve));
  assert.equal(ctx.levels[0].prize.id,'clamped');
  bet.value='10';const next=ctx.refreshLadder();assert.equal(requests.length,3);
  requests[2].resolve({levels:levels('fresh-round')});await next;
});
