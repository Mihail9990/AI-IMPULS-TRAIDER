import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {RecoveryActions,nativeState,createCommandDiagnostics,DIAGNOSTIC_LABELS} from './recovery-actions.mjs';
const nonce='a'.repeat(32),values={ranges:[{id:'1',side:'BUY',price:100,volume:0.01}],initial_sl_distance:0.7,reentry_distance:0.7};
function state(n=nonce){return {web_trading:true,state_freshness:{version:1,nonce:n,age_ms:null},widget:{schema_version:1,mode:'native',account_trade_mode:'demo',snapshot:{status:'fresh',context_id:'fixture-context',generation:'fixture-gen'},positions:[],orders:[],ranges:[],executor_ready:true,reconciliation_required:false,capabilities:{set_ranges:true,cancel_ranges:true,close_cancel_range_ids:[]}}};}
function harness(options={}){
 const map=new Map(),calls=[];let body,actionPath;
 const storage={getItem:k=>map.get(k)??null,setItem:(k,v)=>map.set(k,v),removeItem:k=>map.delete(k)};
 const fetchImpl=async(path,opt)=>{calls.push({path,opt});let result;
  if(path.endsWith('/state')){result=state(opt.headers['X-Recovery-State-Nonce']);if(options.mutate)options.mutate(result,calls.filter(c=>c.path.endsWith('/state')).length);}
  else if(opt.method==='POST'){body=JSON.parse(opt.body);actionPath=path;if(options.timeout)throw new Error('secret MUST NEVER ESCAPE');result={accepted:true};}
  else {let payload={...body,kind:actionPath.endsWith('/lot-schedule')?'lot_schedule':'ranges'};delete payload.request_id;result={context_id:'fixture-context',command:{request_id:body.request_id,phase:'completed',outcome:'confirmed',payload}};if(options.receipt)options.receipt(result);}
  if(options.response){const override=options.response(path,opt,result,calls);if(override)return override;}
  return {ok:true,status:200,text:async()=>JSON.stringify(result)};
 };
 const core=new RecoveryActions({fetchImpl,cryptoImpl:webcrypto,storage,token:async()=>'fixture-token',confirm:async()=>options.confirm!==false,allowTrading:options.allow!==false,onDiagnostic:options.onDiagnostic,locks:{request:async(_n,_o,fn)=>fn()}});
 core.enable({operatorEnabled:true,migrationAcknowledged:true,priorOutcomeResolved:options.resolved!==false});
 return {core,calls,map,storage};
}
test('nonce proof rejects missing/mismatch/unavailable but never time-gates',()=>{
 assert.throws(()=>nativeState(state(), 'b'.repeat(32)));const s=state();delete s.state_freshness;assert.throws(()=>nativeState(s,nonce));
 for(const age of [null,-1,0,15001,900000]){const r=state();r.state_freshness.age_ms=age;assert.equal(nativeState(r,nonce).mode,'native');}
});
test('default disabled and unresolved migration cannot send',async()=>{for(const o of [{allow:false},{resolved:false}]){const h=harness(o);await assert.rejects(h.core.execute('ranges',values));assert.equal(h.calls.length,0);}});
test('confirmed execution two state GET, one POST, one receipt; journal contains no key/body',async()=>{
 const h=harness();const saved=[];const original=h.storage.setItem;h.storage.setItem=(k,v)=>{saved.push(v);original(k,v);};
 assert.equal((await h.core.execute('ranges',values)).status,'confirmed');assert.equal(h.calls.length,4);assert.equal(h.calls.filter(c=>c.opt.method==='POST').length,1);assert.equal(h.map.size,0);
 assert(!saved[0].includes('fixture-token'));assert(!saved[0].includes('price'));assert(!saved[0].includes('payload'));assert(h.calls[0].opt.headers['X-Recovery-State-Nonce']!==h.calls[1].opt.headers['X-Recovery-State-Nonce']);
});
test('cancel confirmation sends no POST',async()=>{const h=harness({confirm:false});assert.equal((await h.core.execute('ranges',values)).status,'cancelled');assert.equal(h.calls.length,1);});
test('context/generation/capability change rejects before POST',async()=>{for(const change of [r=>r.widget.snapshot.generation='other',r=>r.widget.snapshot.context_id='other',r=>r.widget.capabilities.set_ranges=false]){const h=harness({mutate:(r,n)=>{if(n===2)change(r);}});await assert.rejects(h.core.execute('ranges',values));assert(!h.calls.some(c=>c.opt.method==='POST'));}});
test('timeout stays pending, blocks all new actions, never retries',async()=>{const h=harness({timeout:true});assert.equal((await h.core.execute('ranges',values)).status,'unknown');await assert.rejects(h.core.execute('cancel'));assert.equal(h.calls.filter(c=>c.opt.method==='POST').length,1);assert(h.core.pending());});
test('wrong receipt payload/context/UUID cannot clear pending',async()=>{for(const receipt of [r=>r.context_id='other',r=>r.command.request_id='other',r=>r.command.payload.ranges[0].price=999]){const h=harness({receipt});assert.equal((await h.core.execute('ranges',values)).status,'unknown');assert(h.core.pending());}});
test('storage failure forbids dispatch',async()=>{const h=harness();h.storage.setItem=()=>{throw new Error('full');};await assert.rejects(h.core.execute('ranges',values));assert(!h.calls.some(c=>c.opt.method==='POST'));});
test('invalid inputs and unavailable exclusive locks fail without POST',async()=>{const h=harness();await assert.rejects(h.core.execute('ranges',{...values,initial_sl_distance:0}));h.core.locks=null;await assert.rejects(h.core.execute('cancel'));assert.equal(h.calls.length,0);});
test('flat-only capabilities with position are rejected',()=>{const s=state();s.widget.positions.push({});assert.throws(()=>nativeState(s,nonce));});
test('partial receipt requires exact requested ids and mixed statuses',async()=>{
 const two={...values,ranges:[...values.ranges,{id:'2',side:'SELL',price:99,volume:0.01}]};
 const h=harness({receipt:r=>{r.command.outcome='partially_applied';r.command.range_outcomes=[{range_id:'1',status:'confirmed'},{range_id:'2',status:'not_dispatched'}];}});
 assert.equal((await h.core.execute('ranges',two)).status,'partially_applied');assert.equal(h.core.pending(),null);
 const bad=harness({receipt:r=>{r.command.outcome='partially_applied';r.command.range_outcomes=[{range_id:'1',status:'confirmed'},{range_id:'1',status:'not_dispatched'}];}});
 assert.equal((await bad.core.execute('ranges',two)).status,'unknown');assert(bad.core.pending());
});
test('terminal rejected same payload clears, nonterminal does not',async()=>{
 const rejected=harness({receipt:r=>{r.command.phase='rejected';r.command.outcome='rejected';}});assert.equal((await rejected.core.execute('ranges',values)).status,'rejected');
 const waiting=harness({receipt:r=>{r.command.phase='dispatching';}});assert.equal((await waiting.core.execute('ranges',values)).status,'unknown');assert(waiting.core.pending());
});
test('reconcile is never automatic and unknown existing journal prevents it',async()=>{
 const h=harness({timeout:true});await h.core.execute('ranges',values);await assert.rejects(h.core.execute('reconcile'));assert(!h.calls.some(c=>c.path.endsWith('/reconcile')));
});
test('corrupt persisted journal blocks dispatch',async()=>{const h=harness();h.storage.setItem('recovery.native.pending.v1','not-json');await assert.rejects(h.core.execute('cancel'));assert.equal(h.calls.length,0);});
test('server web_trading must be exact true at both reads',async()=>{for(const flag of [false,undefined,'true',1]){const h=harness({mutate:r=>{r.web_trading=flag;}});await assert.rejects(h.core.execute('ranges',values));assert(!h.calls.some(c=>c.opt.method==='POST'));}const h=harness({mutate:(r,n)=>{if(n===2)r.web_trading=false;}});await assert.rejects(h.core.execute('ranges',values));assert(!h.calls.some(c=>c.opt.method==='POST'));});
test('SL minimum and fixed starting volume cannot be bypassed',async()=>{for(const v of [{...values,initial_sl_distance:.59},{...values,ranges:[{...values.ranges[0],volume:.02}]}]){const h=harness();await assert.rejects(h.core.execute('ranges',v));assert.equal(h.calls.length,0);}});
test('reconcile explicitly unavailable even in armed session',async()=>{const h=harness();await assert.rejects(h.core.execute('reconcile'));assert.equal(h.calls.length,0);});

