import {lotSchedule,lotScheduleInput} from './control-policy.mjs';
const BASE='/api/recovery', KEY='recovery.native.pending.v1';
const paths={ranges:`${BASE}/ranges`,cancel:`${BASE}/cancel`,close1:`${BASE}/ranges/1/close-cancel`,close2:`${BASE}/ranges/2/close-cancel`,lotSchedule:`${BASE}/lot-schedule`,reconcile:`${BASE}/reconcile`};
const fail=code=>{throw new Error(code)};
const object=x=>x!==null&&typeof x==='object'&&!Array.isArray(x);
const finite=x=>typeof x==='number'&&Number.isFinite(x)&&x>0;
function canonical(value,key=''){
 if(['price','volume','initial_sl_distance','reentry_distance'].includes(key)){
  if((typeof value!=='number'&&typeof value!=='string')||!Number.isFinite(Number(value)))fail('BAD_PAYLOAD');return Number(value);
 }
 if(Array.isArray(value))return value.map(x=>canonical(x));
 if(object(value))return Object.fromEntries(Object.keys(value).sort().map(k=>[k,canonical(value[k],k)]));
 return value;
}
function expectedPayload(action,body){let p={...body};delete p.request_id;p.kind=action==='ranges'?'ranges':action==='cancel'?'cancel':action==='reconcile'?'reconcile':action==='lotSchedule'?'lot_schedule':'close_cancel';if(action==='close1'||action==='close2')p.range_id=action==='close1'?'1':'2';return p;}
export function nativeState(response,nonce){
 const proof=response?.state_freshness, w=(response?.state??response)?.widget;
 if(!object(proof)||proof.version!==1||proof.nonce!==nonce||!/^([0-9a-f]{32})$/.test(nonce)||!(proof.age_ms===null||Number.isSafeInteger(proof.age_ms)))fail('UNBOUND_STATE');
 if(!object(w)||w.schema_version!==1||w.mode!=='native'||w.snapshot?.status!=='fresh'||typeof w.snapshot.context_id!=='string'||!w.snapshot.context_id||!(w.snapshot.generation===null||typeof w.snapshot.generation==='string'))fail('NATIVE_STATE_UNAVAILABLE');
 if(!['real','demo','contest','unknown'].includes(w.account_trade_mode)||!Array.isArray(w.positions)||!Array.isArray(w.orders)||!Array.isArray(w.ranges)||typeof w.executor_ready!=='boolean'||typeof w.reconciliation_required!=='boolean')fail('BAD_STATE');
 const cap=w.capabilities;if(!object(cap)||typeof cap.set_ranges!=='boolean'||typeof cap.cancel_ranges!=='boolean'||!Array.isArray(cap.close_cancel_range_ids))fail('BAD_STATE');
 const ids=w.ranges.map(r=>r.id);if(new Set(ids).size!==ids.length||ids.some(id=>!['1','2'].includes(id))||new Set(cap.close_cancel_range_ids).size!==cap.close_cancel_range_ids.length||cap.close_cancel_range_ids.some(id=>!ids.includes(id)))fail('BAD_STATE');
 if(w.positions.length&&(cap.set_ranges||cap.cancel_ranges))fail('BAD_STATE');
 return {...JSON.parse(JSON.stringify(w)),webTrading:response.web_trading===true};
}
function allowed(w,action){
 if(action==='reconcile')return false; // Not wired until its receipt contract is independently approved.
 if(w.webTrading!==true)return false;
 if(w.reconciliation_required)return false;
 if(action==='lotSchedule')return !w.positions.length&&w.recovery_cycle_active!==true&&w.capabilities.set_lot_schedule===true&&lotSchedule(w)!==null;
 if(action==='ranges')return w.positions.length===0&&w.capabilities.set_ranges;
 if(action==='cancel')return w.positions.length===0&&w.executor_ready&&w.capabilities.cancel_ranges;
 return w.executor_ready&&w.capabilities.close_cancel_range_ids.includes(action==='close1'?'1':'2');
}
function input(action,p){
 if(action==='lotSchedule'){
  const expected=lotSchedule(p&&{lot_schedule:p.expected_lot_schedule});
  if(!expected)fail('BAD_INPUT');
  return {lot_schedule:lotScheduleInput(p.lot_schedule),expected_lot_schedule:expected};
 }

 if(action!=='ranges')return {};
 if(!object(p)||p.initial_sl_distance<0.60||!Array.isArray(p.ranges)||p.ranges.length<1||p.ranges.length>2||!finite(p.initial_sl_distance)||!finite(p.reentry_distance))fail('BAD_INPUT');
 const seen=new Set();const ranges=p.ranges.map(r=>{if(!object(r)||!['1','2'].includes(r.id)||seen.has(r.id)||!['BUY','SELL'].includes(r.side)||!finite(r.price)||r.volume!==0.01)fail('BAD_INPUT');seen.add(r.id);return {id:r.id,side:r.side,price:r.price,volume:r.volume};});
 return {ranges,initial_sl_distance:p.initial_sl_distance,reentry_distance:p.reentry_distance};
}
// UI-only, bounded and deliberately never persisted; accepts no raw command data.
const DIAGNOSTIC_MAX=80;
const diagnosticUUID=/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const diagnosticPhases=['FIRST_GET','SECOND_GET','POST','RECEIPT_GET'];
const requestReasons=['NETWORK','TIMEOUT','ACCESS','HTTP','BODY','LOCAL'];
export const DIAGNOSTIC_LABELS=Object.freeze(Object.assign({
 ACTION_BEGIN:'Начата новая попытка.',
 LOCK_UNAVAILABLE:'Блокировка вкладок недоступна.',
 ACTION_BLOCKED:'Отправка запрещена до первого запроса.',
 INPUT_INVALID:'Данные команды не прошли проверку.',
 PRIOR_PENDING:'У предыдущей команды неизвестный исход.',
 FIRST_GET_START:'Проверка состояния перед подтверждением.',
 FIRST_GET_OK:'Состояние перед подтверждением получено.',
 FIRST_GET_PROOF:'Первый снимок не подтверждён сервером.',
 FIRST_CAPABILITY:'По первому снимку команда недоступна.',
 CONFIRM_WAIT:'Ожидается подтверждение пользователя.',
 CONFIRM_OPEN:'Окно подтверждения открыто.',
 CONFIRM_CLOSED:'Команда не подтверждена.',
 CONFIRM_OK:'Подтверждение получено.',
 POSTCONFIRM_BLOCKED:'После подтверждения отправка заблокирована.',
 SECOND_GET_START:'Повторная проверка состояния после подтверждения.',
 SECOND_GET_OK:'Повторный снимок получен.',
 SECOND_GET_PROOF:'Повторный снимок не подтверждён сервером.',
 CONTEXT_CHANGED:'После подтверждения изменился контекст счёта.',
 GENERATION_CHANGED:'После подтверждения изменилось состояние диапазонов.',
 SECOND_CAPABILITY:'После подтверждения сервер больше не разрешает команду.',
 LOT_SCHEDULE_CHANGED:'Шкала лотов изменилась. Требуется новое подтверждение.',
 JOURNAL_START:'Подготовка защищённой записи перед отправкой.',
 JOURNAL_SAVED:'Локальная запись проверена.',
 JOURNAL_FAILED:'Не удалось подготовить локальную запись.',
 POST_START:'Браузер начал отправку команды на VPS.',
 POST_OK:'Получен ответ на отправку. Это ещё не подтверждение результата.',
 NOT_SENT:'Команда в этой попытке не отправлялась.',
 POST_OUTCOME_UNKNOWN:'После начала отправки результат неизвестен. Не повторяйте команду.',
 RECEIPT_START:'Запрос результата в журнале VPS. Только чтение.',
 RECEIPT_EMPTY:'Нет локальной команды для сверки.',
 RECEIPT_NOT_FOUND:'Записи ещё нет. Это не доказывает, что команда не была отправлена.',
 RECEIPT_MISMATCH:'Полученная запись не соответствует команде.',
 RECEIPT_PENDING:'В журнале пока нет окончательного результата.',
 RECEIPT_CONFIRMED:'Сервер подтвердил результат команды.',
 RECEIPT_REJECTED:'Сервер зафиксировал отклонение команды.',
 RECEIPT_PARTIAL:'Сервер подтвердил частичное применение.',
 JOURNAL_CLEANUP_FAILED:'Не удалось завершить локальную сверку.'
},Object.fromEntries(diagnosticPhases.flatMap(phase=>requestReasons.map(reason=>[
 phase+'_'+reason, {
  NETWORK:'Запрос не получил надёжного ответа.', TIMEOUT:'Истекло время ожидания запроса.',
  ACCESS:'Сервер отказал в доступе.', HTTP:'Сервер вернул ошибку HTTP.',
  BODY:'Ответ сервера не прошёл проверку формата.', LOCAL:'Запрос не был подготовлен в браузере.'
 }[reason]
])))));
export function createCommandDiagnostics(){
 const rows=[];
 return Object.freeze({
  append(event){
   const code=event?.code,id=event?.request_id;
   if(typeof code!=='string'||!Object.hasOwn(DIAGNOSTIC_LABELS,code))return false;
   if(id!=null&&(typeof id!=='string'||!diagnosticUUID.test(id)))return false;
   rows.push(Object.freeze({time:new Date().toISOString(),code,request_id:id??null}));
   if(rows.length>DIAGNOSTIC_MAX)rows.shift();
   return true;
  },
  read:()=>rows.slice(),
  clear(){rows.length=0;}
 });
}
function emit(sink,code,id){
 if(typeof sink!=='function'||!Object.hasOwn(DIAGNOSTIC_LABELS,code))return;
 // Optional diagnostics must never affect a trading guard or network operation.
 try{sink({code,request_id:typeof id==='string'&&diagnosticUUID.test(id)?id:null});}catch{}
}
export class RecoveryActions {
 constructor({fetchImpl=globalThis.fetch,cryptoImpl=globalThis.crypto,storage,token,confirm,allowTrading=false,timeoutMs=12000,locks=globalThis.navigator?.locks,onDiagnostic}){
  this.locks=locks;this.fetch=fetchImpl;this.crypto=cryptoImpl;this.storage=storage;this.token=token;this.confirm=confirm;this.allowTrading=allowTrading===true;this.enabled=false;this.busy=false;this.timeoutMs=timeoutMs;this.onDiagnostic=onDiagnostic;
 }
 enable({operatorEnabled,migrationAcknowledged,priorOutcomeResolved}={}){
  this.enabled=this.allowTrading&&operatorEnabled===true&&migrationAcknowledged===true&&priorOutcomeResolved===true;
  return this.enabled;
 }
 pending(){let raw;try{raw=this.storage.getItem(KEY);}catch{fail('STORAGE_UNAVAILABLE');}if(raw===null)return null;try{const p=JSON.parse(raw);if(p.version!==1||!Object.hasOwn(paths,p.action)||!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(p.request_id)||typeof p.context!=='string'||!p.context||!/^[0-9a-f]{64}$/.test(p.digest)||!Array.isArray(p.rangeIds))fail('PENDING_CORRUPT');return p;}catch{fail('PENDING_CORRUPT');}}
 async digest(payload){const bytes=new TextEncoder().encode(JSON.stringify(canonical(payload)));return [...new Uint8Array(await this.crypto.subtle.digest('SHA-256',bytes))].map(b=>b.toString(16).padStart(2,'0')).join('');}
 nonce(){return [...this.crypto.getRandomValues(new Uint8Array(16))].map(b=>b.toString(16).padStart(2,'0')).join('');}
 diagnostic(code,id=null){emit(this.onDiagnostic,code,id);}
 async request(path,body,nonce,phase,onFetch){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),this.timeoutMs);
  let reason='LOCAL';
  try{
   const key=await this.token();if(body&&!this.enabled)fail('TRADING_DISABLED');const session=key?.browserSession===true&&/^[A-Za-z0-9_-]{43}$/.test(key.csrf);if(!session&&(typeof key!=='string'||!key))fail('KEY_UNAVAILABLE');
   const headers={'Accept':'application/json','Cache-Control':'no-store'};
   if(session){headers['X-Recovery-Session']='1';if(body)headers['X-Recovery-CSRF']=key.csrf;}else headers.Authorization=`Bearer ${key}`;
   if(nonce)headers['X-Recovery-State-Nonce']=nonce;
   if(body)headers['Content-Type']='application/json';
   const options={method:body?'POST':'GET',headers,body:body?JSON.stringify(body):undefined,cache:'no-store',credentials:session?'same-origin':'omit',redirect:'error',signal:controller.signal};
   reason='NETWORK';if(onFetch)onFetch();
   const r=await this.fetch(path,options);
   if(!r.ok){reason=r.status===401||r.status===403?'ACCESS':'HTTP';fail('REQUEST_FAILED');}
   reason='BODY';
   const text=await r.text();if(text.length>1000000)fail('BAD_RESPONSE');return JSON.parse(text);
  }catch{if(phase)phase(controller.signal.aborted?'TIMEOUT':reason);fail('REQUEST_UNCONFIRMED');}finally{clearTimeout(timer);}
 }
 async getState(stage,note){
  if(stage)note(stage+'_START');
  const nonce=this.nonce();
  const response=await this.request(`${BASE}/state`,null,nonce,stage&&((reason)=>note(stage+'_'+reason)));
  let state;
  try{state=nativeState(response,nonce);}catch(e){if(stage)note(stage+'_PROOF');throw e;}
  if(stage)note(stage+'_OK');
  return state;
 }
 async execute(action,params={}){
  const trace={requestId:null,postStarted:false,terminal:false};
  trace.note=code=>this.diagnostic(code,trace.requestId);
  trace.note('ACTION_BEGIN');
  try{
   if(!this.locks?.request){trace.note('LOCK_UNAVAILABLE');fail("EXCLUSIVE_LOCK_UNAVAILABLE");}
   return await this.locks.request("recovery.native.dispatch.v1",{mode:"exclusive"},()=>this._execute(action,params,trace));
  }catch(e){if(!trace.terminal)trace.note(trace.postStarted?'POST_OUTCOME_UNKNOWN':'NOT_SENT');throw e;}
 }
 async _execute(action,params,trace){
  const {note}=trace;
  if(this.busy||!this.enabled||!Object.hasOwn(paths,action)||action==='reconcile')note('ACTION_BLOCKED');
  if(this.busy)fail('BUSY');if(!this.enabled)fail('TRADING_DISABLED');if(!Object.hasOwn(paths,action)||action==='reconcile')fail('BAD_ACTION');
  try{if(this.pending()){note('PRIOR_PENDING');fail('PENDING_UNRESOLVED');}}catch(e){if(e.message!=='PENDING_UNRESOLVED')note('JOURNAL_FAILED');throw e;}
  this.busy=true;
  try{
   let values;try{values=input(action,params);}catch(e){note('INPUT_INVALID');throw e;}
   const shown=await this.getState('FIRST_GET',note);if(!allowed(shown,action)){note('FIRST_CAPABILITY');fail('CAPABILITY_DENIED');}
   if(action==='lotSchedule'&&JSON.stringify(lotSchedule(shown))!==JSON.stringify(values.expected_lot_schedule)){note('LOT_SCHEDULE_CHANGED');fail('STATE_CHANGED');}
   const body={...values,request_id:this.crypto.randomUUID(),expected_context:shown.snapshot.context_id,expected_generation:shown.snapshot.generation};
   trace.requestId=body.request_id;note('CONFIRM_WAIT');
   const accepted=await this.confirm(Object.freeze({action,accountMode:shown.account_trade_mode,body:JSON.parse(JSON.stringify(body)),warning:action==='reconcile'?'Evidence-only; may allow configured strategy to continue.':'Explicit trading command. No automatic retry.'}));
   if(accepted!==true){note('CONFIRM_CLOSED');note('NOT_SENT');return {status:'cancelled'};}
   note('CONFIRM_OK');
   if(!this.enabled||this.pending()){note('POSTCONFIRM_BLOCKED');fail('TRADING_DISABLED');}
   const current=await this.getState('SECOND_GET',note);
   if(current.snapshot.context_id!==body.expected_context){note('CONTEXT_CHANGED');fail('STATE_CHANGED');}
   if(current.snapshot.generation!==body.expected_generation){note('GENERATION_CHANGED');fail('STATE_CHANGED');}
   if(!allowed(current,action)){note('SECOND_CAPABILITY');fail('STATE_CHANGED');}
   if(action==='lotSchedule'&&JSON.stringify(lotSchedule(current))!==JSON.stringify(body.expected_lot_schedule)){note('LOT_SCHEDULE_CHANGED');fail('STATE_CHANGED');}
   note('JOURNAL_START');
   let record;
   try{record={version:1,action,request_id:body.request_id,context:body.expected_context,digest:await this.digest(expectedPayload(action,body)),rangeIds:(body.ranges??[]).map(r=>r.id)};}
   catch(e){note('JOURNAL_FAILED');throw e;}
   // Persist and verify before the ONLY POST. No body, token or raw response is stored.
   const encoded=JSON.stringify(record);try{this.storage.setItem(KEY,encoded);if(this.storage.getItem(KEY)!==encoded)fail('STORAGE_UNAVAILABLE');}catch{note('JOURNAL_FAILED');fail('STORAGE_UNAVAILABLE');}
   note('JOURNAL_SAVED');
   if(!this.enabled){note('POSTCONFIRM_BLOCKED');fail('TRADING_DISABLED');}
   try{await this.request(paths[action],body,null,reason=>note('POST_'+reason),()=>{trace.postStarted=true;note('POST_START');});}
   catch{note(trace.postStarted?'POST_OUTCOME_UNKNOWN':'NOT_SENT');return {status:'unknown',request_id:record.request_id};}
   note('POST_OK');
   // POST success is not a receipt. Resolution uses authenticated receipt GET only.
   const result=await this._resolve(trace);
   if(result.status==='unknown')note('POST_OUTCOME_UNKNOWN');
   return result;
  }finally{this.busy=false;}
 }
 async resolve(){
  if(!this.locks?.request){this.diagnostic('LOCK_UNAVAILABLE');fail("EXCLUSIVE_LOCK_UNAVAILABLE");}
  return this.locks.request("recovery.native.dispatch.v1",{mode:"exclusive"},()=>this._resolve());
 }
 async _resolve(trace){
  let p;try{p=this.pending();}catch(e){this.diagnostic('JOURNAL_FAILED');throw e;}
  if(!p){this.diagnostic('RECEIPT_EMPTY');return {status:'none'};}
  const note=code=>this.diagnostic(code,p.request_id);
  const unknown=code=>{if(code)note(code);return {status:'unknown',request_id:p.request_id};};
  let response;note('RECEIPT_START');
  try{response=await this.request(`${BASE}/commands/${p.request_id}`,null,null,reason=>note('RECEIPT_GET_'+reason));}catch{return unknown();}
  const c=response?.command;
  if(response?.context_id!==p.context||c?.request_id!==p.request_id)return unknown('RECEIPT_MISMATCH');
  if(c.phase==='not_recorded')return unknown('RECEIPT_NOT_FOUND');
  if(!object(c.payload)||await this.digest(c.payload)!==p.digest)return unknown('RECEIPT_MISMATCH');
  const terminal=(c.phase==='completed'&&['confirmed','partially_applied'].includes(c.outcome))||(c.phase==='rejected'&&c.outcome==='rejected');
  if(!terminal)return unknown('RECEIPT_PENDING');
  if(c.outcome==='partially_applied'){
   const rows=c.range_outcomes;if(p.action!=='ranges'||!Array.isArray(rows)||rows.length!==p.rangeIds.length||new Set(rows.map(r=>r.range_id)).size!==rows.length||rows.some(r=>!p.rangeIds.includes(r.range_id)||!['confirmed','not_dispatched','rejected'].includes(r.status))||!rows.some(r=>r.status==='confirmed')||!rows.some(r=>r.status!=='confirmed'))return unknown('RECEIPT_MISMATCH');
  }
  if(trace)trace.terminal=true;
  note(c.outcome==='confirmed'?'RECEIPT_CONFIRMED':c.outcome==='rejected'?'RECEIPT_REJECTED':'RECEIPT_PARTIAL');
  // Never clear a replaced journal entry (other tab/operator).
  try{
   if(JSON.stringify(this.pending())!==JSON.stringify(p))fail('PENDING_CHANGED');
   this.storage.removeItem(KEY);if(this.storage.getItem(KEY)!==null)fail('STORAGE_UNAVAILABLE');
  }catch(e){note('JOURNAL_CLEANUP_FAILED');throw e;}
  return {status:c.outcome,request_id:p.request_id,range_outcomes:c.range_outcomes??[]};
 }
}
