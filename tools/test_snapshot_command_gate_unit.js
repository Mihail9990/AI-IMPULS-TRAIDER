'use strict';
// Exact production handlers, mocked HTTP only: never connects to MT5.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const src=fs.readFileSync(require('path').join(__dirname,'../web/basket-panel/panel.js'),'utf8');
const store=new Map(),requests=[];let response='APPLIED',serial=0;
const c=vm.createContext({view:{scope:'live',revision:17,read_only:false},v3Fresh:false,v3ContextInvalid:false,busy:false,pending:null,pendingScope:null,preview:null,lastActionReceipt:null,
 crypto:{randomUUID:()=>`id-${++serial}`},localStorage:{setItem:(k,v)=>store.set(k,v),removeItem:k=>store.delete(k)},storageKey:k=>k,
 notice(){},updateBotActivity(){},headers:()=>({'X-Controller-Scope':'live'}),apiPath:()=>'/api/action',AbortSignal,poll:async()=>{},rememberWaiting(){},
 fetch:async(url,opt)=>{requests.push(JSON.parse(opt.body));if(response==='timeout')throw new DOMException('timeout','TimeoutError');return{ok:typeof response!=='number',status:typeof response==='number'?response:200,json:async()=>({status:response})};}});
vm.runInContext(src.slice(src.indexOf(' function clearPending()'),src.indexOf(' async function command(')),c);
vm.runInContext(src.split('\n').find(x=>x.includes(' const v3Blocked=')),c);
const run=code=>vm.runInContext(code,c);
(async()=>{
 assert.equal(run('v3Blocked()'),false,'transport freshness is not command permission');
 await run("send({type:'command',text:'/stop'})");assert.equal(requests.length,1);assert.equal(requests[0].revision,17);assert.equal(c.pending,null);
 for(const field of ['read_only','manual_reason','migration_block','time_block']){c.view[field]=true;assert.equal(run('v3Blocked()'),true,field);delete c.view[field];}
 c.v3ContextInvalid=true;assert.equal(run('v3Blocked()'),true);await assert.rejects(run("send({type:'command',text:'/stop'})"));assert.equal(requests.length,1);c.v3ContextInvalid=false;
 const old=c.view;c.view=null;assert.equal(run('v3Blocked()'),true);await assert.rejects(run("send({type:'command',text:'/stop'})"));c.view=old;
 await assert.rejects(run("send({type:'command',text:'/stop'},17,'other')"));assert.equal(requests.length,1);
 response='timeout';await assert.rejects(run("send({type:'command',text:'/stop'})"));assert.equal(requests.length,2);const pending=c.pending;assert(pending);assert.equal(store.get('request'),pending);
 await assert.rejects(run("send({type:'command',text:'/stop'})"));assert.equal(requests.length,2,'uncertain result never auto-retried or resubmitted');assert.equal(run('v3Blocked()'),true);
 run('clearPending()');response='UNKNOWN';await run("send({type:'command',text:'/stop'})");assert(c.pending);await assert.rejects(run("send({type:'command',text:'/stop'})"));assert.equal(requests.length,3);
 run('clearPending()');response='WAITING_DATA';await run("send({type:'command',text:'/stop'})");assert.equal(c.pending,null);assert.equal(requests.length,4);
 for(const code of [401,403]){response=code;await assert.rejects(run("send({type:'command',text:'/stop'})"));assert.equal(c.v3ContextInvalid,true);assert.equal(c.pending,null);const n=requests.length;await assert.rejects(run("send({type:'command',text:'/stop'})"));assert.equal(requests.length,n);c.v3ContextInvalid=false;}
 console.log('PASS stale snapshot submission, no first/invalid context, scope/revision, client hard-state guards and persistent UNKNOWN/timeout duplicate prevention; no trades');
})().catch(e=>{console.error(e);process.exitCode=1;});
