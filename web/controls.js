import {RecoveryActions} from './recovery-actions.mjs';
import {gate,rangeInput} from './control-policy.mjs';
function init(){
 const view=window.RecoveryView;if(!view||window.recoveryControlsInitialized)return;window.recoveryControlsInitialized=true;
 const $=id=>document.getElementById(id);let active=false,confirmAnswer=null,seenContext=null;
 const storage={getItem:k=>localStorage.getItem(k),setItem:(k,v)=>localStorage.setItem(k,v),removeItem:k=>localStorage.removeItem(k)};
 const names={ranges:'Выставить новые диапазоны',cancel:'Отменить все диапазоны',close1:'Закрыть позицию и отменить диапазон №1',close2:'Закрыть позицию и отменить диапазон №2'};
 const modes={real:'Реальный счёт',demo:'Демо-счёт',contest:'Конкурсный счёт',unknown:'Тип счёта не подтверждён'};
 const safeOrigin=()=>location.protocol==='https:'||(location.protocol==='http:'&&['localhost','127.0.0.1','[::1]'].includes(location.hostname));
 function confirm(info){return new Promise(resolve=>{
  if(!active||!safeOrigin()){resolve(false);return;}confirmAnswer=resolve;
  const lines=[names[info.action],'Сервер: '+location.host,'Счёт: '+modes[info.accountMode]];
  if(info.action==='ranges'){for(const r of info.body.ranges)lines.push('Диапазон №'+r.id+': '+r.side+' по '+r.price+' · 0,01 лота');lines.push('Исходный SL: '+info.body.initial_sl_distance,'После SL поддержки: '+info.body.reentry_distance,'Прежние диапазоны будут заменены.');}
  else if(info.action==='cancel')lines.push('Все действующие диапазоны будут отменены. Ожидание новых.');
  else lines.push('Позиция выбранной стороны будет закрыта, её триггеры отменены. Другая сторона не отменяется.');
  lines.push('Новый request_id: '+info.body.request_id,'Если результат неизвестен, команда не повторяется.');
  $('confirm-details').textContent=lines.join('\n');
  try{$('confirm-action').showModal();}catch{confirmAnswer=null;resolve(false);}
 });}
 const core=new RecoveryActions({storage,token:view.getToken,confirm,allowTrading:true});
 function answer(value){if(!confirmAnswer)return;const done=confirmAnswer;confirmAnswer=null;$('confirm-action').close();done(value===true);}
 $('accept-action').addEventListener('click',()=>answer(true));$('decline-action').addEventListener('click',()=>answer(false));$('confirm-action').addEventListener('cancel',e=>{e.preventDefault();answer(false);});
 function update(){
  const state=view.getState(),ctx=state?.widget?.snapshot?.context_id;
  if(ctx&&seenContext&&ctx!==seenContext){$('migration-ack').checked=false;$('operator-enable').checked=false;answer(false);}
  if(ctx)seenContext=ctx;
  let pending=null,error=false;try{pending=core.pending();}catch{error=true;}
  const hasLocks=!!navigator.locks?.request&&safeOrigin();
  const g=gate(state,$('migration-ack').checked,$('operator-enable').checked,hasLocks,pending||error);
  const dispatchGate=gate(state,$('migration-ack').checked,$('operator-enable').checked,hasLocks,active?error:(pending||error));
  core.enable({operatorEnabled:dispatchGate.enabled,migrationAcknowledged:$('migration-ack').checked,priorOutcomeResolved:$('migration-ack').checked});
  $('cutover').hidden=!g.armed;$('trade-controls').hidden=!g.armed;
  $('control-mode').textContent=g.enabled?'Управление разрешено':'Просмотр';$('release-mode').textContent=g.enabled?'УПРАВЛЕНИЕ':'ПРОСМОТР';$('control-summary').textContent=g.enabled?'По подтверждению':'Просмотр';
  $('control-gate').textContent=error?'Локальный журнал недоступен или повреждён. Новые команды запрещены.':pending?'Есть неподтверждённая веб-команда. Только проверка записи, без повтора.':!g.armed?'Сервер не разрешил торговое управление либо нет подтверждённого снимка.':!hasLocks?'Браузер не поддерживает защищённую межвкладочную блокировку. Доступен только просмотр.':!$('migration-ack').checked?'Сначала лично проверьте исход прежней команды Android.':!$('operator-enable').checked?'Включите кнопки для этой вкладки, если готовы управлять.':'Каждая команда требует отдельного подтверждения и повторной проверки VPS.';
  for(const [id,allowed]of [['set-ranges',g.ranges],['cancel-ranges',g.cancel],['close-one',g.close1],['close-two',g.close2]])$(id).disabled=!allowed||active;
  $('web-pending').hidden=!pending;$('web-pending').textContent=pending?'Неподтверждённая веб-команда: '+pending.request_id+'. Закрытие вкладки не стирает эту запись.':'';
  $('resolve-web').hidden=!pending;$('resolve-web').disabled=active||!hasLocks;
 }
 function showResult(result){const labels={cancelled:'Отмена до отправки.',confirmed:'Результат команды подтверждён журналом VPS.',rejected:'Команда отклонена. Автоматического повтора нет.',partially_applied:'Применено частично. Автоматического повтора нет.',unknown:'Исход неизвестен. Не повторяйте команду; проверьте её запись.',none:'Ожидающей веб-команды нет.'};let message=labels[result.status]||'Результат требует проверки.';const status={confirmed:'первичная отправка подтверждена',rejected:'отклонён',not_dispatched:'не отправлен'};for(const r of result.range_outcomes??[])message+='\nДиапазон №'+r.range_id+': '+(Object.hasOwn(status,r.status)?status[r.status]:'не подтверждён');$('action-result').textContent=message;}
 async function run(action,input){if(active)return;active=true;view.setActionBusy(true);update();try{showResult(action==='resolve'?await core.resolve():await core.execute(action,input));}catch{ $('action-result').textContent='Действие не подтверждено или недоступно. Проверьте состояние и журнал; автоматического повтора нет.';}finally{active=false;view.setActionBusy(false);update();view.refresh();}}
 $('range-form').addEventListener('submit',e=>{e.preventDefault();let input;try{input=rangeInput($('range-count').value,{sl:$('initial-sl').value,reentry:$('reentry').value,prices:[$('range-price-1').value,$('range-price-2').value],sides:[$('range-side-1').value,$('range-side-2').value]});}catch(err){$('action-result').textContent=err.message;return;}run('ranges',input);});
 $('range-count').addEventListener('change',()=>{$('range-two').hidden=$('range-count').value!=='2';});
 $('cancel-ranges').addEventListener('click',()=>run('cancel'));$('close-one').addEventListener('click',()=>run('close1'));$('close-two').addEventListener('click',()=>run('close2'));$('resolve-web').addEventListener('click',()=>run('resolve'));
 for(const id of ['migration-ack','operator-enable'])$(id).addEventListener('change',()=>{update();if(!core.enabled)answer(false);});
 window.addEventListener('recovery-state',update);window.addEventListener('recovery-session-change',()=>{$('migration-ack').checked=false;$('operator-enable').checked=false;core.enable({});answer(false);update();});
 window.addEventListener('pagehide',()=>{core.enable({});answer(false);});update();
}
if(window.RecoveryView)init();else window.addEventListener('recovery-ready',init,{once:true});
