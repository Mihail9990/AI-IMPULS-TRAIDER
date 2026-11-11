'use strict';
// Fake DOM and synthetic candles only. No broker/service/network is contacted.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const src=fs.readFileSync(path.join(__dirname,'../web/basket-panel/analysis.js'),'utf8');
class Element{constructor(tag='div'){this.tag=tag;this.children=[];this.attributes={};this.hidden=false;this.textContent='';}setAttribute(k,v){this.attributes[k]=v;}append(...n){this.children.push(...n);}replaceChildren(...n){this.children=n;}}
const nodes={},get=id=>nodes[id]??=new Element(),requests=[],timers=new Map(),listeners={};let clock=1000,timer=0,redraws=0;
const document={hidden:false,getElementById:get,createElementNS:(_,tag)=>new Element(tag),addEventListener:(key,fn)=>listeners[key]=fn};
const preferences={liquidity:true,mainTrend:true,opacity:25,colors:{zone:'#67ddeb'}};
const ctx=vm.createContext({document,AbortController,Date:class extends Date{static now(){return clock;}},setTimeout:()=>++timer,clearTimeout(){},setInterval:f=>{timers.set(++timer,f);return timer;},clearInterval:i=>timers.delete(i),console});
ctx.fetch=(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));ctx.getHeaders=()=>({'X-Local-Control':'1','X-Controller-Scope':'live'});ctx.getPreferences=()=>preferences;ctx.onChange=()=>redraws++;
vm.runInContext(src,ctx);const ui=vm.runInContext('createMainAnalysisUI({document,fetch,getHeaders,getPreferences,onChange})',ctx);
const start=1804800000,bars=Array.from({length:130},(_,i)=>({utc_time:start+i*60+(i>=40?3600:0),open:100,high:103,low:99,close:102})),closed=bars.at(-1).utc_time+60;
const feed=()=>({instance:'native-1',symbol:'XAUUSD.s',source:'MT5',connected:true,status:'FRESH',timestamp_semantics:'VERIFIED',timestamp:closed+1,age_seconds:0,candles:[...bars,{utc_time:closed,open:102,high:104,low:101,close:103}]});
const snapshot=(patch={})=>({schema:'analysis_v1',source:'MT5',symbol:'XAUUSD.s',feed_instance:'native-1',timeframe:'M1',status:'READY',timestamp_semantics:'VERIFIED',as_of:closed+1,closed_through:closed,bars,modules:{smc:{status:'READY',components:{liquidity:{status:'READY'}}},trendln:{status:'READY'}},overlays:{structure:[{kind:'liquidity_zone',start_time:bars[80].utc_time,end_time:bars.at(-1).utc_time,top:103,bottom:101},{kind:'liquidity_sweep',start_time:bars.at(-1).utc_time,price:103}],trendlines:[{kind:'trendline',points:[{time:bars[0].utc_time,price:100},{time:bars.at(-1).utc_time,price:102}]}]},...patch});
const settle=async(req,value)=>{req.resolve({ok:true,json:async()=>value});await new Promise(r=>setImmediate(r));};
const svg=new Element('svg'),walk=n=>[n,...n.children.flatMap(walk)];
function draw(candles=feed().candles.slice(-80)){svg.replaceChildren();ui.draw({svg,candles,x:i=>10+(i+.5)*8,y:p=>400-(Number(p)-98)*40,left:0,right:700,top:20,bottom:480});return walk(svg);}
const kinds=()=>draw().filter(n=>n.attributes['data-analysis-kind']).map(n=>n.attributes['data-analysis-kind']);
async function reload(data){ui.setVisible(false);ui.setVisible(true);await settle(requests.at(-1),data);}
(async()=>{
 assert.equal(requests.length,0);ui.updateFeed(feed(),true);assert.equal(requests.length,1);assert.equal(timers.size,1);assert.equal(requests[0].url,'/api/analysis?timeframe=M1');assert.equal(requests[0].options.method,'GET');
 await settle(requests.at(-1),snapshot());assert.deepEqual(kinds(),['liquidity_zone','liquidity_sweep','trendline']);assert.equal(get('main-analysis-status').textContent,'');
 const shapes=draw();assert(shapes.some(n=>n.attributes['pointer-events']==='none'));assert(!shapes.some(n=>n.tag==='text'),'main analytical overlays have no text labels');assert.equal(shapes.find(n=>n.attributes['data-analysis-kind']==='liquidity_zone').attributes['fill-opacity'],'0.25');
 assert(Number(shapes.find(n=>n.tag==='polyline').attributes.points.split(',')[0])<0,'line crossing viewport is retained and clipped');
 const initial=requests.length;for(let i=0;i<100;i++)ui.updateFeed(feed(),true);assert.equal(requests.length,initial,'trading poll does not re-fetch analysis');
 preferences.liquidity=false;ui.refresh();await settle(requests.at(-1),snapshot());assert.deepEqual(kinds(),['trendline']);preferences.mainTrend=false;ui.refresh();assert.equal(timers.size,0);assert.equal(kinds().length,0);assert.equal(get('main-analysis-source').textContent,'');
 preferences.liquidity=true;preferences.mainTrend=true;ui.refresh();await settle(requests.at(-1),snapshot());
 for(const [patch,reason]of [[{status:'STALE'},'устарели'],[{status:'TIME_UNVERIFIED'},'время не подтверждено'],[{feed_instance:'other'},'источник изменился'],[{symbol:'OTHER'},'источник изменился'],[{timeframe:'H1'},'не закрыты']]){await reload(snapshot(patch));assert.equal(kinds().length,0);assert(get('main-analysis-status').textContent.includes(reason),get('main-analysis-status').textContent);}
 await reload(snapshot());const conflicting=feed().candles.slice(-80);conflicting[0]={...conflicting[0],close:101};draw(conflicting);assert(!walk(svg).some(n=>n.attributes['data-analysis-kind']));assert(get('main-analysis-status').textContent.includes('различаются'));
 for(const bad of [closed-60,1e20,Infinity,NaN,null,closed+.5]){await reload(snapshot({closed_through:bad}));assert.doesNotThrow(()=>kinds());assert.equal(kinds().length,0);assert(get('main-analysis-status').textContent.includes('некорректное время закрытых'));}
 await reload(snapshot());ui.updateFeed({...feed(),timestamp:closed+61},true);assert.equal(kinds().length,0);assert(get('main-analysis-status').textContent.includes('последней закрытой'));ui.updateFeed(feed(),true);
 await reload(snapshot());clock+=21000;assert.equal(kinds().length,0);assert(get('main-analysis-status').textContent.includes('устарел'));clock+=1;
 await reload(snapshot({modules:{smc:{status:'READY',components:{liquidity:{status:'ERROR',reason:'COMPONENT_FAILED'}}},trendln:{status:'UNAVAILABLE',reason:'missing'}}}));assert(!kinds().includes('trendline'));assert(get('main-analysis-status').textContent.includes('COMPONENT_FAILED'));assert(get('main-analysis-status').textContent.includes('trendln'));
 await reload(snapshot());ui.updateFeed({...feed(),timestamp_semantics:'UNVERIFIED'},true);assert.equal(kinds().length,0);assert(get('main-analysis-status').textContent.includes('время не подтверждено'));assert.equal(timers.size,0);
 for(const field of ['connected','source']){const invalid=field==='connected'?false:'OTHER';ui.updateFeed({...feed(),[field]:invalid},true);const old=requests.length;ui.updateFeed(feed(),true);assert.equal(requests.length,old+1,field+' recovery starts polling');await settle(requests.at(-1),snapshot());assert.equal(kinds().length,3);}
 ui.updateFeed(feed(),false);assert.equal(kinds().length,0);assert.equal(timers.size,0);ui.updateFeed(feed(),true);const pending=requests.at(-1);ui.setVisible(false);assert(pending.options.signal.aborted);await settle(pending,snapshot());assert.equal(kinds().length,0);assert.equal(timers.size,0);
 ui.setVisible(true);requests.at(-1).reject(Error('offline'));await new Promise(r=>setImmediate(r));assert.equal(kinds().length,0);assert.equal(get('main-analysis-status').textContent,'offline');
 ui.setVisible(false);document.hidden=true;ui.setVisible(true);const before=requests.length;listeners.visibilitychange();assert.equal(requests.length,before);document.hidden=false;listeners.visibilitychange();assert.equal(requests.length,before+1);await settle(requests.at(-1),snapshot());assert.equal(kinds().length,3);
 await reload(snapshot({overlays:{trendlines:[{kind:'trendline',points:[{time:bars[40].utc_time-1800,price:100},{time:bars.at(-1).utc_time,price:102}]}]}}));assert.equal(kinds().length,0,'missing timestamp never fabricated');
 await reload(snapshot());const create=document.createElementNS;document.createElementNS=()=>{throw Error('synthetic render fault');};assert.doesNotThrow(()=>kinds(),'overlay fault cannot escape into main chart rendering');document.createElementNS=create;assert(get('main-analysis-status').textContent.includes('ошибка отображения'));assert.equal(kinds().length,0);
 assert(requests.every(r=>r.options.method==='GET'&&r.url==='/api/analysis?timeframe=M1'));assert(redraws>0);ui.setVisible(false);
 console.log('PASS main M1 overlays: toggles, bounded polling, native source binding, OHLC alignment/gaps, clipping, freshness, partial errors, disconnect/recovery, abort races, hidden tabs and zero trade calls. Browser rendering NOT verified.');
})().catch(e=>{console.error(e);process.exitCode=1;});
