'use strict';
// Executes actual browser helpers/poll/export source. No network or broker.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.join(__dirname,'../web/basket-panel'),source=fs.readFileSync(path.join(root,'panel.js'),'utf8'),diagnosticSource=fs.readFileSync(path.join(root,'diagnostics.js'),'utf8');
let tick=0,wall=1800000000000;const clock=()=>tick+=2;
const ctx=vm.createContext({Date,TextEncoder,performance:{now:clock},wall:()=>wall,mono:clock});vm.runInContext(diagnosticSource,ctx);
const diagnostics=vm.runInContext('createPanelDiagnostics({wall,mono})',ctx),records=()=>JSON.parse(diagnostics.text().split('\n').slice(1).join('\n')).records;
const secrets='password cookie bearer account path private@example.com';
diagnostics.record({source:'STATE',stage:'FETCH',outcome:'ERROR',error_type:'TypeError',reason:'NETWORK_ERROR',request_ms:4,message:secrets,stack:secrets,url:secrets,account:secrets,headers:{cookie:secrets},body:secrets});
assert(!diagnostics.text().includes(secrets));assert.equal(records()[0].error_type,'TypeError');
for(let i=0;i<40;i++)diagnostics.record({source:'STATE',stage:'FETCH',outcome:'ERROR',error_type:'TypeError',reason:'NETWORK_ERROR',request_ms:i});
assert.equal(records().length,1);assert.equal(records()[0].count,41);assert.equal(records()[0].max_request_ms,39);
for(let i=0;i<180;i++){tick+=60000;diagnostics.record({source:'STATE',stage:'COMPLETE',outcome:'OK',request_ms:1,feed_sequence:i});}
assert.equal(records().length,120);assert(Buffer.byteLength(diagnostics.text())<=65536);assert(JSON.parse(diagnostics.text().split('\n').slice(1).join('\n')).dropped_records>0);
diagnostics.record({source:'STATE',stage:'COMPLETE',outcome:'OK',feed_sequence:Infinity,quote_age_seconds:NaN,generated_at:-1,reason:secrets,error_type:secrets});assert(!diagnostics.text().includes(secrets));

vm.runInContext(fs.readFileSync(path.join(root,'analysis.js'),'utf8'),ctx);
const snapshot={status:'STALE',diagnostics:{reason:'PROOF_EXPIRED_BEFORE_PUBLISH',proof_status:'READY',error_code:'HISTORY_UNAVAILABLE',supervisor_state:'READY',proof_age_seconds:0.3,expected_closed_through:1800000000,history_ms:1,calculation_ms:6500,cache_age_seconds:1,secret:secrets},bars:[],symbol:secrets,feed_instance:secrets};ctx.snapshot=snapshot;
const facts=vm.runInContext('analysisDiagnosticFacts(snapshot)',ctx);diagnostics.record({source:'ANALYSIS_PATTERNS',stage:'COMPLETE',outcome:'OK',...facts});assert.equal(records().at(-1).analysis_reason,'PROOF_EXPIRED_BEFORE_PUBLISH');assert.equal(records().at(-1).calculation_ms,6500);assert(!diagnostics.text().includes(secrets));
ctx.snapshot.diagnostics.reason=secrets;ctx.snapshot.diagnostics.error_code=secrets;diagnostics.record({source:'ANALYSIS_PATTERNS',stage:'COMPLETE',outcome:'OK',...vm.runInContext('analysisDiagnosticFacts(snapshot)',ctx)});assert(!diagnostics.text().includes(secrets));
diagnostics.record({source:'STATE',stage:'COMPLETE',outcome:'OK',feed_status:'OBSERVED',timestamp_semantics:'BROKER_RAW',time_domain:'BROKER_RAW'});assert.equal(records().at(-1).feed_status,'OBSERVED');assert.equal(records().at(-1).timestamp_semantics,'BROKER_RAW');assert.equal(records().at(-1).time_domain,'BROKER_RAW');
let testMode='ok',fetches=0;const nodes={};const $=id=>nodes[id]??={hidden:false,textContent:'',value:'live'};
Object.assign(ctx,{$,document:{hidden:false},polling:false,busy:false,view:null,v3Fresh:false,diagnosticRenderComponent:null,pending:null,pendingScope:null,mainAnalysisUI:null,headers:()=>({}),apiPath:()=>'/api/state',AbortSignal,diagnostics,
 diagnosticNow:clock,diagnosticRecord:v=>diagnostics.record(v),diagnosticFacts:()=>({feed_status:ctx.view?.feed?.status,timestamp_semantics:ctx.view?.feed?.timestamp_semantics,quote_age_seconds:ctx.view?.feed?.age_seconds,read_only:ctx.view?.read_only,fresh_gate:ctx.v3Fresh}),
 mt5DisplayAnchor:null,observeMt5DisplayClock(){},renderMt5TimeControls(){},rememberWaiting(){},clearPending(){},reconcileWaiting(){if(testMode==='reconcile')throw new DOMException(secrets,'SecurityError');},
 v3Render(){if(testMode==='recovery')throw new RangeError(secrets);},connectionLabel(){},updateBotActivity(){},draw(){},
 fetch:async()=>{fetches++;if(testMode==='timeout')throw new DOMException(secrets,'TimeoutError');if(testMode==='network')throw new TypeError(secrets);return {ok:testMode!=='http',status:testMode==='http'?401:200,json:async()=>{if(testMode==='parse-timeout')throw new DOMException(secrets,'TimeoutError');if(testMode==='parse')throw new SyntaxError(secrets);return {contract:testMode==='validate'?'wrong':'ticket_basket_v1',mode:'auto',read_only:true,feed:{status:'FRESH',age_seconds:.5,quote:{bid:'4000.12',ask:'4000.34'},timestamp_semantics:'VERIFIED'}};}};},
 render(){nodes.quote={textContent:`Bid ${ctx.view.feed.quote.bid} / Ask ${ctx.view.feed.quote.ask}`};if(['render','recovery'].includes(testMode))throw new TypeError(secrets);}});
