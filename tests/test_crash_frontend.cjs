const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../templates/index.html'),'utf8');
function section(a,b){return html.slice(html.indexOf(a),html.indexOf(b,html.indexOf(a)))}
function clock(){let ms=1000;const ctx=vm.createContext({performance:{now:()=>ms},Date,Math,Number});vm.runInContext(section('var CR={','const CR_PAD='),ctx);vm.runInContext(section('const crLead=','function crPath('),ctx);return{ctx,ev:e=>vm.runInContext(e,ctx),advance:n=>ms+=n,apply:d=>{if(ctx.crAcceptState(d)){ctx.CR.st=d;return true}return false}}}
const sample=(x=100,now=10000,phase='flying',id=1)=>({now,phase,multiplier_x100:x,growth:.09,round:{id,launch_at:9000,...phase==='crashed'?{crash:x/100}:{}},my_bet:{state:phase==='crashed'?'lost':'active'}});
test('a stalled connection cannot push a crashing round far past the last confirmed x',()=>{
 const {ctx,ev,advance,apply}=clock();apply(sample(109,10000));advance(10000);
 const cap=Math.exp(.09*(10000+ev('crLead()')-9000)/1000);
 assert(ctx.crMultCont()<=cap+1e-9,'extrapolation is bounded by one poll cycle');
 assert(ctx.crMultNow()<1.2);
 apply(sample(100,21000,'crashed'));assert.equal(ctx.crMultNow(),1);
});
test('the multiplier grows continuously between polls and never moves backwards',()=>{
 const {ctx,advance,apply}=clock();apply(sample(109,10000));let prev=1,steps=new Set();
 for(let i=0;i<40;i++){advance(16);const m=ctx.crMultCont();assert(m>=prev,'monotonic');steps.add(m.toFixed(5));prev=m}
 assert(steps.size>=25,'a fresh value is produced on (almost) every frame, not once per poll');
 apply(sample(110,10250));advance(16);assert(ctx.crMultCont()>=prev);
});
test('the clock follows the lowest-latency sample so slow responses do not slow the number down',()=>{
 const {ev,advance,apply}=clock();apply(sample(109,10000));advance(300);apply(sample(110,10200));
 const fast=ev('crSrv()');advance(250);apply(sample(111,10650));assert(ev('crSrv()')>=fast+250-1e-6,'a slower sample must not move the clock backwards');
});
test('late responses cannot revive a crashed round or rewind a new round',()=>{
 const {ctx,apply}=clock();apply(sample(123,11000,'crashed'));assert.equal(apply(sample(120,10900)),false);assert.equal(ctx.crPhase(),'crashed');apply(sample(100,12000,'betting',2));assert.equal(apply(sample(123,11000,'crashed',1)),false);
});
test('the puck keeps substantial glide during its last three seconds and lands exactly',()=>{
 const ctx=vm.createContext({Math});vm.runInContext(section('function arenaDist(','function arenaFrame('),ctx);let spin={total:2850,dur:15000};const remaining=spin.total-ctx.arenaDist(spin,12000);assert(remaining>=110&&remaining<=115);assert.equal(ctx.arenaDist(spin,15000),2850);assert.equal(ctx.arenaDist(spin,16000),2850);assert.equal(ctx.arenaDist(spin,0),0);
});
