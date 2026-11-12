'use strict';
// Pure read-only chart helpers. No broker, action or order APIs.
const analysisPeriods={M1:60,M15:900,M30:1800,H1:3600};
const analysisNumber=v=>typeof v!=='boolean'&&v!==null&&v!==undefined&&v!==''&&Number.isFinite(Number(v));
// Numeric broker coordinates are formatted as a calendar only, never converted to UTC.
const analysisRaw=data=>data?.timestamp_semantics==='BROKER_RAW'&&data?.time_domain==='BROKER_RAW';
const analysisDomain=data=>analysisRaw(data)?'BROKER_RAW':data?.timestamp_semantics==='VERIFIED'&&(!data.time_domain||data.time_domain==='UTC')?'UTC':null;
const analysisBarTime=(bar,data)=>analysisRaw(data)?bar?.broker_time??bar?.raw_time:bar?.utc_time;
const analysisTimeLabel=data=>analysisRaw(data)?'время MT5':'UTC';
function analysisStamp(value,data){
 if(!analysisDomain(data)||!analysisNumber(value)||Number(value)<=0||Number(value)>=8640000000000)return '—';
 return new Date(Number(value)*1000).toISOString().replace('T',' ').slice(0,19)+' '+analysisTimeLabel(data);
}
function analysisSnapshotError(data){
 const number=analysisNumber,time=t=>number(t)&&Number(t)>0&&Number(t)<8640000000000;
 if(data?.schema!=='analysis_v1'||data.source!=='MT5'||!analysisPeriods[data.timeframe])return 'Несовместимый снимок анализа';
 if(!analysisDomain(data))return 'Время не подтверждено · свечи скрыты';
 const bars=data.bars,seconds=analysisPeriods[data.timeframe];
 if(!Array.isArray(bars)||bars.length>10000||!time(data.as_of)||bars.some((b,i)=>{const t=analysisBarTime(b,data);return !b||!time(t)||!Number.isInteger(Number(t))||Number(t)+seconds>Number(data.as_of)||(analysisRaw(data)&&(b.closed!==true||b.time_domain!=='BROKER_RAW'||b.utc_time!=null||(b.raw_time!=null&&Number(b.raw_time)!==Number(t))))||['open','high','low','close'].some(k=>!number(b[k])||Number(b[k])<=0)||Number(b.low)>Math.min(Number(b.open),Number(b.close))||Number(b.high)<Math.max(Number(b.open),Number(b.close))||(i>0&&Number(t)<=Number(analysisBarTime(bars[i-1],data)));}))return 'Некорректный снимок: свечи повреждены, не закрыты или время недопустимо';
 return '';
}
function drawAnalysisOverlays({document,svg,data,groups,tx,y,left,right,top,bottom,figureLabels=false,opacity=.12,zoneColor='#67ddeb',onEvent=()=>{}}){
 const number=analysisNumber,node=(tag,attrs={},text='')=>{const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,String(v));n.textContent=String(text);return n;};
 const clip=node('svg',{x:left,y:top,width:right-left,height:bottom-top,viewBox:left+' '+top+' '+(right-left)+' '+(bottom-top),overflow:'hidden','pointer-events':'none','data-analysis-layer':'true'});
 let count=0;
 for(const[key,module]of groups){
  if(data.modules?.[module]?.status!=='READY')continue;
  for(const o of (Array.isArray(data.overlays?.[key])?data.overlays[key]:[]).slice(-150)){
   if(!o||typeof o!=='object')continue;
   const points=Array.isArray(o.points)?o.points:[];if(points.length>40)continue;
   const xx=tx(o.start_time??points[0]?.time??o.formed_at),end=tx(o.end_time??points.at(-1)?.time??o.start_time??o.formed_at);
   const figure=key==='figures',liquidity=o.kind==='liquidity_zone',sweep=o.kind==='liquidity_sweep';
   const color=figure?'#d5a4ff':liquidity||sweep?zoneColor:['bullish','BUY','BULLISH',1].includes(o.direction)?'#5dd6b0':['bearish','SELL','BEARISH',-1].includes(o.direction)?'#ff8585':'#e6be70';
   let shape=null;
   if(points.length>=2){if(points.some(p=>!p||tx(p.time)===null||!number(p.price)||Number(p.price)<=0||!Number.isFinite(y(p.price))))continue;shape=node('polyline',{points:points.map(p=>tx(p.time)+','+y(p.price)).join(' '),fill:'none',stroke:color,'stroke-width':2});}
   else if(xx!==null&&end!==null&&number(o.top)&&number(o.bottom)&&Number(o.top)>=Number(o.bottom)&&Number(o.bottom)>0)shape=node('rect',{x:Math.min(xx,end),y:y(o.top),width:Math.max(3,Math.abs(end-xx)),height:Math.max(2,Math.abs(y(o.top)-y(o.bottom))),fill:color,'fill-opacity':opacity,stroke:color});
   else if(xx!==null&&number(o.price)&&Number(o.price)>0){const yy=y(o.price);if(sweep)shape=node('polygon',{points:[[xx,yy-7],[xx+7,yy],[xx,yy+7],[xx-7,yy]].map(p=>p.join(',')).join(' '),fill:'#10151c',stroke:color,'stroke-width':2});else shape=end!==null&&end!==xx?node('line',{x1:xx,x2:end,y1:yy,y2:yy,stroke:color,'stroke-width':2}):node('circle',{cx:xx,cy:yy,r:5,fill:color});}
   const coordinates=points.length>=2?points.map(p=>tx(p.time)):[xx,end??xx];
   if(!shape||coordinates.some(v=>v===null||!Number.isFinite(v))||Math.max(...coordinates)<left||Math.min(...coordinates)>right)continue;
   shape.setAttribute('data-analysis-kind',String(o.kind??key));if(figure||liquidity)shape.setAttribute('stroke-dasharray',figure?'8 4':'3 4');if(figure)shape.setAttribute('stroke-width',3);
   clip.append(shape);
   // Labels identify detected patterns/figures, never standing explanations or legends.
   if(figureLabels&&(figure||key==='candlestick')){const candidates=figure?points:[{time:o.start_time??o.formed_at,price:o.price}];const anchor=candidates.filter(p=>tx(p.time)>=left&&tx(p.time)<=right&&y(p.price)>=top&&y(p.price)<=bottom).at(-1);if(anchor)clip.append(node('text',{x:Math.min(Math.max(left+4,tx(anchor.time)),right-4),y:Math.min(bottom-4,Math.max(top+14,y(anchor.price)-8)),fill:color,'font-size':13,'text-anchor':tx(anchor.time)<(left+right)/2?'start':'end','data-figure-label':figure?'true':'false','data-pattern-label':key==='candlestick'?'true':'false'},String(o.label??o.kind).slice(0,45)));}
   onEvent(o,key);count++;
  }
 }
 svg.append(clip);return count;
}
// Independent, read-only analysis transport. Never consumes trading chart candles.
function analysisDiagnosticFacts(data){const d=data?.diagnostics;return {analysis_status:data?.status,timestamp_semantics:data?.timestamp_semantics,time_domain:data?.time_domain,generated_at:data?.generated_at,as_of:data?.as_of,closed_through:data?.closed_through,bar_count:Array.isArray(data?.bars)?data.bars.length:undefined,analysis_reason:d?.reason??data?.reason,last_failure_reason:d?.last_failure_reason,worker_error:d?.error_code,worker_state:d?.supervisor_state,worker_failures:d?.failures,proof_status:d?.proof_status,proof_age_seconds:d?.proof_age_seconds,expected_closed_through:d?.expected_closed_through,history_ms:d?.history_ms,calculation_ms:d?.calculation_ms,ipc_ms:d?.ipc_ms,cache_age_seconds:d?.cache_age_seconds,cache_reused:d?.cache_reused};}
function analysisDiagnosticReport(diagnostics,value){try{diagnostics?.record(value);}catch{}}
function analysisDiagnosticNow(diagnostics){try{return diagnostics?.now()??0;}catch{return 0;}}
function createAnalysisUI({document,fetch,getHeaders,diagnostics=null}){
 const $=id=>document.getElementById(id),ns='http://www.w3.org/2000/svg',periods=['M1','M15','M30','H1'];
 const cards=['patterns','structure'].map(id=>({id,seq:0,controller:null,data:null,count:100,anchor:null,priceZoom:1,pricePan:0,pointers:new Map(),gesture:null}));
 const states={READY:'Готово',STALE:'Устаревшие данные',DISCONNECTED:'Нет связи с MT5',TIME_UNVERIFIED:'Время не подтверждено',WARMING_UP:'Недостаточно истории',UNAVAILABLE:'Недоступно',ERROR:'Ошибка',LICENSE_PENDING:'Лицензия не согласована'};
 let visible=false,timer=null,expanded='structure',liveFeed=null,feedAvailable=false;
 const number=v=>typeof v!=='boolean'&&v!==null&&v!==undefined&&v!==''&&Number.isFinite(Number(v));
 const node=(tag,attrs={},text='')=>{const n=document.createElementNS(ns,tag);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,String(v));n.textContent=String(text);return n;};
 const active=()=>visible&&!document.hidden&&!$('workspace').hidden;
 function clear(card){card.data=null;$(card.id+'-chart').replaceChildren();$(card.id+'-events').replaceChildren();$(card.id+'-source').textContent='';$(card.id+'-modules').textContent='';$(card.id+'-empty').textContent='';$(card.id+'-notes').textContent='';}
 function status(card,message,warning=true){$(card.id+'-status').hidden=!warning;$(card.id+'-status').textContent=message;$(card.id+'-status').dataset.tone=warning?'warning':'info';}
 function render(card,data){
  const id=card.id,svg=$(id+'-chart'),moduleKeys=id==='patterns'?['talib']:['geometry'],stamp=t=>analysisStamp(t,data),barTime=b=>analysisBarTime(b,data);
  clear(card,'Нет подтверждённых закрытых свечей');
  card.data=data;
  status(card,(states[data.status]??'Неизвестное состояние')+(data.reason?' · '+String(data.reason):''),data.status!=='READY');
  $(id+'-source').textContent=String(data.symbol??'')+' · '+data.timeframe+' · MT5 · снимок '+stamp(data.as_of)+'\nЗакрыто по '+stamp(data.closed_through)+' · рассчитано '+stamp(data.generated_at);
  $(id+'-modules').textContent=moduleKeys.map(k=>k+': '+(states[data.modules?.[k]?.status]??'Недоступно')+(data.modules?.[k]?.reason?' · '+data.modules[k].reason:'')).join('\n');
  $(id+'-notes').textContent=(Array.isArray(data.notes)?data.notes:[]).slice(0,12).map(note=>String(note).slice(0,500)).join(' · ');
  // Only an explicit UTC or BROKER_RAW domain can provide time coordinates.
  if(!['READY','STALE'].includes(data.status))return;
  const invalid=analysisSnapshotError(data);if(invalid){status(card,invalid);return;}
  const allBars=data.bars;
  let end=allBars.length;
  if(card.anchor!==null){const found=allBars.findIndex(b=>Number(barTime(b))>card.anchor);end=found<0?allBars.length:found;}
  end=Math.max(Math.min(allBars.length,Math.min(card.count,allBars.length)),end);
  const offset=Math.max(0,end-card.count),bars=allBars.slice(offset,end);
  const matchingFeed=feedAvailable&&liveFeed?.connected===true&&liveFeed.source==='MT5'&&liveFeed.status===(analysisRaw(liveFeed)?'OBSERVED':'FRESH')&&liveFeed.symbol===data.symbol&&liveFeed.instance===data.feed_instance&&analysisDomain(liveFeed)===analysisDomain(data);
  const q=matchingFeed?liveFeed.quote:null,quote=q&&number(q.bid)&&number(q.ask)&&Number(q.bid)>0&&Number(q.ask)>=Number(q.bid)?q:null;
  if(!bars.length){status(card,'Нет подтверждённых закрытых свечей');return;}
  svg.replaceChildren();
  const prices=bars.flatMap(b=>[Number(b.low),Number(b.high)]);if(quote&&card.anchor===null)prices.push(Number(quote.bid),Number(quote.ask));
  const min=Math.min(...prices),max=Math.max(...prices),pad=Math.max((max-min)*.08,Math.abs(max)*.00005,.00001),span=(max-min+2*pad)/card.priceZoom,mid=(min+max)/2+card.pricePan,lo=mid-span/2,hi=mid+span/2;
  const width=Math.max(240,svg.clientWidth||800),height=Math.max(80,svg.clientHeight||400);svg.setAttribute('viewBox','0 0 '+width+' '+height);
  const left=8,right=width-104,top=16,bottom=height-25,step=(right-left)/(bars.length+5),y=p=>bottom-(Number(p)-lo)/(hi-lo)*(bottom-top),x=i=>left+(i+.5)*step;
  card.layout={left,right,top,bottom,span,step,end,allBars,barTime};svg.dataset.firstCandle=String(barTime(bars[0]));svg.dataset.visibleCount=String(bars.length);
  // Overlay endpoints must refer to actual closed bars. Map full-history indices
  // into the visible window, then clip SVG geometry. Never invent bars in gaps.
  const barIndices=new Map(allBars.map((b,i)=>[Number(barTime(b)),i]));
  function tx(t){if(!number(t)||!barIndices.has(Number(t)))return null;return x(barIndices.get(Number(t))-offset);}
  for(let i=0;i<5;i++){const price=lo+(hi-lo)*i/4,yy=y(price);svg.append(node('line',{x1:left,x2:right,y1:yy,y2:yy,stroke:'#293545'}),node('text',{x:right+8,y:yy+4,fill:'#a4b9cc','font-size':12},price.toFixed(2)));}
  bars.forEach((b,i)=>{const color=Number(b.close)>=Number(b.open)?'#26a69a':'#ef5350',xx=x(i),body=node('rect',{x:xx-step*.32,y:Math.min(y(b.open),y(b.close)),width:Math.max(1,step*.64),height:Math.max(1,Math.abs(y(b.open)-y(b.close))),fill:color});body.append(node('title',{},stamp(barTime(b))+' · O '+b.open+' H '+b.high+' L '+b.low+' C '+b.close));svg.append(node('line',{x1:xx,x2:xx,y1:y(b.high),y2:y(b.low),stroke:color}),body);});
  for(const index of [...new Set([0,Math.floor((bars.length-1)/2),bars.length-1])])svg.append(node('text',{x:x(index),y:height-6,fill:'#a4b9cc','font-size':11,'text-anchor':index===0?'start':index===bars.length-1?'end':'middle'},stamp(barTime(bars[index])).slice(5,16)));
  svg.append(node('text',{x:right+8,y:height-6,fill:'#a4b9cc','font-size':11},analysisTimeLabel(data)));
  const groups=id==='patterns'?[['candlestick','talib']]:[['figures','geometry']];
  const count=drawAnalysisOverlays({document,svg,data,groups,tx,y,left,right,top,bottom,figureLabels:false,onEvent(o,key){
   const detail=(key==='figures'?'Геометрия · ':'')+String(o.label??o.kind)+' · '+String(o.status??'')+' · сформировано '+stamp(o.formed_at)+' · обнаружено в снимке '+stamp(o.known_at);
   const li=document.createElement('li');li.textContent=detail;$(id+'-events').append(li);
  }});
  // Live Bid/Ask shares the existing feed, never creates candles or analysis evidence.
  if(quote){const badges=[];for(const [key,color]of [['ask','#ff5b63'],['bid','#24d99a']]){const price=Number(quote[key]),yy=y(price);if(yy<top||yy>bottom)continue;svg.append(node('line',{x1:left,x2:right,y1:yy,y2:yy,stroke:color,'stroke-width':1,'data-analysis-quote':key}));let labelY=Math.max(top+12,Math.min(bottom-3,yy));if(badges.some(p=>Math.abs(p-labelY)<15))labelY=labelY+16<=bottom-3?labelY+16:Math.max(top+12,labelY-16);badges.push(labelY);svg.append(node('text',{x:right+5,y:labelY,fill:color,'font-size':12,'data-analysis-price':key},key.toUpperCase()+' '+price.toFixed(2)));}}
  // The arrow shows direction only: its pixel length is NOT a price target.
  const feedClosed=(liveFeed?.candles??[]).filter(b=>analysisRaw(liveFeed)?b.closed===true:b.closed!==false&&Number(b.utc_time)+60<=Number(liveFeed?.timestamp));
  const newestFeed=feedClosed.at(-1),analysisCurrent=matchingFeed&&newestFeed&&Number(analysisBarTime(newestFeed,liveFeed))+60<Number(data.closed_through)+analysisPeriods[data.timeframe];
  const scenario=analysisCurrent&&typeof selectAnalysisScenario==='function'?selectAnalysisScenario(data,{group:id==='patterns'?'candlestick':'figures'}):null;
  if(scenario&&card.anchor===null){const xx=x(bars.length-1),yy=y(scenario.price),sign=scenario.direction==='bullish'?-1:1,ex=Math.min(right-3,xx+step*3),ey=Math.max(top+9,Math.min(bottom-9,yy+sign*32));if(yy>=top&&yy<=bottom&&ex>xx){const arrow=node('g',{'data-analysis-scenario':scenario.direction});arrow.append(node('title',{},'Возможное направление, не целевая цена и не гарантия'),node('line',{x1:xx,y1:yy,x2:ex,y2:ey,stroke:'#5ddc75','stroke-width':3,'stroke-dasharray':'5 4'}));const angle=Math.atan2(ey-yy,ex-xx);arrow.append(node('polyline',{points:[[ex-9*Math.cos(angle-.5),ey-9*Math.sin(angle-.5)],[ex,ey],[ex-9*Math.cos(angle+.5),ey-9*Math.sin(angle+.5)]].map(p=>p.join(',')).join(' '),stroke:'#5ddc75','stroke-width':3,fill:'none'}));svg.append(arrow);}}
  const ready=moduleKeys.every(k=>data.modules?.[k]?.status==='READY');
  if(data.status==='READY'&&!ready)status(card,moduleKeys.filter(k=>data.modules?.[k]?.status!=='READY').map(k=>k+': '+(states[data.modules?.[k]?.status]??'Недоступно')+(data.modules?.[k]?.reason?' · '+String(data.modules[k].reason):'')).join(' · '));
  $(id+'-empty').textContent=count?'Отображено событий: '+count:ready?'На показанных свечах подтверждённых признаков нет.':'Признаки недоступны или история ещё накапливается. Отсутствие меток не означает отсутствие паттернов.';
 }
 async function load(card){
  if(!active())return;
  const timeframe=$(card.id+'-timeframe').value;if(!periods.includes(timeframe))return;const lookback=Number($(card.id+'-lookback').value)||500;if(![50,100,200,300,500].includes(lookback))return;
  const seq=++card.seq;card.controller?.abort();const controller=new AbortController();card.controller=controller;
  const timeout=setTimeout(()=>controller.abort(),8000),started=analysisDiagnosticNow(diagnostics),source=card.id==='patterns'?'ANALYSIS_PATTERNS':'ANALYSIS_STRUCTURE';
  let stage='FETCH',httpStatus,data,mark=started;const timings={};
  const finishStage=key=>{const end=analysisDiagnosticNow(diagnostics);timings[key]=Math.max(0,end-mark);mark=end;};
  const report=(outcome,reason,error)=>analysisDiagnosticReport(diagnostics,{source,stage,outcome,reason,error_type:error?.name,timeframe,http_status:httpStatus,request_ms:analysisDiagnosticNow(diagnostics)-started,...timings,...analysisDiagnosticFacts(data)});
  if(!card.data)status(card,'Загрузка '+timeframe+'…');
  try{const r=await fetch('/api/analysis?timeframe='+timeframe+(lookback===500?'':'&bars='+lookback),{method:'GET',headers:getHeaders(),signal:controller.signal,cache:'no-store'});httpStatus=r.status;finishStage('fetch_ms');if(!r.ok)throw Error('Анализ недоступен: HTTP '+r.status);
   stage='PARSE';data=await r.json();finishStage('parse_ms');
   if(seq!==card.seq||!active()||$(card.id+'-timeframe').value!==timeframe||(Number($(card.id+'-lookback').value)||500)!==lookback){report('CANCELLED','REQUEST_ABORTED');return;}
   stage='VALIDATE';if(data.schema!=='analysis_v1'||data.timeframe!==timeframe||data.source!=='MT5'||(data.requested_bars??500)!==lookback)throw Error('Несовместимый снимок анализа');finishStage('validation_ms');
   stage='RENDER';render(card,data);finishStage('render_ms');if(['READY','STALE'].includes(data.status)&&analysisSnapshotError(data)){stage='VALIDATE';report('ERROR','SNAPSHOT_INVALID');}else{stage='COMPLETE';report('OK');}
  }catch(e){
   const failedTiming={FETCH:'fetch_ms',PARSE:'parse_ms',VALIDATE:'validation_ms',RENDER:'render_ms'}[stage];if(failedTiming&&timings[failedTiming]===undefined)timings[failedTiming]=Math.max(0,analysisDiagnosticNow(diagnostics)-mark);
   if(seq!==card.seq||!active()){report('CANCELLED','REQUEST_ABORTED',e);return;}
   const timedOut=['FETCH','PARSE'].includes(stage)&&['AbortError','TimeoutError'].includes(e?.name);
   const reason=timedOut?'REQUEST_TIMEOUT':stage==='FETCH'?(httpStatus>=400?'HTTP_ERROR':'NETWORK_ERROR'):({PARSE:'JSON_INVALID',VALIDATE:'SNAPSHOT_INVALID',RENDER:'RENDER_ERROR'}[stage]??'OTHER');report('ERROR',reason,e);
   clear(card,'Данные анализа недоступны');status(card,e.name==='AbortError'?'Истекло время ожидания анализа':e.message);
  }
  finally{clearTimeout(timeout);if(seq===card.seq)card.controller=null;}
 }
 function stop(){clearInterval(timer);timer=null;for(const card of cards){card.seq++;card.controller?.abort();card.controller=null;}}
 function start(){stop();if(!active())return;for(const card of cards){card.data=null;clear(card);load(card);}timer=setInterval(()=>{if(!active())return;for(const card of cards)if(!card.controller)load(card);},15000);}
 function repaint(card){if(active()&&card.data)render(card,card.data);}
 function layout(){for(const c of cards){const open=expanded===c.id;$(c.id+'-card').dataset.collapsed=String(!open);$(c.id+'-expand').setAttribute('aria-expanded',String(open));$(c.id+'-expand').textContent=open?'⌃':'⌄';repaint(c);}}
 function reset(card){card.count=100;card.anchor=null;card.priceZoom=1;card.pricePan=0;card.pointers.clear();card.gesture=null;repaint(card);}
 function zoom(card,factor){card.count=Math.max(10,Math.min(500,Math.round(card.count*factor)));repaint(card);}
 for(const card of cards){
  const svg=$(card.id+'-chart');svg.setAttribute('tabindex','0');
  $(card.id+'-expand').onclick=()=>{expanded=expanded===card.id?null:card.id;layout();};
  $(card.id+'-zoom-in').onclick=()=>zoom(card,.75);$(card.id+'-zoom-out').onclick=()=>zoom(card,1.33);$(card.id+'-reset').onclick=()=>reset(card);
  const reload=()=>{card.seq++;card.controller?.abort();reset(card);card.data=null;clear(card);load(card);};
  $(card.id+'-timeframe').onchange=reload;$(card.id+'-lookback').onchange=reload;
  const point=e=>({x:e.clientX,y:e.clientY});
  function begin(){const pts=[...card.pointers.values()],l=card.layout;if(!l)return;card.gesture={points:pts,count:card.count,end:l.end,span:l.span,pan:card.pricePan,zoom:card.priceZoom,step:l.step,distance:pts.length===2?Math.hypot(pts[1].x-pts[0].x,pts[1].y-pts[0].y):0};}
  svg.addEventListener('pointerdown',e=>{if(!card.data||!card.layout||e.button>0||card.pointers.size>=2)return;e.preventDefault();card.pointers.set(e.pointerId,point(e));svg.setPointerCapture?.(e.pointerId);begin();});
  svg.addEventListener('pointermove',e=>{if(!card.pointers.has(e.pointerId)||!card.gesture)return;e.preventDefault();card.pointers.set(e.pointerId,point(e));const pts=[...card.pointers.values()],g=card.gesture,l=card.layout;if(pts.length===2&&g.distance>0){const dist=Math.hypot(pts[1].x-pts[0].x,pts[1].y-pts[0].y);if(dist>5){card.count=Math.max(10,Math.min(500,Math.round(g.count*g.distance/dist)));card.priceZoom=Math.max(.2,Math.min(20,g.zoom*dist/g.distance));}}else if(pts.length===1){const dx=pts[0].x-g.points[0].x,dy=pts[0].y-g.points[0].y,end=Math.max(Math.min(card.count,l.allBars.length),Math.min(l.allBars.length,Math.round(g.end-dx/g.step)));card.anchor=end===l.allBars.length?null:Number(l.barTime(l.allBars[end-1]));card.pricePan=g.pan+dy/Math.max(1,l.bottom-l.top)*g.span;}repaint(card);});
  const end=e=>{card.pointers.delete(e.pointerId);if(card.pointers.size)begin();else card.gesture=null;};
  svg.addEventListener('pointerup',end);svg.addEventListener('pointercancel',end);svg.addEventListener('lostpointercapture',end);
  svg.addEventListener('wheel',e=>{e.preventDefault();if(e.shiftKey){card.priceZoom=Math.max(.2,Math.min(20,card.priceZoom*(e.deltaY<0?1.2:1/1.2)));repaint(card);}else zoom(card,e.deltaY<0?.85:1.18);},{passive:false});
  svg.addEventListener('keydown',e=>{if(['+','=','-','Home'].includes(e.key)){e.preventDefault();if(e.key==='Home')reset(card);else zoom(card,e.key==='-'?1.33:.75);}});
 }
 layout();
 document.addEventListener('visibilitychange',()=>{if(document.hidden)stop();else if(visible)start();});
 if(typeof ResizeObserver!=='undefined'){const observer=new ResizeObserver(()=>{if(active())for(const card of cards)if(card.data)render(card,card.data);});for(const card of cards)observer.observe($(card.id+'-chart'));}
 return{setVisible(value){visible=value;if(visible)start();else{stop();for(const c of cards){c.pointers.clear();c.gesture=null;}}},updateFeed(value,isAvailable){liveFeed=value;feedAvailable=isAvailable;for(const c of cards)repaint(c);}};
}

