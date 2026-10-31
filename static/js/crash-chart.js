/* GemDrop Crash chart. Round data and cash-out logic stay in the game controller. */
(function () {
  'use strict';
  var canvas=document.getElementById('crLine'),stage=document.getElementById('crStage'),button=document.getElementById('crViewBtn');
  if(!canvas||!stage)return;
  var ctx=canvas.getContext('2d'),last={phase:'betting',multiplier:1},burst=0,raf=0;
  if(!ctx)return;
  function quiet(){return stage.dataset.motion==='quiet'||document.documentElement.classList.contains('perf-lite')||(window.matchMedia&&matchMedia('(prefers-reduced-motion: reduce)').matches)}
  function draw(phase,multiplier){
    last={phase:phase,multiplier:multiplier};
    if(quiet())burst=0;
    var w=canvas.clientWidth,h=canvas.clientHeight;if(!w||!h){burst=0;return;}
    var d=Math.min(2,window.devicePixelRatio||1);
    if(canvas.width!==Math.round(w*d)||canvas.height!==Math.round(h*d)){canvas.width=Math.round(w*d);canvas.height=Math.round(h*d)}
    ctx.setTransform(d,0,0,d,0,0);ctx.clearRect(0,0,w,h);
    var left=24,right=w-28,top=h*.46,bottom=h-34;
    ctx.lineWidth=1;ctx.strokeStyle='#f3efe60a';
    for(var i=0;i<5;i++){var y=top+(bottom-top)*i/4;ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(right,y);ctx.stroke()}
    for(i=0;i<6;i++){var x=left+(right-left)*i/5;ctx.beginPath();ctx.moveTo(x,top);ctx.lineTo(x,bottom);ctx.stroke()}
    ctx.font='9px Montserrat, sans-serif';ctx.fillStyle='#766f62';ctx.textAlign='left';ctx.fillText('СТАРТ',left,h-13);ctx.textAlign='right';ctx.fillText('КРИВАЯ РОСТА',right,h-13);
    var p=phase==='betting'?0:(typeof window.crProgress==='function'?window.crProgress(multiplier):Math.min(1,Math.pow(Math.log(Math.max(1,multiplier))/Math.log(40),.8)));
    var points=[];for(i=0;i<=64;i++){var u=p*i/64;points.push([left+(right-left)*u,bottom-(bottom-top)*Math.pow(u,1.55)])}
    var col=phase==='crashed'?'#c79a88':'#e4d8bd',end=points[64];
    function path(){ctx.beginPath();points.forEach(function(q,j){j?ctx.lineTo(q[0],q[1]):ctx.moveTo(q[0],q[1])})}
    if(p>0){
      path();ctx.lineTo(end[0],bottom);ctx.lineTo(left,bottom);ctx.closePath();var fill=ctx.createLinearGradient(0,top,0,bottom);fill.addColorStop(0,col+'24');fill.addColorStop(1,col+'00');ctx.fillStyle=fill;ctx.fill();
      path();ctx.lineWidth=2.5;ctx.lineJoin='round';ctx.lineCap='round';ctx.strokeStyle=col;ctx.stroke();
    }
    if(phase==='flying'&&!quiet()){ctx.save();ctx.shadowColor='#e4d8bd55';ctx.shadowBlur=22;ctx.fillStyle='#e4d8bd10';ctx.beginPath();ctx.arc(end[0],end[1],18,0,Math.PI*2);ctx.fill();ctx.restore()}
    ctx.save();ctx.translate(end[0],end[1]);ctx.rotate(-.15);ctx.fillStyle='#181610';ctx.strokeStyle=col;ctx.lineWidth=1.5;ctx.beginPath();ctx.moveTo(0,-10);ctx.lineTo(8,0);ctx.lineTo(0,11);ctx.lineTo(-8,0);ctx.closePath();ctx.fill();ctx.stroke();ctx.beginPath();ctx.moveTo(-8,0);ctx.lineTo(8,0);ctx.moveTo(0,-10);ctx.lineTo(0,11);ctx.stroke();ctx.restore();
    if(burst&&!quiet()){var k=Math.min(1,(performance.now()-burst)/650);ctx.save();ctx.globalAlpha=1-k;ctx.strokeStyle=col;ctx.lineWidth=1;ctx.beginPath();ctx.arc(end[0],end[1],12+28*k,0,Math.PI*2);ctx.stroke();ctx.restore();if(k>=1)burst=0}
  }
  function finishBurst(){raf=0;draw(last.phase,last.multiplier);if(burst)raf=requestAnimationFrame(finishBurst)}
  window.crLineDraw=function(phase,multiplier){
    if(phase==='crashed'&&last.phase!=='crashed'&&!quiet()){burst=performance.now();if(!raf)raf=requestAnimationFrame(finishBurst)}
    if(phase!=='crashed'){burst=0;if(raf)cancelAnimationFrame(raf);raf=0}
    draw(phase,multiplier);
  };
  stage.classList.add('lv');
  try{stage.dataset.motion=localStorage.getItem('gd_crash_motion')||'full'}catch(e){}
  function sync(){if(button)button.setAttribute('aria-pressed',String(stage.dataset.motion==='quiet'));draw(last.phase,last.multiplier)}
  if(button)button.addEventListener('click',function(){stage.dataset.motion=stage.dataset.motion==='quiet'?'full':'quiet';try{localStorage.setItem('gd_crash_motion',stage.dataset.motion)}catch(e){}sync()});
  window.addEventListener('resize',sync);sync();
})();
