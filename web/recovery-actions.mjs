// Offline draft. Integrator must keep allowTrading=false until migration is approved.
const BASE='/api/recovery', KEY='recovery.native.pending.v1';
const paths={ranges:`${BASE}/ranges`,cancel:`${BASE}/cancel`,close1:`${BASE}/ranges/1/close-cancel`,close2:`${BASE}/ranges/2/close-cancel`,reconcile:`${BASE}/reconcile`};
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
function expectedPayload(action,body){let p={...body};delete p.request_id;p.kind=action==='ranges'?'ranges':action==='cancel'?'cancel':action==='reconcile'?'reconcile':'close_cancel';if(action==='close1'||action==='close2')p.range_id=action==='close1'?'1':'2';return p;}
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
 if(w.webTrading!==true||w.reconciliation_required)return false;
 if(action==='ranges')return w.positions.length===0&&w.capabilities.set_ranges;
 if(action==='cancel')return w.positions.length===0&&w.executor_ready&&w.capabilities.cancel_ranges;
 return w.executor_ready&&w.capabilities.close_cancel_range_ids.includes(action==='close1'?'1':'2');
}
function input(action,p){
 if(action!=='ranges')return {};
 if(!object(p)||p.initial_sl_distance<0.60||!Array.isArray(p.ranges)||p.ranges.length<1||p.ranges.length>2||!finite(p.initial_sl_distance)||!finite(p.reentry_distance))fail('BAD_INPUT');
 const seen=new Set();const ranges=p.ranges.map(r=>{if(!object(r)||!['1','2'].includes(r.id)||seen.has(r.id)||!['BUY','SELL'].includes(r.side)||!finite(r.price)||r.volume!==0.01)fail('BAD_INPUT');seen.add(r.id);return {id:r.id,side:r.side,price:r.price,volume:r.volume};});
 return {ranges,initial_sl_distance:p.initial_sl_distance,reentry_distance:p.reentry_distance};
}
export class RecoveryActions {
 constructor({fetchImpl=globalThis.fetch,cryptoImpl=globalThis.crypto,storage,token,confirm,allowTrading=false,timeoutMs=12000,locks=globalThis.navigator?.locks}){
  this.locks=locks;this.fetch=fetchImpl;this.crypto=cryptoImpl;this.storage=storage;this.token=token;this.confirm=confirm;this.allowTrading=allowTrading===true;this.enabled=false;this.busy=false;this.timeoutMs=timeoutMs;
 }
 enable({operatorEnabled,migrationAcknowledged,priorOutcomeResolved}={}){
  this.enabled=this.allowTrading&&operatorEnabled===true&&migrationAcknowledged===true&&priorOutcomeResolved===true;
  return this.enabled;
 }
 pending(){let raw;try{raw=this.storage.getItem(KEY);}catch{fail('STORAGE_UNAVAILABLE');}if(raw===null)return null;try{const p=JSON.parse(raw);if(p.version!==1||!Object.hasOwn(paths,p.action)||!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(p.request_id)||typeof p.context!=='string'||!p.context||!/^[0-9a-f]{64}$/.test(p.digest)||!Array.isArray(p.rangeIds))fail('PENDING_CORRUPT');return p;}catch{fail('PENDING_CORRUPT');}}
 async digest(payload){const bytes=new TextEncoder().encode(JSON.stringify(canonical(payload)));return [...new Uint8Array(await this.crypto.subtle.digest('SHA-256',bytes))].map(b=>b.toString(16).padStart(2,'0')).join('');}
 nonce(){return [...this.crypto.getRandomValues(new Uint8Array(16))].map(b=>b.toString(16).padStart(2,'0')).join('');}
 async request(path,body,nonce){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),this.timeoutMs);
  try{
   const key=await this.token();if(body&&!this.enabled)fail('TRADING_DISABLED');if(typeof key!=='string'||!key)fail('KEY_UNAVAILABLE');
   const headers={'Authorization':`Bearer ${key}`,'Accept':'application/json','Cache-Control':'no-store'};
   if(nonce)headers['X-Recovery-State-Nonce']=nonce;
   if(body)headers['Content-Type']='application/json';
   const r=await this.fetch(path,{method:body?'POST':'GET',headers,body:body?JSON.stringify(body):undefined,cache:'no-store',credentials:'omit',redirect:'error',signal:controller.signal});
   if(!r.ok)fail(r.status===401||r.status===403?'ACCESS_DENIED':'REQUEST_FAILED');
   const text=await r.text();if(text.length>1000000)fail('BAD_RESPONSE');return JSON.parse(text);
  }catch{fail('REQUEST_UNCONFIRMED');}finally{clearTimeout(timer);}
 }
 async getState(){const nonce=this.nonce();return nativeState(await this.request(`${BASE}/state`,null,nonce),nonce);}
 async execute(action,params={}){
  if(!this.locks?.request)fail("EXCLUSIVE_LOCK_UNAVAILABLE");
  return this.locks.request("recovery.native.dispatch.v1",{mode:"exclusive"},()=>this._execute(action,params));
 }
 async _execute(action,params={}){
  if(this.busy)fail('BUSY');if(!this.enabled)fail('TRADING_DISABLED');if(!Object.hasOwn(paths,action)||action==='reconcile')fail('BAD_ACTION');if(this.pending())fail('PENDING_UNRESOLVED');
  this.busy=true;
  try{
   const values=input(action,params),shown=await this.getState();if(!allowed(shown,action))fail('CAPABILITY_DENIED');
   const body={...values,request_id:this.crypto.randomUUID(),expected_context:shown.snapshot.context_id,expected_generation:shown.snapshot.generation};
   const accepted=await this.confirm(Object.freeze({action,accountMode:shown.account_trade_mode,body:JSON.parse(JSON.stringify(body)),warning:action==='reconcile'?'Evidence-only; may allow configured strategy to continue.':'Explicit trading command. No automatic retry.'}));
   if(accepted!==true)return {status:'cancelled'};
   if(!this.enabled||this.pending())fail('TRADING_DISABLED');
   const current=await this.getState();
   if(current.snapshot.context_id!==body.expected_context||current.snapshot.generation!==body.expected_generation||!allowed(current,action))fail('STATE_CHANGED');
   const record={version:1,action,request_id:body.request_id,context:body.expected_context,digest:await this.digest(expectedPayload(action,body)),rangeIds:(body.ranges??[]).map(r=>r.id)};
   // Persist and verify before the ONLY POST. No body, token or raw response is stored.
   const encoded=JSON.stringify(record);try{this.storage.setItem(KEY,encoded);if(this.storage.getItem(KEY)!==encoded)fail('STORAGE_UNAVAILABLE');}catch{fail('STORAGE_UNAVAILABLE');}
   if(!this.enabled)fail('TRADING_DISABLED');
   try{await this.request(paths[action],body);}catch{return {status:'unknown',request_id:record.request_id};}
   // POST success is not a receipt. Resolution uses authenticated receipt GET only.
   return await this._resolve();
  }finally{this.busy=false;}
 }
 async resolve(){
  if(!this.locks?.request)fail("EXCLUSIVE_LOCK_UNAVAILABLE");
  return this.locks.request("recovery.native.dispatch.v1",{mode:"exclusive"},()=>this._resolve());
 }
 async _resolve(){
  const p=this.pending();if(!p)return {status:'none'};
  let response;try{response=await this.request(`${BASE}/commands/${p.request_id}`);}catch{return {status:'unknown',request_id:p.request_id};}
  const c=response?.command;
  if(response?.context_id!==p.context||c?.request_id!==p.request_id||!object(c.payload)||await this.digest(c.payload)!==p.digest)return {status:'unknown',request_id:p.request_id};
  const terminal=(c.phase==='completed'&&['confirmed','partially_applied'].includes(c.outcome))||(c.phase==='rejected'&&c.outcome==='rejected');
  if(!terminal)return {status:'unknown',request_id:p.request_id};
  if(c.outcome==='partially_applied'){
   const rows=c.range_outcomes;if(p.action!=='ranges'||!Array.isArray(rows)||rows.length!==p.rangeIds.length||new Set(rows.map(r=>r.range_id)).size!==rows.length||rows.some(r=>!p.rangeIds.includes(r.range_id)||!['confirmed','not_dispatched','rejected'].includes(r.status))||!rows.some(r=>r.status==='confirmed')||!rows.some(r=>r.status!=='confirmed'))return {status:'unknown',request_id:p.request_id};
  }
  // Never clear a replaced journal entry (other tab/operator).
  if(JSON.stringify(this.pending())!==JSON.stringify(p))fail('PENDING_CHANGED');
  this.storage.removeItem(KEY);if(this.storage.getItem(KEY)!==null)fail('STORAGE_UNAVAILABLE');
  return {status:c.outcome,request_id:p.request_id,range_outcomes:c.range_outcomes??[]};
 }
}
