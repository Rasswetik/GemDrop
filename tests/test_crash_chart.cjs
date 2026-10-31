const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../static/js/crash-chart.js'),'utf8');
function setup(){
 let queue=new Map(),id=0,click,pressed;
 const gradient={addColorStop(){}};
 const context=new Proxy({}, {get:(t,k)=>k in t?t[k]:k==='createLinearGradient'?()=>gradient:()=>{}});
 const canvas={clientWidth:390,clientHeight:340,width:0,height:0,getContext:()=>context};
 const stage={dataset:{},classList:{add(){}}},button={addEventListener:(e,cb)=>click=cb,setAttribute:(k,v)=>pressed=v};
 const frame=vm.createContext({document:{getElementById:id=>({crLine:canvas,crStage:stage,crViewBtn:button})[id],documentElement:{classList:{contains:()=>false}}},window:{devicePixelRatio:1,matchMedia:()=>({matches:false}),addEventListener(){}},matchMedia:()=>({matches:false}),localStorage:{getItem:()=>null,setItem(){}},Math,performance:{now:()=>100},requestAnimationFrame:cb=>{queue.set(++id,cb);return id},cancelAnimationFrame:i=>queue.delete(i)});
 vm.runInContext(source,frame);
 return{canvas,stage,draw:frame.window.crLineDraw,toggle:()=>click(),tick:()=>{const [i,cb]=queue.entries().next().value;queue.delete(i);cb()},get pending(){return queue.size},get pressed(){return pressed}};
}
test('turning effects off stops an in-progress crash burst',()=>{
 const c=setup();c.draw('flying',2);c.draw('crashed',2);assert.equal(c.pending,1);c.toggle();assert.equal(c.pressed,'true');c.tick();assert.equal(c.pending,0);
});
test('hiding the chart during a burst cannot leave an animation loop running',()=>{
 const c=setup();c.draw('flying',2);c.draw('crashed',2);c.canvas.clientWidth=0;c.tick();assert.equal(c.pending,0);
});
test('a new round cancels the previous crash animation',()=>{
 const c=setup();c.draw('flying',2);c.draw('crashed',2);assert.equal(c.pending,1);c.draw('betting',1);assert.equal(c.pending,0);
});
