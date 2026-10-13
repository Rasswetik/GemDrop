const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../templates/index.html'),'utf8');
function section(a,b){return html.slice(html.indexOf(a),html.indexOf(b,html.indexOf(a)))}
function clock(){let ms=1000;const ctx=vm.createContext({performance:{now:()=>ms},Date,Math,Number});vm.runInContext(section('var CR={','const CR_PAD='),ctx);vm.runInContext(section('function crMultCont(){','function crPath('),ctx);return{ctx,advance:n=>ms+=n,apply:d=>{if(ctx.crAcceptState(d)){ctx.CR.st=d;return true}return false}}}
const sample=(x=100,now=10000,phase='flying',id=1)=>({now,phase,multiplier_x100:x,growth:.09,round:{id,launch_at:9000,...phase==='crashed'?{crash:x/100}:{}},my_bet:{state:phase==='crashed'?'lost':'active'}});
test('a 1.00x crash never displays 1.15x while its final response is delayed',()=>{
 const {ctx,advance,apply}=clock();apply(sample());advance(10000);assert.equal(ctx.crMultNow(),1);assert.equal(ctx.crMultCont(),1);apply(sample(100,11000,'crashed'));assert.equal(ctx.crMultNow(),1);
});
test('smooth flight only interpolates inside the server-confirmed multiplier',()=>{
 const {ctx,advance,apply}=clock();apply(sample(110));let values=[];for(let i=0;i<25;i++){advance(20);values.push(ctx.crMultCont())}assert(values.every(x=>x>=1&&x<=1.1));assert.equal(values.at(-1),1.1);apply(sample(120,10400));advance(125);assert(ctx.crMultCont()>1.1&&ctx.crMultCont()<1.2);advance(10000);assert.equal(ctx.crMultNow(),1.2);
});
test('late responses cannot revive a crashed round or rewind a new round',()=>{
 const {ctx,apply}=clock();apply(sample(123,11000,'crashed'));assert.equal(apply(sample(120,10900)),false);assert.equal(ctx.crPhase(),'crashed');apply(sample(100,12000,'betting',2));assert.equal(apply(sample(123,11000,'crashed',1)),false);
});
test('the puck keeps substantial glide during its last three seconds and lands exactly',()=>{
 const ctx=vm.createContext({Math});vm.runInContext(section('function arenaDist(','function arenaFrame('),ctx);let spin={total:2850,dur:15000};const remaining=spin.total-ctx.arenaDist(spin,12000);assert(remaining>=110&&remaining<=115);assert.equal(ctx.arenaDist(spin,15000),2850);assert.equal(ctx.arenaDist(spin,16000),2850);assert.equal(ctx.arenaDist(spin,0),0);
});
