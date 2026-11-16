'use strict';
const fs=require('fs'),vm=require('vm'),path=require('path'),assert=require('assert');
const src=fs.readFileSync(path.join(__dirname,'../web/basket-panel/panel.js'),'utf8');
const element={},sent=[],prompts=[],errors=[];let accept=false,blocked=false;
const ctx=vm.createContext({$:()=>element,mode:'paper',view:{cycle_id:'c',scope:'s',revision:3,debt:'2.50',positions:[],orders:[{id:'pending'}],v3_controls:{can_writeoff_recovery:true}},v3Blocked:()=>blocked,confirm:text=>{prompts.push(text);return accept;},send:async(a,r,s)=>sent.push({a,r,s}),error:m=>errors.push(m)});
vm.runInContext(src.split('\n').find(x=>x.includes("$('writeoff-recovery').onclick=")),ctx);
const render=()=>vm.runInContext(src.split('\n').find(x=>x.includes("$('writeoff-recovery').disabled=")),ctx);
(async()=>{
 render();assert.equal(element.disabled,false,'known pending orders do not block allowed flat reset');
 await element.onclick();assert.equal(sent.length,0,'cancel does not submit');accept=true;await element.onclick();assert.equal(sent.length,1);assert.equal(sent[0].a.type,'writeoff_recovery');assert.equal(sent[0].a.expected_debt,'2.50');assert.equal(sent[0].r,3);assert.equal(sent[0].s,'s');assert(prompts.at(-1).includes('триггеры и выбранная свеча сохраняются'));
 ctx.view.positions=[{id:'filled'}];render();assert(element.disabled);await element.onclick();assert.equal(sent.length,1);
 ctx.view.positions=[];blocked=true;render();assert(element.disabled);await element.onclick();assert.equal(sent.length,1);
 blocked=false;ctx.view.v3_controls.can_writeoff_recovery=false;render();assert(element.disabled);await element.onclick();assert.equal(sent.length,1);
 assert.deepEqual(errors,[]);console.log('PASS flat pending-preserving waiver guard, confirmation cancel, snapshot payload and unsafe UI blocking');
})().catch(e=>{console.error(e);process.exitCode=1;});
