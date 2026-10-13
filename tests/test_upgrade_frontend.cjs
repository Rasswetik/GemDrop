// Run with node --test tests/test_upgrade_frontend.cjs (no npm dependencies).
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../templates/index.html'),'utf8');
function section(start,end){return html.slice(html.indexOf(start),html.indexOf(end,html.indexOf(start)))}
function deferred(){let resolve,reject;const promise=new Promise((ok,fail)=>{resolve=ok;reject=fail});return{promise,resolve,reject}}
function selection(){
  const elements=new Map(),requests=[];
  const ctx=vm.createContext({
    $:id=>{if(!elements.has(id))elements.set(id,{textContent:'',disabled:false,classList:{contains:()=>true}});return elements.get(id)},
    api:url=>{const d=deferred();requests.push({...d,url});return d.promise},
    upgradeSource:{type:'ton',price_ton:2,balance_source:'real'},upgradeGoal:null,upgradePreviewData:null,
    upgradeBusy:false,upgradePendingId:42,upgradePreviewRequest:0,closed:false,
    upgradeLoading:false,upgradeLoadError:'',upgradeInventory:[],upgradeCatalog:[],upgradeSettings:{},
    ugLoadModelIndex(){},upgradeList(){},
    stopUpgradeIdle(){},closeUgm(){ctx.closed=true},toast(){},
    paintUpgrade(){
      // Balance refreshes replace the source object, preserving the selected stake.
      ctx.upgradeSource={...ctx.upgradeSource};elements.get('upgradeAction')||ctx.$('upgradeAction');
      ctx.$('upgradeAction').disabled=!ctx.upgradePreviewData;
    },
    upgradePreviewUrl:id=>'/api/upgrade/preview?gift_id='+id
  });
  vm.runInContext(section('function upgradeSourceKey(', 'function setUpgradePrice('),ctx);
  return {ctx,requests};
}
const goal=(model='Pumpkin')=>({id:'pepe@m:'+model,name:'Plush Pepe · '+model,model,price_ton:40,type:'gift'});

test('a model is selected and the sheet closes before server validation finishes',async()=>{
  const {ctx,requests}=selection(),chosen=goal();
  const pending=ctx.selectUpgradeGoal(chosen);
  assert.equal(ctx.upgradeGoal,chosen);assert.equal(ctx.closed,true);
  assert.equal(ctx.$('upgradeAction').disabled,true);assert.equal(ctx.upgradePendingId,null);
  // An unrelated repaint must not invalidate the same selected stake.
  ctx.paintUpgrade();requests[0].resolve({chance:5,target:{id:'pepe',price_ton:41,model:'Pumpkin'}});await pending;
  assert.equal(ctx.$('upgradeAction').disabled,false);assert.equal(ctx.upgradeGoal.price_ton,41);
  assert.equal(ctx.upgradeGoal.id,'pepe@m:Pumpkin');
  assert.equal(ctx.upgradePreviewData.chance,5);
});
test('an older response cannot overwrite a more recently selected target',async()=>{
  const {ctx,requests}=selection();
  const first=ctx.selectUpgradeGoal(goal('Original')),second=ctx.selectUpgradeGoal(goal());
  requests[1].resolve({chance:5});await second;
  requests[0].resolve({chance:16,target:{model:'Original'}});await first;
  assert.equal(ctx.upgradeGoal.model,'Pumpkin');assert.equal(ctx.upgradePreviewData.chance,5);
});
test('changing the stake while validation is pending discards that response',async()=>{
  const {ctx,requests}=selection(),pending=ctx.selectUpgradeGoal(goal());
  ctx.upgradeSource={type:'ton',price_ton:1,balance_source:'real'};
  requests[0].resolve({chance:5});await pending;
  assert.equal(ctx.upgradePreviewData,null);assert.equal(ctx.$('upgradeAction').disabled,true);
});
test('a failed validation keeps the chosen model visible with the action disabled',async()=>{
  const {ctx,requests}=selection(),chosen=goal(),pending=ctx.selectUpgradeGoal(chosen);
  requests[0].reject(Error('Price changed'));await pending;
  assert.equal(ctx.upgradeGoal,chosen);assert.equal(ctx.$('upgradeStatus').textContent,'Price changed');
  assert.equal(ctx.$('upgradeAction').disabled,true);
});
test('a background preview refresh cannot overwrite a newer model selection',async()=>{
  const {ctx,requests}=selection();ctx.upgradeGoal=goal('Original');
  const refreshing=ctx.loadUpgrade();
  requests[0].resolve({items:[]});requests[1].resolve({gifts:[]});requests[2].resolve({});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(requests.length,4);
  const selected=ctx.selectUpgradeGoal(goal());requests[4].resolve({chance:5});await selected;
  requests[3].resolve({chance:16,target:{model:'Original'}});await refreshing;
  assert.equal(ctx.upgradeGoal.model,'Pumpkin');assert.equal(ctx.upgradePreviewData.chance,5);
});

