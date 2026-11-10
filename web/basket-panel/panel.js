'use strict';
(() => {
 const $=id=>document.getElementById(id),svg=$('chart'),ns='http://www.w3.org/2000/svg';
 const mode='live';
 let plotBottom=480,plotHeight=450,frameHeight=520;
 const storageKey=k=>'basket-'+k;
 const apiPath=kind=>'/api/'+kind;
 let view=null,pending=localStorage.getItem(storageKey('request')),pendingScope=localStorage.getItem(storageKey('scope')),busy=false,polling=false,loadedScope=null,preview=null,lines=[],zoom=1,pan=0,gesture=null,hold=null,timeOffset=0,visibleCount=60,previewFocus=null,previewWasOpen=false;
 let lastActionReceipt=null;
 const names={INITIAL_SIZE:'Объём начальной позиции',INITIAL_ENTRY_DISTANCE:'STOP от Close M1',INITIAL_SL_DISTANCE:'Начальный SL',INITIAL_TP_DISTANCE:'Начальный TP',BASKET_COUNT:'Число билетов корзины',TICKET_SIZE:'Объём одного билета',RECOVERY_SL_DISTANCE:'Фиксированный SL корзины',COST_RESERVE:'Резерв будущих расходов, валюта счёта',TP_STEP_MULTIPLIER:'Множитель шага TP (не меньше 1)',CYCLE_COOLDOWN_SECONDS:'Пауза после цикла, секунды'};
 const headers=()=>({'X-Local-Control':'1','X-Controller-Scope':view?.scope??''});
 function error(text){$('action-error').textContent=text;if(preview)$('preview-error').textContent=text;}
 function clearPending(){pending=null;pendingScope=null;localStorage.removeItem(storageKey('request'));localStorage.removeItem(storageKey('scope'));}
 async function send(action,revision=view?.revision,scope=view?.scope){
  if(busy||pending)throw Error('Предыдущий исход не сверён. Повтор запрещён.');
  if(!view||scope!==view.scope)throw Error('Контекст изменился; обновите предпросмотр.');
  const request={id:crypto.randomUUID(),revision,action};pending=request.id;pendingScope=scope;localStorage.setItem(storageKey('request'),pending);localStorage.setItem(storageKey('scope'),scope);busy=true;lastActionReceipt=null;updateBotActivity();
  try{const r=await fetch(apiPath('action'),{method:'POST',headers:{...headers(),'Content-Type':'application/json'},body:JSON.stringify(request),signal:AbortSignal.timeout(4000)});
   if(!r.ok){if([400,401,403,409,413,429].includes(r.status))clearPending();throw Error(r.status===409?'Снимок устарел; обновите предпросмотр.':'Команда отклонена сервером.');}
   const receipt=await r.json();lastActionReceipt={status:receipt.status,reason:receipt.reason};if(['APPLIED','REJECTED','CONFLICT'].includes(receipt.status))clearPending();error(actionReceiptText(receipt.status,receipt.reason));
  }finally{busy=false;await poll();updateBotActivity();}
 }
 async function command(text,message){if(!confirm(message))return;try{await send({type:'command',text});}catch(e){error(e.message);}}
 const keyboard=$('cycle-keyboard');
 try{keyboard.open=localStorage.getItem('cycle-keyboard-open')!=='false';}catch{}
 keyboard.addEventListener('toggle',()=>{try{localStorage.setItem('cycle-keyboard-open',String(keyboard.open));}catch{}});
 $('cancel-initial').onclick=async()=>{
  if(!view)return;const revision=view.revision,scope=view.scope;
  const action={type:'cancel_initial',cycle_id:view.cycle_id,expected_orders:view.orders.map(o=>o.id)};
  if(!confirm('Отменить только начальный триггер? После следующей закрытой направленной M1 он снова выставится по свечному сигналу. Если ордер успел исполниться, позиция сохраняется.'))return;
  try{await send(action,revision,scope);}catch(e){error(e.message);}
 };
 async function reverseLadder(enabled){if(!view)return;const scope=view.scope,revision=view.revision,action={type:'reverse_ladder',cycle_id:view.cycle_id,enabled,expected_positions:view.positions.map(p=>p.id).sort()};if(!confirm(enabled?'После TP1: назначить SL по долям i/N от СОБСТВЕННОГО фактического входа каждой оставшейся части к текущей серверной цене? Билеты — по номерам TP. Уровни не округляются и не следуют за ценой.':'Перенести SL каждой оставшейся части на её СОБСТВЕННЫЙ фактический вход? TP сохраняются.'))return;try{await send(action,revision,scope);}catch(e){error(e.message);}}
 $('own-entry-sl').onclick=async()=>{if(!view)return;const scope=view.scope,revision=view.revision,action={type:'own_entry_sl',cycle_id:view.cycle_id,expected_positions:view.positions.map(p=>p.id).sort()};try{await send(action,revision,scope);}catch(e){error(e.message);}};
 $('reverse-enable').onclick=()=>reverseLadder(true);$('reverse-disable').onclick=()=>reverseLadder(false);
 $('start').onclick=()=>command('/start','Запустить следование закрытым M1 и восстановление корзинами?');
 $('stop').onclick=()=>command('/stop','Остановить новые входы и отменить ожидающие ордера? Открытые билеты сохраняют SL/TP. Долг не списывается.');
 $('resume').onclick=()=>command('/resume_time','Запросить свежую проверку времени? Другие блокировки не снимаются.');
 $('reset').onclick=()=>command('/reset_manual','Подтвердить ручную сверку всех ордеров и билетов? Требуются доказанное закрытие и отсутствие неопределённости. Долг сохраняется.');
 $('login-button').onclick=async()=>{const password=$('password').value;$('password').value='';try{const r=await fetch('/api/login',{method:'POST',headers:{...headers(),'Content-Type':'application/json'},body:JSON.stringify({password}),signal:AbortSignal.timeout(3000)});if(!r.ok)throw Error('Вход отклонён');$('login-error').textContent='';await poll();}catch(e){$('login-error').textContent=e.message;}};
 $('logout').onclick=async()=>{await fetch('/api/logout',{method:'POST',headers:{...headers(),'Content-Type':'application/json'},body:'{}'});location.reload();};
 // Activity uses controller snapshots and receipts, never inferred fills.
 function actionReceiptText(status,reason){
  if(status==='APPLIED')return 'Команда обработана контроллером; исполнение и позиции проверяются отдельно по снимку.';
  if(status==='REJECTED')return 'Команда отклонена до исполнения'+(reason?': '+reason:'.');
  if(status==='CONFLICT')return 'Команда отклонена: снимок устарел. Нужен новый предпросмотр.';
  if(status==='UNKNOWN')return 'Исход команды неизвестен. Повтор запрещён до сверки.';
  return 'Команда ожидает результата контроллера; исполнение не подтверждено.';
 }
 function updateBotActivity(){
  const write=(id,text)=>{if($(id).textContent!==text)$(id).textContent=text;};
  const role='LIVE';
  if(!view){write('bot-activity-state',role+' · Ожидание снимка контроллера.');return;}
  const phases={WAIT_START:'Ожидание запуска',WAIT_CANDLE:'Ожидание новой закрытой свечи',INITIAL_WAIT:'Ожидание начального входа',INITIAL_PENDING:'Ожидание начального триггера',INITIAL_POSITION:'Сопровождение начальной позиции',RECOVERY_POSITION:'Сопровождение корзины RECOVERY',RECOVERY_PENDING:'Ожидание ордеров RECOVERY / сверки',RECOVERY_CLEANUP:'Очистка ордеров RECOVERY / сверка',V3_WAIT_ENTRY:'Ожидание вашего входа RECOVERY',V3_WAIT_CANDLE:'Ожидание выбранной свечи RECOVERY',COOLDOWN:'Пауза перед новым циклом',PAUSED:'Новые входы остановлены',PAUSED_CYCLE:'Цикл приостановлен',MANUAL:'Требуется ручная сверка',SAFE_PAUSE:'Защитная пауза',MIGRATION_BLOCK:'Блокировка переноса состояния'};
  const warnings=[];if(!v3Fresh)warnings.push('Нет свежего снимка: ниже последние полученные данные.');
  if(view.read_only)warnings.push('READ ONLY');
  if(view.migration_block)warnings.push(String(view.migration_block));if(view.manual_reason)warnings.push(String(view.manual_reason));
  if(view.time_block)warnings.push(timeSafetyText());
  if(view.feed?.status!=='FRESH'||!feedUtcVerified())warnings.push('Котировки или время не подтверждены как свежие.');
  write('bot-activity-warning',warnings.join(' · '));
  let detail=phases[view.phase]??('Состояние контроллера: '+(view.phase??'не указано'));
  if(view.v3_request)detail+=' · Запланирован '+view.v3_request.order_type+' по закрытию выбранной свечи';
  if(view.v3_link?.cancel)detail+=' · Отмена связанных триггеров ожидает сверки';
  else if(view.v3_link?.replace)detail+=' · Перенос связанных триггеров ожидает сверки';
  if(view.edit)detail+=' · Правка уровней: '+view.edit.stage;
  if(view.stopped&&!['PAUSED','PAUSED_CYCLE'].includes(view.phase))detail+=' · Новые входы остановлены';
  const count=items=>Array.isArray(items)?items.length:'—';
  write('bot-activity-state',role+' · '+detail+' · Снимок: позиций '+count(view.positions)+', ордеров '+count(view.orders));
  let action='';
  if(busy)action='Команда отправляется; исполнение не подтверждено.';
  else if(pending){action=pendingScope!==view.scope?'Ожидается сверка команды другого контекста; новые команды заблокированы.':actionReceiptText(view.action_results?.[pending]?.status,view.action_results?.[pending]?.reason);}
  else if(lastActionReceipt)action='Последняя команда: '+actionReceiptText(lastActionReceipt.status,lastActionReceipt.reason);
  write('bot-activity-action',action);
  const events=Array.isArray(view.journal)?view.journal.filter(x=>typeof x==='string'&&x.trim()):[];
  write('bot-activity-event',events.length?'Последнее сообщение контроллера: '+events[events.length-1]:'Сообщений контроллера пока нет.');
 }
 // V3 reuses scoped receipts and snapshot revisions; no live execution is enabled here.
 // Display-only helpers: timestamp labels never establish a trading time contract.
 const feedUtcVerified=()=>view?.feed?.timestamp_semantics==='VERIFIED';
 function utcLabel(timestamp,short=false){
  if(!feedUtcVerified()||timestamp===null||timestamp===undefined||timestamp==='')return '';
  const date=new Date(Number(timestamp)*1000);
  return Number.isFinite(date.getTime())?(short?date.toISOString().slice(11,16):date.toISOString()):'';
 }
 function timeSafetyText(){
  if(!view?.time_block)return '';
  const reason=view.time_status?.current_reason??view.time_status?.latched_reason??view.time_block.reason;
  const message={MT5_UTC_WAIT_FRESH_DATA:'ожидание свежего тика и M1.',MT5_UTC_WAIT_REFERENCE:'ожидание независимой проверки UTC.',MT5_UTC_CONTRACT_MISMATCH:'метки MT5 не соответствуют UTC; запустите read-only диагностику.',MT5_UTC_READ_TIMEOUT:'чтение MT5 заняло слишком долго.'}[reason]??'нужна повторная проверка времени.';
  return 'Время: '+message+(reason?' ['+reason+']':'')+' Блокировка сохранена; нажмите «Проверить время» для повторной проверки.';
 }
 let v3Fresh=false,v3PresetKey='';
 const v3Enabled=()=>view?.recovery_mode==='V3';
 const v3Blocked=()=>!view||!v3Fresh||busy||!!pending||view.read_only||!!view.manual_reason||!!view.migration_block||!!view.time_block;
 function connectionLabel(){
  const status=view?.feed?.status,verified=feedUtcVerified(),fresh=v3Fresh&&status==='FRESH'&&verified;
  const role=view?.read_only?'LIVE · READ ONLY':'LIVE';
  const warning=!v3Fresh?'нет свежего снимка':!verified?'время не проверено':status!=='FRESH'?'котировки не свежие':'';
  $('drawer-toggle').textContent='Команды · '+role+(warning?' · '+warning:'');
  $('drawer-toggle').dataset.warning=String(!fresh);$('drawer-toggle').title=role+' · '+(warning||'Свежие данные')+' · возраст котировки: '+(view?.feed?.age_seconds??'—')+' сек.';
  svg.dataset.feedFresh=String(fresh);
 }
 const v3Count=()=>{const n=Number($('v3-count').value);if(!Number.isInteger(n)||n<1||n>20)throw Error('Количество ступеней: целое число от 1 до 20.');return n;};
 function v3InputMode(){
  const stop=$('v3-order-type').value==='STOP';
  const now=$('v3-timing').querySelector('option[value="now"]');now.disabled=stop;
  if(stop&&$('v3-timing').value==='now')$('v3-timing').value='current';
  const candle=$('v3-timing').value!=='now';$('v3-side').disabled=candle;
  $('v3-direction-note').textContent=candle?'Направление — цвет выбранной свечи. При doji ордера не выставляются.':'Направление выбираете вы. Фактическая цена зависит от исполнения.';
 }
 function v3FillSettings(){
  if(!view)return;
  $('v3-setting-distance').value=view.v3_settings?.entry_distance??'1.00';
  (view.v3_settings?.count_presets??[4,6,10]).forEach((v,i)=>{if(i<3)$('v3-preset-'+i).value=v;});
 }
 function v3Render(){
  const enabled=v3Enabled(),controls=view?.v3_controls??{},blocked=v3Blocked();
  $('v3-panels').hidden=false;$('v3-settings').hidden=false;
  $('chart-and-tools').classList.toggle('v3-active',true);
  $('save-v3-settings').disabled=blocked||!enabled;
  const distance=Number(view?.v3_sl_distance);
  $('v3-sl-value').textContent=Number.isFinite(distance)&&distance>0?distance.toFixed(2):'—';
  $('v3-sl-minus').disabled=blocked||!enabled||controls.can_adjust_sl!==true||!Number.isFinite(distance)||distance<=.1;
  $('v3-sl-plus').disabled=blocked||!enabled||controls.can_adjust_sl!==true||!Number.isFinite(distance)||distance<=0;
  $('v3-sl-status').textContent='Шаг 0.10. Брокер может отклонить недопустимый SL; результат проверяется по каждому билету.';
  $('v3-submit-entry').disabled=blocked||!enabled||controls.can_enter!==true;
  $('v3-cancel-request').hidden=!view?.v3_request;
  $('v3-cancel-request').disabled=blocked||!enabled||!view?.v3_request;
  const request=view?.v3_request;const targetTime=request&&Number.isFinite(Number(request.bar_id))?(utcLabel(Number(request.bar_id)+60)?utcLabel(Number(request.bar_id)+60).slice(11,19)+' UTC':'выбранной свечи (время не проверено)'):'выбранной свечи';
  $('v3-request-status').textContent=request?'Ожидается закрытие '+targetTime+': '+request.order_type+', '+request.count+' × 0.01. Направление по цвету свечи; doji — пропуск.':'Нет запланированного входа. Для следующей попытки RECOVERY дождитесь закрытия всей корзины.';
  const linked=!!view?.v3_link,canCancel=controls.can_cancel_link===true;
  $('v3-link-toggle').textContent=linked?'Отменить триггеры на SL':'Выставить триггеры на SL…';
  $('v3-link-toggle').dataset.cancel=String(linked);
  $('v3-link-toggle').disabled=blocked||!enabled||(linked?!canCancel:controls.can_link!==true);
  const link=view?.v3_link;
  $('v3-link-status').textContent=linked?(link.cancel?'Отмена запрошена; ожидается сверка MT5.':link.replace?'Перенос SL и связанных триггеров; ожидается сверка.':link.count+' × 0.01 '+link.side+' STOP на '+link.price+'. Якорь — SL первой ступени.'):'Триггеры не выставлены. После выставления они связаны с SL первой ступени.';
  $('v3-control-reason').textContent=!v3Fresh?'Нет свежего снимка. Действия заблокированы.':controls.reason??'';
  $('v3-entry-distance').textContent='STOP: от Close выбранной свечи на '+(view?.v3_settings?.entry_distance??'—')+'. Каждая ступень — 0.01 лота.';
  const presets=view?.v3_settings?.count_presets??[4,6,10],key=JSON.stringify(presets);
  if(key!==v3PresetKey){v3PresetKey=key;$('v3-presets').replaceChildren();presets.slice(0,3).forEach((n,i)=>{const b=document.createElement('button');b.type='button';b.textContent=String(n);b.title='Пресет '+(i+1)+': '+n+' ступеней';b.onclick=()=>{$('v3-count').value=String(n);};$('v3-presets').append(b);});}
  v3InputMode();
 }
 $('v3-order-type').onchange=v3InputMode;$('v3-timing').onchange=v3InputMode;
 for(const id of ['v3-sl-panel','v3-entry-panel']){try{$(id).open=localStorage.getItem(id+'-open')!=='false';}catch{}$(id).addEventListener('toggle',()=>{try{localStorage.setItem(id+'-open',String($(id).open));}catch{}});}
 $('save-v3-settings').onclick=async()=>{if(v3Blocked()||!v3Enabled())return;const distance=$('v3-setting-distance').value.trim(),presets=[0,1,2].map(i=>Number($('v3-preset-'+i).value));if(!distance||!Number.isFinite(Number(distance))||Number(distance)<=0||presets.some(n=>!Number.isInteger(n)||n<1||n>20)){error('Нужен положительный отступ и три целых количества от 1 до 20.');return;}const revision=view.revision,scope=view.scope,action={type:'v3_settings',cycle_id:view.cycle_id,entry_distance:distance,count_presets:presets};if(!confirm('Сохранить настройки V3?\nОтступ от Close: '+distance+'\nПресеты ступеней: '+presets.join(', ')))return;try{await send(action,revision,scope);}catch(e){error(e.message);}};
 $('v3-submit-entry').onclick=async()=>{if(v3Blocked()||!v3Enabled()||view.v3_controls?.can_enter!==true)return;try{const action={type:'v3_entry',cycle_id:view.cycle_id,order_type:$('v3-order-type').value,timing:$('v3-timing').value,side:$('v3-side').value,count:v3Count()},revision=view.revision,scope=view.scope;if(action.order_type==='STOP'&&action.timing==='now')throw Error('STOP выставляется только после текущей или следующей M1.');const timing=action.timing==='now'?'Сейчас':action.timing==='current'?'После закрытия текущей M1':'После закрытия следующей M1';if(!confirm('ТОРГОВЛЯ'+' — '+action.order_type+', '+action.count+' × 0.01 лота\n'+timing+'\nНаправление: '+(action.timing==='now'?action.side:'по цвету свечи; doji — пропуск')+(action.order_type==='STOP'?'\nОтступ от Close: '+view.v3_settings.entry_distance:'\nЦена определяется рыночным исполнением')+'\nПосле исполнения бот сопровождает корзину.'))return;await send(action,revision,scope);}catch(e){error(e.message);}};
 $('v3-cancel-request').onclick=async()=>{if(v3Blocked()||!v3Enabled()||!view.v3_request)return;const action={type:'v3_cancel_request',cycle_id:view.cycle_id},revision=view.revision,scope=view.scope;if(!confirm('Отменить запланированный вход по свече? Уже исполненные позиции не закрываются.'))return;try{await send(action,revision,scope);}catch(e){error(e.message);}};
 $('v3-link-toggle').onclick=async()=>{if(v3Blocked()||!v3Enabled())return;try{const cancel=!!view.v3_link;if(cancel?view.v3_controls?.can_cancel_link!==true:view.v3_controls?.can_link!==true)return;const action=cancel?{type:'v3_cancel_link',cycle_id:view.cycle_id}:{type:'v3_link',cycle_id:view.cycle_id,count:v3Count(),expected_positions:view.positions.map(p=>p.id).sort()},revision=view.revision,scope=view.scope;if(!cancel&&!confirm('Выставить '+action.count+' встречных STOP × 0.01 на SL первой ступени? Они следуют за её SL; исполнение и отмена не атомарны.'))return;await send(action,revision,scope);}catch(e){error(e.message);}};
 async function v3AdjustSl(delta){if(v3Blocked()||!v3Enabled()||view.v3_controls?.can_adjust_sl!==true)return;const old=Number(view.v3_sl_distance),distance=(Math.round(old*100)+delta)/100;if(!Number.isFinite(distance)||distance<=0)return;const action={type:'v3_adjust_sl',cycle_id:view.cycle_id,distance:distance.toFixed(2),expected_positions:view.positions.map(p=>p.id).sort()},revision=view.revision,scope=view.scope;if(!confirm('Изменить расстояние SL действующих позиций: '+old.toFixed(2)+' → '+distance.toFixed(2)+'?\nСвязанные триггеры следуют за SL первой ступени. Частичный отказ требует сверки.'))return;try{await send(action,revision,scope);}catch(e){error(e.message);}}
 $('v3-sl-minus').onclick=()=>v3AdjustSl(-10);$('v3-sl-plus').onclick=()=>v3AdjustSl(10);

 function fields(){if(!view)return;v3FillSettings();$('fields').replaceChildren();for(const [k,v] of Object.entries(view.settings)){if(['EXPANSION_DISTANCE','TICKET_SIZE'].includes(k)||(view.tp_distances&&['COST_RESERVE','TP_STEP_MULTIPLIER'].includes(k)))continue;const label=document.createElement('label');label.textContent=names[k]??k;const input=document.createElement('input');input.id='setting-'+k;input.value=v;input.inputMode='decimal';label.append(input);const active=document.createElement('small');active.textContent='В цикле: '+(view.active_settings?.[k]??'—');label.append(active);$('fields').append(label);} $('tp-grid-settings').hidden=!view.tp_distances;$('tp-fields').replaceChildren();if(view.tp_distances)for(let i=0;i<Number(view.settings.BASKET_COUNT);i++){const label=document.createElement('label');label.textContent='TP '+(i+1)+(i===0?' · обязательно':' · пусто = авто');const input=document.createElement('input');input.id='tp-distance-'+i;input.inputMode='decimal';input.value=view.tp_distances[i]??'';label.append(input);const active=document.createElement('small');active.textContent='В цикле: '+(view.active_tp_distances?.[i]??'авто / не задано');label.append(active);$('tp-fields').append(label);}}
 $('refresh-settings').onclick=()=>{fields();error('Загружены актуальные параметры.');};
 $('save-settings').onclick=async()=>{if(!view)return;const settings={};for(const k of Object.keys(view.settings)){if(['EXPANSION_DISTANCE','TICKET_SIZE'].includes(k)||(view.tp_distances&&['COST_RESERVE','TP_STEP_MULTIPLIER'].includes(k))){settings[k]=view.settings[k];continue;}const raw=$('setting-'+k).value.trim();if(raw===''||!Number.isFinite(Number(raw))){error('Проверьте числовое значение: '+(names[k]??k));return;}settings[k]=k==='BASKET_COUNT'?Number(raw):raw;}const revision=view.revision,scope=view.scope;if(!confirm('Применить параметры? Объём открытых билетов не меняется.\n'+Object.entries(settings).filter(([k])=>!['EXPANSION_DISTANCE','TICKET_SIZE'].includes(k)).map(([k,v])=>(names[k]??k)+': '+v).join('\n')))return;try{await send({type:'basket_settings',settings,cycle_id:view.cycle_id},revision,scope);}catch(e){error(e.message);}};
 $('writeoff-recovery').onclick=async()=>{if(!view)return;const v=view,revision=v.revision,scope=v.scope;const action={type:'writeoff_recovery',cycle_id:v.cycle_id,expected_debt:v.debt,confirmed:true};if(!confirm('ТОРГОВЛЯ'+' — списать RECOVERY '+v.debt+' UST? Фактическая доходность не изменится: убыток уже учтён. Позиции и ордера не закрываются. После сверки начнётся ожидание новой M1.'))return;try{await send(action,revision,scope);}catch(e){error(e.message);}};
 $('save-tp-grid').onclick=async()=>{if(!view?.tp_distances)return;const values=[...view.tp_distances],scope=view.scope,revision=view.revision,cycle_id=view.cycle_id;for(let i=0;i<Number(view.settings.BASKET_COUNT);i++){const raw=$('tp-distance-'+i).value.trim();if((i===0&&!raw)||(raw&&(!Number.isFinite(Number(raw))||Number(raw)<=0))){error('Задайте положительное расстояние TP '+(i+1));return;}values[i]=raw||null;}if(!confirm('Применить расстояния TP? SL остаются на фактических текущих уровнях.\n'+values.slice(0,Number(view.settings.BASKET_COUNT)).map((v,i)=>'TP '+(i+1)+': '+(v??'авто')).join('\n')))return;try{await send({type:'tp_distances',cycle_id,distances:values},revision,scope);}catch(e){error(e.message);}};
 function node(tag,attrs,text){const n=document.createElementNS(ns,tag);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);if(text!==undefined)n.textContent=text;return n;}
 // Viewport changes only: never request or alter orders while navigating history.
 function visibleCandles(){const all=view?.feed?.candles??[];const count=Math.max(10,Math.min(240,Math.round(visibleCount)));timeOffset=Math.max(0,Math.min(timeOffset,Math.max(0,all.length-count)));const end=all.length-Math.round(timeOffset);return all.slice(Math.max(0,end-count),end);}
 function domain(){const prices=[...lines.map(l=>l.price),...visibleCandles().flatMap(c=>[Number(c.low),Number(c.high)]),Number(view?.feed?.quote?.bid),Number(view?.feed?.quote?.ask)].filter(x=>Number.isFinite(x)&&x>0);let lo=Math.min(...prices),hi=Math.max(...prices);if(!prices.length){lo=0;hi=1;}const mid=(lo+hi)/2+pan,span=Math.max(1,hi-lo)*1.25/zoom;return{min:mid-span/2,max:mid+span/2};}
 function syncPreviewDialog(){
  const open=!!preview;$('preview-backdrop').hidden=!open;
  for(const id of ['command-drawer','settings-page','bottom-tabs','analysis-pane','bot-activity'])$(id).inert=open;
  if(open&&!previewWasOpen){previewFocus=document.activeElement;$('cancel-level').focus({preventScroll:true});}
  if(!open&&previewWasOpen)(previewFocus?.isConnected?previewFocus:$('drawer-toggle')).focus({preventScroll:true});
  previewWasOpen=open;
 }
 function draw(){
  syncPreviewDialog();
  if(!view)return;lines=[];const colors=prefs.colors;
  for(const [side,price] of Object.entries(view.levels)){const pendingOrder=view.orders.some(o=>o.side===side&&Number(o.price)===Number(price));if(pendingOrder||view.frozen_levels)lines.push({key:'trigger-'+side,kind:'trigger',side,price:Number(price),label:side+(pendingOrder?' STOP':' · уровень цикла'),color:colors[side]});}
  for(const p of view.positions)for(const key of ['entry','tp','sl'])lines.push({key:p.id+'-'+key,kind:key,pid:p.id,price:Number(p[key]),label:key.toUpperCase()+' #'+(p.ticket??p.id).slice(-6),color:colors[key]});
  if(!lines.length&&!(view.feed.candles??[]).length&&!Number(view.feed.quote?.bid)){svg.replaceChildren(node('text',{x:230,y:250,fill:'#969ba6','font-size':28},'Ожидание реальных данных MT5'));$('level-buttons').replaceChildren();drawAnalysis();return;}
  frameHeight=Math.max(240,svg.clientHeight/Math.max(1,svg.clientWidth)*1000);plotBottom=frameHeight-45;plotHeight=plotBottom-20;svg.setAttribute('viewBox','0 0 1000 '+frameHeight);
  const labelSize=innerWidth<680?32:17;const labelRows=[];
  const d=domain(),y=p=>plotBottom-(p-d.min)/(d.max-d.min)*plotHeight;svg.replaceChildren();
  for(let i=0;i<=6;i++){const price=d.min+(d.max-d.min)*i/6,py=y(price);svg.append(node('line',{x1:0,x2:855,y1:py,y2:py,stroke:colors.grid}),node('text',{x:868,y:py+5,fill:'#afc0d2','font-size':labelSize},price.toFixed(2)));}
  const candles=visibleCandles(),w=840/Math.max(1,candles.length);svg.dataset.firstCandle=String(candles[0]?.utc_time??'');svg.dataset.visibleCount=String(candles.length);candles.forEach((c,i)=>{const x=10+i*w,green=Number(c.close)>=Number(c.open),color=green?colors.up:colors.down;svg.append(node('line',{x1:x,x2:x,y1:y(Number(c.high)),y2:y(Number(c.low)),stroke:color}),node('rect',{x:x-w*.3,y:Math.min(y(Number(c.open)),y(Number(c.close))),width:Math.max(2,w*.6),height:Math.max(1,Math.abs(y(Number(c.open))-y(Number(c.close)))),fill:color}));});
  for(const l of lines){const py=y(l.price);if(py<0||py>frameHeight)continue;const g=node('g',{'data-line':l.key,visibility:prefs.mainCycle?'visible':'hidden'});g.append(node('line',{x1:0,x2:855,y1:py,y2:py,stroke:l.color,'stroke-width':2,'stroke-dasharray':'none'}),node('line',{x1:0,x2:855,y1:py,y2:py,stroke:'transparent','stroke-width':18}));g.append(node('title',{},l.label+' '+l.price.toFixed(2)));if(!labelRows.some(row=>Math.abs(row-py)<labelSize)){g.append(node('text',{x:868,y:py-5,fill:l.color,'font-size':labelSize-2},l.price.toFixed(2)));labelRows.push(py);}svg.append(g);}
  // Bid/Ask are display-only live quote lines, never draggable trading levels.
  const quoteLabels=[];
  for(const [key,color] of [['ask','#ff5b63'],['bid','#24d99a']]){
   const price=Number(view.feed.quote?.[key]);if(!Number.isFinite(price)||price<=0)continue;
   const py=y(price);if(py<20||py>plotBottom)continue;
   let ly=py;for(const previous of quoteLabels)if(Math.abs(ly-previous)<labelSize+8)ly=previous+labelSize+8;
   ly=Math.max(20+labelSize/2,Math.min(plotBottom-labelSize/2,ly));if(quoteLabels.some(previous=>Math.abs(ly-previous)<labelSize+8))ly=Math.max(20+labelSize/2,quoteLabels[0]-labelSize-8);quoteLabels.push(ly);
   const badgeX=innerWidth<680?798:845;
   const g=node('g',{'data-quote':key,'pointer-events':'none'});
   g.append(node('line',{x1:0,x2:badgeX-5,y1:py,y2:py,stroke:color,'stroke-width':1.5,'stroke-dasharray':'3 4'}),node('line',{x1:badgeX-5,x2:badgeX,y1:py,y2:ly,stroke:color}),node('rect',{x:badgeX,y:ly-labelSize/2-5,width:998-badgeX,height:labelSize+10,rx:7,fill:'#08090b',stroke:color}),node('text',{x:badgeX+7,y:ly+labelSize/3,fill:'#f1f3f5','font-size':labelSize*.9},key.toUpperCase()+' '+price.toFixed(2)));svg.append(g);
  }
  candles.forEach((c,i)=>{if(i%Math.max(1,Math.floor(candles.length/5))===0&&utcLabel(c.utc_time,true)){const label=utcLabel(c.utc_time,true);svg.append(node('text',{x:10+i*w,y:frameHeight-10,fill:'#969ba6','font-size':labelSize},label));}});
  svg.append(node('text',{x:868,y:frameHeight-10,fill:'#969ba6','font-size':labelSize},feedUtcVerified()?'UTC':'RAW'));
  if(!feedUtcVerified())svg.append(node('title',{},'Время MT5 не проверено; часовые метки скрыты.'));
  if(!candles.length)svg.append(node('text',{x:300,y:250,fill:'#969ba6','font-size':20},'Ожидание данных MT5'));
  if(preview){const py=y(preview.value);svg.append(node('line',{x1:0,x2:855,y1:py,y2:py,stroke:'#fff','stroke-width':3,'stroke-dasharray':'none'}));}
  drawAnalysis();
  $('level-buttons').replaceChildren();for(const l of lines){const b=document.createElement('button');b.textContent=l.label+' '+l.price.toFixed(2);b.style.borderColor=l.color;b.disabled=l.kind==='entry';b.onclick=()=>openPreview(l,l.price);$('level-buttons').append(b);}
 }
 function openPreview(line,value){if(!view||pending)return;$('preview-error').textContent='';preview={line:{...line},value,backend:structuredClone(view)};$('preview').hidden=false;$('new-price').value=value.toFixed(2);$('preview-text').textContent=line.label+': '+line.price.toFixed(2)+' → '+value.toFixed(2);draw();}
 function priceAt(clientY){const r=svg.getBoundingClientRect(),d=domain(),point=svg.createSVGPoint();point.x=r.left;point.y=clientY;const local=point.matrixTransform(svg.getScreenCTM().inverse());return Math.round((d.min+(plotBottom-local.y)/plotHeight*(d.max-d.min))*100)/100;}
 const pointers=new Map();let pinch=null;
 const distanceBetween=points=>Math.hypot(points[0].x-points[1].x,points[0].y-points[1].y);
 function cancelGesture(){clearTimeout(hold);pointers.clear();pinch=null;gesture=null;}
 svg.addEventListener('pointerdown',e=>{
  if(preview)return;svg.setPointerCapture(e.pointerId);pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});
  if(pointers.size>1){clearTimeout(hold);gesture=null;pinch={distance:distanceBetween([...pointers.values()]),count:visibleCount,zoom};return;}
  const l=lines.find(l=>l.key===e.target.closest('[data-line]')?.dataset.line);
  gesture={id:e.pointerId,start:e.clientY,startX:e.clientX,pan,offset:timeOffset,span:domain().max-domain().min,line:l,drag:false,moved:false};
  if(l&&l.kind!=='entry')hold=setTimeout(()=>{if(gesture&&!gesture.moved&&gesture.id===e.pointerId){gesture.drag=true;openPreview(l,l.price);}},500);
 });
 svg.addEventListener('pointermove',e=>{
  if(!pointers.has(e.pointerId))return;pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});
  if(pinch&&pointers.size===2){const ratio=distanceBetween([...pointers.values()])/Math.max(1,pinch.distance);visibleCount=Math.max(10,Math.min(240,pinch.count/ratio));draw();return;}
  if(!gesture||gesture.id!==e.pointerId)return;
  if(gesture.drag&&preview){preview.value=priceAt(e.clientY);$('new-price').value=preview.value.toFixed(2);$('preview-text').textContent=preview.line.label+': '+preview.line.price.toFixed(2)+' → '+preview.value.toFixed(2);draw();return;}
  const dx=e.clientX-gesture.startX,dy=e.clientY-gesture.start;if(Math.hypot(dx,dy)<=8&&!gesture.moved)return;
  clearTimeout(hold);gesture.moved=true;const r=svg.getBoundingClientRect();
  // Drag candles to the right to reveal earlier data. Forward vertical swipes also reveal history.
  if(Math.abs(dx)>=Math.abs(dy))timeOffset=gesture.offset+dx/Math.max(1,r.width*.84)*visibleCount;
  else if(e.clientX-r.left>r.width*.855)pan=gesture.pan+dy/Math.max(1,r.height)*gesture.span;
  else timeOffset=gesture.offset-dy/Math.max(1,r.height)*visibleCount;
  draw();
 });
 const end=e=>{clearTimeout(hold);pointers.delete(e.pointerId);if(e.type==='pointercancel'&&gesture?.drag){preview=null;$('preview').hidden=true;}gesture=null;if(pointers.size<2)pinch=null;draw();};
 svg.addEventListener('pointerup',end);svg.addEventListener('pointercancel',end);svg.addEventListener('lostpointercapture',end);
 svg.addEventListener('wheel',e=>{e.preventDefault();if(preview)return;visibleCount=Math.max(10,Math.min(240,visibleCount*(e.deltaY>0?1.12:1/1.12)));draw();},{passive:false});
 $('zoom-in').onclick=()=>{visibleCount=Math.max(10,visibleCount/1.3);draw();};$('zoom-out').onclick=()=>{visibleCount=Math.min(240,visibleCount*1.3);draw();};$('chart-reset').onclick=()=>{zoom=1;pan=0;timeOffset=0;visibleCount=60;draw();};
 $('cancel-level').onclick=()=>{preview=null;$('preview').hidden=true;draw();};
 $('apply-level').onclick=async()=>{if(!preview)return;const p=preview,value=Number($('new-price').value);if(!Number.isFinite(value)||value<=0){error('Цена должна быть положительным числом.');return;}let action;if(p.line.kind==='trigger'){action={type:'basket_levels',cycle_id:p.backend.cycle_id,expected_orders:p.backend.orders.map(o=>o.id),expected:p.backend.levels,levels:{...p.backend.levels,[p.line.side]:String(value)}};}else{const position=p.backend.positions.find(x=>x.id===p.line.pid);action={type:'protection',cycle_id:p.backend.cycle_id,position_id:position.id,old:{sl:position.sl,tp:position.tp},new:{sl:position.sl,tp:position.tp,[p.line.kind]:String(value)}};}if(!confirm('Подтвердить '+p.line.label+' → '+value+'? SL меняется для всех живых билетов этой корзины, TP выбранного билета фиксируется вручную; незаданные TP пересчитываются для покрытия долга. Другие ручные TP сохраняются. Недопустимая сетка отклоняется.'))return;try{await send(action,p.backend.revision,p.backend.scope);preview=null;$('preview').hidden=true;}catch(e){error(e.message);}draw();};
 function render(){
  connectionLabel();updateBotActivity();
  $('phase').textContent=view.phase;$('cycle').textContent=view.cycle_id??'Ожидание цикла';$('debt').textContent=view.debt+' UST';$('realized').textContent='Фактический net: '+view.realized+' UST';$('feed-status').textContent=view.feed.status;$('mode').textContent=view.read_only?'READ_ONLY':view.stopped?'Новые входы остановлены':'EXECUTION';
  $('safety').textContent=view.migration_block??view.manual_reason??timeSafetyText();$('quote').textContent='Bid '+(view.feed.quote?.bid??'—')+' / Ask '+(view.feed.quote?.ask??'—')+' · возраст '+(view.feed.age_seconds??'—')+' сек.';
  $('state-heading').textContent='Брокерское состояние';
  $('execution-note').textContent='Открытые билеты сохраняют брокерские SL/TP. Закрытие браузера не останавливает сервис.';
  $('source-label').textContent='Источник: MT5 / торговый контур'+' · '+(utcLabel(view.feed.timestamp)||'время MT5 не проверено');
  $('reverse-status').textContent=(view.reverse_ladders??[]).some(x=>x.enabled)?'Обратная лестница включена. Фактические SL показаны на графике; автоматического подтягивания нет.':'Обратная лестница выключена; доступна после подтверждённого переноса SL по TP1.';
  $('projection').textContent=view.projection?'Прибыль по текущим TP: '+view.projection.gross_tp+'; резерв расходов: '+view.projection.cost_reserve+'; ожидаемое недопокрытие: '+view.projection.shortfall+' UST.'+' Будущие комиссии неизвестны до подтверждения.':'Расчёт покрытия пока недоступен.';
  $('edit-status').textContent=view.edit?'Правка '+view.edit.stage+': показываются фактические данные брокера. При частичном отказе автоматизация остановится.':'';$('journal').textContent=view.journal.join('\n');
  $('tickets').replaceChildren();for(const p of [...view.positions.map(p=>({...p,label:'Позиция'})),...view.orders.map(p=>({...p,entry:p.price,label:'STOP'}))]){const row=document.createElement('tr');for(const text of[p.ticket??p.id,p.label,p.side+' / '+p.size,p.entry,p.tp,p.sl]){const td=document.createElement('td');td.textContent=text;row.append(td);}$('tickets').append(row);}
  $('request-status').textContent=pending?'Запрос '+pending+': '+(pendingScope===view.scope?view.action_results[pending]?.status??'ожидание':'другой контекст')+'. До сверки новые команды запрещены. При UNKNOWN проверьте журнал и брокера на VPS; перезагрузка не разрешает повтор.':'';
  for(const id of ['start','stop','resume','reset','cancel-initial','save-settings','reverse-enable','reverse-disable','save-tp-grid','apply-level'])$(id).disabled=!!pending||busy;
  $('own-entry-sl').disabled=!!pending||busy||view.read_only||!view.positions.length||(!v3Enabled()||view.v3_controls?.can_own_entry_sl!==true)||!!view.edit||!!view.manual_close||!!view.manual_reason||!!view.migration_block||!!view.time_block;
  $('writeoff-recovery').disabled=!!pending||busy||view.read_only||!view.stopped||Number(view.debt)<=0||view.positions.length>0||view.orders.length>0;
  $('writeoff-history').textContent=(view.recovery_writeoffs??[]).map(x=>'Списано '+x.amount+' '+x.currency+' · '+new Date(x.at_utc*1000).toISOString()).join(' | ');
  $('reverse-enable').disabled=!!pending||busy||view.reverse_controls?.can_enable!==true;
  $('reverse-disable').disabled=!!pending||busy||view.reverse_controls?.can_disable!==true;
  $('reverse-enable').title=view.reverse_controls?.reason??'';$('reverse-disable').title=view.reverse_controls?.reason??'';
  if(loadedScope!==view.scope){fields();loadedScope=view.scope;}v3Render();draw();
 }
 async function poll(){if(polling||busy)return;polling=true;try{const r=await fetch(apiPath('state'),{headers:headers(),signal:AbortSignal.timeout(2500)});if(!r.ok){if([401,403].includes(r.status)){$('login').hidden=false;$('workspace').hidden=true;}throw Error('Нет авторизованного снимка');}const v=await r.json();if(v.contract!=='ticket_basket_v1'||v.mode==='PAPER')throw Error('Несовместимая версия контроллера');view=v;v3Fresh=true;const status=pending&&pendingScope===v.scope?v.action_results[pending]?.status:null;if(['APPLIED','REJECTED','CONFLICT'].includes(status)){lastActionReceipt={status,reason:v.action_results[pending]?.reason};if(status!=='APPLIED')error(status==='CONFLICT'?'Снимок устарел; создайте новый предпросмотр.':(v.action_results[pending]?.reason??'Команда отклонена до исполнения. Проверьте параметры; очередь разблокирована.'));clearPending();}$('login').hidden=true;$('workspace').hidden=false;render();}catch(e){v3Fresh=false;if(view){v3Render();connectionLabel();updateBotActivity();}$('feed-status').textContent='Нет свежего снимка';}finally{polling=false;}}
 const defaultPrefs={mainTrend:false,mainCycle:true,pane:null,pattern:{trend:false,cycle:true},figure:{trend:false,cycle:true},liquidity:false,opacity:20,colors:{BUY:'#26a69a',SELL:'#ef5350',sl:'#ef5350',tp:'#26a69a',entry:'#e0b565',up:'#26a69a',down:'#ef5350',grid:'#25282e',zone:'#e0b565'}};
 let prefs=structuredClone(defaultPrefs);try{const saved=JSON.parse(localStorage.getItem('chart-preferences-v1')??'null');if(saved){for(const k of ['mainTrend','mainCycle','liquidity'])if(typeof saved[k]==='boolean')prefs[k]=saved[k];if(['pattern','figure',null].includes(saved.pane))prefs.pane=saved.pane;for(const pane of ['pattern','figure'])for(const k of ['trend','cycle'])if(typeof saved[pane]?.[k]==='boolean')prefs[pane][k]=saved[pane][k];if(Number.isFinite(saved.opacity))prefs.opacity=Math.max(0,Math.min(100,saved.opacity));for(const k of Object.keys(prefs.colors))if(/^#[0-9a-f]{6}$/i.test(saved.colors?.[k]))prefs.colors[k]=saved.colors[k];}}catch{}
 const savePrefs=()=>{localStorage.setItem('chart-preferences-v1',JSON.stringify(prefs));layout();draw();};
 function layout(){$('analysis-pane').hidden=!prefs.pane;$('chart-workspace').classList.toggle('split',!!prefs.pane);$('main-trend').checked=prefs.mainTrend;$('main-cycle').checked=prefs.mainCycle;$('liquidity-visible').checked=prefs.liquidity;$('zone-opacity').value=prefs.opacity;if(prefs.pane){$('analysis-title').textContent=prefs.pane==='pattern'?'Свечные паттерны':'Графические фигуры';$('analysis-trend').checked=prefs[prefs.pane].trend;$('analysis-cycle').checked=prefs[prefs.pane].cycle;}}
 function drawAnalysis(){const target=$('analysis-chart');target.replaceChildren();if(!prefs.pane)return;const height=Math.max(240,target.clientHeight/Math.max(1,target.clientWidth)*1000),ratio=(height-65)/plotHeight;target.setAttribute('viewBox','0 0 1000 '+height);for(const child of svg.children){if(child.hasAttribute('data-line')&&!prefs[prefs.pane].cycle)continue;const copy=child.cloneNode(true);if(copy.hasAttribute('data-line'))copy.setAttribute('visibility','visible');for(const element of [copy,...copy.querySelectorAll('*')]){for(const attr of ['y','y1','y2'])if(element.hasAttribute(attr)){const value=Number(element.getAttribute(attr));element.setAttribute(attr,value>=frameHeight-15?height-10:20+(value-20)*ratio);}if(element.tagName==='rect'&&element.hasAttribute('height'))element.setAttribute('height',Number(element.getAttribute('height'))*ratio);}target.append(copy);}}

 $('open-pattern').onclick=()=>{prefs.pane=prefs.pane==='pattern'?null:'pattern';savePrefs();};$('open-figure').onclick=()=>{prefs.pane=prefs.pane==='figure'?null:'figure';savePrefs();};$('close-analysis').onclick=()=>{prefs.pane=null;savePrefs();};
 for(const [id,key]of [['main-trend','mainTrend'],['main-cycle','mainCycle'],['liquidity-visible','liquidity']])$(id).onchange=e=>{prefs[key]=e.target.checked;savePrefs();};
 for(const key of ['trend','cycle'])$('analysis-'+key).onchange=e=>{if(prefs.pane){prefs[prefs.pane][key]=e.target.checked;savePrefs();}};
 $('zone-opacity').oninput=e=>{prefs.opacity=Number(e.target.value);savePrefs();};
 const colorNames={BUY:'BUY STOP',SELL:'SELL STOP',sl:'SL',tp:'TP',entry:'Вход',up:'Растущая свеча',down:'Падающая свеча',grid:'Сетка',zone:'Будущие зоны'};
 for(const [k,v]of Object.entries(prefs.colors)){const label=document.createElement('label');label.textContent=colorNames[k];const input=document.createElement('input');input.type='color';input.id='color-'+k;input.value=v;input.oninput=()=>{prefs.colors[k]=input.value;savePrefs();};label.append(input);$('color-fields').append(label);}
 let logPage=0;
 async function showLogs(){const scope=$('log-scope').value;try{const r=await fetch('/api/logs?scope='+encodeURIComponent(scope)+'&page='+logPage,{headers:{'X-Local-Control':'1'},signal:AbortSignal.timeout(4000)});if(!r.ok)throw Error('Логи недоступны: '+r.status);const v=await r.json();if(scope!==$('log-scope').value)return;$('logs-view').textContent=v.text;$('logs-status').textContent='Страница '+(v.page+1)+' · ограниченная выгрузка';$('logs-prev').disabled=v.page===0;$('logs-next').disabled=!v.has_more;}catch(e){$('logs-status').textContent=e.message;}}
 $('logs-refresh').onclick=()=>{logPage=0;showLogs();};$('log-scope').onchange=()=>{logPage=0;showLogs();};$('logs-prev').onclick=()=>{logPage=Math.max(0,logPage-1);showLogs();};$('logs-next').onclick=()=>{logPage++;showLogs();};
 $('logs-download').onclick=async()=>{const scope=$('log-scope').value;try{const r=await fetch('/api/logs/download?scope='+encodeURIComponent(scope),{headers:{'X-Local-Control':'1'},signal:AbortSignal.timeout(5000)});if(!r.ok)throw Error('Скачивание отклонено: '+r.status);const blob=await r.blob();if(blob.size>1048576)throw Error('Файл превысил лимит');const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='tradfi-'+scope+'-logs.txt';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);$('logs-status').textContent='Подготовлен файл UTF-8: '+a.download;}catch(e){$('logs-status').textContent=e.message;}};
 // Local display preference only; each controller role has its own storage key.
 const chartHeightKey=storageKey('chart-height-v1');
 function normalizeChartHeight(value){
  const percent=typeof value?.percent==='number'&&Number.isFinite(value.percent)?Math.max(30,Math.min(85,Math.round(value.percent/5)*5)):60;
  return{mode:value?.mode==='custom'?'custom':'auto',percent};
 }
 let chartHeight=normalizeChartHeight(null);
 try{chartHeight=normalizeChartHeight(JSON.parse(localStorage.getItem(chartHeightKey)??'null'));}catch{}
 function applyChartHeight(save=false){
  chartHeight=normalizeChartHeight(chartHeight);
  $('chart-page').dataset.chartHeight=chartHeight.mode;
  $('chart-page').style.setProperty('--chart-height',chartHeight.percent+'vh');
  $('chart-page').style.setProperty('--chart-height-dynamic',chartHeight.percent+'dvh');
  $('chart-height-mode').value=chartHeight.mode;$('chart-height-range').value=String(chartHeight.percent);
  $('chart-height-range').disabled=chartHeight.mode!=='custom';$('chart-height-value').textContent=chartHeight.percent+'%';
  $('chart-height-status').textContent=chartHeight.mode==='auto'?'График занимает доступную высоту.':'Выбрано '+chartHeight.percent+'% высоты экрана.';
  if(save){try{localStorage.setItem(chartHeightKey,JSON.stringify(chartHeight));}catch{$('chart-height-status').textContent+=' Сохранение недоступно: выбор действует до перезагрузки.';}}
  requestAnimationFrame(draw);
 }
 $('chart-height-mode').onchange=()=>{chartHeight.mode=$('chart-height-mode').value;applyChartHeight(true);};
 $('chart-height-range').oninput=()=>{chartHeight.percent=Number($('chart-height-range').value);applyChartHeight(true);};
 $('chart-height-reset').onclick=()=>{chartHeight.mode='auto';applyChartHeight(true);};
 applyChartHeight();
 // Presentation-only navigation: the LIVE role and API paths never change.
 let activeTab='chart';
 function setTab(tab){if(preview)return;cancelGesture();activeTab=tab==='settings'?'settings':'chart';for(const name of ['chart','settings']){$(name+'-page').hidden=activeTab!==name;$('tab-'+name).setAttribute('aria-selected',String(activeTab===name));}requestAnimationFrame(draw);}
 $('tab-chart').onclick=()=>setTab('chart');$('tab-settings').onclick=()=>setTab('settings');
 for(const name of ['chart','settings'])$('tab-'+name).addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();const other=name==='chart'?'settings':'chart';setTab(other);$('tab-'+other).focus();}});
 function setDrawer(open){$('command-drawer').dataset.open=String(open);$('drawer-toggle').setAttribute('aria-expanded',String(open));$('drawer-content').hidden=!open;requestAnimationFrame(draw);}
 $('drawer-toggle').onclick=()=>setDrawer($('command-drawer').dataset.open!=='true');
 let drawerY=null,drawerSwiped=false;
 $('drawer-toggle').addEventListener('pointerdown',e=>{drawerY=e.clientY;drawerSwiped=false;$('drawer-toggle').setPointerCapture(e.pointerId);});
 $('drawer-toggle').addEventListener('pointerup',e=>{if(drawerY!==null&&Math.abs(e.clientY-drawerY)>22){drawerSwiped=true;setDrawer(e.clientY<drawerY);}drawerY=null;});
 $('drawer-toggle').addEventListener('click',e=>{if(drawerSwiped){e.stopImmediatePropagation();drawerSwiped=false;}},true);
 $('drawer-toggle').addEventListener('pointercancel',()=>{drawerY=null;drawerSwiped=false;});
 document.addEventListener('keydown',e=>{if(preview&&e.key==='Tab'){const controls=[...$('preview').querySelectorAll('input,button')].filter(x=>!x.disabled),current=controls.indexOf(document.activeElement),next=e.shiftKey?(current<=0?controls.length-1:current-1):(current+1)%controls.length;e.preventDefault();controls[next]?.focus();return;}if(e.key==='Escape'){if(preview){$('cancel-level').click();}else if($('command-drawer').dataset.open==='true'){setDrawer(false);$('drawer-toggle').focus();}else setTab('chart');}});
 // Reflow the SVG after drawer resizing and device rotation without affecting its data.
 if(typeof ResizeObserver!=='undefined')new ResizeObserver(()=>requestAnimationFrame(draw)).observe($('chart-workspace'));
 layout();window.addEventListener('resize',()=>requestAnimationFrame(draw));
 setInterval(poll,1000);poll();
})();
