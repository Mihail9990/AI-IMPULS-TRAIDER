'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const src=fs.readFileSync(path.join(__dirname,'../web/basket-panel/panel.js'),'utf8');
const code=src.slice(src.indexOf(' // Activity uses controller snapshots'),src.indexOf(' // V3 reuses scoped receipts'));
const elements={};let writes=0;
const $=id=>elements[id]??=new class{constructor(){this.value='';this.dataset={};}get textContent(){return this.value;}set textContent(value){writes++;this.value=value;}};
const ctx=vm.createContext({$,view:null,mode:'live',v3Fresh:true,busy:false,pending:null,pendingScope:null,lastActionReceipt:null,feedUtcVerified:()=>true,timeSafetyText:()=> 'Блокировка времени сохраняется.',send:()=>assert.fail('Notifications are read-only')});
const run=()=>vm.runInContext('updateBotActivity()',ctx);
vm.runInContext(src.slice(src.indexOf(' function friendlyReason('),src.indexOf(' function error(')),ctx);vm.runInContext(code,ctx);run();assert($('bot-activity-state').textContent.includes('Ожидание снимка'));
ctx.view={scope:'live',phase:'V3_WAIT_ENTRY',feed:{status:'FRESH'},positions:[],orders:[],journal:[],action_results:{}};run();assert($('bot-activity-state').textContent.includes('Ожидание вашего входа'));assert($('bot-activity-event').textContent.includes('пока нет'));
const count=writes;run();assert.equal(writes,count,'unchanged polls do not spam live region');
ctx.view.journal=['old event','<script>literal controller text</script>'];run();assert.equal($('bot-activity-event').textContent,'Последнее сообщение контроллера: <script>literal controller text</script>');
ctx.busy=true;run();assert($('bot-activity-action').textContent.includes('отправляется'));assert($('bot-activity-action').textContent.includes('не подтверждено'));
ctx.busy=false;ctx.pending='req';ctx.pendingScope='live';ctx.view.action_results.req={status:'UNKNOWN'};run();assert($('bot-activity-action').textContent.includes('неизвестен'));assert($('bot-activity-action').textContent.includes('Повтор запрещён'));
ctx.pendingScope='other';run();assert($('bot-activity-action').textContent.includes('другого контекста'));ctx.pending=null;
for(const [status,word] of [['APPLIED','обработана контроллером'],['REJECTED','отклонена до исполнения'],['CONFLICT','снимок устарел'],['PENDING','исполнение не подтверждено']]){ctx.lastActionReceipt={status};run();assert($('bot-activity-action').textContent.includes(word),status);assert(!$('bot-activity-action').textContent.includes('сделка исполнена'));}
ctx.v3Fresh=false;ctx.view.read_only=true;ctx.view.time_block={reason:'blocked'};ctx.view.manual_reason='manual block';run();assert($('bot-activity-warning').textContent.includes('Нет свежего снимка'));assert($('bot-activity-warning').textContent.includes('Только просмотр'));assert($('bot-activity-warning').textContent.includes('manual block'));assert($('bot-activity-warning').textContent.includes('Блокировка времени'));
ctx.view.phase='RECOVERY_POSITION';ctx.view.positions=[{},{}];ctx.view.orders=[{}];ctx.view.v3_link={cancel:true};run();assert($('bot-activity-state').textContent.includes('LIVE'));assert($('bot-activity-state').textContent.includes('позиций 2, ордеров 1'));assert($('bot-activity-state').textContent.includes('ожидает сверки'));
ctx.view.v3_link=null;ctx.view.v3_request={order_type:'STOP'};ctx.view.phase='NEW_PHASE';run();assert($('bot-activity-state').textContent.includes('Состояние контроллера: NEW_PHASE'));assert($('bot-activity-state').textContent.includes('Запланирован STOP'));
ctx.view.manual_reason='TP1_OWN_ENTRY_SL_UNAVAILABLE';ctx.view.journal=['MANUAL: TP1_OWN_ENTRY_SL_UNAVAILABLE'];run();assert(!$('bot-activity-warning').textContent.includes('TP1_OWN_ENTRY_SL_UNAVAILABLE'));assert($('bot-activity-warning').textContent.includes('не удалось перенести SL'));assert(!$('bot-activity-event').textContent.includes('TP1_OWN_ENTRY_SL_UNAVAILABLE'));assert.equal($('bot-activity-warning').dataset.tone,'warning');
for(const [status,tone] of [['APPLIED','success'],['UNKNOWN','unknown'],['REJECTED','warning'],['PENDING','info']]){ctx.lastActionReceipt={status};run();assert.equal($('bot-activity-action').dataset.tone,tone);}
console.log('PASS actual-source bot activity: source-only states/events, sent vs applied vs unknown, per-role labels, warnings/stale and no repeated unchanged announcements. Browser geometry NOT verified.');