const defaultLots=['0.01',...Array(23).fill('0.02')];
const changedLots=['0.01','0,03',...Array(22).fill('0.02')];
const lots=(opts={})=>harness({...opts,mutate:(r,n)=>{
 r.widget.lot_schedule=[...defaultLots];r.widget.capabilities.set_lot_schedule=true;
 opts.mutate?.(r,n);
}});
const lotPayload={lot_schedule:changedLots,expected_lot_schedule:defaultLots};
test('lot schedule uses authenticated exact-decimal command and unchanged slot one',async()=>{
 const h=lots();
 assert.equal((await h.core.execute('lotSchedule',lotPayload)).status,'confirmed');
 const post=h.calls.find(c=>c.opt.method==='POST');
 assert.equal(post.path,'/api/recovery/lot-schedule');
 const body=JSON.parse(post.opt.body);
 assert.deepEqual(body.expected_lot_schedule,defaultLots);
 assert.equal(body.lot_schedule[0],'0.01');
 assert.equal(body.lot_schedule[1],'0.03');
 assert.equal(h.map.size,0);
});
test('lot schedule rejects invalid initial lot and does not coerce numeric payloads',async()=>{
 for(const table of [[...changedLots.slice(1)],['0.02',...changedLots.slice(1)],['0.01',0.03,...changedLots.slice(2)]]){
  const h=lots();await assert.rejects(h.core.execute('lotSchedule',{...lotPayload,lot_schedule:table}));
  assert.equal(h.calls.length,0);
 }
});
test('lot schedule refuses open, active, permission-lost or concurrently edited settings',async()=>{
 const blockers=[r=>{r.widget.recovery_cycle_active=true;},r=>{r.widget.capabilities.set_lot_schedule=false;},r=>{
  r.widget.positions=[{}];r.widget.capabilities.set_ranges=false;r.widget.capabilities.cancel_ranges=false;
 },r=>{r.widget.lot_schedule[1]='0.04';}];
 for(const block of blockers){
  const h=lots({mutate:(r,n)=>{if(n===2)block(r);}});
  await assert.rejects(h.core.execute('lotSchedule',lotPayload),/STATE_CHANGED/);
  assert(!h.calls.some(c=>c.opt.method==='POST'));
 }
});


