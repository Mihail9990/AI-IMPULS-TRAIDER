'use strict';
// Node-only DOM doubles: validates actual V3 handler code, not browser layout.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const root=path.resolve(__dirname,'../web/basket-panel'),src=fs.readFileSync(path.join(root,'panel.js'),'utf8'),html=fs.readFileSync(path.join(root,'index.html'),'utf8');
assert(!html.includes('tab-live')&&!html.includes('tab-paper'),'no mode tabs');
assert(!src.includes('switchMode'),'role cannot switch');
for(const id of ['paper-controls','paper-history','paper-resume','paper-flatten','paper-save-cost'])assert(!html.includes('id="'+id+'"'),'removed simulator control '+id);
assert(!html.includes('<option value="paper">'),'no simulator log option');
for(const role of ['live','paper']){
 const c=vm.createContext({document:{body:{dataset:{role}}}});
 vm.runInContext(src.slice(src.indexOf(' const mode='),src.indexOf(' let view=')),c);
 assert.equal(vm.runInContext("apiPath('action')",c),'/api/action');
 assert.equal(vm.runInContext("storageKey('request')",c),'basket-request');
 assert.equal(vm.runInContext('mode',c),'live');
 assert.throws(()=>vm.runInContext("mode='other'",c));
}
const ids=[...html.matchAll(/id="([^"]+)"/g)].map(x=>x[1]);assert.equal(new Set(ids).size,ids.length,'unique HTML IDs');
class Element{
 constructor(){this.value='';this.textContent='';this.disabled=false;this.hidden=false;this.open=true;this.dataset={};this.children=[];this.classList={toggle:()=>{}};this.events={};this.attributes={};this.style={};this.clientHeight=520;this.clientWidth=1000;}
 focus(){}
 append(...xs){this.children.push(...xs);}setAttribute(k,v){this.attributes[k]=v;}replaceChildren(...xs){this.children=xs;}querySelector(){return this.now??=new Element();}addEventListener(type,fn){this.events[type]=fn;}
}
const elements=Object.fromEntries(ids.map(id=>[id,new Element()]));
const $=id=>{assert(elements[id],'known element '+id);return elements[id];};
Object.assign($('v3-order-type'),{value:'STOP'});$('v3-timing').value='current';$('v3-side').value='BUY';$('v3-count').value='6';
const base={recovery_mode:'V3',cycle_id:'c1',scope:'live',revision:7,read_only:false,positions:[{id:'p1'}],orders:[],v3_sl_distance:'1.50',v3_settings:{entry_distance:'1.00',count_presets:[4,6,10]},v3_controls:{can_enter:true,can_link:true,can_cancel_link:false,can_adjust_sl:true,reason:''}};
const actions=[],dialogs=[],errors=[],store=new Map();let accept=true;
const ctx=vm.createContext({$,document:{createElement:()=>new Element()},view:structuredClone(base),busy:false,pending:null,mode:'live',confirm:msg=>{dialogs.push(msg);return accept;},send:async(a,r,s)=>actions.push({a,r,s}),error:m=>errors.push(m),localStorage:{getItem:k=>store.get(k),setItem:(k,v)=>store.set(k,v)},fields:()=>{},console});
const part=src.slice(src.indexOf(' // V3 reuses scoped receipts'),src.indexOf(' function fields(){'));
vm.runInContext(part,ctx);vm.runInContext('v3Fresh=true;v3FillSettings();v3Render()',ctx);
async function main(){
 // Execute actual chart rendering with verified, unverified and missing semantics.
 ctx.svg=$('chart');ctx.ns='http://www.w3.org/2000/svg';ctx.document.createElementNS=()=>new Element();
 Object.assign(ctx,{lines:[],zoom:1,pan:0,timeOffset:0,visibleCount:60,frameHeight:520,plotBottom:480,plotHeight:450,innerWidth:1000,preview:null,previewFocus:null,previewWasOpen:false,prefs:{colors:{}},drawAnalysis:()=>{}});
 vm.runInContext(src.slice(src.indexOf(' function node('),src.indexOf(' function openPreview(')),ctx);
 const stamp=1804800000;
 const chartView={...structuredClone(base),levels:{},positions:[],orders:[],feed:{timestamp_semantics:'VERIFIED',timestamp:stamp,candles:[{utc_time:stamp,raw_time:stamp,open:1,close:2,low:1,high:2}],quote:{}}};
 ctx.view=chartView;vm.runInContext('draw()',ctx);
 assert($('chart').children.some(x=>x.textContent==='UTC'));
 assert($('chart').children.some(x=>x.textContent===new Date(stamp*1000).toISOString().slice(11,16)));
 for(const semantics of ['UNVERIFIED',undefined]){
  ctx.view.feed.timestamp_semantics=semantics;vm.runInContext('draw()',ctx);
  assert(!$('chart').children.some(x=>x.textContent==='UTC'),'unverified axis has no UTC claim');
  assert(!$('chart').children.some(x=>/^\d\d:\d\d$/.test(x.textContent)),'unverified clock ticks hidden, even if utc_time is supplied');
  assert($('chart').children.some(x=>x.textContent==='RAW'));
  assert.equal(vm.runInContext('utcLabel(view.feed.timestamp)',ctx),'');
 }
 ctx.view.v3_request={bar_id:stamp,order_type:'STOP',count:6};vm.runInContext('v3Render()',ctx);
 assert(!$('v3-request-status').textContent.includes('UTC'));
 ctx.view.feed.timestamp_semantics='VERIFIED';vm.runInContext('v3Render()',ctx);
 assert($('v3-request-status').textContent.includes('UTC'));
 for(const timestamp of [null,undefined,'','not a timestamp',1e30])assert.equal(vm.runInContext('utcLabel('+JSON.stringify(timestamp)+')',ctx),'');
 // Latest diagnostic is presentation-only; the original latch still blocks controls.
 ctx.view=structuredClone(base);ctx.view.time_block={reason:'MT5_UTC_WAIT_REFERENCE'};
 ctx.view.time_status={latched_reason:'MT5_UTC_WAIT_REFERENCE',current_reason:'MT5_UTC_CONTRACT_MISMATCH'};
 const safetyLine=src.split('\n').find(line=>line.includes("$('safety').textContent="));ctx.view.feed={};
 vm.runInContext(safetyLine+'v3Render()',ctx);
 assert($('safety').textContent.includes('MT5_UTC_CONTRACT_MISMATCH'));
 assert(!$('safety').textContent.includes('WAIT_REFERENCE'));
 assert($('safety').textContent.includes('Блокировка сохранена'));
 assert($('safety').textContent.includes('Проверить время'));
 assert.equal($('v3-submit-entry').disabled,true);
 assert.equal(ctx.view.time_block.reason,'MT5_UTC_WAIT_REFERENCE');
 delete ctx.view.time_status;vm.runInContext(safetyLine,ctx);assert($('safety').textContent.includes('MT5_UTC_WAIT_REFERENCE'),'legacy snapshot fallback');
 ctx.view.time_status={latched_reason:'MT5_UTC_WAIT_REFERENCE',current_reason:null};
 vm.runInContext(safetyLine+'v3Render()',ctx);assert.equal($('v3-submit-entry').disabled,true,'null current reason cannot clear latch');
 ctx.view.time_block=null;vm.runInContext(safetyLine,ctx);assert.equal($('safety').textContent,'','presentation status cannot establish latch');
 ctx.view=structuredClone(base);vm.runInContext('v3Render()',ctx);
 // Execute the actual existing-button render assignment, retaining all legacy guards.
 const ownEntryLine=src.split('\n').find(line=>line.includes("$('own-entry-sl').disabled="));
 const renderOwn=()=>vm.runInContext(ownEntryLine,ctx);
 ctx.view.orders=[{id:'linked-stop'}];ctx.view.v3_controls.can_own_entry_sl=true;
 renderOwn();assert.equal($('own-entry-sl').disabled,false,'V3 permits known linked pending');
 ctx.view.v3_controls.can_own_entry_sl=false;renderOwn();assert.equal($('own-entry-sl').disabled,true,'two groups rejected by backend flag');
 delete ctx.view.v3_controls.can_own_entry_sl;renderOwn();assert.equal($('own-entry-sl').disabled,true,'missing V3 permission fails closed');
 ctx.view.v3_controls.can_own_entry_sl=true;
 for(const field of ['read_only','edit','manual_close','manual_reason','migration_block','time_block']){ctx.view[field]=true;renderOwn();assert.equal($('own-entry-sl').disabled,true,field);ctx.view[field]=false;}
 ctx.busy=true;renderOwn();assert.equal($('own-entry-sl').disabled,true);ctx.busy=false;
 ctx.pending='unknown';renderOwn();assert.equal($('own-entry-sl').disabled,true);ctx.pending=null;
 ctx.view.recovery_mode='V1';renderOwn();assert.equal($('own-entry-sl').disabled,true,'V1 pending behavior unchanged');
 ctx.view.orders=[];renderOwn();assert.equal($('own-entry-sl').disabled,true,'legacy state never permits V3 actions');
 ctx.view=structuredClone(base);
 assert.equal($('v3-panels').hidden,false);assert(!elements['manual-close-controls']);assert.equal($('v3-side').disabled,true);assert.equal($('v3-presets').children.length,3);
 $('v3-presets').children[2].onclick();assert.equal($('v3-count').value,'10');
 let before=actions.length;$('v3-count').value='1.5';await $('v3-submit-entry').onclick();assert.equal(actions.length,before);assert(errors.at(-1).includes('целое'));
 $('v3-count').value='8';$('v3-order-type').value='MARKET';$('v3-timing').value='now';$('v3-side').value='SELL';$('v3-timing').onchange();assert.equal($('v3-side').disabled,false);
 accept=false;await $('v3-submit-entry').onclick();assert.equal(actions.length,before);accept=true;
 await $('v3-submit-entry').onclick();assert.equal(actions.at(-1).a.count,8);assert.equal(actions.at(-1).a.side,'SELL');assert.equal(actions.at(-1).a.timing,'now');assert.equal(actions.at(-1).r,7);assert.equal(actions.at(-1).s,'live');
 $('v3-order-type').value='STOP';$('v3-order-type').onchange();assert.equal($('v3-timing').value,'current');assert.equal($('v3-side').disabled,true);
 $('v3-timing').value='next';await $('v3-submit-entry').onclick();assert.equal(actions.at(-1).a.order_type,'STOP');assert.equal(actions.at(-1).a.timing,'next');
 await $('v3-sl-minus').onclick();assert.equal(actions.at(-1).a.distance,'1.40');await $('v3-sl-plus').onclick();assert.equal(actions.at(-1).a.distance,'1.60');
 const d=dialogs.length;await $('v3-link-toggle').onclick();assert.equal(actions.at(-1).a.type,'v3_link');assert.equal(dialogs.length,d+1);
 ctx.view.v3_link={side:'SELL',price:'3998.5',count:6};ctx.view.v3_controls.can_cancel_link=true;vm.runInContext('v3Render()',ctx);const d2=dialogs.length;await $('v3-link-toggle').onclick();assert.equal(actions.at(-1).a.type,'v3_cancel_link');assert.equal(dialogs.length,d2,'cancel no dialog');
 ctx.view.v3_request={bar_id:1804800000,order_type:'STOP',count:6};vm.runInContext('v3Render()',ctx);assert.equal($('v3-cancel-request').hidden,false);await $('v3-cancel-request').onclick();assert.equal(actions.at(-1).a.type,'v3_cancel_request');
 $('v3-setting-distance').value='2.00';$('v3-preset-0').value='3';$('v3-preset-1').value='5';$('v3-preset-2').value='9';await $('save-v3-settings').onclick();assert.equal(actions.at(-1).a.entry_distance,'2.00');assert.equal(actions.at(-1).a.count_presets.join(','),'3,5,9');
 $('v3-preset-1').value='0';before=actions.length;await $('save-v3-settings').onclick();assert.equal(actions.length,before);
 for(const block of ['busy','pending','read_only','stale']){
  ctx.busy=block==='busy';ctx.pending=block==='pending'?'req':null;ctx.view.read_only=block==='read_only';vm.runInContext('v3Fresh='+String(block!=='stale')+';v3Render()',ctx);
  before=actions.length;await $('v3-link-toggle').onclick();await $('v3-submit-entry').onclick();await $('v3-sl-plus').onclick();assert.equal(actions.length,before,block);assert.equal($('v3-link-toggle').disabled,true);
 }
 ctx.busy=false;ctx.pending=null;ctx.view.read_only=false;vm.runInContext('v3Fresh=true',ctx);
 ctx.view.recovery_mode='V1';ctx.view.migration_block='Legacy active cycle needs reconciliation';vm.runInContext('v3Render()',ctx);
 assert.equal($('v3-panels').hidden,false,'V3 remains the only panel even for blocked legacy state');assert.equal($('v3-submit-entry').disabled,true);
 before=actions.length;await $('v3-submit-entry').onclick();await $('save-v3-settings').onclick();assert.equal(actions.length,before);
 assert(!elements['recovery-mode']);assert(!elements['save-recovery-mode']);assert(!elements['manual-close-submit']);
 assert(!src.includes("type:'recovery_mode'"));assert(!src.includes("type:'manual_close'"));
 assert(!html.includes('value="V1"'));assert(!html.includes('V3.3'));
 for(const id of ['start','stop','resume','reset','cancel-initial','own-entry-sl','reverse-enable','reverse-disable','save-settings','save-tp-grid','writeoff-recovery','logs-download'])assert(elements[id],'retained control '+id);
 ctx.view=structuredClone(base);vm.runInContext('v3Render()',ctx);
 // Settings still submit the full server schema without exposing retired range/lot inputs.
 ctx.view.settings={EXPANSION_DISTANCE:'2',TICKET_SIZE:'0.03',INITIAL_SIZE:'0.01',BASKET_COUNT:6};
 elements['setting-INITIAL_SIZE']=new Element();elements['setting-INITIAL_SIZE'].value='0.02';
 elements['setting-BASKET_COUNT']=new Element();elements['setting-BASKET_COUNT'].value='8';
 ctx.names={};
 vm.runInContext(src.split('\n').find(line=>line.includes("$('save-settings').onclick=")),ctx);
 await $('save-settings').onclick();assert.equal(actions.at(-1).a.type,'basket_settings');
 assert.equal(actions.at(-1).a.settings.EXPANSION_DISTANCE,'2');assert.equal(actions.at(-1).a.settings.TICKET_SIZE,'0.03');
 assert.equal(actions.at(-1).a.settings.INITIAL_SIZE,'0.02');assert.equal(actions.at(-1).a.settings.BASKET_COUNT,8);
 assert(!dialogs.at(-1).includes('EXPANSION_DISTANCE'));assert(!dialogs.at(-1).includes('TICKET_SIZE'));
 $('v3-sl-panel').open=false;$('v3-sl-panel').events.toggle();assert.equal(store.get('v3-sl-panel-open'),'false');assert.equal($('v3-entry-panel').open,true);
 // Actual viewport, quote rendering and navigation code; no action requests allowed.
 ctx.view={...structuredClone(chartView),feed:{...chartView.feed,timestamp_semantics:'VERIFIED',quote:{bid:'1.50',ask:'1.55'},candles:Array.from({length:180},(_,i)=>({utc_time:stamp+i*60,open:1,close:2,low:1,high:2}))}};
 ctx.visibleCount=60;ctx.timeOffset=0;vm.runInContext('draw()',ctx);
 assert.equal($('chart').dataset.firstCandle,String(stamp+120*60));
 assert.equal($('chart').children.filter(x=>x.attributes['data-quote']).length,2,'Bid/Ask rendered independently of positions');
 $('chart').setPointerCapture=()=>{};$('chart').getBoundingClientRect=()=>({left:0,top:0,width:390,height:600});
 ctx.hold=null;ctx.gesture=null;ctx.setTimeout=()=>123;ctx.clearTimeout=()=>{};
 vm.runInContext(src.slice(src.indexOf(' const pointers=new Map()'),src.indexOf(" $('cancel-level').onclick")),ctx);
 const event=(id,x,y)=>({pointerId:id,clientX:x,clientY:y,target:{closest:()=>null},preventDefault:()=>{},type:'pointerup'});
 before=actions.length;
 $('chart').events.pointerdown(event(1,80,300));$('chart').events.pointermove(event(1,180,300));$('chart').events.pointerup(event(1,180,300));
 assert(Number($('chart').dataset.firstCandle)<stamp+120*60,'drag right reveals older candles');
 const first=Number($('chart').dataset.firstCandle);
 $('chart').events.pointerdown(event(2,150,400));$('chart').events.pointermove(event(2,150,200));$('chart').events.pointerup(event(2,150,200));
 assert(Number($('chart').dataset.firstCandle)<first,'forward swipe reveals older candles');
 const count=ctx.visibleCount;
 $('chart').events.pointerdown(event(3,100,200));$('chart').events.pointerdown(event(4,200,200));$('chart').events.pointermove(event(4,300,200));
 assert(ctx.visibleCount<count,'pinch out zooms candles');$('chart').events.pointerup(event(3,100,200));$('chart').events.pointerup(event(4,300,200));
 $('chart-reset').onclick();assert.equal(ctx.timeOffset,0);assert.equal(ctx.visibleCount,60);
 ctx.requestAnimationFrame=fn=>fn();ctx.document.addEventListener=()=>{};
 for(const e of Object.values(elements))e.focus=()=>{};
 vm.runInContext(src.slice(src.indexOf(' // Presentation-only navigation:'),src.indexOf(' layout();window.addEventListener')),ctx);
 $('tab-settings').onclick();assert.equal($('chart-page').hidden,true);assert.equal($('settings-page').hidden,false);assert.equal($('tab-settings').attributes['aria-selected'],'true');
 $('tab-chart').onclick();assert.equal($('chart-page').hidden,false);
 $('drawer-toggle').onclick();assert.equal($('drawer-content').hidden,false);$('drawer-toggle').onclick();assert.equal($('drawer-content').hidden,true);
 ctx.preview={line:{},value:1};$('tab-settings').onclick();assert.equal($('settings-page').hidden,true,'active edit preview cannot disappear behind settings');ctx.preview=null;
 ctx.view.feed.status='FRESH';vm.runInContext('v3Fresh=true;connectionLabel()',ctx);assert.equal($('chart').dataset.feedFresh,'true');
 vm.runInContext('v3Fresh=false;connectionLabel()',ctx);assert($('drawer-toggle').textContent.includes('нет свежего снимка'));assert.equal($('chart').dataset.feedFresh,'false');
 ctx.view.feed.timestamp_semantics='UNVERIFIED';vm.runInContext('v3Fresh=true;connectionLabel()',ctx);assert($('drawer-toggle').textContent.includes('время не проверено'));
 ctx.preview={value:1.5};vm.runInContext('draw()',ctx);assert.equal($('preview-backdrop').hidden,false);assert.equal($('command-drawer').inert,true);assert.equal($('bottom-tabs').inert,true);
 ctx.preview=null;vm.runInContext('draw()',ctx);assert.equal($('preview-backdrop').hidden,true);assert.equal($('command-drawer').inert,false);
 assert.equal(actions.length,before,'all gestures and navigation are display-only');
 console.log('PASS Node V3 handler tests: actual source, DOM doubles, inputs/actions/revisions, V3-only controls, legacy migration guard, confirmation/cancel, controls, stale/read-only/UNKNOWN guards. Browser layout NOT verified.');
}
main().catch(e=>{console.error(e);process.exit(1);});
