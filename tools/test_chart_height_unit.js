'use strict';
// Execute the actual local-preference code, with no backend or order API.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(path.join(__dirname,'../web/basket-panel/panel.js'),'utf8');
const code=source.slice(source.indexOf(' // Local display preference only;'),source.indexOf(' // Presentation-only navigation:'));
assert(code.includes('applyChartHeight()'));
const saved=new Map();
function make(broken=false){
 const elements={};let draws=0;
 const $=id=>elements[id]??=( {value:'',textContent:'',disabled:false,dataset:{},style:{setProperty(k,v){this[k]=v;}}} );
 const ctx=vm.createContext({$,storageKey:k=>'basket-'+k,localStorage:{getItem:k=>saved.get(k)??null,setItem:(k,v)=>{if(broken)throw Error('storage unavailable');saved.set(k,v);}},requestAnimationFrame:fn=>fn(),draw:()=>{draws++;},send:()=>assert.fail('display preference must not send a command')});
 vm.runInContext(code,ctx);return{$,ctx,get draws(){return draws;}};
}
let live=make();
assert.equal(live.$('chart-page').dataset.chartHeight,'auto');assert(live.$('chart-height-range').disabled);
live.$('chart-height-mode').value='custom';live.$('chart-height-mode').onchange();
live.$('chart-height-range').value='45';live.$('chart-height-range').oninput();
assert.equal(live.$('chart-page').style['--chart-height'],'45vh');assert.equal(live.$('chart-page').style['--chart-height-dynamic'],'45dvh');
assert.equal(live.$('chart-height-value').textContent,'45%');assert(!live.$('chart-height-range').disabled);
live=make();assert.equal(live.$('chart-height-range').value,'45');
live.$('chart-height-reset').onclick();assert.equal(live.$('chart-page').dataset.chartHeight,'auto');assert(live.$('chart-height-range').disabled);
for(const [value,expected] of [[10,30],[200,85],[43,45],[Infinity,60],[NaN,60],['65',60]]){const normalized=vm.runInContext('normalizeChartHeight',live.ctx)({mode:'custom',percent:value});assert.equal(normalized.percent,expected);}
saved.set('basket-chart-height-v1','bad JSON');live=make();assert.equal(live.$('chart-page').dataset.chartHeight,'auto');
saved.set('basket-chart-height-v1',JSON.stringify({mode:'unexpected',percent:55}));live=make();assert.equal(live.$('chart-page').dataset.chartHeight,'auto');
const blocked=make(true);blocked.$('chart-height-mode').value='custom';blocked.$('chart-height-mode').onchange();assert(blocked.$('chart-height-status').textContent.includes('до перезагрузки'));assert(blocked.draws>1);
console.log('PASS actual-source chart height: bounds/step, auto/reset, LIVE storage/reload, malformed/unavailable storage, redraw-only. Browser geometry NOT verified.');