vm.runInContext(source.slice(source.indexOf(' async function poll(){'),source.indexOf(' const defaultPrefs=')),ctx);
const poll=()=>vm.runInContext('poll()',ctx);
(async()=>{
 await poll();assert.equal(ctx.v3Fresh,true);assert.equal(records().at(-1).stage,'COMPLETE');
 for(const [mode,stage,reason]of [['http','FETCH','HTTP_ERROR'],['timeout','FETCH','REQUEST_TIMEOUT'],['network','FETCH','NETWORK_ERROR'],['parse','PARSE','JSON_INVALID'],['parse-timeout','PARSE','REQUEST_TIMEOUT'],['validate','VALIDATE','CONTRACT_MISMATCH'],['reconcile','RECONCILE','RECONCILE_ERROR'],['render','RENDER','RENDER_ERROR']]){
  testMode=mode;await poll();assert.equal(ctx.v3Fresh,false,mode);assert.equal(ctx.polling,false);assert.equal(records().at(-1).stage,stage);assert.equal(records().at(-1).reason,reason);assert(!diagnostics.text().includes(secrets));
 }
 assert.equal(nodes.quote.textContent,'Bid 4000.12 / Ask 4000.34');assert.equal(ctx.view.feed.status,'FRESH');assert(nodes['feed-status'].textContent.includes('Ошибка отображения'));assert.equal(records().at(-1).http_status,200);
 testMode='recovery';await poll();assert.equal(ctx.v3Fresh,false);assert.equal(records().at(-1).stage,'RECOVERY_RENDER');assert(nodes['feed-status'].textContent.includes('Команды заблокированы'));
 testMode='ok';await poll();assert.equal(ctx.v3Fresh,true);ctx.busy=true;const count=fetches;await poll();assert.equal(fetches,count);ctx.busy=false;
 ctx.diagnosticRecord=()=>{};await poll();assert.equal(ctx.v3Fresh,true,'diagnostic absence does not change gate');
 // Browser-only export works without a server request; ordinary exports retain
 // their sanitized server data and append the bounded local facts.
 let serverMode='ok',downloads=[],downloadFetches=0;
 Object.assign(ctx,{Blob,URL:{createObjectURL(blob){downloads.push(blob);return 'blob:local-test';},revokeObjectURL(){}},setTimeout(){},diagnosticText:()=>diagnostics.text(),document:{body:{append(){}},createElement:()=>({click(){},remove(){}})},
 fetch:async()=>{downloadFetches++;if(serverMode==='offline')throw new TypeError('offline');return {ok:true,json:async()=>({text:'Sanitized server log',page:0,has_more:false}),blob:async()=>serverMode==='oversize'?new Blob(['x'.repeat(1048577)]):new Blob(['Sanitized server log'])};}});
 vm.runInContext(source.slice(source.indexOf(' let logPage=0;'),source.indexOf(' // Local display preference only;')),ctx);
 $('log-scope').value='browser';await vm.runInContext('showLogs()',ctx);assert.equal(downloadFetches,0);assert(nodes['logs-view'].textContent.includes('Browser diagnostics'));await $('logs-download').onclick();assert.equal(downloadFetches,0);assert((await downloads.at(-1).text()).includes('Browser diagnostics'));
 $('log-scope').value='live';await vm.runInContext('showLogs()',ctx);assert(nodes['logs-view'].textContent.includes('Sanitized server log'));assert(nodes['logs-view'].textContent.includes('Browser diagnostics'));await $('logs-download').onclick();let exported=await downloads.at(-1).text();assert(exported.includes('Sanitized server log')&&exported.includes('Browser diagnostics'));assert(downloads.at(-1).size<=1048576+65536+2);assert(!exported.includes(secrets));
 const accepted=downloads.length;serverMode='oversize';await $('logs-download').onclick();assert.equal(downloads.length,accepted);serverMode='offline';await $('logs-download').onclick();assert.equal(downloads.length,accepted);assert(nodes['logs-status'].textContent.includes('Диагностику браузера'));
 $('log-scope').value='browser';await $('logs-download').onclick();assert.equal(downloads.length,accepted+1);

 const pendingPages=[];ctx.fetch=(url)=>new Promise(resolve=>pendingPages.push({url,resolve}));$('log-scope').value='live';
 vm.runInContext('logPage=0',ctx);const oldPage=vm.runInContext('showLogs()',ctx);vm.runInContext('logPage=1',ctx);const newPage=vm.runInContext('showLogs()',ctx);
 pendingPages[1].resolve({ok:true,json:async()=>({text:'Page one',page:1,has_more:false})});await newPage;assert.equal(nodes['logs-view'].textContent,'Page one');
 pendingPages[0].resolve({ok:true,json:async()=>({text:'Obsolete page zero',page:0,has_more:true})});await oldPage;assert.equal(nodes['logs-view'].textContent,'Page one','obsolete log response cannot replace newer page');
 console.log('PASS bounded private browser diagnostics; exact-source FETCH/PARSE/VALIDATE/RECONCILE/RENDER classification; fail-closed gate; recovery; authenticated log appendix and offline browser-only export');
})().catch(e=>{console.error(e);process.exitCode=1;});
