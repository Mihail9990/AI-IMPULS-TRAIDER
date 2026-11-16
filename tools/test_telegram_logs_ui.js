'use strict';
// Offline handler tests: no Telegram, broker or other network is contacted.
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync('web/basket-panel/panel.js','utf8');
const block=source.slice(source.indexOf(' // Telegram diagnostics are independent'),source.indexOf(' // End Telegram diagnostics controls.'));
const html=fs.readFileSync('web/basket-panel/index.html','utf8');
assert(html.includes('id="logs-telegram-send" disabled'));
function fixture(saved=null){
 const elements={},calls=[],timers=[],storage=new Map(saved?[['basket-telegram-log-request',saved]]:[]);
 const el=id=>elements[id]??=( {disabled:false,textContent:'',dataset:{},addEventListener(type,fn){this[type]=fn;}} );
 let respond=async()=>({ok:true,json:async()=>({status:'READY',queued_records:3})});
 const ctx=vm.createContext({$:el,notice:(id,text,tone)=>{el(id).textContent=text;el(id).dataset.tone=tone;},storageKey:x=>'basket-'+x,localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},crypto:{randomUUID:()=> 'request-1'},fetch:async(...args)=>{calls.push(args);return respond(...args);},AbortSignal:{timeout:()=>null},setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout:()=>{},Number});
 vm.runInContext(block,ctx);return{el,calls,timers,storage,respond:fn=>respond=fn,run:s=>vm.runInContext(s,ctx)};
}
(async()=>{
 const f=fixture();await f.run('telegramRefresh()');assert(!f.el('logs-telegram-send').disabled);
 // SAFE_PAUSE/read_only is intentionally not consulted; transport is non-trading.
 let release;f.respond(()=>new Promise(resolve=>release=resolve));
 const send=f.el('logs-telegram-send').onclick();assert(f.el('logs-telegram-send').disabled);
 await f.el('logs-telegram-send').onclick();assert.equal(f.calls.filter(c=>c[0].endsWith('/send')).length,1);
 assert.deepEqual(JSON.parse(f.calls.at(-1)[1].body),{request_id:'request-1'});
 release({ok:true,json:async()=>({status:'QUEUED',request_id:'request-1',last_result:'QUEUED'})});await send;
 assert(f.el('logs-telegram-send').disabled);assert(f.storage.size===1);
 f.respond(async()=>({ok:true,json:async()=>({status:'READY',request_id:'request-1',last_result:'SENT'})}));await f.run('telegramRefresh()');
 assert.equal(f.el('logs-telegram-status').dataset.tone,'success');assert.equal(f.storage.size,0);assert(!f.el('logs-telegram-send').disabled);
 assert(f.calls.some(c=>c[0]==='/api/logs/telegram?request_id=request-1'),'poll exact durable receipt');
 // A lost POST response is followed only by GET, never another POST.
 f.respond(async()=>{throw Error('network');});await f.el('logs-telegram-send').onclick();
 const posts=f.calls.filter(c=>c[0].endsWith('/send')).length;
 f.respond(async()=>({ok:true,json:async()=>({status:'UNKNOWN',request_id:'request-1',last_result:'UNKNOWN'})}));await f.timers.at(-1)();
 assert.equal(f.calls.filter(c=>c[0].endsWith('/send')).length,posts);assert(f.el('logs-telegram-send').disabled);assert(f.el('logs-telegram-status').textContent.includes('мог уже прийти'));
 // Reload preserves unknown request; another request's success is not ours.
 const g=fixture('request-old');g.respond(async()=>({ok:true,json:async()=>({status:'READY',request_id:'someone-else',last_result:'SENT'})}));await g.run('telegramRefresh()');
 assert(g.el('logs-telegram-send').disabled);assert.notEqual(g.el('logs-telegram-status').dataset.tone,'success');
 for(const status of ['DISABLED','CONFIGURATION_ERROR']){const h=fixture();h.respond(async()=>({ok:true,json:async()=>({status})}));await h.run('telegramRefresh()');assert(h.el('logs-telegram-send').disabled);}
 const e=fixture('request-1');e.respond(async()=>({ok:true,json:async()=>({status:'READY',request_id:'request-1',last_result:'EMPTY'})}));await e.run('telegramRefresh()');assert(e.el('logs-telegram-status').textContent.includes('нет'));assert(!e.el('logs-telegram-send').disabled);
 const r=fixture('request-1');r.respond(async()=>({ok:true,json:async()=>({status:'RETRY',request_id:'request-1',last_result:'RETRY'})}));await r.run('telegramRefresh()');assert(r.el('logs-telegram-status').textContent.includes('сохранены'));assert(!r.el('logs-telegram-send').disabled);
 const c=fixture();await c.run('telegramRefresh()');c.respond(async()=>({ok:true,json:async()=>({status:'SENDING',request_id:'coalesced',last_result:'SENDING'})}));await c.el('logs-telegram-send').onclick();assert.equal(c.storage.get('basket-telegram-log-request'),'coalesced');
 const a=fixture();await a.run('telegramRefresh()');a.respond(async()=>({ok:true,json:async()=>({status:'SENDING',request_id:null,last_result:null})}));await a.el('logs-telegram-send').onclick();assert.equal(a.storage.size,0);assert(a.el('logs-telegram-send').disabled);
 const missing=fixture('request-old');missing.respond(async()=>({ok:true,json:async()=>({status:'READY',request_id:null,last_result:null})}));await missing.run('telegramRefresh()');assert(!missing.el('logs-telegram-send').disabled);assert.equal(missing.el('logs-telegram-send').textContent,'Повторить запрос отправки');assert(!missing.calls.some(c=>c[0].endsWith('/send')));missing.respond(async()=>({ok:true,json:async()=>({status:'QUEUED',request_id:'request-old',last_result:'QUEUED'})}));await missing.el('logs-telegram-send').onclick();assert.equal(JSON.parse(missing.calls.at(-1)[1].body).request_id,'request-old');
 for(const status of [400,401,403]){const denied=fixture();await denied.run('telegramRefresh()');denied.respond(async()=>({ok:false,status}));await denied.el('logs-telegram-send').onclick();assert.equal(denied.storage.size,0);assert(denied.el('logs-telegram-status').textContent.includes(status===400?'отклонил':'Войдите'));denied.respond(async()=>({ok:true,json:async()=>({status:'READY',request_id:null,last_result:null})}));await denied.run('telegramRefresh()');assert(!denied.el('logs-telegram-send').disabled);}
 console.log('PASS Telegram controls: double click, queued/confirmed/empty/rejected/unknown, lost reply, reload, disabled, no trading dependency');
})().catch(e=>{console.error(e);process.exitCode=1;});
