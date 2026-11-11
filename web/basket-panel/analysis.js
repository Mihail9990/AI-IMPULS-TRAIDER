'use strict';
// Independent, read-only analysis transport. Never consumes trading chart candles.
function createAnalysisUI({document,fetch,getHeaders}){
 const $=id=>document.getElementById(id),ns='http://www.w3.org/2000/svg',periods=['M1','M15','M30','H1'];
 const cards=['patterns','structure'].map(id=>({id,seq:0,controller:null}));
 const states={READY:'Готово',STALE:'Устаревшие данные',DISCONNECTED:'Нет связи с MT5',TIME_UNVERIFIED:'Время не подтверждено',WARMING_UP:'Недостаточно истории',UNAVAILABLE:'Недоступно',ERROR:'Ошибка',LICENSE_PENDING:'Лицензия не согласована'};
 let visible=false,timer=null;
 const number=v=>typeof v!=='boolean'&&v!==null&&v!==undefined&&v!==''&&Number.isFinite(Number(v));
 const stamp=t=>number(t)&&Number(t)>0&&Number(t)<8640000000000?new Date(Number(t)*1000).toISOString().replace('T',' ').slice(0,19)+' UTC':'—';
 const node=(tag,attrs={},text='')=>{const n=document.createElementNS(ns,tag);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,String(v));n.textContent=String(text);return n;};
 const active=()=>visible&&!document.hidden&&!$('workspace').hidden;
 function clear(card,message){$(card.id+'-chart').replaceChildren(node('text',{x:25,y:190,fill:'#b5c5d5','font-size':18},message));$(card.id+'-events').replaceChildren();$(card.id+'-source').textContent='';$(card.id+'-modules').textContent='';$(card.id+'-empty').textContent='';$(card.id+'-notes').textContent='';}
 function status(card,message,warning=true){$(card.id+'-status').textContent=message;$(card.id+'-status').dataset.tone=warning?'warning':'info';}
 function render(card,data){
  const id=card.id,svg=$(id+'-chart'),moduleKeys=id==='patterns'?['talib']:['smc','trendln','geometry'];
  clear(card,'Нет подтверждённых закрытых свечей');
  status(card,(states[data.status]??'Неизвестное состояние')+(data.reason?' · '+String(data.reason):''),data.status!=='READY');
  $(id+'-source').textContent=String(data.symbol??'')+' · '+data.timeframe+' · MT5 · снимок '+stamp(data.as_of)+'\nЗакрыто по '+stamp(data.closed_through)+' · рассчитано '+stamp(data.generated_at);
  $(id+'-modules').textContent=moduleKeys.map(k=>k+': '+(states[data.modules?.[k]?.status]??'Недоступно')+(data.modules?.[k]?.reason?' · '+data.modules[k].reason:'')).join('\n');
  $(id+'-notes').textContent=(Array.isArray(data.notes)?data.notes:[]).slice(0,12).map(note=>String(note).slice(0,500)).join(' · ');
  // Unverified or unknown snapshots must not present apparently authoritative UTC candles.
  if(!['READY','STALE'].includes(data.status))return;
  if(data.timestamp_semantics!=='VERIFIED'){status(card,'Время не подтверждено · свечи скрыты');return;}
  const allBars=data.bars,seconds={M1:60,M15:900,M30:1800,H1:3600}[data.timeframe];
  const validSnapshotTime=t=>number(t)&&Number(t)>0&&Number(t)<8640000000000;
  const validTime=t=>validSnapshotTime(t)&&Number.isInteger(Number(t));
  const invalid=!Array.isArray(allBars)||allBars.length>10000||!validSnapshotTime(data.as_of)||!seconds||allBars.some((b,i)=>!b||!validTime(b.utc_time)||Number(b.utc_time)+seconds>Number(data.as_of)||['open','high','low','close'].some(k=>!number(b[k])||Number(b[k])<=0)||Number(b.low)>Math.min(Number(b.open),Number(b.close))||Number(b.high)<Math.max(Number(b.open),Number(b.close))||(i>0&&Number(b.utc_time)<=Number(allBars[i-1].utc_time)));
  if(invalid){status(card,'Некорректный снимок: свечи повреждены, не закрыты или время недопустимо');return;}
  const bars=allBars.slice(-100),offset=allBars.length-bars.length;
  if(!bars.length)return;
  svg.replaceChildren();
  const min=Math.min(...bars.map(b=>Number(b.low))),max=Math.max(...bars.map(b=>Number(b.high))),pad=Math.max((max-min)*.08,Math.abs(max)*.00005,.00001),lo=min-pad,hi=max+pad;
  const left=12,right=710,top=22,bottom=355,step=(right-left)/bars.length,y=p=>bottom-(Number(p)-lo)/(hi-lo)*(bottom-top),x=i=>left+(i+.5)*step;
  // Overlay endpoints must refer to actual closed bars. Map full-history indices
  // into the visible window, then clip SVG geometry. Never invent bars in gaps.
  const barIndices=new Map(allBars.map((b,i)=>[Number(b.utc_time),i]));
  function tx(t){if(!number(t)||!barIndices.has(Number(t)))return null;return x(barIndices.get(Number(t))-offset);}
  for(let i=0;i<5;i++){const price=lo+(hi-lo)*i/4,yy=y(price);svg.append(node('line',{x1:left,x2:right,y1:yy,y2:yy,stroke:'#293545'}),node('text',{x:718,y:yy+4,fill:'#a4b9cc','font-size':12},price.toFixed(2)));}
  bars.forEach((b,i)=>{const color=Number(b.close)>=Number(b.open)?'#26a69a':'#ef5350',xx=x(i),body=node('rect',{x:xx-step*.32,y:Math.min(y(b.open),y(b.close)),width:Math.max(1,step*.64),height:Math.max(1,Math.abs(y(b.open)-y(b.close))),fill:color});body.append(node('title',{},stamp(b.utc_time)+' · O '+b.open+' H '+b.high+' L '+b.low+' C '+b.close));svg.append(node('line',{x1:xx,x2:xx,y1:y(b.high),y2:y(b.low),stroke:color}),body);});
  for(const index of [...new Set([0,Math.floor((bars.length-1)/2),bars.length-1])])svg.append(node('text',{x:x(index),y:379,fill:'#a4b9cc','font-size':11,'text-anchor':index===0?'start':index===bars.length-1?'end':'middle'},stamp(bars[index].utc_time).slice(5,16)));
  svg.append(node('text',{x:738,y:379,fill:'#a4b9cc','font-size':11},'UTC'));
  const groups=id==='patterns'?[['candlestick','talib']]:[['structure','smc'],['trendlines','trendln'],['figures','geometry']];let count=0;
  for(const [key,module]of groups){if(data.modules?.[module]?.status!=='READY')continue;for(const o of (Array.isArray(data.overlays?.[key])?data.overlays[key]:[]).slice(-150)){
   const points=Array.isArray(o.points)?o.points:[],xx=tx(o.start_time??points[0]?.time??o.formed_at),end=tx(o.end_time??points.at(-1)?.time??o.start_time??o.formed_at);
   const figure=key==='figures',liquidity=o.kind==='liquidity_zone',sweep=o.kind==='liquidity_sweep';
   const color=figure?'#d5a4ff':liquidity||sweep?'#67ddeb':['bullish','BUY','BULLISH',1].includes(o.direction)?'#5dd6b0':['bearish','SELL','BEARISH',-1].includes(o.direction)?'#ff8585':'#e6be70';
   let shape=null;
   if(points.length>=2&&points.every(p=>tx(p.time)!==null&&number(p.price)))shape=node('polyline',{points:points.map(p=>tx(p.time)+','+y(p.price)).join(' '),fill:'none',stroke:color,'stroke-width':2});
   else if(xx!==null&&end!==null&&number(o.top)&&number(o.bottom))shape=node('rect',{x:Math.min(xx,end),y:y(Math.max(o.top,o.bottom)),width:Math.max(3,Math.abs(end-xx)),height:Math.max(2,Math.abs(y(o.top)-y(o.bottom))),fill:color,'fill-opacity':.12,stroke:color});
   else if(sweep&&xx!==null&&number(o.price)){const yy=y(o.price);shape=node('polygon',{points:[[xx,yy-7],[xx+7,yy],[xx,yy+7],[xx-7,yy]].map(p=>p.join(',')).join(' '),fill:'#10151c',stroke:color,'stroke-width':2});}
   else if(xx!==null&&number(o.price)){shape=end!==null&&end!==xx?node('line',{x1:xx,x2:end,y1:y(o.price),y2:y(o.price),stroke:color,'stroke-width':2}):node('circle',{cx:xx,cy:y(o.price),r:5,fill:color});}
   if(shape){shape.setAttribute('data-analysis-kind',String(o.kind??key));if(figure||liquidity)shape.setAttribute('stroke-dasharray',figure?'8 4':'3 4');if(figure)shape.setAttribute('stroke-width',3);}
   const coordinates=points.length>=2?points.map(p=>tx(p.time)):[xx,end??xx];
   if(!shape||coordinates.some(v=>v===null)||Math.max(...coordinates)<left||Math.min(...coordinates)>right)continue;
   const label=String(o.label??o.kind??module),detail=(figure?'Геометрия · ':liquidity?'Зона OHLC · ':sweep?'Пересечение OHLC · ':'')+label+' · '+String(o.status??'')+' · сформировано '+stamp(o.formed_at)+' · обнаружено в снимке '+stamp(o.known_at)+(liquidity?' · '+String(o.zone_state??''):'');
   const clipped=node('svg',{x:left,y:top,width:right-left,height:bottom-top,viewBox:left+' '+top+' '+(right-left)+' '+(bottom-top),overflow:'hidden'});shape.append(node('title',{},detail));clipped.append(shape);svg.append(clipped);
   if(xx!==null&&xx>=left&&xx<=right&&number(o.price)&&y(o.price)>=top&&y(o.price)<=bottom)svg.append(node('text',{x:Math.min(xx+6,right-100),y:Math.max(top+12,y(o.price)-8),fill:color,'font-size':11},label.slice(0,28)));
   const li=document.createElement('li');li.textContent=detail;$(id+'-events').append(li);count++;
  }}
  const ready=moduleKeys.every(k=>data.modules?.[k]?.status==='READY');
  $(id+'-empty').textContent=count?'Отображено событий: '+count:ready?'На показанных свечах подтверждённых признаков нет.':'Признаки недоступны или история ещё накапливается. Отсутствие меток не означает отсутствие паттернов.';
 }
 async function load(card){
  if(!active())return;
  const timeframe=$(card.id+'-timeframe').value;if(!periods.includes(timeframe))return;
  const seq=++card.seq;card.controller?.abort();const controller=new AbortController();card.controller=controller;
  const timeout=setTimeout(()=>controller.abort(),8000);
  status(card,'Загрузка '+timeframe+'…',false);
  try{const r=await fetch('/api/analysis?timeframe='+timeframe,{method:'GET',headers:getHeaders(),signal:controller.signal,cache:'no-store'});if(!r.ok)throw Error('Анализ недоступен: HTTP '+r.status);const data=await r.json();
   if(seq!==card.seq||!active()||$(card.id+'-timeframe').value!==timeframe)return;
   if(data.schema!=='analysis_v1'||data.timeframe!==timeframe||data.source!=='MT5')throw Error('Несовместимый снимок анализа');render(card,data);
  }catch(e){if(seq!==card.seq||!active())return;clear(card,'Данные анализа недоступны');status(card,e.name==='AbortError'?'Истекло время ожидания анализа':e.message);}
  finally{clearTimeout(timeout);if(seq===card.seq)card.controller=null;}
 }
 function stop(){clearInterval(timer);timer=null;for(const card of cards){card.seq++;card.controller?.abort();card.controller=null;}}
 function start(){stop();if(!active())return;cards.forEach(load);timer=setInterval(()=>{if(!active())return;for(const card of cards)if(!card.controller)load(card);},15000);}
 for(const card of cards)$(card.id+'-timeframe').onchange=()=>{card.seq++;card.controller?.abort();clear(card,'Ожидание '+$(card.id+'-timeframe').value);load(card);};
 document.addEventListener('visibilitychange',()=>{if(document.hidden)stop();else if(visible)start();});
 return{setVisible(value){visible=value;if(visible)start();else stop();}};
}
