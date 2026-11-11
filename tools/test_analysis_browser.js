'use strict';
// Offline analysis browser QA. Every request is fulfilled locally or aborted.
// Run: node tools/test_analysis_browser.js (requires a browser-capable host).
// No service, MT5, broker, login, or action endpoint is contacted.
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),assert=require('assert');
const root=path.resolve(__dirname,'../web/basket-panel'),paper=root.includes('Simylator'),profile={id:'six',name:'Six exact stages',stages:Array.from({length:6},(_,i)=>({volume:'0.01',tp_distance:String((i+1)*.25)})),total_volume:'0.06',sl_distance:'1.5'};
const state={contract:'ticket_basket_v1',mode:paper?'PAPER':'LIVE',scope:paper?'PAPER-fixture':'live-ui',revision:1,cycle_id:'c1',phase:'RECOVERY',debt:'5',realized:'0',stopped:true,read_only:false,settings:{INITIAL_SIZE:'0.09',INITIAL_ENTRY_DISTANCE:'1',INITIAL_SL_DISTANCE:'3',INITIAL_TP_DISTANCE:'.25',BASKET_COUNT:6,TICKET_SIZE:'.01',RECOVERY_SL_DISTANCE:'1.5',EXPANSION_DISTANCE:'1',CYCLE_COOLDOWN_SECONDS:'0'},feed:{status:'FRESH',timestamp_semantics:'VERIFIED',quote:{bid:'4000',ask:'4000.23'},age_seconds:0,candles:[]},levels:{},positions:[],orders:[],journal:[],action_results:{},recovery_mode:'V3',v3_settings:{entry_distance:'1',count_presets:[4,6,10]},v3_sl_distance:'1.5',v3_controls:{can_enter:true,can_link:true,can_cancel_link:false,can_adjust_sl:true},recovery_profiles:{selected_profile_id:'six',profiles:[profile,{id:'one',name:'One 0.10',stages:[{volume:'0.10',tp_distance:'1.25'}]}],recovery_sl_distance:'1.5'},selected_recovery_profile:profile,current_recovery_profiles:[{...profile,group_id:'frozen-1'}],paper:{history:[]}};
state.feed.candles=Array.from({length:300},(_,i)=>({utc_time:1700000000+i*60,open:'3999.90',close:'4000.10',high:'4000.25',low:'3999.80'}));

