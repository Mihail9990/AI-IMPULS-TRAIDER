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
function drawAnalysisOverlays({document,svg,data,groups,tx,y,left,right,top,bottom,opacity=.12,zoneColor='#67ddeb',onEvent=()=>{}}){
 const number=analysisNumber,node=(tag,attrs={},text='')=>{const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,String(v));n.textContent=String(text);return n;};
 const clip=node('svg',{x:left,y:top,width:right-left,height:bottom-top,viewBox:left+' '+top+' '+(right-left)+' '+(bottom-top),overflow:'hidden','pointer-events':'none','data-analysis-layer':'true'});
 let count=0;
 for(const[key,module]of groups){
  if(key!=='structure'||module!=='smc'||data.modules?.smc?.status!=='READY')continue;
  for(const o of (Array.isArray(data.overlays?.[key])?data.overlays[key]:[]).slice(-150)){
   if(!o||typeof o!=='object')continue;
   const points=Array.isArray(o.points)?o.points:[];if(points.length>40)continue;
   const xx=tx(o.start_time??points[0]?.time??o.formed_at),end=tx(o.end_time??points.at(-1)?.time??o.start_time??o.formed_at);
   const liquidity=o.kind==='liquidity_zone',sweep=o.kind==='liquidity_sweep';
   const color=liquidity||sweep?zoneColor:['bullish','BUY','BULLISH',1].includes(o.direction)?'#5dd6b0':['bearish','SELL','BEARISH',-1].includes(o.direction)?'#ff8585':'#e6be70';
   let shape=null;
   if(points.length>=2){if(points.some(p=>!p||tx(p.time)===null||!number(p.price)||Number(p.price)<=0||!Number.isFinite(y(p.price))))continue;shape=node('polyline',{points:points.map(p=>tx(p.time)+','+y(p.price)).join(' '),fill:'none',stroke:color,'stroke-width':2});}
   else if(xx!==null&&end!==null&&number(o.top)&&number(o.bottom)&&Number(o.top)>=Number(o.bottom)&&Number(o.bottom)>0)shape=node('rect',{x:Math.min(xx,end),y:y(o.top),width:Math.max(3,Math.abs(end-xx)),height:Math.max(2,Math.abs(y(o.top)-y(o.bottom))),fill:color,'fill-opacity':opacity,stroke:color});
   else if(xx!==null&&number(o.price)&&Number(o.price)>0){const yy=y(o.price);if(sweep)shape=node('polygon',{points:[[xx,yy-7],[xx+7,yy],[xx,yy+7],[xx-7,yy]].map(p=>p.join(',')).join(' '),fill:'#10151c',stroke:color,'stroke-width':2});else shape=end!==null&&end!==xx?node('line',{x1:xx,x2:end,y1:yy,y2:yy,stroke:color,'stroke-width':2}):node('circle',{cx:xx,cy:yy,r:5,fill:color});}
   const coordinates=points.length>=2?points.map(p=>tx(p.time)):[xx,end??xx];
   if(!shape||coordinates.some(v=>v===null||!Number.isFinite(v))||Math.max(...coordinates)<left||Math.min(...coordinates)>right)continue;
   shape.setAttribute('data-analysis-kind',String(o.kind??key));if(liquidity)shape.setAttribute('stroke-dasharray','3 4');
   clip.append(shape);
   onEvent(o,key);count++;
  }
 }
 svg.append(clip);return count;
}
// Independent, read-only analysis transport. Never consumes trading chart candles.
function analysisDiagnosticFacts(data){const d=data?.diagnostics;return {analysis_status:data?.status,timestamp_semantics:data?.timestamp_semantics,time_domain:data?.time_domain,generated_at:data?.generated_at,as_of:data?.as_of,closed_through:data?.closed_through,bar_count:Array.isArray(data?.bars)?data.bars.length:undefined,analysis_reason:d?.reason??data?.reason,last_failure_reason:d?.last_failure_reason,worker_error:d?.error_code,worker_state:d?.supervisor_state,worker_failures:d?.failures,proof_status:d?.proof_status,proof_age_seconds:d?.proof_age_seconds,expected_closed_through:d?.expected_closed_through,history_ms:d?.history_ms,calculation_ms:d?.calculation_ms,ipc_ms:d?.ipc_ms,cache_age_seconds:d?.cache_age_seconds,cache_reused:d?.cache_reused};}
function analysisDiagnosticReport(diagnostics,value){try{diagnostics?.record(value);}catch{}}
function analysisDiagnosticNow(diagnostics){try{return diagnostics?.now()??0;}catch{return 0;}}
function createMainAnalysisUI({document,fetch,getHeaders,getPreferences,onChange,diagnostics=null}){
 const $=id=>document.getElementById(id),number=analysisNumber;
 let visible=true,feed=null,fresh=false,data=null,received=0,seq=0,controller=null,timer=null,context='';
 const active=()=>visible&&!document.hidden&&!$('workspace').hidden&&getPreferences().liquidity;
 const status=text=>{if($('main-analysis-status').textContent!==text)$('main-analysis-status').textContent=text;$('main-analysis-status').hidden=!text;};
 function reason(){
  if(feed&&!analysisDomain(feed))return 'Анализ: время не подтверждено';
  if(!fresh||!feed||feed.status!==(analysisRaw(feed)?'OBSERVED':'FRESH')||feed.connected!==true)return 'Анализ: нет свежего снимка MT5';
  if(feed.source!=='MT5'||!feed.instance||!feed.symbol)return 'Анализ: источник не подтверждён';
  return '';
 }
 function stop(){seq++;controller?.abort();controller=null;clearInterval(timer);timer=null;data=null;received=0;$('main-analysis-source').textContent='';}
 function refresh(){
  const next=[visible,document.hidden,$('workspace').hidden,getPreferences().liquidity,feed?.instance,feed?.symbol,feed?.connected,feed?.source,fresh,feed?.status,feed?.timestamp_semantics,feed?.time_domain].join('|');
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
  const prefs=getPreferences(),groups=[];if(prefs.liquidity)groups.push(['structure','smc']);
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
