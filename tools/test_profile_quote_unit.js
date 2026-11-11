'use strict';
// Actual-source regression checks with DOM doubles. No network, controller or MT5.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(__dirname,'../web/basket-panel');
const source=fs.readFileSync(path.join(root,'panel.js'),'utf8');
const html=fs.readFileSync(path.join(root,'index.html'),'utf8');
class Element{
 constructor(tag='div'){this.tag=tag;this.value='';this.open=false;this.children=[];this.attributes={};this.dataset={};this.style={};this.clientWidth=390;this.clientHeight=520;}
 append(...children){this.children.push(...children);}
 replaceChildren(...children){this.children=children;}
 setAttribute(key,value){this.attributes[key]=value;}
 scrollIntoView(){this.scrolled=true;}
 focus(){this.focused=true;}
 addEventListener(type,handler){(this.events??={})[type]=handler;}
 setPointerCapture(){}
 getBoundingClientRect(){return{left:0,top:0,width:this.clientWidth,height:this.clientHeight};}
 createSVGPoint(){return{x:0,y:0,matrixTransform(m){return{x:(this.x-m.left)/m.scale,y:(this.y-m.top)/m.scale};}};}
 getScreenCTM(){const r=this.getBoundingClientRect(),height=Number(this.attributes.viewBox.split(' ')[3]),scale=Math.min(r.width/1000,r.height/height);return{inverse:()=>({scale,left:r.left+(r.width-1000*scale)/2,top:r.top+(r.height-height*scale)/2})};}
}
const elements=Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(x=>[x[1],new Element()]));
const $=id=>{assert(elements[id],'known HTML ID '+id);return elements[id];};
const profile={id:'saved',name:'Saved profile',stages:[{volume:'0.02',tp_distance:'0.25'},{volume:'0.01',tp_distance:'0.50'}]};
const state={scope:'fixture',recovery_profiles:{profiles:[profile],selected_profile_id:'saved'}};
let accept=true,reloads=0;const dialogs=[];
const ctx=vm.createContext({$,view:state,document:{createElement:tag=>new Element(tag),createElementNS:(_,tag)=>new Element(tag)},confirm:message=>{dialogs.push(message);return accept;},send:()=>assert.fail('presentation must not send an action'),error:()=>{},fields:()=>{reloads++;}});
vm.runInContext(source.slice(source.indexOf(' // Profile editor keeps'),source.indexOf(' function v3InputMode()')),ctx);
vm.runInContext('loadProfileDraft(view.recovery_profiles.profiles[0])',ctx);
$('recovery-profile-select').value='saved';
$('recovery-profile-new').onclick();
assert($('recovery-profile-editor').open,'New reveals the closed editor');
assert($('recovery-profile-settings').open);
assert($('recovery-profile-editor').scrolled&&$('recovery-profile-name').focused,'visible keyboard focus');
assert.equal($('recovery-profile-name').value,'Новый профиль');
assert.equal($('recovery-stage-rows').children.length,1);
assert.equal(state.recovery_profiles.selected_profile_id,'saved','New does not select or persist anything');
let before=dialogs.length;$('recovery-profile-new').onclick();assert.equal(dialogs.length,before,'unchanged repeat click needs no warning');
$('recovery-profile-name').value='Keep this draft';
vm.runInContext("profileRows[0].volume='0.101';profileRows[0].tp_distance='0.251'",ctx);
const draft=vm.runInContext('profileDraftValue()',ctx);
for(const button of ['recovery-profile-new','recovery-profile-edit']){
 $('recovery-profile-editor').open=false;accept=false;before=dialogs.length;$(button).onclick();
 assert.equal(dialogs.length,before+1,'replacement of dirty draft asks first');
 assert.equal(vm.runInContext('profileDraftValue()',ctx),draft,'Cancel preserves every draft value');
 assert($('recovery-profile-editor').open,'Cancel returns to the existing draft');
}
// Polling and closing the editor are not draft replacement operations.
$('recovery-profile-editor').open=false;
assert.equal(vm.runInContext('profileDraftValue()',ctx),draft);
accept=true;$('recovery-profile-edit').onclick();
assert.equal($('recovery-profile-name').value,'Saved profile');
assert.equal($('recovery-stage-rows').children.length,2);
assert(!vm.runInContext('profileDraftDirty()',ctx));
$('recovery-add-stage').onclick();assert(vm.runInContext('profileDraftDirty()',ctx),'adding a row is a draft edit');
vm.runInContext(source.split('\n').find(line=>line.includes("$('refresh-settings').onclick=")),ctx);
accept=false;$('refresh-settings').onclick();assert.equal(reloads,0,'refresh cannot silently discard the draft');
accept=true;$('refresh-settings').onclick();assert.equal(reloads,1);
// Draw actual SVG nodes over narrow/wide panes, candle counts and zoom levels.
Object.assign(ctx,{svg:$('chart'),ns:'svg',lines:[],zoom:1,pan:0,timeOffset:0,visibleCount:60,frameHeight:520,plotBottom:480,plotHeight:450,plotRight:855,innerWidth:1280,preview:null,previewFocus:null,previewWasOpen:false,prefs:{colors:{}},drawAnalysis:()=>{},utcLabel:()=>'',feedUtcVerified:()=>true});
vm.runInContext(source.slice(source.indexOf(' function node('),source.indexOf(' function openPreview(')),ctx);
let cases=0;
for(const width of [220,320,390,680,1280])for(const height of [120,520])for(const count of [1,10,60,240])for(const zoom of [1,2])for(const base of [4000,100000]){
 ctx.svg.clientWidth=width;ctx.svg.clientHeight=height;ctx.visibleCount=count;ctx.timeOffset=0;ctx.zoom=zoom;
 ctx.view={levels:{},orders:[],positions:[],feed:{quote:{bid:base,ask:base+.02},candles:Array.from({length:count},(_,i)=>({utc_time:i,open:base-.1,close:base+.1,low:base-.2,high:base+.2}))}};
 vm.runInContext('draw()',ctx);
 const candles=ctx.svg.children.filter(n=>n.tag==='rect');
 const quotes=ctx.svg.children.filter(n=>n.attributes['data-quote']);
 assert.equal(quotes.length,2);
 const right=Math.max(...candles.map(n=>Number(n.attributes.x)+Number(n.attributes.width)));
 const badges=quotes.map(n=>n.children.find(c=>c.tag==='rect').attributes);
 for(const badge of badges){assert(right+18<=Number(badge.x),'candle ends before the reserved quote gutter');assert(Number(badge.x)+Number(badge.width)<=1000,'quote stays within the SVG');}
 assert(Math.abs(Number(badges[0].y)-Number(badges[1].y))>=Number(badges[0].height)-.001,'Bid and Ask badges do not overlap vertically');
 for(const quote of quotes){assert.equal(quote.attributes['pointer-events'],'none');assert(!quote.attributes['data-line'],'quotes never become draggable levels');}
 cases++;
}
// All cycle levels and the preview use the same thicker stroke at any viewport size.
for(const width of [320,390,680,1280]){
 ctx.svg.clientWidth=width;ctx.svg.clientHeight=520;ctx.zoom=1;ctx.pan=0;
 ctx.prefs.mainCycle=true;ctx.prefs.colors={BUY:'green',SELL:'red',entry:'white',tp:'cyan',sl:'orange'};
 ctx.view={levels:{BUY:4001,SELL:3999},orders:[{side:'BUY',price:4001},{side:'SELL',price:3999}],positions:[{id:'position',entry:4000,tp:4002,sl:3998}],feed:{quote:{bid:4000,ask:4000.02},candles:[]}};
 ctx.preview={value:4000.5,dragging:true};vm.runInContext('draw()',ctx);
 const levels=ctx.svg.children.filter(n=>n.attributes['data-line']);assert.equal(levels.length,5);
 for(const level of levels){
  const strokes=level.children.filter(n=>n.tag==='line');
  assert.equal(strokes[0].attributes['stroke-width'],4,'cycle stroke doubled to 4 SVG units');
  assert.equal(strokes[1].attributes['stroke-width'],18,'hit target stays broad');
 }
 const preview=ctx.svg.children.find(n=>n.tag==='line'&&n.attributes.stroke==='#fff');assert.equal(preview.attributes['stroke-width'],4);
 for(const quote of ctx.svg.children.filter(n=>n.attributes['data-quote']))assert.equal(quote.children[0].attributes['stroke-width'],1.5,'Bid/Ask unchanged');
}
ctx.preview=null;
assert(html.includes('Удерживайте линию 0,5 секунды'),'visible help matches the 500ms hold');
// Short wide panes letterbox the SVG: screen x850 is the price gutter, not candle history.
Object.assign(ctx,{hold:null,gesture:null,setTimeout:()=>123,clearTimeout:()=>{}});
vm.runInContext(source.slice(source.indexOf(' const pointers=new Map()'),source.indexOf(" $('cancel-level').onclick")),ctx);
ctx.svg.clientWidth=1280;ctx.svg.clientHeight=120;ctx.zoom=1;ctx.pan=0;ctx.timeOffset=120;ctx.visibleCount=60;
ctx.view.feed.candles=Array.from({length:300},(_,i)=>({utc_time:i,open:3999.9,close:4000.1,low:3999.8,high:4000.2}));ctx.view.feed.quote={bid:4000,ask:4000.02};
vm.runInContext('draw()',ctx);
const event=(x,y)=>({pointerId:1,clientX:x,clientY:y,target:{closest:()=>null},preventDefault:()=>{},type:'pointerup'});
const handlers=ctx.svg.events;
handlers.pointerdown(event(850,60));handlers.pointermove(event(850,80));handlers.pointerup(event(850,80));
assert.equal(ctx.timeOffset,120,'price-gutter vertical drag must not scroll candle history');assert(ctx.pan>0,'price-gutter vertical drag pans prices');
ctx.pan=0;vm.runInContext('draw()',ctx);const offset=ctx.timeOffset;
handlers.pointerdown(event(600,60));handlers.pointermove(event(650,60));handlers.pointerup(event(650,60));
assert(Math.abs(ctx.timeOffset-offset-100/ctx.plotRight*ctx.visibleCount)<1e-8,'horizontal delta uses SVG pixels after letterboxing');
console.log(`PASS profile open/focus, repeated clicks, dirty-draft cancel/replace/refresh; ${cases} actual-source chart gutter cases, 4-unit cycle/preview SVG strokes and letterboxed pointer navigation. DOM doubles, not browser rendering.`);