const rawMode=process.argv.includes('--raw');
const seconds={M1:60,M15:900,M30:1800,H1:3600},end=1804802400;
function utcSnapshot(tf='M1'){
 const bars=Array.from({length:100},(_,i)=>({utc_time:end-(100-i)*seconds[tf],open:3999.90,close:4000.10,high:4000.25,low:3999.80}));
 const a=bars[70].utc_time,b=bars[95].utc_time;
 return {schema:'analysis_v1',source:'MT5',symbol:'XAUUSD.s',feed_instance:'offline-fixture',timeframe:tf,status:'READY',timestamp_semantics:'VERIFIED',as_of:end+10,closed_through:end,generated_at:end+10,bars,
  modules:{talib:{status:'READY'},smc:{status:'READY'},trendln:{status:'READY'},geometry:{status:'READY'}},
  notes:['Offline explanatory fixture; must remain outside canvas'],overlays:{
   candlestick:[{kind:'engulfing',label:'Candle fixture',start_time:b,price:4000.1}],
   structure:[{kind:'liquidity_zone',start_time:a,end_time:b,top:4000.2,bottom:4000,label:'OHLC estimate'}],
   trendlines:[{kind:'trendline',points:[{time:a,price:3999.9},{time:b,price:4000.1}]}],
   figures:[{kind:'figure_double_top',label:'Double top fixture',points:[{time:a,price:4000.1},{time:b,price:4000.2}]}]}};
}
function snapshot(tf='M1'){
 const value=utcSnapshot(tf);if(!rawMode)return value;
 return {...value,timestamp_semantics:'BROKER_RAW',time_domain:'BROKER_RAW',broker_time:value.as_of,bars:value.bars.map(b=>({...b,broker_time:b.utc_time,raw_time:b.utc_time,utc_time:null,time_domain:'BROKER_RAW',closed:true}))};
}
Object.assign(state.feed,{source:'MT5',instance:'offline-fixture',symbol:'XAUUSD.s',connected:true,timestamp:end+10,age_seconds:0,candles:snapshot().bars});
if(rawMode)Object.assign(state.feed,{status:'OBSERVED',timestamp_semantics:'BROKER_RAW',time_domain:'BROKER_RAW',timestamp:null,age_seconds:null,broker_time:end+10,raw_time:end+10});
state.positions=[{id:'fixture-ticket',ticket:'123456',side:'BUY',entry:'4000.00',sl:'3999.50',tp:'4000.60',volume:'0.01'}];
state.orders=[{id:'fixture-order',side:'BUY',price:'4000.40'}];state.levels={BUY:'4000.40'};
async function ready(page,id){await page.waitForFunction(id=>document.getElementById(id+'-chart').querySelectorAll('rect').length>=100,id);}
async function equalLayout(page){
 const boxes=await page.locator('.analysis-card').evaluateAll(ns=>ns.map(n=>{const c=n.querySelector('svg');return {height:n.getBoundingClientRect().height,chart:c.getBoundingClientRect().height,y:n.getBoundingClientRect().y};}));
 assert.equal(boxes.length,2);assert(boxes.every(b=>b.height>80&&b.chart>20),'charts remain usable');
 assert(Math.abs(boxes[0].height-boxes[1].height)<=1,'equal half-height cards');
 assert(Math.abs(boxes[0].chart-boxes[1].chart)<=1,'equal default chart heights');
 assert(Math.abs(boxes[0].y+boxes[0].height-boxes[1].y)<=1,'cards stacked without gaps');
 assert.equal(await page.locator('.analysis-info[open]').count(),0);
 for(const id of ['patterns','structure']){
  const axis=await page.locator('#'+id+'-chart text:not([data-figure-label])').allTextContents();
  assert.equal(axis.length,9,'five prices, three times and UTC retained');
  assert(axis.every(t=>/^(?:\d+\.\d{2}|\d{2}-\d{2} \d{2}:\d{2}|UTC|время MT5)$/.test(t)),'only price/time axes, never explanatory chart text');
 }
 assert.equal(await page.locator('#patterns-chart [data-pattern-label="true"]').textContent(),'Candle fixture');
 assert.equal(await page.locator('#structure-chart [data-figure-label="true"]').textContent(),'Double top fixture');
 assert(!(await page.locator('#analysis-page').innerText()).includes('Offline explanatory fixture'),'details collapsed by default');
}
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',headless:true,args:['--no-sandbox']});
 try{for(const viewport of [{width:320,height:844},{width:390,height:844},{width:1280,height:844},{width:844,height:390}]){
  const context=await browser.newContext({viewport,serviceWorkers:'block'}),page=await context.newPage(),errors=[],writes=[],unexpected=[],requests=[];
  let failTf=null;
  page.on('pageerror',e=>errors.push(e.message));
  await context.route('**/*',async route=>{
   const req=route.request(),u=new URL(req.url());
   if(req.method()!=='GET'){writes.push(req.url());return route.abort();}
   if(u.origin!=='http://localhost:8765'){unexpected.push(req.url());return route.abort();}
   if(u.pathname==='/api/state')return route.fulfill({json:state});
   if(u.pathname==='/api/analysis'){
    const tf=u.searchParams.get('timeframe');requests.push(tf);
    assert.equal(req.headers()['x-local-control'],'1');assert.equal(req.headers()['x-controller-scope'],state.scope);
    if(tf===failTf)return route.fulfill({status:503,body:'offline fixture failure'});
    if(!seconds[tf]){unexpected.push(req.url());return route.abort();}
    return route.fulfill({json:snapshot(tf)});
   }
   const name=u.pathname==='/'?'index.html':u.pathname.slice(1);
   if(['index.html','diagnostics.js','panel.js','analysis.js','panel.css'].includes(name))return route.fulfill({body:fs.readFileSync(path.join(root,name),'utf8'),contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html'});
   if(u.pathname!=='/favicon.ico')unexpected.push(req.url());return route.abort();
  });
  try{
   await page.goto('http://localhost:8765/');await page.locator('#workspace').waitFor({state:'visible'});
   await page.waitForFunction(()=>document.querySelector('#chart [data-quote="bid"]'));
   assert.equal(requests.length,0,'disabled overlays do not fetch');
   if(rawMode){assert((await page.locator('#chart').textContent()).includes('время MT5'));assert(!(await page.locator('#chart').textContent()).includes('UTC'));assert((await page.locator('#source-label').textContent()).includes('время MT5'));assert(!(await page.locator('#quote').textContent()).includes('возраст'));assert.equal(await page.locator('#v3-submit-entry').isDisabled(),false);}

   await page.locator('#liquidity-visible').check();await page.waitForFunction(()=>document.querySelector('#chart [data-analysis-kind="liquidity_zone"]'));
   await page.locator('#main-trend').check();await page.waitForFunction(()=>document.querySelector('#chart [data-analysis-kind="trendline"]'));
   assert.equal(await page.locator('#chart [data-analysis-layer] text').count(),0);
   assert.equal(await page.locator('#chart [data-analysis-layer]').getAttribute('pointer-events'),'none');
   assert.equal(await page.locator('#chart [data-quote]').count(),2,'Bid/Ask retained');
   for(const key of ['fixture-ticket-entry','fixture-ticket-sl','fixture-ticket-tp','trigger-BUY'])assert.equal(await page.locator('#chart [data-line="'+key+'"]').count(),1,'trading line retained: '+key);
   await page.locator('#liquidity-visible').uncheck();await page.waitForFunction(()=>!document.querySelector('#chart [data-analysis-kind="liquidity_zone"]'));
   await page.locator('#main-trend').uncheck();await page.waitForFunction(()=>!document.querySelector('#chart [data-analysis-layer]'));
   await page.locator('#tab-analysis').click();await ready(page,'patterns');await ready(page,'structure');await equalLayout(page);if(rawMode){for(const id of ['patterns','structure']){assert((await page.locator('#'+id+'-source').textContent()).includes('время MT5'));assert(!(await page.locator('#'+id+'-chart').textContent()).includes('UTC'));}if(process.env.UI_SCREENSHOT_DIR)await page.screenshot({path:path.join(process.env.UI_SCREENSHOT_DIR,'raw-analysis-'+viewport.width+'x'+viewport.height+'.png')});}
   for(const tf of ['M15','M30','H1','M1']){
    await page.locator('#patterns-timeframe').selectOption(tf);await ready(page,'patterns');
    assert.equal(await page.locator('#structure-timeframe').inputValue(),'M1');assert((await page.locator('#patterns-source').textContent()).includes(tf));
   }
   await page.locator('#structure-timeframe').selectOption('H1');await ready(page,'structure');assert.equal(await page.locator('#patterns-timeframe').inputValue(),'M1');
   failTf='M30';await page.locator('#patterns-timeframe').selectOption('M30');await page.waitForFunction(()=>document.getElementById('patterns-status').textContent.includes('503'));
   assert.equal(await page.locator('#patterns-chart > *').count(),0);assert.equal(await page.locator('#patterns-chart text').count(),0);assert(await page.locator('#patterns-status').isVisible());
   assert((await page.locator('#structure-source').textContent()).includes('H1'),'error isolated');
   await page.setViewportSize({width:viewport.width,height:viewport.height-20});await page.waitForTimeout(100);assert.equal(await page.locator('#patterns-chart > *').count(),0,'resize does not resurrect failed snapshot');
   failTf=null;await page.locator('#patterns-timeframe').selectOption('M1');await ready(page,'patterns');
   for(let i=0;i<2;i++){await page.locator('#tab-chart').click();await page.locator('#tab-analysis').click();await ready(page,'patterns');await ready(page,'structure');}
   await page.locator('#tab-analysis').focus();await page.keyboard.press('Home');assert(await page.locator('#chart-page').isVisible());
   assert.deepEqual(writes,[],'read-only controls never POST or send trading actions');assert.deepEqual(unexpected,[]);assert.deepEqual(errors,[]);
   console.log('PASS offline analysis UI '+(rawMode?'BROKER_RAW ':'UTC ')+viewport.width+'x'+viewport.height);
  }finally{await context.close();}
 }}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
