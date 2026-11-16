'use strict';
// Execute the real gesture handlers without a browser, network or trade gateway.
const assert=require('assert'),fs=require('fs'),vm=require('vm'),path=require('path');
const root=path.resolve(__dirname,'../web/basket-panel');
const src=fs.readFileSync(path.join(root,'panel.js'),'utf8'),html=fs.readFileSync(path.join(root,'index.html'),'utf8');
const nodes={};
class Element {
 constructor(){this.dataset={open:'false'};this.style={};this.attributes={};this.events={};this.classes={};this.inert=true;this.classList={toggle:(key,on)=>this.classes[key]=on};}
 setAttribute(k,v){this.attributes[k]=v;}focus(){}setPointerCapture(id){this.capture=id;}
 getBoundingClientRect(){return {width:320};}addEventListener(type,fn){this.events[type]=fn;}
}
const $=id=>nodes[id]??=new Element();
const ctx=vm.createContext({$,send:()=>assert.fail('Drawer must not trade'),fetch:()=>assert.fail('Drawer must not access network')});
vm.runInContext(src.slice(src.indexOf(' // Each drawer owns'),src.indexOf(" $('save-v3-settings').onclick")),ctx);
let pointer=0;
const event=(id,x=0,y=0,extra={})=>({pointerId:id,clientX:x,clientY:y,isPrimary:true,button:0,preventDefault(){},stopPropagation(){},...extra});
const fire=(name,type,e)=>$('toggle-'+name+'-panel').events[type](e);
const open=name=>$('v3-'+name+'-panel').dataset.open==='true';
const click=(name,detail=1)=>$('toggle-'+name+'-panel').onclick({detail,preventDefault(){}});
const drag=(name,dx,dy=0,end='pointerup')=>{const id=++pointer;fire(name,'pointerdown',event(id));fire(name,'pointermove',event(id,dx,dy));fire(name,end,event(id,dx,dy));};
click('sl');assert(open('sl'));assert.equal($('toggle-sl-panel').textContent,'<');
click('entry');assert(open('sl')&&open('entry'));click('sl');assert(!open('sl')&&open('entry'));
drag('sl',120);assert(open('sl'));assert.equal($('v3-sl-panel').style.transform,'');click('sl');assert(open('sl'),'release click must not undo drag');
drag('sl',-100);assert(!open('sl'));click('sl');assert(!open('sl'));assert(open('entry'));
for(const [dx,dy] of [[20,0],[-100,0],[5,80],[40,90]]){drag('sl',dx,dy);click('sl');assert(!open('sl'),'short, vertical or outward drag cannot open');}
drag('sl',120,0,'pointercancel');click('sl');assert(!open('sl'));assert.equal($('v3-sl-panel').style.transform,'');
drag('sl',120,0,'lostpointercapture');click('sl');assert(!open('sl'));
// Wrong pointer, secondary mouse and interrupted multi-touch are ignored safely.
for(const extra of [{button:2},{isPrimary:false}]){const id=++pointer;fire('sl','pointerdown',event(id,0,0,extra));fire('sl','pointerup',event(id,100));assert(!open('sl'));}
let id=++pointer;fire('sl','pointerdown',event(id));fire('sl','pointermove',event(id+1,150));assert.equal($('v3-sl-panel').style.transform,'');fire('sl','pointercancel',event(id));
id=++pointer;fire('sl','pointerdown',event(id));fire('sl','pointerdown',event(id+1,0,0,{isPrimary:false}));fire('sl','pointerup',event(id,100));click('sl');assert(!open('sl'));
// Tab change / protection preview aborts an in-flight drag without opening it.
id=++pointer;fire('sl','pointerdown',event(id));fire('sl','pointermove',event(id,100));vm.runInContext('cancelSidePanelDrags()',ctx);fire('sl','pointerup',event(id,150));click('sl');assert(!open('sl'));
assert.equal($('v3-sl-panel').classes.dragging,false);assert.equal($('v3-sl-panel').style.transform,'');
click('sl',0);assert(open('sl'),'keyboard click remains available');$('close-sl-panel').onclick();assert(!open('sl'));assert.equal($('v3-sl-panel').inert,true);
// A pointerdown after suppression resets only that handle; tap then click opens it.
id=++pointer;fire('sl','pointerdown',event(id));fire('sl','pointerup',event(id,2,1));click('sl');assert(open('sl'));
assert(src.includes('if(open&&!previewWasOpen){cancelSidePanelDrags();'));
assert(src.split('\n').find(line=>line.includes('function setTab(tab)')).includes('cancelSidePanelDrags();'));
const link=html.indexOf('id="v3-link-toggle"'),commands=html.indexOf('id="command-drawer"'),entry=html.indexOf('id="v3-entry-panel"');
assert(link>commands&&commands>entry);assert(link<html.indexOf('id="preview-backdrop"'));assert.equal((html.match(/id="v3-link-toggle"/g)||[]).length,1);
assert(html.includes('aria-label="Открыть контроль SL"'));assert(html.includes('aria-label="Открыть выставление ордеров"'));
console.log('PASS arrow taps/keyboard, directional drags, thresholds, cancellation, lost capture, secondary pointer, interruption, independent drawers, zero trade calls and SL trigger in Commands');
