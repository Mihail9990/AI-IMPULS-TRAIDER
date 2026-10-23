'use strict';
(() => {
 const $ = id => document.getElementById(id);
 const colors={buy:'#5ba8ff',sell:'#eaaa5c',entry:'#c3abff',tp:'#5cdbb4',sl:'#ff758d'};
 const labels={buy:'BUY pending',sell:'SELL pending',entry:'Entry · только чтение',tp:'Take profit',sl:'Stop loss'};
 const initialSettings={mode:'semi',engine:'fixed',parts:3,range:3,'sl-distance':3,continuation:1};
 const state={scenario:'position',levels:{entry:4139,tp:4140,sl:4136},settings:{...initialSettings},revision:0,preview:null,settingsPreview:null,bid:4139.45,ask:4139.55,lastTick:Date.now(),connected:true,stream:true,ticks:0,zoom:1,panX:0,panY:0};
 let candles=Array.from({length:70},(_,i)=>{const c=4137.7+i*.022+Math.sin(i*.4)*.35;return {open:c-.08*Math.cos(i),close:c,high:c+.13,low:c-.17};});
 const connectionMode=new URLSearchParams(location.search).get('controller'),native=connectionMode==='native',controlled=native||connectionMode==='mock';
 const pendingStorageKey=native?'tradfi-native-pending-request':'mock-pending-request';
 if(native){candles=[];Object.assign(state,{scenario:'waiting',levels:{},bid:null,ask:null,lastTick:0,connected:false});}
let backendView=null,inflight=false,pendingRequest=null,pendingScope=null,settingsLoadedScope=null;
 const svg=$('chart'); const ns='http://www.w3.org/2000/svg';
 function node(tag,attrs,text){if(tag==='text'&&svg.clientWidth<600&&attrs['font-size']){attrs['font-size']*=2;const r=svg.getBoundingClientRect(),stretch=(r.height/480)/(r.width/1000);attrs.x/=stretch;attrs.transform='scale('+stretch+',1)';}const n=document.createElementNS(ns,tag);Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,v));if(text!==undefined)n.textContent=text;return n;}
 function log(text){const li=document.createElement('li'),time=document.createElement('time');time.textContent=new Date().toLocaleTimeString('ru-RU')+(native?' · NATIVE':' · MOCK');li.append(time,document.createTextNode(text));$('log').prepend(li);while($('log').children.length>80)$('log').lastChild.remove();}
 const fmt=v=>v===null?'—':Number(v).toFixed(2);
 const domain=()=>{if(!native)return {min:4134+state.panY,max:4143+state.panY};
 const values=[state.bid,state.ask,...Object.values(state.levels),...candles.flatMap(c=>[c.low,c.high])].filter(v=>v!==null&&Number.isFinite(v));
 const lo=Math.min(...values),hi=Math.max(...values),margin=Math.max(.5,(hi-lo)*.1);
 return {min:lo-margin+state.panY,max:hi+margin+state.panY};};
 function y(price){const d=domain();return 445-(price-d.min)/(d.max-d.min)*420;}
 function priceAt(clientY){const r=svg.getBoundingClientRect(),sy=(clientY-r.top)/r.height*480,d=domain();return Math.round((d.min+(445-sy)/420*(d.max-d.min))*100)/100;}
 function render(){
  svg.replaceChildren();
  if(native&&state.bid===null){svg.append(node('text',{x:60,y:200,fill:'#b9cce3','font-size':20},'Ожидание данных MT5'));$('bid').textContent='—';$('ask').textContent='—';$('level-list').replaceChildren();return;}
  const d=domain();
  for(let i=0;i<=6;i++){const p=d.min+(d.max-d.min)*i/6,py=y(p);svg.append(node('line',{x1:35,x2:900,y1:py,y2:py,stroke:'#213148','stroke-width':1}),node('text',{x:svg.clientWidth<600?780:910,y:py+4,fill:'#8398b2','font-size':12},fmt(p)));}
  if(!native)svg.append(node('rect',{x:35,y:y(4142),width:865,height:Math.max(0,y(4139)-y(4142)),fill:'#32619b',opacity:.09}),node('rect',{x:35,y:y(4136),width:865,height:Math.max(0,y(4134)-y(4136)),fill:'#956d32',opacity:.09}));
  if(native)for(const side of ['buy','sell']){const level=state.levels[side],anchor=side==='buy'?state.ask:state.bid;if(level===undefined)continue;svg.append(node('rect',{x:35,y:Math.min(y(level),y(anchor)),width:865,height:Math.abs(y(level)-y(anchor)),fill:colors[side],opacity:.1}));}
  const width=(native?840/Math.max(1,candles.length):11)*state.zoom;
  candles.forEach((c,i)=>{const x=48+i*width+state.panX;if(x<35||x>890)return;const color=c.close>=c.open?'#52c7b0':'#e47d8e';svg.append(node('line',{x1:x,x2:x,y1:y(c.high),y2:y(c.low),stroke:color}),node('rect',{x:x-width*.28,y:Math.min(y(c.open),y(c.close)),width:Math.max(2,width*.56),height:Math.max(1,Math.abs(y(c.open)-y(c.close))),fill:color}));});
  [['Bid',state.bid,'#c3d5e9'],['Ask',state.ask,'#7e96b5']].forEach(([name,p,color])=>{svg.append(node('line',{x1:35,x2:900,y1:y(p),y2:y(p),stroke:color,'stroke-dasharray':'2 5'}),node('text',{x:40,y:y(p)+(name==='Ask'?20:-9),fill:color,'font-size':11},`${name} ${fmt(p)}`));});
  Object.entries(state.levels).forEach(([key,p])=>{const group=node('g',{'data-key':key,class:'level-hit',tabindex:key==='entry'?-1:0,role:key==='entry'?'img':'button','aria-label':`${labels[key]} ${fmt(p)}`});group.append(node('line',{x1:35,x2:900,y1:y(p),y2:y(p),stroke:'transparent','stroke-width':22}),node('line',{x1:35,x2:900,y1:y(p),y2:y(p),stroke:colors[key],'stroke-width':1.5,'stroke-dasharray':key==='entry'?'4 4':''}),node('rect',{x:svg.clientWidth<600?460:650,y:y(p)-(svg.clientWidth<600?20:12),width:svg.clientWidth<600?430:246,height:svg.clientWidth<600?40:24,rx:4,fill:'#122238',stroke:colors[key]}),node('text',{x:svg.clientWidth<600?475:660,y:y(p)+6,fill:colors[key],'font-size':13},`${svg.clientWidth<600?key.toUpperCase():labels[key]}  ${fmt(p)}`));group.addEventListener('keydown',e=>{if(e.key==='Enter'&&key!=='entry')previewLevel(key,p);});svg.append(group);});
  if(state.preview&&['move','create'].includes(state.preview.kind)){const p=state.preview.value;svg.append(node('line',{x1:35,x2:900,y1:y(p),y2:y(p),stroke:'#ffffff','stroke-width':2,'stroke-dasharray':'6 4'}),node('text',{x:45,y:y(p)-6,fill:'#fff','font-size':12},`PREVIEW ${fmt(p)} · не применено`));}
  svg.append(node('text',{x:45,y:470,fill:'#70859f','font-size':11},native?(backendView?.source+' · native M1'):(svg.clientWidth<600?'MOCK · M1':'Синтетические свечи · MOCK · MT5 не подключён')));
  $('level-list').replaceChildren();
  Object.entries(state.levels).forEach(([key,p])=>{const el=document.createElement(key==='entry'?'span':'button');el.className=key==='entry'?'read-only':'';el.style.setProperty('--level-color',colors[key]);el.textContent=`${labels[key]} ${fmt(p)}`;el.dataset.level=key;el.dataset.price=fmt(p);if(key!=='entry')el.onclick=()=>previewLevel(key,p);$('level-list').append(el);if(['buy','sell'].includes(key)){const del=document.createElement('button');del.textContent=`Удалить ${key.toUpperCase()}`;del.dataset.delete=key;del.onclick=()=>previewAction('delete',key,p);$('level-list').append(del);}});
  if(state.scenario==='position'){const close=document.createElement('button');close.id='close-position';close.textContent='Закрыть позицию…';close.onclick=()=>previewAction('close','entry',state.bid);$('level-list').append(close);}
  $('bid').textContent=fmt(state.bid);$('ask').textContent=fmt(state.ask);$('phase-value').textContent='DEMO-024 · RECOVERY / '+(state.scenario==='position'?'POSITION':state.scenario==='pending'?'PENDING':'WAIT_COMMAND');
 }
 function showPreview(){const p=state.preview;if(!p){$('preview').hidden=true;render();return;}$('preview').hidden=false;$('preview-error').textContent='';$('preview-title').textContent=p.kind==='close'?'Закрыть активную позицию по рынку':p.kind==='delete'?`Удалить ${labels[p.key]}`:p.kind==='create'?`Создать ${labels[p.key]}`:`Перенести ${labels[p.key]}`;$('preview-values').textContent=`${p.old===null?'Новый уровень':fmt(p.old)} → ${p.kind==='delete'?'отмена':fmt(p.value)} · обычный TP 0.25, recovery TP расчётный · ${native?'NATIVE':'MOCK'}`;$('new-price').value=fmt(p.value);$('new-price').disabled=['delete','close'].includes(p.kind);$('apply-preview').textContent=p.kind==='close'?(native?'Подтвердить закрытие':'Подтвердить закрытие · MOCK'):p.kind==='delete'?(native?'Подтвердить удаление':'Подтвердить удаление · MOCK'):(native?'Apply · подтвердить команду':'Apply · только MOCK');render();}
 function previewAction(kind,key,value){state.preview={kind,key,value,old:state.levels[key]??null,revision:state.revision,backend:backendView?JSON.parse(JSON.stringify(backendView)):null};showPreview();}
 function previewLevel(key,value){if(key==='entry')return;previewAction('move',key,value);}
 function createAt(price){if(native&&state.bid===null)return;const key=price>state.ask?'buy':'sell',old=state.levels[key]??null;state.preview={kind:old===null?'create':'move',key,value:price,old,revision:state.revision,backend:backendView?JSON.parse(JSON.stringify(backendView)):null};showPreview();}
 $('new-price').addEventListener('input',()=>{if(!state.preview)return;state.preview.value=Number($('new-price').value);$('preview-values').textContent=`${state.preview.old===null?'Новый уровень':fmt(state.preview.old)} → ${fmt(state.preview.value)} · ${native?'NATIVE':'MOCK'}`;render();});
 $('cancel-preview').onclick=()=>{state.preview=null;showPreview();};
 $('apply-preview').onclick=async()=>{const p=state.preview;if(!p||inflight)return;if(controlled){await applyBackendPreview(p);return;}const fail=text=>{$('preview-error').textContent=text;};if(!state.connected||Date.now()-state.lastTick>3000)return fail('Нет свежего mock-снимка; Apply запрещён.');if(p.revision!==state.revision)return fail('Состояние изменилось. Создайте новый preview.');if(!Number.isFinite(p.value)||p.value<=0||Math.abs(p.value*100-Math.round(p.value*100))>1e-6)return fail('Требуется положительная цена с шагом 0.01.');if(['create','move'].includes(p.kind)){
   if((p.key==='buy'&&p.value<=state.ask)||(p.key==='sell'&&p.value>=state.bid))return fail(`Уровень уже пересечён. Bid ${fmt(state.bid)}, Ask ${fmt(state.ask)}. MARKET не создаётся.`);
   if(p.key==='tp'&&p.value<=state.bid)return fail('Новый TP уже достигнут; здесь требуется отдельное подтверждение закрытия, не скрытая мутация.');
   if(p.key==='sl'&&p.value>=state.bid)return fail('SL BUY должен быть ниже Bid.');state.levels[p.key]=p.value;
  }else if(p.kind==='delete')delete state.levels[p.key];else if(p.kind==='close'){state.levels={};state.scenario='waiting';log('MOCK закрытие: фактической сделки нет; RECOVERY не пересчитан. Новые входы остановлены, ожидание команды.');}
  state.revision++;log(`Подтверждено только в MOCK: ${p.kind} ${p.key.toUpperCase()} ${fmt(p.value)}. Команда брокеру не отправлена.`);state.preview=null;showPreview();};
 document.querySelectorAll('[data-scenario]').forEach(b=>b.onclick=()=>{state.scenario=b.dataset.scenario;state.levels=state.scenario==='position'?{entry:4139,tp:4140,sl:4136}:{buy:4141,sell:4133};state.revision++;state.preview=null;document.querySelectorAll('[data-scenario]').forEach(x=>x.classList.toggle('selected',x===b));showPreview();log('Переключён вымышленный сценарий '+state.scenario);});
 $('zoom-in').onclick=()=>{state.zoom=Math.min(4,state.zoom*1.25);render();};$('zoom-out').onclick=()=>{state.zoom=Math.max(.5,state.zoom/1.25);render();};$('reset-chart').onclick=()=>{state.zoom=1;state.panX=0;state.panY=0;render();};
 const pointers=new Map();let gesture=null;
 svg.addEventListener('pointerdown',e=>{svg.setPointerCapture(e.pointerId);pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});if(pointers.size>=2){if(pointers.size>2)return;if(gesture?.kind==='level'&&gesture.moved){state.preview=null;$('preview').hidden=true;}const a=[...pointers.values()];gesture={kind:'pinch',distance:Math.hypot(a[0].x-a[1].x,a[0].y-a[1].y),zoom:state.zoom,moved:true};return;}const key=e.target.closest('[data-key]')?.dataset.key;gesture={kind:key?'level':'pan',key,x:e.clientX,y:e.clientY,panX:state.panX,panY:state.panY,span:domain().max-domain().min,moved:false};});
 svg.addEventListener('pointermove',e=>{if(!pointers.has(e.pointerId)||!gesture)return;pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});if(gesture.kind==='pinch'){if(pointers.size===2){const a=[...pointers.values()],dist=Math.hypot(a[0].x-a[1].x,a[0].y-a[1].y);state.zoom=Math.max(.5,Math.min(4,gesture.zoom*dist/Math.max(1,gesture.distance)));render();}return;}const dx=e.clientX-gesture.x,dy=e.clientY-gesture.y;if(Math.hypot(dx,dy)>7)gesture.moved=true;if(!gesture.moved)return;if(gesture.kind==='pan'){state.panX=gesture.panX+dx/svg.getBoundingClientRect().width*1000;state.panY=gesture.panY+dy/svg.getBoundingClientRect().height*gesture.span;render();}else if(gesture.key!=='entry'){state.preview={kind:'move',key:gesture.key,value:priceAt(e.clientY),old:state.levels[gesture.key],revision:state.revision,backend:backendView?JSON.parse(JSON.stringify(backendView)):null};render();}});
 const end=e=>{if(!pointers.has(e.pointerId))return;pointers.delete(e.pointerId);if(!gesture)return;if(gesture.kind==='pinch'){if(!pointers.size)gesture=null;return;}if(e.type==='pointercancel'){state.preview=null;gesture=null;showPreview();return;}if(gesture.kind==='level'&&gesture.key!=='entry'){if(state.preview&&gesture.moved)showPreview();else previewLevel(gesture.key,state.levels[gesture.key]);}else if(gesture.kind==='pan'&&!gesture.moved)createAt(priceAt(e.clientY));gesture=null;};svg.addEventListener('pointerup',end);svg.addEventListener('pointercancel',end);
 svg.addEventListener('wheel',e=>{e.preventDefault();state.zoom=Math.max(.5,Math.min(4,state.zoom*(e.deltaY<0?1.1:.9)));render();},{passive:false});
 $('preview-settings').onclick=()=>{const next={};for(const key of Object.keys(initialSettings)){const val=$(key).value;next[key]=['mode','engine'].includes(key)?val:Number(val);}if(!Number.isInteger(next.parts)||next.parts<1||next.parts>100||['range','sl-distance','continuation'].some(k=>!Number.isFinite(next[k])||next[k]<=0)){$('settings-error').textContent='Проверьте положительные расстояния и целое N от 1 до 100.';return;}$('settings-error').textContent='';state.settingsPreview=next;$('settings-diff').textContent=Object.keys(next).map(k=>`${k}: ${state.settings[k]} → ${next[k]}`).join('\n')+'\nТолько интерфейс MOCK. Перерасчёт стратегии не выполняется.';$('settings-preview').hidden=false;};
 $('cancel-settings').onclick=()=>{state.settingsPreview=null;$('settings-preview').hidden=true;};$('apply-settings').onclick=async()=>{if(!state.settingsPreview||inflight)return;if(controlled){await applyBackendSettings();return;}state.settings=state.settingsPreview;state.settingsPreview=null;$('settings-preview').hidden=true;$('mode-value').textContent=state.settings.mode==='semi'?'Полуавтоматический':'Автоматический';log('MOCK настройки подтверждены. Конфигурация бота не изменена.');};$('clear-log').onclick=()=>$('log').replaceChildren();
 document.querySelectorAll('[data-tab]').forEach(button=>button.onclick=()=>{const chart=button.dataset.tab==='chart';$('market').hidden=!chart;$('overview').hidden=chart;document.querySelectorAll('[data-tab]').forEach(b=>b.setAttribute('aria-selected',String(b===button)));});
 const stream=document.createElement('button');stream.id='toggle-stream';stream.textContent='Пауза mock потока';stream.onclick=()=>{state.stream=!state.stream;stream.textContent=state.stream?'Пауза mock потока':'Продолжить mock поток';updateFreshness();};document.querySelector('.chart-toolbar').append(stream);
 const disconnect=document.createElement('button');disconnect.id='toggle-connection';disconnect.textContent='Mock disconnect';disconnect.onclick=()=>{state.connected=!state.connected;disconnect.textContent=state.connected?'Mock disconnect':'Mock reconnect';updateFreshness();};document.querySelector('.chart-toolbar').append(disconnect);
 function quoteAge(){return native?(state.feedAge===null?Infinity:state.feedAge+Math.max(0,performance.now()-(state.feedReceived??performance.now()))/1000):Math.max(0,(Date.now()-state.lastTick)/1000);}
 function freshEnough(){return state.connected&&quoteAge()<=(native?5:3)&&(!native||state.feedStatus==='FRESH');}
 function updateFreshness(){const age=quoteAge(),status=!state.connected?'DISCONNECTED':native&&state.feedStatus!=='FRESH'?state.feedStatus:age>(native?5:3)?'STALE':native?'FRESH':'MOCK FRESH';
 $('freshness').textContent=status+' · tick '+(state.lastTick?new Date(state.lastTick).toISOString():'UTC не проверен')+' · '+(Number.isFinite(age)?age.toFixed(1)+' s':'—');
 $('freshness').dataset.status=status.toLowerCase().replace('mock ','');}
 function apiHeaders(){return native?{'X-Local-Control':'1','X-Controller-Scope':backendView?.scope??''}:{'X-Local-Mock':'1'};}

 setInterval(()=>{if(controlled){pollBackend();return;}if(state.stream&&state.connected){state.ticks++;state.bid=4139.45+Math.sin(state.ticks*.25)*.08;state.bid=Math.round(state.bid*100)/100;state.ask=Math.round((state.bid+.1)*100)/100;state.lastTick=Date.now();render();}updateFreshness();},1000);
 window.localPanel=Object.freeze({snapshot:()=>JSON.parse(JSON.stringify(state))});
 async function postAction(action,revision){
  if(inflight||pendingRequest)throw Error('Предыдущий исход не подтверждён; повтор запрещён до сверки.');
  inflight=true;
  try{const request={id:crypto.randomUUID(),revision,action};pendingRequest=request.id;pendingScope=backendView?.scope??null;localStorage.setItem(pendingStorageKey,pendingRequest);if(native)localStorage.setItem(pendingStorageKey+'-scope',pendingScope);
   const response=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json',...apiHeaders()},body:JSON.stringify(request),signal:AbortSignal.timeout(3000)});
   if(!response.ok){if([400,401,403,409,413,429].includes(response.status)){pendingRequest=null;localStorage.removeItem(pendingStorageKey);}throw Error(response.status===409?'Состояние изменилось: обновите preview':'Команда отклонена');}
   const result=await response.json();
   log((native?'NATIVE controller: ':'MOCK controller: ')+action.type+' · '+result.status);
   if(['APPLIED','REJECTED'].includes(result.status)){pendingRequest=null;localStorage.removeItem(pendingStorageKey);}
   if(!['APPLIED','QUEUED'].includes(result.status))throw Error('Исход '+result.status+'. Не повторяйте команду без сверки состояния.');
  }catch(error){log('Команда не подтверждена: '+error.message);throw error;}
  finally{inflight=false;}
 }
 async function applyBackendPreview(p){
  try{
   if(!freshEnough())throw Error('Нет свежего проверенного снимка; Apply запрещён.');
   if(!p.backend)throw Error('Обновите preview контроллера');
   if(native&&p.backend.scope!==backendView?.scope)throw Error('Контекст счёта изменился; создайте новый preview.');
   const v=p.backend,pos=v.positions[0];let action;
   if(p.kind==='close')action={type:'close_position',position_id:pos.id,expected_size:pos.size,confirmed:true};
   else if(p.kind==='delete'){const o=v.orders.find(o=>o.side.toLowerCase()===p.key);action={type:'delete_trigger',order_id:o.id,old_price:o.price};}
   else if(['sl','tp'].includes(p.key)){action={type:'protection',position_id:pos.id,old:{sl:pos.sl,tp:pos.tp},new:{sl:pos.sl,tp:pos.tp,[p.key]:String(p.value)}};}
   else{const levels=Object.fromEntries(v.orders.map(o=>[o.side,o.price]));levels[p.key.toUpperCase()]=String(p.value);action={type:'triggers',representation:'prices',levels};}
   await postAction(action,p.backend.revision);state.preview=null;showPreview();await pollBackend();
  }catch(error){$('preview-error').textContent=error.message;}
 }
 async function applyBackendSettings(){
  try{const next=state.settingsPreview;
   if(native&&state.settingsScope!==backendView?.scope)throw Error('Контекст счёта изменился; создайте новый preview.');
   const recovery={...backendView.modules.recovery,parts:next.parts,after_sl:String(next.range),fixed_sl:String(next['sl-distance']),continuation:String(next.continuation)};
   await postAction({type:'settings',mode:next.mode,engine:next.engine,recovery},state.settingsRevision);
   state.settingsPreview=null;$('settings-preview').hidden=true;await pollBackend();
  }catch(error){$('settings-error').textContent=error.message;}
 }
 async function pollBackend(){
  if(inflight||state.polling)return;state.polling=true;
  try{const response=await fetch('/api/state',{headers:apiHeaders(),signal:AbortSignal.timeout(1500)});
   if(!response.ok){if(native&&[401,403].includes(response.status))$('native-login').hidden=false;throw Error('Backend unavailable');}
   const v=await response.json();if(v.symbol!=='XAUUSD.s'||(native?(!['MT5','INJECTED_MT5'].includes(v.source)||v.feed?.feed_version!=='mt5_chart_v1'):v.source!=='MOCK'))throw Error('Unexpected source');
   backendView=v;state.connected=native?v.feed.connected:true;
   if(native){state.feedAge=v.feed.age_seconds??null;state.feedReceived=performance.now();state.feedStatus=v.feed.status;
    candles=v.feed.candles.map(c=>Object.fromEntries(['open','high','low','close'].map(k=>[k,Number(c[k])])));
    document.querySelector('.demo-banner').textContent=v.source+' · '+v.feed.feed_version+' · '+v.feed.timestamp_semantics+' TIMESTAMPS';
    document.querySelector('.connection').childNodes[1].textContent=v.source==='INJECTED_MT5'?'Offline fixture':'MT5 backend';
    document.querySelector('.fixture-label').textContent=v.source+' / M1';$('native-login').hidden=true;
   }
   const outcome=pendingRequest&&(!native||pendingScope===v.scope)?(v.action_results[pendingRequest]?.status??v.action_results[pendingRequest]):null;
   if(pendingRequest&&['APPLIED','REJECTED','CONFLICT'].includes(outcome)){log('Сверка команды '+pendingRequest+': '+outcome);pendingRequest=null;localStorage.removeItem(pendingStorageKey);}
   $('apply-preview').disabled=!!pendingRequest;$('apply-settings').disabled=!!pendingRequest;state.lastTick=v.timestamp===null?0:v.timestamp*1000;
   state.bid=v.quote?Number(v.quote.bid):null;state.ask=v.quote?Number(v.quote.ask):null;state.revision=v.revision;
   state.levels=Object.fromEntries(v.orders.map(o=>[o.side.toLowerCase(),Number(o.price)]));
   const pos=v.positions[0];state.scenario=pos?'position':v.orders.length?'pending':'waiting';
   if(pos)Object.assign(state.levels,{entry:Number(pos.entry),tp:Number(pos.tp),sl:Number(pos.sl)});
   state.settings={mode:v.mode,engine:v.modules.engine,parts:v.modules.recovery.parts,range:Number(v.modules.recovery.after_sl),'sl-distance':Number(v.modules.recovery.fixed_sl),continuation:Number(v.modules.recovery.continuation)};
   if(settingsLoadedScope!==(v.scope??'mock')){for(const key of Object.keys(initialSettings))$(key).value=state.settings[key];settingsLoadedScope=v.scope??'mock';}
   render();$('phase-value').textContent=(v.cycle_id?v.cycle_id.slice(0,8):'—')+' / '+v.phase;
   $('debt-value').textContent=(v.recovery?v.recovery.debt:'0')+(native?' · валюта счёта':' MOCK');
   $('parts-value').textContent=(v.recovery?v.recovery.parts_remaining:'—')+' оставшихся частей';
   $('mode-value').textContent=v.mode==='semi'?'Полуавтоматический':'Автоматический';
   document.querySelector('.summary .amber').textContent=v.time_block?'TIME BLOCK':native?'NATIVE GUARDS':'FAKE ADAPTER ONLY';
   document.querySelector('.summary .amber').nextElementSibling.textContent=v.time_block?v.time_block.reason:native?v.feed.status:'Нет MT5 · только искусственный брокер';
  }catch(error){state.connected=false;}finally{state.polling=false;updateFreshness();}
 }
 if(controlled){
  pendingRequest=localStorage.getItem(pendingStorageKey);if(native)pendingScope=localStorage.getItem(pendingStorageKey+'-scope');
  document.querySelectorAll('[data-scenario]').forEach(b=>b.disabled=true);
  stream.disabled=true;disconnect.disabled=true;
  document.querySelector('.aside-note').textContent='Общий контроллер на localhost: только искусственный брокер. Цикл продолжает работать при закрытии страницы. Перезапуск demo-процесса сбрасывает вымышленные данные.';
  document.querySelector('.demo-banner').textContent='LOCAL MOCK CONTROLLER · MT5 НЕ ПОДКЛЮЧЁН · КОМАНДЫ ТОЛЬКО FAKE АДАПТЕРУ';
  $('settings').querySelector('.card-head p').textContent='Apply изменяет общий MOCK контроллер';
  $('settings').querySelector('.muted').textContent='Смена движка — следующий полный цикл; N пересчитывает TP текущего fixed recovery.';
  $('journal').querySelector('.card-head p').textContent='Журнал локальных команд; состояние хранится backend процесса.';
  document.querySelector('footer').textContent='Локальный MOCK контроллер без credentials и MT5. Не предназначен для публичного размещения.';
  if(native){
   document.querySelector('.demo-banner').textContent='NATIVE MT5 · ожидание авторизации и данных';
   document.querySelector('.aside-note').textContent='Контроллер работает независимо от страницы. Все изменения требуют Apply и проверки брокерского состояния.';
   document.querySelector('h1 span').textContent='NATIVE';document.querySelector('.fixture-label').textContent='MT5 / M1';
   document.querySelector('.market .card-head p').textContent='Свечи backend · перенос создаёт только preview';
   $('overview').querySelector('p').textContent='Native MT5 pipeline. Источник, свежесть и статус связи показаны на графике; непроверенное время не выдаётся за UTC.';
   $('settings').querySelector('.card-head p').textContent='Apply изменяет общий контроллер';
   $('preview').querySelector('small').textContent='Apply отправляет подтверждённую команду. Исполнение сверяется по состоянию брокера.';
   $('preview').querySelector('.eyebrow').textContent='ПРЕДПРОСМОТР КОМАНДЫ';
   document.querySelectorAll('.summary article small')[0].textContent='Режим общего контроллера';
   document.querySelectorAll('.summary article small')[1].textContent='Состояние backend';
   document.querySelectorAll('.summary article label')[2].textContent='RECOVERY';
   $('apply-settings').textContent='Apply · подтвердить настройки';stream.hidden=true;disconnect.hidden=true;
   svg.setAttribute('aria-label','График XAUUSD.s из backend; изменение уровня требует Apply');
   $('debt-value').textContent='—';$('parts-value').textContent='Нет снимка';$('phase-value').textContent='Ожидание данных';
   document.querySelector('.summary .amber').textContent='AUTH REQUIRED';
   document.querySelector('footer').textContent='Localhost control · credentials отсутствуют в frontend. Управление недоступно без настройки backend auth.';
   const form=document.createElement('section');form.id='native-login';form.className='card';
   const label=document.createElement('label');label.textContent='Доступ к локальному backend';const input=document.createElement('input');input.type='password';input.autocomplete='current-password';input.id='web-password';
   const button=document.createElement('button');button.textContent='Войти';const error=document.createElement('p');error.id='login-error';form.append(label,input,button,error);document.querySelector('header').after(form);
   button.onclick=async()=>{const password=input.value;input.value='';try{const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json','X-Local-Control':'1'},body:JSON.stringify({password}),signal:AbortSignal.timeout(2000)});if(!r.ok)throw Error('Авторизация недоступна или отклонена');error.textContent='';await pollBackend();}catch(e){error.textContent=e.message;}};
  }
  const original=$('preview-settings').onclick;$('preview-settings').onclick=()=>{state.settingsRevision=backendView?.revision;state.settingsScope=backendView?.scope;original();if(controlled)$('settings-diff').textContent=$('settings-diff').textContent.replace('Только интерфейс MOCK. Перерасчёт стратегии не выполняется.','После Apply команда поступит общему контроллеру; результат требует сверки.');};
  pollBackend();
 }
 log(native?'Native UI: ожидание авторизации и проверенного backend snapshot.':'Загружен локальный MOCK. Все уровни вымышлены. Entry доступен только для чтения.');render();updateFreshness();
})();
