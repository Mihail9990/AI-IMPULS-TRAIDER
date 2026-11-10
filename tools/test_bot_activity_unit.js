'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const src=fs.readFileSync(path.join(__dirname,'../web/basket-panel/panel.js'),'utf8');
const code=src.slice(src.indexOf(' // Activity uses controller snapshots'),src.indexOf(' // V3 reuses scoped receipts'));
const elements={};let writes=0;
const $=id=>elements[id]??=new class{constructor(){this.value='';this.dataset={};}get textContent(){return this.value;}set textContent(value){writes++;this.value=value;}};
const ctx=vm.createContext({$,view:null,mode:'live',v3Fresh:true,busy:false,pending:null,pendingScope:null,lastActionReceipt:null,feedUtcVerified:()=>true,timeSafetyText:()=> 'Блокировка времени сохраняется.',send:()=>assert.fail('Notifications are read-only')});
const run=()=>vm.runInContext('updateBotActivity()',ctx);
vm.runInContext(src.slice(src.indexOf(' function friendlyReason('),src.indexOf(' function error(')),ctx);vm.runInContext(code,ctx);run();assert($('bot-activity-state').textContent.includes('Ожидание снимка'));
ctx.view={scope:'live',phase:'V3_WAIT_ENTRY',feed:{status:'FRESH'},positions:[],orders:[],journal:[],action_results:{}};run();assert($('bot-activity-state').textContent.includes('Ожидаю ручной ордер'));
const count=writes;run();assert.equal(writes,count,'unchanged polls do not spam live region');
ctx.view.journal=['old event','<script>literal controller text</script>'];run();assert.equal(ctx.view.journal.at(-1),'<script>literal controller text</script>','journal data stays unchanged');assert(!src.includes("write('bot-activity-event'"),'raw latest journal omitted from notifications');
ctx.busy=true;run();assert($('bot-activity-action').textContent.includes('отправляется'));assert($('bot-activity-action').textContent.includes('не подтверждено'));
ctx.busy=false;ctx.pending='req';ctx.pendingScope='live';ctx.view.action_results.req={status:'UNKNOWN'};run();assert($('bot-activity-action').textContent.includes('неизвестен'));assert($('bot-activity-action').textContent.includes('Повтор запрещён'));
ctx.pendingScope='other';run();assert($('bot-activity-action').textContent.includes('другого контекста'));ctx.pending=null;
for(const [status,word] of [['APPLIED','обработана контроллером'],['REJECTED','отклонена до исполнения'],['CONFLICT','снимок устарел'],['PENDING','исполнение не подтверждено']]){ctx.lastActionReceipt={status};run();assert($('bot-activity-action').textContent.includes(word),status);assert(!$('bot-activity-action').textContent.includes('сделка исполнена'));}
ctx.v3Fresh=false;ctx.view.read_only=true;ctx.view.time_block={reason:'blocked'};ctx.view.manual_reason='manual block';run();assert($('bot-activity-warning').textContent.includes('Нет свежего снимка'));assert($('bot-activity-warning').textContent.includes('Только просмотр'));assert($('bot-activity-warning').textContent.includes('manual block'));assert($('bot-activity-warning').textContent.includes('Блокировка времени'));
ctx.mode='live';ctx.view.phase='RECOVERY_POSITION';ctx.view.positions=[{},{}];ctx.view.orders=[{}];ctx.view.v3_link={cancel:true};run();assert(!$('bot-activity-state').textContent.includes('LIVE'));assert(!$('bot-activity-state').textContent.includes('Снимок:'));assert($('bot-activity-state').textContent.includes('ожидает сверки'));
ctx.v3Fresh=true;ctx.view.positions=[];ctx.view.orders=[];ctx.view.manual_reason=null;ctx.view.time_block=null;ctx.view.v3_link=null;ctx.view.v3_request={order_type:'STOP'};ctx.view.phase='NEW_PHASE';run();assert($('bot-activity-state').textContent.includes('Состояние контроллера: NEW_PHASE'));assert($('bot-activity-state').textContent.includes('Запланирован STOP'));
ctx.view.manual_reason='TP1_OWN_ENTRY_SL_UNAVAILABLE';ctx.view.journal=['MANUAL: TP1_OWN_ENTRY_SL_UNAVAILABLE'];run();assert(!$('bot-activity-warning').textContent.includes('TP1_OWN_ENTRY_SL_UNAVAILABLE'));assert($('bot-activity-warning').textContent.includes('не удалось перенести SL'));assert.equal($('bot-activity-warning').dataset.tone,'warning');
for(const [status,tone] of [['APPLIED','success'],['UNKNOWN','unknown'],['REJECTED','warning'],['PENDING','info']]){ctx.lastActionReceipt={status};run();assert.equal($('bot-activity-action').dataset.tone,tone);}
ctx.v3Fresh=true;ctx.view.manual_reason=null;ctx.view.time_block=null;ctx.view.read_only=false;ctx.view.positions=[];
for(const [phase,orders,text] of [['WAIT_CANDLE',[],'Ожидаю закрытие свечи'],['V3_WAIT_CANDLE',[],'Ожидаю закрытие свечи'],['INITIAL_PENDING',[{}],'Ожидаю исполнения триггер ордера'],['RECOVERY_PENDING',[{}],'Ожидаю исполнения триггер ордера'],['INITIAL_POSITION',[],'Ожидаю исполнения TP или SL'],['RECOVERY_POSITION',[],'Ожидаю исполнения TP или SL'],['V3_WAIT_ENTRY',[],'Ожидаю ручной ордер']]){ctx.view.phase=phase;ctx.view.orders=orders;ctx.view.positions=phase.endsWith('_POSITION')?[{}]:[];run();assert($('bot-activity-state').textContent.includes(text));assert.equal($('bot-activity-state').dataset.tone,'info');}
ctx.view.manual_reason='BLOCKED';ctx.view.phase='V3_WAIT_ENTRY';run();assert(!$('bot-activity-state').textContent.includes('Ожидаю ручной ордер'));
console.log('PASS actual-source bot activity: source-only states/events, sent vs applied vs unknown, per-role labels, warnings/stale and no repeated unchanged announcements. Browser geometry NOT verified.');

ctx.view.manual_reason='V3_PARTIAL_OR_REJECTED_BATCH';run();assert($('bot-activity-warning').textContent.includes('ордеров отклонены'));
ctx.view.manual_reason='V3_LINK_EDIT_REJECTED';run();assert($('bot-activity-warning').textContent.includes('триггеров отклонено'));
const css=fs.readFileSync(path.join(__dirname,'../web/basket-panel/panel.css'),'utf8');
assert(css.includes('#bot-activity #bot-activity-state,#bot-activity p[data-tone=info]{color:#83c3ff;font-weight:700}'));
const html=fs.readFileSync(path.join(__dirname,'../web/basket-panel/index.html'),'utf8');
assert(!html.includes('id="bot-activity-event"'));assert(html.includes('id="journal"'));
assert(css.includes('width:clamp(104px,30vw,132px)'));assert(css.includes('#v3-panels .sl-stepper{grid-template-columns:minmax(0,1fr)'));