test('diagnostics prove a pre-POST failure after confirmation without exposing state values',async()=>{
 const diag=createCommandDiagnostics();
 let eventKeys;
 const h=harness({onDiagnostic:e=>{eventKeys=Object.keys(e);diag.append(e);},mutate:(r,n)=>{if(n===2)r.widget.snapshot.context_id='private-context';}});
 await assert.rejects(()=>h.core.execute('ranges',values),/STATE_CHANGED/);
 assert.equal(h.calls.filter(c=>c.opt.method==='POST').length,0);
 assert.deepEqual(diag.read().map(row=>row.code),['ACTION_BEGIN','FIRST_GET_START','FIRST_GET_OK','CONFIRM_WAIT','CONFIRM_OK','SECOND_GET_START','SECOND_GET_OK','CONTEXT_CHANGED','NOT_SENT']);
 assert.deepEqual(eventKeys,['code','request_id']);
 const rows=diag.read();
 assert.ok(rows[3].request_id&&rows[3].request_id===rows.at(-1).request_id);
 assert.ok(rows.every(row=>!JSON.stringify(row).includes('private-context')&&!JSON.stringify(row).includes('fixture-token')));
 assert.ok(rows.every(row=>Object.hasOwn(DIAGNOSTIC_LABELS,row.code)&&!Number.isNaN(Date.parse(row.time))));
});
test('diagnostics distinguish rejected first/second GET, malformed proof and local journal failures before dispatch',async()=>{
 const variants=[
  [{response:(p,o,r,c)=>c.length===1?{ok:false,status:401}:null},'FIRST_GET_ACCESS'],
  [{response:(p,o,r,c)=>c.length===2?{ok:false,status:502}:null},'SECOND_GET_HTTP'],
  [{mutate:(result,n)=>{if(n===2)result.state_freshness.nonce='not-the-nonce';}},'SECOND_GET_PROOF'],
  [{mutate:(result,n)=>{if(n===2)result.widget.capabilities.set_ranges=false;}},'SECOND_CAPABILITY']
 ];
 for(const [options,code]of variants){
  const d=createCommandDiagnostics(),h=harness({...options,onDiagnostic:d.append});
  await assert.rejects(()=>h.core.execute('ranges',values));
  assert.ok(d.read().some(row=>row.code===code));
  assert.equal(d.read().at(-1).code,'NOT_SENT');
  assert.ok(!h.calls.some(call=>call.opt.method==='POST'));
 }
 const d=createCommandDiagnostics(),h=harness({onDiagnostic:d.append});
 h.storage.setItem=()=>{throw Error('do not log this')};
 await assert.rejects(()=>h.core.execute('ranges',values),/STORAGE_UNAVAILABLE/);
 assert.deepEqual(d.read().slice(-3).map(row=>row.code),['JOURNAL_START','JOURNAL_FAILED','NOT_SENT']);
 assert.ok(!JSON.stringify(d.read()).includes('do not log this'));
});
test('only the fetch boundary counts as POST started and a failure after it is unknown',async()=>{
 const uncertain=createCommandDiagnostics(), h=harness({timeout:true,onDiagnostic:uncertain.append});
 assert.equal((await h.core.execute('ranges',values)).status,'unknown');
 assert.equal(h.calls.filter(c=>c.opt.method==='POST').length,1);
 assert.equal(uncertain.read().at(-1).code,'POST_OUTCOME_UNKNOWN');
 assert.ok(uncertain.read().some(row=>row.code==='POST_START'));
 assert.ok(!JSON.stringify(uncertain.read()).includes('secret MUST NEVER ESCAPE'));
 const blocked=createCommandDiagnostics(), h2=harness({onDiagnostic:blocked.append});
 let tokenRequests=0;
 h2.core.token=async()=>++tokenRequests===3?null:'fixture-token';
 assert.equal((await h2.core.execute('ranges',values)).status,'unknown');
 assert.deepEqual(blocked.read().slice(-3).map(row=>row.code),['JOURNAL_SAVED','POST_LOCAL','NOT_SENT']);
 assert.ok(!h2.calls.some(c=>c.opt.method==='POST'));
});
test('receipt diagnostics remain read-only and differentiate not recorded from confirmed',async()=>{
 const d=createCommandDiagnostics(),h=harness({onDiagnostic:d.append,receipt:r=>{r.command.phase='not_recorded';r.command.payload=null;}});
 assert.equal((await h.core.execute('ranges',values)).status,'unknown');
 assert.ok(d.read().some(row=>row.code==='RECEIPT_NOT_FOUND'));
 assert.equal(d.read().at(-1).code,'POST_OUTCOME_UNKNOWN');
 const r=await h.core.resolve();assert.equal(r.status,'unknown');
 assert.equal(h.calls.filter(c=>c.opt.method==='POST').length,1);
 assert.equal(h.calls.at(-1).opt.method,'GET');
 const t=createCommandDiagnostics(),h2=harness({onDiagnostic:t.append});
 h2.storage.removeItem=()=>{throw Error('local error');};
 await assert.rejects(()=>h2.core.execute('ranges',values),/local error/);
 assert.deepEqual(t.read().slice(-2).map(row=>row.code),['RECEIPT_CONFIRMED','JOURNAL_CLEANUP_FAILED']);
});
test('diagnostic buffer is bounded, sanitizes arbitrary data and is independent of dispatch',async()=>{
 const d=createCommandDiagnostics();
 assert.equal(d.append({code:'raw secret',request_id:null}),false);
 assert.equal(d.append({code:'NOT_SENT',request_id:'Bearer key'}),false);
 for(let i=0;i<81;i++)assert.ok(d.append({code:'NOT_SENT',request_id:null,secret:'should be dropped'}));
 assert.equal(d.read().length,80);
 assert.ok(d.read().every(row=>Object.keys(row).length===3&&row.request_id===null));
 d.clear();assert.equal(d.read().length,0);
 const h=harness({onDiagnostic:()=>{throw Error('optional logger failed');}});
 assert.equal((await h.core.execute('ranges',values)).status,'confirmed');
});
test('browser session uses scope and CSRF for one confirmed POST and never stores CSRF',async()=>{
 const h=harness(),csrf='X'.repeat(43),saved=[];
 h.core.token=async()=>({browserSession:true,csrf,mode:'native'});
 const original=h.storage.setItem;h.storage.setItem=(k,v)=>{saved.push(v);original(k,v);};
 assert.equal((await h.core.execute('ranges',values)).status,'confirmed');
 assert.equal(h.calls.filter(c=>c.opt.method==='POST').length,1);
 for(const {opt} of h.calls){
  assert.equal(opt.credentials,'same-origin');
  assert.equal(opt.headers.Authorization,undefined);
  assert.equal(opt.headers['X-Recovery-Session'],'1');
  assert.equal(opt.headers['X-Recovery-CSRF'],opt.method==='POST'?csrf:undefined);
 }
 assert(saved.every(s=>!s.includes(csrf)));
});
