'use strict';
// Node-only DOM doubles: validates actual V3 handler code, not browser layout.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const root=path.resolve(__dirname,'../web/basket-panel'),src=fs.readFileSync(path.join(root,'panel.js'),'utf8'),html=fs.readFileSync(path.join(root,'index.html'),'utf8');
assert(!html.includes('tab-live')&&!html.includes('tab-paper'),'no mode tabs');
assert(!src.includes('switchMode'),'role cannot switch');
for(const id of ['paper-controls','paper-history','paper-resume','paper-flatten','paper-save-cost'])assert(!html.includes('id="'+id+'"'),'removed simulator control '+id);
assert(!html.includes('<option value="paper">'),'no simulator log option');
const writeoffHandler=src.split('\n').find(x=>x.includes("$('writeoff-recovery').onclick"));assert(writeoffHandler.includes('if(!confirm('),'writeoff still requires confirmation');
for(const role of ['live','paper']){
 const c=vm.createContext({document:{body:{dataset:{role}}}});
 vm.runInContext(src.slice(src.indexOf(' const mode='),src.indexOf(' let view=')),c);
 assert.equal(vm.runInContext("apiPath('action')",c),'/api/action');
 assert.equal(vm.runInContext("storageKey('request')",c),'basket-request');
 assert.equal(vm.runInContext('mode',c),'live');
 assert.throws(()=>vm.runInContext("mode='other'",c));
}
assert(html.includes('id="v3-sl-confirm"'),'SL has explicit commit control');
assert(html.includes('class="entry-details"'),'order explanations remain accessible in details');
assert(html.includes('aria-label="Расстояние SL от входа"'));
const compactCss=fs.readFileSync(path.join(root,'panel.css'),'utf8');
assert(compactCss.includes('position:absolute;top:auto;bottom:60px'),'drawers anchor at chart bottom');
assert(compactCss.includes('max-height:42%'),'order drawer leaves most chart visible');
const ids=[...html.matchAll(/id="([^"]+)"/g)].map(x=>x[1]);assert.equal(new Set(ids).size,ids.length,'unique HTML IDs');
class Element{
 constructor(){this.value='';this.textContent='';this.disabled=false;this.hidden=false;this.open=true;this.dataset={};this.children=[];this.classList={toggle:()=>{}};this.events={};this.attributes={};this.style={};this.clientHeight=520;this.clientWidth=1000;}
 focus(){}
 scrollIntoView(){}
 createSVGPoint(){return{x:0,y:0,matrixTransform(m){return{x:(this.x-m.left)/m.scale,y:(this.y-m.top)/m.scale};}};}
 getScreenCTM(){const r=this.getBoundingClientRect(),height=Number(this.attributes.viewBox.split(' ')[3]),scale=Math.min(r.width/1000,r.height/height);return{inverse:()=>({scale,left:r.left+(r.width-1000*scale)/2,top:r.top+(r.height-height*scale)/2})};}
 append(...xs){this.children.push(...xs);}setAttribute(k,v){this.attributes[k]=v;}replaceChildren(...xs){this.children=xs;}querySelector(){return this.now??=new Element();}addEventListener(type,fn){this.events[type]=fn;}
}
const elements=Object.fromEntries(ids.map(id=>[id,new Element()]));
const $=id=>{assert(elements[id],'known element '+id);return elements[id];};
Object.assign($('v3-order-type'),{value:'STOP'});$('v3-timing').value='current';$('v3-side').value='BUY';
const base={recovery_mode:'V3',cycle_id:'c1',scope:'live',revision:7,read_only:false,positions:[{id:'p1'}],orders:[],v3_sl_distance:'1.50',v3_settings:{entry_distance:'1.00',count_presets:[4,6,10]},v3_controls:{can_enter:true,can_link:true,can_cancel_link:false,can_adjust_sl:true,reason:''}};
const profile={id:'six',name:'Six',stages:Array.from({length:6},(_,i)=>({volume:'0.01',tp_distance:String((i+1)*.25)})),total_volume:'0.06',sl_distance:'1.50'};
base.recovery_profiles={selected_profile_id:'six',profiles:[profile,{id:'mix',name:'Mix',stages:[{volume:'0.05',tp_distance:'0.25'},{volume:'0.02',tp_distance:'0.50'},{volume:'0.01',tp_distance:'0.75'}]}],recovery_sl_distance:'1.50'};base.selected_recovery_profile=profile;base.current_recovery_profiles=[{...profile,group_id:'g1'}];
const actions=[],dialogs=[],errors=[],store=new Map();let accept=true;
const ctx=vm.createContext({$,friendlyReason:x=>x??'',document:{createElement:()=>new Element()},view:structuredClone(base),busy:false,pending:null,mode:'live',confirm:msg=>{dialogs.push(msg);return accept;},send:async(a,r,s)=>actions.push({a,r,s}),error:m=>errors.push(m),localStorage:{getItem:k=>store.get(k),setItem:(k,v)=>store.set(k,v)},fields:()=>{},console});
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
 vm.runInContext(safetyLine+'\nv3Render()',ctx);
 assert.equal($('safety').textContent,'');
 $('safety').textContent=vm.runInContext('timeSafetyText()',ctx);
 assert($('safety').textContent.includes('MT5_UTC_CONTRACT_MISMATCH'));
 assert(!$('safety').textContent.includes('WAIT_REFERENCE'));
 assert($('safety').textContent.includes('Блокировка сохранена'));
 assert($('safety').textContent.includes('Проверить время'));
 assert.equal($('v3-submit-entry').disabled,true);
 assert.equal(ctx.view.time_block.reason,'MT5_UTC_WAIT_REFERENCE');
 delete ctx.view.time_status;assert(vm.runInContext('timeSafetyText()',ctx).includes('MT5_UTC_WAIT_REFERENCE'),'legacy snapshot fallback');
 ctx.view.time_status={latched_reason:'MT5_UTC_WAIT_REFERENCE',current_reason:null};
 vm.runInContext(safetyLine+'\nv3Render()',ctx);
 assert.equal($('safety').textContent,'');
 $('safety').textContent=vm.runInContext('timeSafetyText()',ctx);assert.equal($('v3-submit-entry').disabled,true,'null current reason cannot clear latch');
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
 assert.equal($('v3-panels').hidden,false);assert(!elements['manual-close-controls']);assert.equal($('v3-side').disabled,true);assert.equal($('recovery-profile-select').children.length,2);
 let before=actions.length;
 $('v3-order-type').value='MARKET';$('v3-timing').value='now';$('v3-side').value='SELL';$('v3-timing').onchange();assert.equal($('v3-side').disabled,false);
 accept=false;await $('v3-submit-entry').onclick();assert.equal(actions.length,before);accept=true;
 await $('v3-submit-entry').onclick();assert(!('count' in actions.at(-1).a));assert.equal(actions.at(-1).a.side,'SELL');assert.equal(actions.at(-1).a.timing,'now');assert.equal(actions.at(-1).r,7);assert.equal(actions.at(-1).s,'live');
 $('v3-order-type').value='STOP';$('v3-order-type').onchange();assert.equal($('v3-timing').value,'current');assert.equal($('v3-side').disabled,true);
 $('v3-timing').value='next';const stopDialogs=dialogs.length;accept=false;await $('v3-submit-entry').onclick();assert.equal(actions.at(-1).a.order_type,'STOP');assert.equal(actions.at(-1).a.timing,'next');assert.equal(dialogs.length,stopDialogs);accept=true;
 ctx.view.v3_controls.can_cancel_entry=true;vm.runInContext('v3Render()',ctx);await $('v3-cancel-entry').onclick();assert.equal(actions.at(-1).a.type,'v3_cancel_entry');assert.equal(dialogs.length,stopDialogs);
 const slBefore=actions.length;
 await $('v3-sl-minus').onclick();assert.equal(actions.length,slBefore,'minus only stages');assert.equal($('v3-sl-value').textContent,'1.40');await $('v3-sl-confirm').onclick();assert.equal(actions.at(-1).a.distance,'1.40');
 await $('v3-sl-plus').onclick();assert.equal(actions.length,slBefore+1,'plus only stages');await $('v3-sl-confirm').onclick();assert.equal(actions.at(-1).a.distance,'1.60');
 await $('v3-sl-confirm').onclick();assert.equal(actions.length,slBefore+2,'repeat confirm cannot resubmit draft');
 await $('v3-sl-plus').onclick();vm.runInContext("setSidePanel('sl',false)",ctx);await $('v3-sl-confirm').onclick();assert.equal(actions.length,slBefore+2,'closing discards draft');
 await $('v3-sl-plus').onclick();ctx.view.revision++;await $('v3-sl-confirm').onclick();assert.equal(actions.length,slBefore+2,'revision drift blocks confirm');ctx.view.revision--;
 await $('v3-sl-plus').onclick();ctx.view.positions=[{id:'p2'}];vm.runInContext('v3Render()',ctx);assert.equal($('v3-sl-confirm').disabled,true,'snapshot position change clears draft');ctx.view.positions=[{id:'p1'}];
 const guardedCount=actions.length;
 ctx.view.v3_sl_distance='1.5';vm.runInContext('v3Render()',ctx);await $('v3-sl-plus').onclick();await $('v3-sl-minus').onclick();assert.equal($('v3-sl-confirm').disabled,true,'same decimal value cannot send');await $('v3-sl-confirm').onclick();assert.equal(actions.length,guardedCount);
 for(const field of ['scope','cycle_id']){await $('v3-sl-plus').onclick();const old=ctx.view[field];ctx.view[field]='changed';await $('v3-sl-confirm').onclick();assert.equal(actions.length,guardedCount,field+' drift must not send');ctx.view[field]=old;}
 await $('v3-sl-plus').onclick();ctx.view.positions[0].sl='123';await $('v3-sl-confirm').onclick();assert.equal(actions.length,guardedCount,'position SL drift must not send');delete ctx.view.positions[0].sl;
 ctx.view.v3_sl_distance='0.00';vm.runInContext('v3Render()',ctx);assert.equal($('v3-sl-value').textContent,'0.00');assert.equal($('v3-sl-minus').disabled,true);assert.equal($('v3-sl-plus').disabled,false);await $('v3-sl-plus').onclick();await $('v3-sl-confirm').onclick();assert.equal(actions.at(-1).a.distance,'0.10');
 ctx.view.v3_sl_distance='0.125';vm.runInContext('v3Render()',ctx);assert.equal($('v3-sl-value').textContent,'0.125');await $('v3-sl-plus').onclick();await $('v3-sl-confirm').onclick();assert.equal(actions.at(-1).a.distance,'0.225');await $('v3-sl-minus').onclick();await $('v3-sl-confirm').onclick();assert.equal(actions.at(-1).a.distance,'0.025');
 ctx.view.v3_sl_distance='1.50';vm.runInContext('v3Render()',ctx);
 const d=dialogs.length;await $('v3-link-toggle').onclick();assert.equal(actions.at(-1).a.type,'v3_link');assert.equal(dialogs.length,d,'trigger placement has no preview dialog');
 ctx.view.v3_link={side:'SELL',price:'3998.5',count:6};ctx.view.v3_controls.can_cancel_link=true;vm.runInContext('v3Render()',ctx);const d2=dialogs.length;await $('v3-link-toggle').onclick();assert.equal(actions.at(-1).a.type,'v3_cancel_link');assert.equal(dialogs.length,d2,'cancel no dialog');
 ctx.view.v3_request={bar_id:1804800000,order_type:'STOP',count:6};vm.runInContext('v3Render()',ctx);assert.equal($('v3-cancel-request').hidden,false);await $('v3-cancel-request').onclick();assert.equal(actions.at(-1).a.type,'v3_cancel_request');
 $('v3-setting-distance').value='2.00';await $('save-v3-settings').onclick();assert.equal(actions.at(-1).a.entry_distance,'2.00');assert(!('count_presets' in actions.at(-1).a));
 $('v3-setting-distance').value='0';before=actions.length;await $('save-v3-settings').onclick();assert.equal(actions.length,before);
 assert.equal(vm.runInContext("profileTotal([{volume:'0.05'},{volume:'0.02'},{volume:'0.01'}])",ctx),'0.08');
 $('recovery-profile-select').value='mix';accept=false;before=actions.length;await $('recovery-profile-apply').onclick();assert.equal(actions.length,before);accept=true;
 await $('recovery-profile-apply').onclick();assert.equal(actions.at(-1).a.profile_id,'mix');assert.equal(actions.at(-1).r,7);
 $('recovery-profile-edit').onclick();assert.equal($('recovery-stage-rows').children.length,3);
 $('recovery-profile-name').value='Exact mixed';vm.runInContext("profileRows[0].volume='0.051';profileRows[0].tp_distance='0.251'",ctx);await $('recovery-profile-save').onclick();assert.equal(actions.at(-1).a.type,'recovery_profiles');assert.equal(actions.at(-1).a.profiles.at(-1).stages[0].volume,'0.051');assert.equal(actions.at(-1).a.profiles.at(-1).stages[0].tp_distance,'0.251');
 vm.runInContext("profileRows[0].volume='0'",ctx);before=actions.length;await $('recovery-profile-save').onclick();assert.equal(actions.length,before);
 $('recovery-profile-new').onclick();for(let i=0;i<25;i++)$('recovery-add-stage').onclick();assert.equal($('recovery-stage-rows').children.length,20);assert.equal($('recovery-add-stage').disabled,true);
 vm.runInContext("view.recovery_profiles.recovery_sl_distance='2'",ctx);before=actions.length;await $('recovery-profile-save').onclick();assert.equal(actions.length,before);assert(errors.at(-1).includes('изменились'));
 ctx.view=structuredClone(base);vm.runInContext('v3FillSettings();v3Render()',ctx);$('recovery-profile-select').value='six';$('recovery-profile-remove').onclick();await $('recovery-profile-save-list').onclick();assert.equal(actions.at(-1).a.profiles.length,1);assert.equal(actions.at(-1).a.selected_profile_id,'mix');assert.equal(actions.at(-1).a.profiles[0].id,'mix');$('recovery-profile-remove').onclick();
 $('recovery-profile-new').onclick();$('recovery-profile-name').value='<img src=x onerror=alert(1)>';await $('recovery-profile-save').onclick();assert.equal(actions.at(-1).a.profiles.at(-1).name,'<img src=x onerror=alert(1)>');
 $('recovery-sl-distance').value='2.25';await $('recovery-save-sl').onclick();assert.equal(actions.at(-1).a.recovery_sl_distance,'2.25');
 ctx.view=structuredClone(base);vm.runInContext('v3Render()',ctx);assert($('recovery-current-summary').textContent.includes('g1'));assert($('recovery-current-summary').textContent.includes('0.06'));
 for(const block of ['busy','pending','read_only','stale']){
  ctx.busy=block==='busy';ctx.pending=block==='pending'?'req':null;ctx.view.read_only=block==='read_only';vm.runInContext('v3Fresh='+String(block!=='stale')+';v3Render()',ctx);
  before=actions.length;await $('v3-link-toggle').onclick();await $('v3-submit-entry').onclick();await $('v3-sl-plus').onclick();await $('v3-sl-confirm').onclick();await $('recovery-profile-apply').onclick();await $('recovery-profile-save').onclick();await $('recovery-save-sl').onclick();assert.equal(actions.length,before,block);assert.equal($('v3-link-toggle').disabled,true);
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
 assert.equal(actions.at(-1).a.settings.INITIAL_SIZE,'0.02');assert.equal(actions.at(-1).a.settings.BASKET_COUNT,6);
 assert(!dialogs.at(-1).includes('EXPANSION_DISTANCE'));assert(!dialogs.at(-1).includes('TICKET_SIZE'));
 $('toggle-sl-panel').onclick();$('toggle-entry-panel').onclick();assert.equal($('v3-sl-panel').dataset.open,'true');assert.equal($('v3-entry-panel').dataset.open,'true');$('close-sl-panel').onclick();assert.equal($('v3-sl-panel').inert,true);assert.equal($('v3-entry-panel').dataset.open,'true');$('toggle-sl-panel').onclick();assert(Number($('v3-sl-panel').style.zIndex)>Number($('v3-entry-panel').style.zIndex),'reopened drawer is on top');
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
 vm.runInContext('v3Fresh=false;connectionLabel()',ctx);assert($('drawer-toggle').title.includes('нет свежего снимка'));assert.equal($('chart').dataset.feedFresh,'false');
 ctx.view.feed.timestamp_semantics='UNVERIFIED';vm.runInContext('v3Fresh=true;connectionLabel()',ctx);assert($('drawer-toggle').title.includes('время не проверено'));
 ctx.preview={value:1.5};vm.runInContext('draw()',ctx);assert.equal($('preview-backdrop').hidden,false);assert.equal($('command-drawer').inert,true);assert.equal($('v3-panels').inert,true);assert.equal($('bottom-tabs').inert,true);
 ctx.preview=null;vm.runInContext('draw()',ctx);assert.equal($('preview-backdrop').hidden,true);assert.equal($('command-drawer').inert,false);assert.equal($('v3-panels').inert,false);
 assert.equal(actions.length,before,'all gestures and navigation are display-only');
 console.log('PASS Node V3 handler tests: actual source, DOM doubles, inputs/actions/revisions, V3-only controls, legacy migration guard, confirmation/cancel, controls, stale/read-only/UNKNOWN guards. Browser layout NOT verified.');
}
main().catch(e=>{console.error(e);process.exit(1);});
