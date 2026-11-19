'use strict';const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const path=require('node:path');const read=name=>fs.readFileSync(path.join(__dirname,'..',name),'utf8');
test('three bottom navigation tabs map to accessible panels; main is default',()=>{
 const html=read('index.html'),css=read('panel.css');
 const tabs=[...html.matchAll(/<button[^>]*role="tab" id="tab-([a-z]+)" aria-selected="(true|false)" aria-controls="view-([a-z]+)"[^>]*tabindex="(-?\d+)"/g)];
 assert.deepEqual(tabs.map(([,name,selected,panel,index])=>[name,selected,panel,index]),[
  ['main','true','main','0'],['settings','false','settings','-1'],['history','false','history','-1']
 ]);
 const panels=[...html.matchAll(/<section id="view-([a-z]+)" role="tabpanel" aria-labelledby="tab-([a-z]+)" tabindex="0"( hidden)?>/g)];
 assert.deepEqual(panels.map(([,view,tab,hidden])=>[view,tab,Boolean(hidden)]),[
  ['main','main',false],['settings','settings',true],['history','history',true]
 ]);
 assert.match(html, /<nav class="bottom-nav"/);
 assert.match(css, /\.bottom-nav\s*\{\s*position:\s*fixed/);
 assert.match(css, /safe-area-inset-bottom/);
 assert.ok(!html.includes('id="chart"'));
 assert.match(html,/ПРОСМОТР/);
});
test('DOM ids unique and script references resolve',()=>{const html=read('index.html'),script=read('panel.js')+read('controls.js');const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(x=>x[1]);assert.equal(ids.length,new Set(ids).size);for(const [,id]of script.matchAll(/\$\('([^']+)'\)/g))assert.ok(ids.includes(id),id);});
test('local assets, no inline JS/CSS, no external sources',()=>{const html=read('index.html');assert.deepEqual([...html.matchAll(/<script[^>]+src="([^"]+)"/g)].map(x=>x[1]),['model.js','panel.js','controls.js']);assert.match(html,/href="panel.css"/);assert.ok(!/\s(?:style|onclick|onload)=/i.test(html));assert.ok(!/(?:src|href)="(?:https?:|\/\/)/i.test(html));});
test('no credentials storage, HTML injection or trade transport',()=>{const code=read('panel.js')+read('model.js');for(const banned of ['localStorage','sessionStorage','document.cookie','innerHTML','insertAdjacentHTML','sendBeacon','console.log','method: \'POST\'','serviceWorker'])assert.ok(!code.includes(banned),banned);assert.match(code,/textContent/);assert.match(code,/redirect: 'error'/);assert.match(code,/pagehide/);});

test('actions are explicit and old Android journal never touched',()=>{const controls=read('controls.js'),core=read('recovery-actions.mjs'),html=read('index.html');assert.match(html,/id="migration-ack"/);assert.match(html,/id="operator-enable"/);assert.match(html,/id="confirm-action"/);assert.match(controls,/addEventListener\('submit'/);assert.ok(!controls.includes("execute('reconcile'"));assert.ok(!controls.includes('setInterval'));assert.match(core,/response.web_trading===true/);assert.match(core,/recovery.native.pending.v1/);assert.ok(!core.includes('recovery-pending-command'));});


test('navigation clicks, shortcut and keyboard update selection, visibility and focus',()=>{
 const vm=require('node:vm'),html=read('index.html');
 const state={focused:null,scrolls:0};
 const make=(id)=>({id,attrs:{},handlers:{},hidden:false,tabIndex:0,classList:{add(){},remove(){},toggle(){}},
  addEventListener(name,handler){this.handlers[name]=handler},
  setAttribute(k,v){this.attrs[k]=v},getAttribute(k){return this.attrs[k]},
  focus(){state.focused=this.id}});
 const ids=Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(([,id])=>[id,make(id)]));
 const names=['main','settings','history'], tabs=names.map(n=>ids['tab-'+n]);
 tabs.forEach((tab,i)=>tab.attrs['aria-controls']='view-'+names[i]);
 const document={getElementById:id=>ids[id],querySelectorAll:()=>tabs,addEventListener(){}};
 const window={RecoveryModel:{},scrollTo(){state.scrolls++;},addEventListener(){},dispatchEvent(){}};
 vm.runInNewContext(read('panel.js'),{window,document,location:{protocol:'file:'},Event:class{}});
 const assertSelection=name=>{
  for(const tab of tabs){
   const selected=tab.id==='tab-'+name;
   assert.equal(tab.attrs['aria-selected'],String(selected));
   assert.equal(tab.tabIndex,selected?0:-1);
   assert.equal(ids[tab.attrs['aria-controls']].hidden,!selected);
  }
 };
 tabs[0].handlers.click();assertSelection('main');assert.equal(state.focused,'tab-main');
 const press=(tab,key)=>{let prevented=false;tab.handlers.keydown({key,preventDefault(){prevented=true;}});assert.ok(prevented);};
 press(tabs[0],'ArrowLeft');assertSelection('history');assert.equal(state.focused,'tab-history');
 press(tabs[2],'ArrowRight');assertSelection('main');
 press(tabs[0],'End');assertSelection('history');
 press(tabs[2],'Home');assertSelection('main');
 ids['open-settings'].handlers.click();assertSelection('settings');assert.equal(state.focused,'key');
 assert.equal(state.scrolls,6);
});
