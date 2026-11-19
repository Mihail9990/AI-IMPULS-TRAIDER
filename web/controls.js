import {RecoveryActions,createCommandDiagnostics,DIAGNOSTIC_LABELS} from './recovery-actions.mjs';
import {gate,rangeInput,lotSchedule,lotScheduleInput} from './control-policy.mjs';
function init(){
 const view=window.RecoveryView;if(!view||window.recoveryControlsInitialized)return;window.recoveryControlsInitialized=true;
 const $=id=>document.getElementById(id);let active=false,confirmAnswer=null,seenContext=null,lotBase=null,lotDirty=false;
 const storage={getItem:k=>localStorage.getItem(k),setItem:(k,v)=>localStorage.setItem(k,v),removeItem:k=>localStorage.removeItem(k)};
 const names={lotSchedule:'Сохранить шкалу лотов RECOVERY',ranges:'Выставить новые диапазоны',cancel:'Отменить все диапазоны',close1:'Закрыть позицию и отменить диапазон №1',close2:'Закрыть позицию и отменить диапазон №2'};
 const modes={real:'Реальный счёт',demo:'Демо-счёт',contest:'Конкурсный счёт',unknown:'Тип счёта не подтверждён'};
 const diagnostics=createCommandDiagnostics();
 function renderDiagnostics(){
  const rows=diagnostics.read(), log=$('diagnostic-log'), isEmpty=rows.length===0;
  log.value=rows.map(row=>row.time+'  '+row.code+(row.request_id?'  '+row.request_id:'')+'\n'+DIAGNOSTIC_LABELS[row.code]).join('\n\n');
  log.hidden=isEmpty;$('diagnostic-empty').hidden=!isEmpty;
  $('diagnostic-copy').disabled=isEmpty;$('diagnostic-clear').disabled=isEmpty;
  if(document.activeElement!==log)log.scrollTop=log.scrollHeight;
 }
 function onDiagnostic(event){try{if(diagnostics.append(event))renderDiagnostics();}catch{}}
 $('diagnostic-clear').addEventListener('click',()=>{diagnostics.clear();renderDiagnostics();$('diagnostic-status').textContent='Диагностика очищена. Журнал команд не изменён.';});
 $('diagnostic-copy').addEventListener('click',async()=>{
  const log=$('diagnostic-log');if(!log.value)return;
  try{await navigator.clipboard.writeText(log.value);$('diagnostic-status').textContent='Диагностика скопирована.';}
  catch{log.focus();log.select();$('diagnostic-status').textContent='Выделенный журнал можно скопировать вручную.';}
 });
 const safeOrigin=()=>location.protocol==='https:'||(location.protocol==='http:'&&['localhost','127.0.0.1','[::1]'].includes(location.hostname));
 function confirm(info){return new Promise(resolve=>{
  if(!active||!safeOrigin()){resolve(false);return;}confirmAnswer=resolve;
  const lines=[names[info.action],'Сервер: '+location.host,'Счёт: '+modes[info.accountMode]];
  if(info.action==='ranges'){for(const r of info.body.ranges)lines.push('Диапазон №'+r.id+': '+r.side+' по '+r.price+' · 0,01 лота');lines.push('Исходный SL: '+info.body.initial_sl_distance,'После SL поддержки: '+info.body.reentry_distance,'Прежние диапазоны будут заменены.');}
  else if(info.action==='lotSchedule'){
   for(let i=1;i<24;i++)if(info.body.lot_schedule[i]!==info.body.expected_lot_schedule[i])lines.push('После '+i+' полных SL: '+info.body.expected_lot_schedule[i]+' → '+info.body.lot_schedule[i]+' лота');
   lines.push('Первый вход: 0,01. Изменение не меняет существующие ордера.');
  }
  else if(info.action==='cancel')lines.push('Все действующие диапазоны и их отложенные ордера будут отменены.');
  else lines.push('Позиция выбранного диапазона будет закрыта, его триггеры отменены. Другой диапазон не изменится.');
  lines.push('ID команды: '+info.body.request_id);
  $('confirm-impact').textContent=info.action==='lotSchedule'?'Шкала применяется к будущим входам.':'Команда меняет ордера на счёте.';
  $('confirm-details').textContent=lines.join('\n');
  try{$('confirm-action').showModal();onDiagnostic({code:'CONFIRM_OPEN',request_id:info.body.request_id});}catch{confirmAnswer=null;resolve(false);}
 });}
 const core=new RecoveryActions({storage,token:view.getToken,confirm,allowTrading:true,onDiagnostic});
 function answer(value){if(!confirmAnswer)return;const done=confirmAnswer;confirmAnswer=null;$('confirm-action').close();done(value===true);}
 $('accept-action').addEventListener('click',()=>answer(true));$('decline-action').addEventListener('click',()=>answer(false));$('confirm-action').addEventListener('cancel',e=>{e.preventDefault();answer(false);});
 const lotInputs=Array.from({length:23},(_,i)=>{
  const label=document.createElement('label');label.className='field';label.textContent='После '+(i+1)+' SL';
  const input=document.createElement('input');input.type='text';input.inputMode='decimal';input.maxLength=25;
  input.autocomplete='off';input.disabled=true;label.append(input);$('lot-schedule-fields').append(label);
  input.addEventListener('input',()=>{lotDirty=true;update();});return input;
 });
 function update(){
  const state=view.getState(),ctx=state?.widget?.snapshot?.context_id;
  if(ctx&&seenContext&&ctx!==seenContext){lotBase=null;lotDirty=false;$('migration-ack').checked=false;$('operator-enable').checked=false;answer(false);}
  if(ctx)seenContext=ctx;
  let pending=null,error=false;try{pending=core.pending();}catch{error=true;}
  const hasLocks=!!navigator.locks?.request&&safeOrigin();
  const g=gate(state,$('migration-ack').checked,$('operator-enable').checked,hasLocks,pending||error);
  const dispatchGate=gate(state,$('migration-ack').checked,$('operator-enable').checked,hasLocks,active?error:(pending||error));
  core.enable({operatorEnabled:dispatchGate.enabled,migrationAcknowledged:$('migration-ack').checked,priorOutcomeResolved:$('migration-ack').checked});
  $('cutover').hidden=!g.armed;$('trade-controls').hidden=!g.armed;
  $('control-mode').textContent=g.enabled?'Управление':'Просмотр';$('release-mode').textContent=g.enabled?'УПРАВЛЕНИЕ':'ПРОСМОТР';$('control-summary').textContent=g.enabled?'По подтверждению':'Просмотр';
  const gateMessage=error?'Локальный журнал недоступен. Новые команды заблокированы.':pending?'Сначала проверьте исход веб-команды.':!g.armed?'Управление недоступно без разрешения VPS и свежего снимка.':!hasLocks?'Браузер не поддерживает безопасную отправку команд.':!$('migration-ack').checked?'Проверьте исход прежних команд во вкладке «История».':!$('operator-enable').checked?'Включите торговые кнопки в настройках.':'';
  $('control-gate').textContent=gateMessage;
  $('trading-access-status').textContent=g.enabled?'Управление включено.':gateMessage;
  for(const [id,allowed]of [['set-ranges',g.ranges],['cancel-ranges',g.cancel],['close-one',g.close1],['close-two',g.close2]])$(id).disabled=!allowed||active;
  const schedule=lotSchedule(state?.widget);
  const staleLot=lotDirty&&JSON.stringify(lotBase)!==JSON.stringify(schedule);
  $('lot-schedule-panel').hidden=!schedule;
  if(schedule&&!lotDirty&&JSON.stringify(lotBase)!==JSON.stringify(schedule)){
   lotBase=schedule;lotInputs.forEach((input,i)=>{input.value=schedule[i+1];});
  }
  const lotEditable=g.lotSchedule&&!staleLot&&!active;
  lotInputs.forEach(input=>{input.disabled=!lotEditable;});
  $('lot-schedule-save').disabled=!lotEditable||!lotDirty;
  $('lot-schedule-reset').disabled=active||!lotDirty;
  $('lot-schedule-status').textContent=staleLot?'Шкала на VPS изменилась. Сбросьте правки.':!g.lotSchedule?'Изменение доступно без открытых позиций и активного RECOVERY.':'';
  $('web-pending').hidden=!pending;$('web-pending').textContent=pending?'Исход веб-команды не подтверждён: '+pending.request_id+'. Не отправляйте её повторно.':'';
  $('resolve-web').hidden=!pending;$('resolve-web').disabled=active||!hasLocks;
 }
 function showResult(result){const labels={cancelled:'Команда не отправлена.',confirmed:'Подтверждено.',rejected:'Команда отклонена.',partially_applied:'Применено частично.',unknown:'Исход неизвестен. Не повторяйте команду.',none:'Ожидающей веб-команды нет.'};let message=labels[result.status]||'Результат требует проверки.';const status={confirmed:'первичная отправка подтверждена',rejected:'отклонён',not_dispatched:'не отправлен'};for(const r of result.range_outcomes??[])message+='\nДиапазон №'+r.range_id+': '+(Object.hasOwn(status,r.status)?status[r.status]:'не подтверждён');$('action-result').textContent=message;}
 async function run(action,input){
  if(active)return;
  active=true;view.setActionBusy(true);update();$('diagnostic-status').textContent='';
  try{
   const result=action==='resolve'?await core.resolve():await core.execute(action,input);
   if(action==='lotSchedule'&&result.status==='confirmed'){lotDirty=false;lotBase=null;}
   const last=diagnostics.read().at(-1);
   if(action!=='resolve'&&result.status==='unknown'&&last?.code==='NOT_SENT')$('action-result').textContent='Команда в этой попытке не отправлялась. Проверьте журнал.';
   else showResult(result);
  }catch{
   const last=diagnostics.read().at(-1);
   $('action-result').textContent=last?.code==='JOURNAL_CLEANUP_FAILED'?
    'VPS вернул окончательный результат, но локальная сверка не завершилась. Проверьте диагностику.':
    action==='resolve'?'Не удалось сверить запись.':
    last?.code==='NOT_SENT'?'Команда не отправлена. Причина: «История → Диагностика веб-команд».':
    'Исход неизвестен. Не повторяйте команду. Проверьте диагностику.';
  }finally{active=false;view.setActionBusy(false);update();view.refresh();}
 }
 $('lot-schedule-reset').addEventListener('click',()=>{lotDirty=false;lotBase=null;update();});
 $('lot-schedule-form').addEventListener('submit',e=>{
  e.preventDefault();if($('lot-schedule-save').disabled||!lotBase)return;
  try{const values=lotScheduleInput(['0.01',...lotInputs.map(input=>input.value)]);
   if(JSON.stringify(values)===JSON.stringify(lotScheduleInput(lotBase))){lotDirty=false;lotBase=null;update();return;}
   run('lotSchedule',{lot_schedule:values,expected_lot_schedule:lotBase});
  }catch(err){onDiagnostic({code:'INPUT_INVALID'});onDiagnostic({code:'NOT_SENT'});$('lot-schedule-status').textContent=err.message;}
 });
 $('range-form').addEventListener('submit',e=>{e.preventDefault();let input;try{input=rangeInput($('range-count').value,{sl:$('initial-sl').value,reentry:$('reentry').value,prices:[$('range-price-1').value,$('range-price-2').value],sides:[$('range-side-1').value,$('range-side-2').value]});}catch(err){onDiagnostic({code:'INPUT_INVALID'});onDiagnostic({code:'NOT_SENT'});$('action-result').textContent=err.message;return;}run('ranges',input);});
 $('range-count').addEventListener('change',()=>{$('range-two').hidden=$('range-count').value!=='2';});
 $('cancel-ranges').addEventListener('click',()=>run('cancel'));$('close-one').addEventListener('click',()=>run('close1'));$('close-two').addEventListener('click',()=>run('close2'));$('resolve-web').addEventListener('click',()=>run('resolve'));
 for(const id of ['migration-ack','operator-enable'])$(id).addEventListener('change',()=>{update();if(!core.enabled)answer(false);});
 window.addEventListener('recovery-state',update);window.addEventListener('recovery-session-change',()=>{lotBase=null;lotDirty=false;lotInputs.forEach(input=>{input.value='';});$('migration-ack').checked=false;$('operator-enable').checked=false;core.enable({});answer(false);update();});
 window.addEventListener('pagehide',()=>{core.enable({});answer(false);diagnostics.clear();renderDiagnostics();});renderDiagnostics();update();
}
if(window.RecoveryView)init();else window.addEventListener('recovery-ready',init,{once:true});
