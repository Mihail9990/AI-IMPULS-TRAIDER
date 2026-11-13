'use strict';
// Tab-local, bounded diagnostic facts only. Never reads storage, URLs, headers,
// bodies, accounts, order IDs, exception messages/stacks, or personal data.
function createPanelDiagnostics({wall=()=>Date.now(),mono=()=>performance.now()}={}){
 const LIMIT=120,MAX_BYTES=65536,records=[],latest=new Map();let dropped=0,total=0;
 const enums={source:['STATE'],stage:['FETCH','PARSE','VALIDATE','RECONCILE','RENDER','RECOVERY_RENDER','COMPLETE','DISPLAY'],outcome:['OK','ERROR','CANCELLED'],reason:['HTTP_ERROR','REQUEST_TIMEOUT','REQUEST_ABORTED','NETWORK_ERROR','JSON_INVALID','CONTRACT_MISMATCH','RECONCILE_ERROR','RENDER_ERROR','RECOVERY_RENDER_ERROR','SNAPSHOT_INVALID','OTHER'],render_component:['TOTALS','STATUS','QUOTE','STATE','TICKETS','CONTROLS','FIELDS','V3','CHART','RECOVERY_V3','RECOVERY_STATUS','RECOVERY_CHART'],error_type:['Error','TypeError','ReferenceError','SyntaxError','RangeError','SecurityError','QuotaExceededError','AbortError','TimeoutError'],timeframe:['M1','M15','M30','H1'],feed_status:['FRESH','OBSERVED','STALE','UNVERIFIED','DISCONNECTED'],timestamp_semantics:['VERIFIED','BROKER_RAW','UNVERIFIED'],time_domain:['UTC','BROKER_RAW']};
 const numeric=new Set(['http_status','request_ms','fetch_ms','parse_ms','validation_ms','reconcile_ms','render_ms','feed_sequence','quote_age_seconds']);
 const bools=new Set(['read_only','fresh_gate','document_hidden']);
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
