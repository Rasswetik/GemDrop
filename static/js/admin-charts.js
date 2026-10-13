window.AdminCharts = {
  render(ctx, data) {
    const {el,svg,money} = ctx,$=id=>document.getElementById(id);
    const colors=['#5798ff','#43c59d','#a993f3','#dda868'];
    function lines(id,fields,out,describe,integer=false){
      const host=$(id),width=Math.max(250,host.clientWidth),height=host.clientHeight || 300,left=50,right=14,top=22,bottom=35,w=width-left-right,h=height-top-bottom;
      const series=data.series,peak=Math.max(1,...series.flatMap(r=>fields.map(([key])=>Number(r[key] || 0))));
      const maximum=integer?Math.max(4,Math.ceil(peak/4)*4):peak*1.15;
      const x=i=>left+(series.length>1?i*w/(series.length-1):w/2),y=v=>top+h-Number(v || 0)/maximum*h;
      const canvas=svg('svg',{viewBox:`0 0 ${width} ${height}`});host.replaceChildren(canvas);
      for(let i=0;i<5;i++){const yy=top+i*h/4;canvas.append(svg('line',{x1:left,x2:width-right,y1:yy,y2:yy,stroke:'#243248'}),svg('text',{x:left-7,y:yy+4,'text-anchor':'end'},integer?maximum*(1-i/4):Number((maximum*(1-i/4)).toFixed(2)).toLocaleString('ru-RU')));}
      const step=Math.max(1,Math.ceil(series.length/(width<450?3:5)));
      series.forEach((r,i)=>{if(i%step===0 || i===series.length-1)canvas.append(svg('text',{x:x(i),y:height-9,'text-anchor':i===0?'start':i===series.length-1?'end':'middle'},r.day.slice(8)+'.'+r.day.slice(5,7)));});
      fields.forEach(([key,color],field)=>{
        const path=series.map((r,i)=>(i?'L':'M')+x(i)+','+y(r[key])).join(' ');
        if(field===0 && series.length>1)canvas.append(svg('path',{d:path+` L${x(series.length-1)},${top+h} L${x(0)},${top+h} Z`,fill:color,'fill-opacity':.06}));
        canvas.append(svg('path',{d:path,stroke:color,'stroke-width':2.4,fill:'none','stroke-linejoin':'round','stroke-linecap':'round'}));
        if(series.length===1)canvas.append(svg('circle',{cx:x(0),cy:y(series[0][key]),r:3,fill:color}));
      });
      const cursor=svg('line',{y1:top,y2:top+h,x1:0,x2:0,stroke:'#7694bf','stroke-dasharray':'3 4',visibility:'hidden'});canvas.append(cursor);
      let selected=0;function select(i){selected=Math.max(0,Math.min(series.length-1,i));const row=series[selected];if(!row)return;cursor.setAttribute('x1',x(selected));cursor.setAttribute('x2',x(selected));cursor.setAttribute('visibility','visible');$(out).textContent=describe(row);}
      const pointer=e=>{const r=canvas.getBoundingClientRect(),position=(e.clientX-r.left)*width/r.width;select(Math.round((position-left)/w*(series.length-1)));};
      canvas.onpointermove=canvas.onclick=pointer;canvas.setAttribute('tabindex','0');canvas.setAttribute('aria-label','График по дням. Стрелки влево и вправо выбирают дату.');canvas.onkeydown=e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();select(e.key==='Home'?0:e.key==='End'?series.length-1:selected+(e.key==='ArrowRight'?1:-1));}};
    }
    lines('gameChart',[['stake',colors[0]],['payout',colors[1]]],'gamePoint',r=>`${r.day} · Ставки ${money(r.stake)} TON · Возврат ${money(r.payout)} TON · ${r.game_count || 0} игр`);
    lines('audienceChart',[['new_users',colors[0]],['active_players',colors[1]],['depositors',colors[2]]],'audiencePoint',r=>`${r.day} · Новых: ${r.new_users || 0} · Играли: ${r.active_players || 0} · Пополняли: ${r.depositors || 0}`,true);
    const host=$('methodsChart'),width=Math.max(250,host.clientWidth),height=host.clientHeight || 250,canvas=svg('svg',{viewBox:`0 0 ${width} ${height}`}),cx=width/2,cy=height/2,r=80,circ=2*Math.PI*r;
    host.replaceChildren(canvas);canvas.append(svg('circle',{cx,cy,r,fill:'none',stroke:'#1b2b42','stroke-width':24}));
    const total=data.methods.reduce((n,x)=>n+x.total,0);let offset=0;
    data.methods.forEach((method,i)=>{const share=total?method.total/total:0;const ring=svg('circle',{cx,cy,r,fill:'none',stroke:colors[i%colors.length],'stroke-width':24,'stroke-dasharray':`${Math.max(0,share*circ-2)} ${circ}`,'stroke-dashoffset':-offset,transform:`rotate(-90 ${cx} ${cy})`});ring.append(svg('title',{},`${ctx.methods[method.method] || method.method}: ${money(method.total)} TON · ${(share*100).toFixed(1)}%`));canvas.append(ring);offset+=share*circ;});
    canvas.append(svg('text',{x:cx,y:cy-6,'text-anchor':'middle',style:'fill:#edf4ff;font:700 21px system-ui'},money(total)),svg('text',{x:cx,y:cy+19,'text-anchor':'middle',style:'fill:#879fbe;font:12px system-ui'},'TON пополнено'));
    $('methods').replaceChildren(...data.methods.map((m,i)=>{const row=el('div',null,'method-row'),label=el('span'),dot=el('i',null,'method-dot');dot.style.background=colors[i%colors.length];label.append(dot,document.createTextNode(ctx.methods[m.method] || m.method));row.append(label,el('strong',money(m.total)+' TON · '+(total?m.total/total*100:0).toFixed(1)+'%'));return row;}));
    if(!data.methods.length)$('methods').append(el('p','За период пополнений нет.','muted'));
  }
};
