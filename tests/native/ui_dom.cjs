// Run with JSDOM_MODULE=/path/to/jsdom when no local browser can open sockets.
const {JSDOM}=require(process.env.JSDOM_MODULE||'jsdom');
const fs=require('fs'),path=require('path'),assert=require('assert');
(async()=>{
 const root=path.resolve(__dirname,'../../web/native-panel');const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://native.test',runScripts:'outside-only',pretendToBeVisual:true});const w=dom.window,actions=[];
 w.structuredClone=structuredClone;w.AbortSignal=AbortSignal;w.confirm=()=>true;w.setInterval=()=>0;w.ResizeObserver=class{observe(){}};
 w.HTMLElement.prototype.scrollIntoView=()=>{};w.SVGElement.prototype.createSVGPoint=()=>({x:0,y:0,matrixTransform(){return this;}});w.SVGElement.prototype.getScreenCTM=()=>({inverse:()=>({})});
 for(const key of ['clientWidth','clientHeight'])Object.defineProperty(w.Element.prototype,key,{get:()=>key==='clientWidth'?1000:520});
 const n={protocol:'native_state_v1',scope:'mock',revision:5,phase:'RUNNING',read_only:false,cycle_id:'1',steps:3,recovery:'2',realized:'-.06',currency:'USD',bid:'100.60',ask:'100.62',broker_time:180,positions:[{id:'77',side:'BUY',volume:'.01',entry:'100.6',sl:'99.1',tp:'100.9',stage:0}],orders:[],history:[],candles:Array.from({length:60},(_,i)=>({time:i*60,open:100,high:100.8,low:99.8,close:100.5})),settings:{initial_sl:'1.5',initial_tp:'.25',offset:'.5',recovery_offset:'.5',recovery_sl:'1.5',sl_first:'.25',sl_second:'.40',sl_first_offset:'-.25',sl_second_offset:'0',profiles:{2:'.3,.7',3:'.3,.7,1'}},action_results:{}};
 w.fetch=async(url,options={})=>{if(url==='/api/action'){actions.push(JSON.parse(options.body));return{ok:true,json:async()=>({status:'AWAITING_EA'})};}if(url==='/api/state')return{ok:true,json:async()=>structuredClone(n)};throw Error('Unexpected request '+url);};
 w.eval(['diagnostics.js','native-adapter.js','panel.js'].map(f=>fs.readFileSync(path.join(root,f),'utf8')).join('\n'));
 const wait=()=>new Promise(r=>setTimeout(r,30));await wait();
 const d=w.document;assert.equal(d.getElementById('workspace').hidden,false);assert.equal(d.getElementById('v3-entry-panel'),null);assert(d.querySelectorAll('#chart rect').length>10,'candles rendered');assert(d.getElementById('chart').textContent.includes('BID'));assert(d.getElementById('chart').textContent.includes('ASK'));assert(!d.getElementById('feed-status').textContent.includes('Ошибка'),'render succeeded');
 d.getElementById('cancel_triggers').click();await wait();d.getElementById('close_all').click();await wait();assert.deepEqual(actions.map(a=>a.action.type),['cancel_triggers','close_all']);
 d.getElementById('monitor-first').value='.30';d.getElementById('monitor-save').click();await wait();assert.equal(actions.at(-1).action.first,'.30');
 const before=d.querySelectorAll('#chart rect').length;d.getElementById('zoom-in').click();assert(d.querySelectorAll('#chart rect').length<before);d.getElementById('chart-reset').click();assert.equal(d.querySelectorAll('#chart rect').length,before);
 assert(d.getElementById('logs-download'));assert(d.getElementById('cancel-initial'));assert(d.getElementById('writeoff-recovery'));assert(d.getElementById('reset-pause'));assert(d.getElementById('reset'));
 dom.window.close();console.log('DOM smoke OK: original panel, candles/Bid/Ask, zoom, independent commands, SL settings, preserved controls');
})().catch(e=>{console.error(e);process.exit(1)});