function animation(){
  const classes=new Set(),requests=[],players=[];
  const box={isConnected:true,_ga:null,append(layer){layer.isConnected=true},classList:{add:x=>classes.add(x),remove:x=>classes.delete(x)}};
  const ctx=vm.createContext({GA:{live:[],max:4},gaOff:()=>false,gaQuery:item=>item.model||item.id,
    gaData:()=>{const d=deferred();requests.push(d);return d.promise},
    gaLib:()=>Promise.resolve({loadAnimation:options=>{
      const listeners={},player={isLoaded:false,destroyed:false,destroy(){this.destroyed=true},addEventListener:(name,cb)=>listeners[name]=cb,listeners,options};players.push(player);return player;
    }}),
    document:{createElement:()=>({isConnected:false,remove(){this.isConnected=false}})},
  });
  vm.runInContext(section('function gaStop(box)', 'function gaHover(box,item)'),ctx);
  return {ctx,box,classes,requests,players};
}
test('PNG remains visible until the player is ready and failed loads can be retried',async()=>{
  const {ctx,box,classes,requests,players}=animation();
  const first=ctx.gaPlay(box,{id:'pepe'});requests[0].resolve(null);await first;
  assert.equal(box._ga,null);assert.equal(classes.has('ga-on'),false);
  const retry=ctx.gaPlay(box,{id:'pepe'});requests[1].resolve({layers:[{}]});await retry;
  assert.equal(classes.has('ga-on'),false);players[0].listeners.DOMLoaded();assert.equal(classes.has('ga-on'),true);
  await ctx.gaPlay(box,{id:'pepe'});assert.equal(players.length,1);
  players[0].listeners.data_failed();assert.equal(box._ga,null);assert.equal(classes.has('ga-on'),false);
});
test('switching models with the same gift ID replaces the old player',async()=>{
  const {ctx,box,requests,players}=animation();
  const first=ctx.gaPlay(box,{id:'pepe',model:'Original'},{key:'pepe'});requests[0].resolve({layers:[{}]});await first;
  const second=ctx.gaPlay(box,{id:'pepe',model:'Pumpkin'},{key:'pepe'});requests[1].resolve({layers:[{}]});await second;
  assert.equal(players.length,2);assert.equal(players[0].destroyed,true);assert.equal(players[1].destroyed,false);
});
test('a rejected old animation request cannot destroy the newer player',async()=>{
  const {ctx,box,requests,players}=animation();
  const first=ctx.gaPlay(box,{id:'first'}),second=ctx.gaPlay(box,{id:'second'});
  requests[1].resolve({layers:[{}]});await second;
  requests[0].reject(Error('old request failed'));await first;
  assert.equal(players.length,1);assert.equal(players[0].destroyed,false);assert.equal(box._ga.key,'|second');
});
test('hovering the outer gift surface plays the current gift without a click',()=>{
  const handlers={},surface={addEventListener:(event,fn)=>handlers[event]=fn,contains:node=>node==='inside'};
  const box={closest:()=>surface},plays=[],stops=[];
  const ctx=vm.createContext({window:{},matchMedia:()=>({matches:false}),gaPlay:(...args)=>plays.push(args),gaStop:target=>stops.push(target)});
  vm.runInContext(section('function gaHover(box,item)', 'const modelPngCache='),ctx);
  let chosen=goal('Original');ctx.gaHover(box,()=>chosen);
  handlers.pointerenter({pointerType:'mouse'});assert.equal(plays[0][0],box);assert.equal(plays[0][1].model,'Original');
  chosen=goal('Pumpkin');handlers.pointerenter({pointerType:'mouse'});assert.equal(plays[1][1].model,'Pumpkin');
  handlers.pointerleave({relatedTarget:'inside'});assert.equal(stops.length,0);
  handlers.pointerleave({relatedTarget:null});assert.equal(stops[0],box);
  assert.equal(handlers.click,undefined);
});