// M1 overlays are display-only. They never replace the trading feed or enter its price domain.
function createMainAnalysisUI({document,fetch,getHeaders,getPreferences,onChange,diagnostics=null}){
 const $=id=>document.getElementById(id),number=analysisNumber;
 let visible=true,feed=null,fresh=false,data=null,received=0,seq=0,controller=null,timer=null,context='';
 const active=()=>visible&&!document.hidden&&!$('workspace').hidden&&(getPreferences().liquidity||getPreferences().mainTrend);
 const status=text=>{if($('main-analysis-status').textContent!==text)$('main-analysis-status').textContent=text;$('main-analysis-status').hidden=!text;};
 function reason(){
  if(feed&&!analysisDomain(feed))return 'Анализ: время не подтверждено';
  if(!fresh||!feed||feed.status!==(analysisRaw(feed)?'OBSERVED':'FRESH')||feed.connected!==true)return 'Анализ: нет свежего снимка MT5';
  if(feed.source!=='MT5'||!feed.instance||!feed.symbol)return 'Анализ: источник не подтверждён';
  return '';
 }
 function stop(){seq++;controller?.abort();controller=null;clearInterval(timer);timer=null;data=null;received=0;$('main-analysis-source').textContent='';}
 function refresh(){
  const next=[visible,document.hidden,$('workspace').hidden,getPreferences().liquidity,getPreferences().mainTrend,feed?.instance,feed?.symbol,feed?.connected,feed?.source,fresh,feed?.status,feed?.timestamp_semantics,feed?.time_domain].join('|');
  if(next===context)return;context=next;stop();
  if(!active()){status('');onChange();return;}
  if(reason()){status(reason());onChange();return;}
  load();timer=setInterval(()=>{if(active()&&!controller)load();},15000);onChange();
 }
 async function load(){
  if(!active()||reason())return;
  const mine=++seq;controller=new AbortController();const request=controller,timeout=setTimeout(()=>request.abort(),8000),started=analysisDiagnosticNow(diagnostics);
  let stage='FETCH',httpStatus,value,diagnosticReason,mark=started;const timings={};
  const finishStage=key=>{const end=analysisDiagnosticNow(diagnostics);timings[key]=Math.max(0,end-mark);mark=end;};
  const report=(outcome,code,error)=>analysisDiagnosticReport(diagnostics,{source:'ANALYSIS_MAIN',stage,outcome,reason:code,error_type:error?.name,timeframe:'M1',http_status:httpStatus,request_ms:analysisDiagnosticNow(diagnostics)-started,...timings,...analysisDiagnosticFacts(value)});
  if(!data)status('Анализ: загрузка M1…');
  try{
   const r=await fetch('/api/analysis?timeframe=M1',{method:'GET',headers:getHeaders(),signal:request.signal,cache:'no-store'});httpStatus=r.status;finishStage('fetch_ms');
   if(!r.ok)throw Error('Анализ: HTTP '+r.status);
   stage='PARSE';value=await r.json();finishStage('parse_ms');if(mine!==seq||!active()){report('CANCELLED','REQUEST_ABORTED');return;}
   stage='VALIDATE';const state={TIME_UNVERIFIED:'время не подтверждено',STALE:'данные устарели',WARMING_UP:'ожидание закрытых свечей',UNAVAILABLE:'недоступно',DISCONNECTED:'нет связи с MT5',ERROR:'ошибка'};
   diagnosticReason='ANALYSIS_NOT_READY';if(value.status!=='READY')throw Error('Анализ: '+(state[value.status]??'неизвестное состояние')+(value.reason?' · '+String(value.reason):''));
   diagnosticReason='SNAPSHOT_INVALID';const invalid=analysisSnapshotError(value);if(invalid||value.timeframe!=='M1')throw Error(invalid||'Анализ: неверный период');
   const end=Number(analysisBarTime(value.bars.at(-1),value))+60;
   if(!number(value.closed_through)||!Number.isInteger(Number(value.closed_through))||Number(value.closed_through)!==end||Number(value.closed_through)>Number(value.as_of))throw Error('Анализ: некорректное время закрытых свечей');
   if(value.symbol!==feed.symbol||value.feed_instance!==feed.instance||analysisDomain(value)!==analysisDomain(feed))throw Error('Анализ: источник изменился, слои скрыты');
   finishStage('validation_ms');data=value;received=Date.now();stage='COMPLETE';report('OK');
  }catch(e){
   const failedTiming={FETCH:'fetch_ms',PARSE:'parse_ms',VALIDATE:'validation_ms'}[stage];if(failedTiming&&timings[failedTiming]===undefined)timings[failedTiming]=Math.max(0,analysisDiagnosticNow(diagnostics)-mark);
   if(mine!==seq||!active()){report('CANCELLED','REQUEST_ABORTED',e);return;}
   const timedOut=['FETCH','PARSE'].includes(stage)&&['AbortError','TimeoutError'].includes(e?.name);
   const code=timedOut?'REQUEST_TIMEOUT':stage==='FETCH'?(httpStatus>=400?'HTTP_ERROR':'NETWORK_ERROR'):stage==='PARSE'?'JSON_INVALID':diagnosticReason??'OTHER';report('ERROR',code,e);
   data=null;$('main-analysis-source').textContent='';status(e.name==='AbortError'?'Анализ: истекло время ожидания':String(e.message));
  }
  finally{clearTimeout(timeout);if(mine===seq){controller=null;onChange();}}
 }
 function draw({svg,candles,x,y,left,right,top,bottom}){
  if(!active())return;
  const blocked=reason();if(blocked){status(blocked);return;}if(!data)return;
  if(!analysisRaw(data)&&(Date.now()-received>20000||Date.now()<received)){status('Анализ: снимок устарел, слои скрыты');return;}
  if(analysisDomain(data)!==analysisDomain(feed)){status('Анализ: источник изменился, слои скрыты');return;}
  // In raw mode the observed native bars, not wall time or quote age, define closure.
  const raw=analysisRaw(feed),closedBars=raw?(feed.candles??[]).filter(b=>b.closed===true):[];
  const expected=raw?(closedBars.length?Number(analysisBarTime(closedBars.at(-1),feed))+60:NaN):Math.floor((Number(feed.timestamp)+Number(feed.age_seconds??0))/60)*60;
  if((!raw&&(!number(feed.timestamp)||!number(feed.age_seconds??0)))||!Number.isFinite(expected)||!number(data.closed_through)||Number(data.closed_through)<expected){status('Анализ: ожидается расчёт последней закрытой M1');return;}
  const history=data.bars,indices=new Map(history.map((b,i)=>[Number(analysisBarTime(b,data)),i]));
  let anchor=null;
  for(let i=0;i<candles.length;i++){
   const candle=candles[i];if(raw&&candle.closed!==true)continue;const index=indices.get(Number(analysisBarTime(candle,feed)));if(index===undefined)continue;
   if(['open','high','low','close'].some(k=>Number(candle[k])!==Number(history[index][k]))){status('Анализ: свечи источников различаются, слои скрыты');return;}
   if(anchor===null)anchor={index,visible:i};
   else if(index-anchor.index!==i-anchor.visible){status('Анализ: история изменилась, слои скрыты');return;}
  }
  if(anchor===null){status('Анализ: нет совпадающих закрытых свечей');return;}
  const tx=t=>number(t)&&indices.has(Number(t))?x(indices.get(Number(t))-anchor.index+anchor.visible):null;
  const prefs=getPreferences(),groups=[];if(prefs.liquidity)groups.push(['structure','smc']);if(prefs.mainTrend)groups.push(['trendlines','trendln']);
  const problems=groups.flatMap(([,name])=>{const module=data.modules?.[name];return module?.status==='READY'?[]:[name+': '+String(module?.reason??module?.status??'недоступно')];});
  // A READY SMC module may still have failed components. Surface them outside the canvas.
  if(prefs.liquidity)for(const[name,value]of Object.entries(data.modules?.smc?.components??{}))if(value?.status!=='READY')problems.push('SMC '+name+': '+String(value?.reason??value?.status??'недоступно'));
  status(problems.length?'Анализ: '+problems.join(' · '):'');
  $('main-analysis-source').textContent='M1 · '+data.symbol+' · закрыто по '+analysisStamp(data.closed_through,data);
  drawAnalysisOverlays({document,svg,data,groups,tx,y,left,right,top,bottom,opacity:prefs.opacity/100,zoneColor:prefs.colors.zone});
 }
 document.addEventListener('visibilitychange',refresh);
 return{draw(args){try{draw(args);}catch(e){analysisDiagnosticReport(diagnostics,{source:'ANALYSIS_MAIN',stage:'DISPLAY',outcome:'ERROR',reason:'RENDER_ERROR',error_type:e?.name,timeframe:'M1',...analysisDiagnosticFacts(data)});data=null;$('main-analysis-source').textContent='';status('Анализ: ошибка отображения, слои скрыты');}},refresh,setVisible(value){visible=value;refresh();},updateFeed(value,isFresh){feed=value;fresh=isFresh;refresh();}};
}
