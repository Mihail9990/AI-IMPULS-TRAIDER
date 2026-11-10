'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const src=fs.readFileSync(path.join(__dirname,'../web/basket-panel/panel.js'),'utf8');
class Element{constructor(){this.events={};this.hidden=true;this.value='';this.dataset={};this.isConnected=true;}addEventListener(k,v){this.events[k]=v;}setPointerCapture(){}getBoundingClientRect(){return {left:0,width:1000,height:500};}focus(){}}
const nodes={},$=id=>nodes[id]??=new Element(),svg=$('chart');let timer=null,delay=0;const sent=[],errors=[];
const base={scope:'LIVE-test',revision:4,cycle_id:'cycle',positions:[{id:'p',sl:'99.00',tp:'105.00'}],orders:[],levels:{}};
const line={key:'p-sl',kind:'sl',pid:'p',price:99,label:'SL · ступень 2 · #p'};
const ctx=vm.createContext({$,svg,document:{activeElement:null},view:structuredClone(base),preview:null,previewFocus:null,previewWasOpen:false,pending:null,busy:false,lines:[line],gesture:null,hold:null,zoom:1,pan:0,timeOffset:0,visibleCount:60,plotRight:850,plotBottom:480,plotHeight:450,structuredClone,
 v3Blocked:()=>ctx.blocked===true,blocked:false,cancelSidePanelDrags:()=>{},domain:()=>({min:90,max:110}),chartPoint:(x,y)=>({x,y}),setTimeout:(fn,ms)=>{timer=fn;delay=ms;return 1;},clearTimeout:()=>{timer=null;},send:async(a,r,s)=>sent.push({a,r,s}),error:e=>errors.push(e),confirm:()=>assert.fail('No second browser confirmation')});
vm.runInContext(src.slice(src.indexOf(' function syncPreviewDialog(){'),src.indexOf(' function draw(){')),ctx);
ctx.draw=()=>vm.runInContext('syncPreviewDialog()',ctx);
vm.runInContext(src.slice(src.indexOf(' function openPreview('),src.indexOf(' function renderTotalsHistory(){')),ctx);
const fire=(type,x=10,y=200,id=1,onLine=true)=>svg.events[type]({type,pointerId:id,button:0,clientX:x,clientY:y,target:{closest:()=>onLine?{dataset:{line:'p-sl'}}:null},preventDefault(){}});
const arm=()=>{fire('pointerdown');assert.equal(delay,1000);assert.equal(ctx.preview,null);const fn=timer;timer=null;fn();assert(ctx.preview?.dragging);assert($('preview').hidden);assert.equal(sent.length,0);};
(async()=>{
 // Early movement scrolls/navigates and cancels long hold.
 fire('pointerdown');assert.equal(delay,1000);fire('pointermove',80,200);assert.equal(timer,null);assert.equal(ctx.preview,null);fire('pointerup',80,200);
 arm();fire('pointermove',10,180);assert(ctx.preview.value!==99);assert.equal(sent.length,0);fire('pointerup',10,180);assert.equal(ctx.preview.dragging,false);assert(!$('preview').hidden);assert($('preview-text').textContent.includes('ступень 2'));$('cancel-level').onclick();assert.equal(ctx.preview,null);assert.equal(sent.length,0);
 for(const type of ['pointercancel','lostpointercapture']){arm();fire(type);assert.equal(ctx.preview,null);assert($('preview').hidden);}
 arm();fire('pointerdown',15,200,2);assert.equal(ctx.preview,null,'multitouch cancels active level drag');
 arm();fire('pointerup');fire('lostpointercapture');assert(ctx.preview,'normal lost capture after release preserves confirmation');ctx.view.revision++;await $('apply-level').onclick();assert.equal(sent.length,0);assert(errors.at(-1).includes('изменились'));$('cancel-level').onclick();ctx.view=structuredClone(base);
 arm();fire('pointerup');ctx.view.positions=[];await $('apply-level').onclick();assert.equal(sent.length,0);$('cancel-level').onclick();ctx.view=structuredClone(base);
 arm();fire('pointermove',10,180);fire('pointerup',10,180);await $('apply-level').onclick();assert.equal(sent.length,1);assert.equal(sent[0].a.type,'protection');assert.equal(sent[0].a.position_id,'p');assert.equal(sent[0].a.old.sl,'99.00');assert.equal(sent[0].r,4);assert.equal(sent[0].s,'LIVE-test');assert.equal(ctx.preview,null);
 ctx.blocked=true;fire('pointerdown');timer();assert.equal(ctx.preview,null);fire('pointerup');assert.equal(sent.length,1);
 console.log('PASS 1000ms hold, early navigation, drag/release compact confirmation, no auto trade, cancellation/multitouch, stale/closed position guards and one explicit send');
})().catch(e=>{console.error(e);process.exitCode=1;});
