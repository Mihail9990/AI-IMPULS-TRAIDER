'use strict';
// Tab-local, bounded diagnostic facts only. Never reads storage, URLs, headers,
// bodies, accounts, order IDs, exception messages/stacks, or personal data.
function createPanelDiagnostics({wall=()=>Date.now(),mono=()=>performance.now()}={}){
 const LIMIT=120,MAX_BYTES=65536,records=[],latest=new Map();let dropped=0,total=0;
 const enums={source:['STATE','ANALYSIS_PATTERNS','ANALYSIS_STRUCTURE','ANALYSIS_MAIN'],stage:['FETCH','PARSE','VALIDATE','RECONCILE','RENDER','RECOVERY_RENDER','COMPLETE','DISPLAY'],outcome:['OK','ERROR','CANCELLED'],reason:['HTTP_ERROR','REQUEST_TIMEOUT','REQUEST_ABORTED','NETWORK_ERROR','JSON_INVALID','CONTRACT_MISMATCH','RECONCILE_ERROR','RENDER_ERROR','RECOVERY_RENDER_ERROR','SNAPSHOT_INVALID','ANALYSIS_NOT_READY','OTHER'],render_component:['TOTALS','STATUS','QUOTE','STATE','TICKETS','CONTROLS','FIELDS','V3','CHART','RECOVERY_ANALYSIS','RECOVERY_V3','RECOVERY_STATUS','RECOVERY_CHART'],error_type:['Error','TypeError','ReferenceError','SyntaxError','RangeError','SecurityError','QuotaExceededError','AbortError','TimeoutError'],timeframe:['M1','M15','M30','H1'],feed_status:['FRESH','STALE','UNVERIFIED','DISCONNECTED'],timestamp_semantics:['VERIFIED','UNVERIFIED'],analysis_status:['READY','STALE','DISCONNECTED','TIME_UNVERIFIED','WARMING_UP','UNAVAILABLE','ERROR']};
 enums.analysis_reason=['PROOF_DISCONNECTED','PROOF_TIME_UNVERIFIED','PROOF_STALE','PROOF_READY','CACHE_REUSED','HISTORY_RETRY_WAIT','PROOF_EXPIRED_DURING_HISTORY','PROOF_EXPIRED_DURING_CALCULATION','PROOF_EXPIRED_BEFORE_PUBLISH','MISSING_LAST_CLOSED_PERIOD','CALCULATED','ANALYSIS_SOURCE_UNAVAILABLE','ANALYSIS_WORKER_UNAVAILABLE','ANALYSIS_STARTUP_UNAVAILABLE','ANALYSIS_ENVIRONMENT_UNAVAILABLE','SOURCE_IDENTITY_MISMATCH'];
 enums.last_failure_reason=enums.analysis_reason;
 enums.proof_status=['READY','STALE','DISCONNECTED','TIME_UNVERIFIED'];
 enums.worker_state=['IDLE','STARTING','WAITING','READY','RETRY_WAIT','CLEANUP_FAILED','RETRY_EXHAUSTED'];
 enums.worker_error=['HISTORY_UNAVAILABLE','HISTORY_LIMIT','FIXED_TIME_PROFILE_REQUIRED','HISTORY_ORDER_OR_BOUNDARY','HISTORY_FUTURE','HISTORY_OHLC','HISTORY_UTC_ORDER','HISTORY_VOLUME','HISTORY_NO_CLOSED_BARS','SOURCE_IDENTITY_MISMATCH','MT5_INITIALIZE_FAILED','RESULT_LIMIT','COMMAND_LIMIT','INVALID_RESULT','WORKER_EXITED','IPC_READ_FAILED','STARTUP_TIMEOUT','RESPONSE_TIMEOUT','SUPERVISOR_START_FAILED','ANALYSIS_ERROR'];
 const numeric=new Set(['http_status','request_ms','fetch_ms','parse_ms','validation_ms','reconcile_ms','render_ms','feed_sequence','quote_age_seconds','generated_at','as_of','closed_through','bar_count','proof_age_seconds','expected_closed_through','history_ms','calculation_ms','ipc_ms','cache_age_seconds','worker_failures']);
 const bools=new Set(['read_only','fresh_gate','document_hidden','cache_reused']);
 const finite=(value)=>typeof value==='number'&&Number.isFinite(value)&&value>=0&&value<=8640000000000;
 const now=()=>{try{const value=mono();return finite(value)?value:0;}catch{return 0;}};
 function project(input){const out={};for(const[key,choices]of Object.entries(enums))if(choices.includes(input[key]))out[key]=input[key];for(const key of numeric)if(finite(input[key]))out[key]=Math.round(input[key]*1000)/1000;for(const key of bools)if(typeof input[key]==='boolean')out[key]=input[key];return out;}
 function record(input){try{
  const value=project(input);if(!value.source||!value.stage||!value.outcome)return;
  const tick=now(),stamp=wall(),at=finite(stamp)?new Date(stamp).toISOString():null;
  const signature=JSON.stringify(Object.fromEntries(Object.entries(value).filter(([key])=>!numeric.has(key))));
  const prior=latest.get(value.source);total++;
  if(prior&&records.includes(prior.row)&&prior.signature===signature&&tick>=prior.started&&tick-prior.started<60000){
   const maximum=Math.max(prior.row.max_request_ms??0,value.request_ms??0);
   Object.assign(prior.row,value,{last_device_utc:at,count:prior.row.count+1,max_request_ms:maximum});return;
  }
  const row={first_device_utc:at,last_device_utc:at,count:1,...value,max_request_ms:value.request_ms??0};records.push(row);latest.set(value.source,{row,signature,started:tick});
  if(records.length>LIMIT){records.shift();dropped++;}
 }catch{/* Logging must never affect display or the trading gate. */}}
 function text(){try{
  const header='Browser diagnostics v1; tab-local only; device UTC is unverified; max_records=120; max_bytes=65536; repeated facts coalesced for 60s; raw messages/stacks/URLs/headers/bodies/accounts/order IDs/storage omitted.\n';
  let selected=records.slice(),omitted=dropped;
  let result='';do{result=header+JSON.stringify({total_events:total,dropped_records:omitted,records:selected},null,2)+'\n';if(new TextEncoder().encode(result).length<=MAX_BYTES)return result;selected.shift();omitted++;}while(selected.length);
  return header+JSON.stringify({total_events:total,dropped_records:omitted,records:[]})+'\n';
 }catch{return 'Browser diagnostics unavailable. No raw data exported.\n';}}
 return{record,text,now};
}
