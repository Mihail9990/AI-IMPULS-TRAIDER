const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),assert=require('assert');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[],actions=[];
 page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());
 const root=path.resolve(__dirname,'../../web/native-panel');
 const state={protocol:'native_state_v1',scope:'native-test',revision:5,phase:'RUNNING',reason:'',read_only:false,cycle_id:'1',steps:3,recovery:'2.00',realized:'-.06',currency:'USD',bid:'100.60',ask:'100.62',tick_size:'.01',broker_time:180,positions:[{id:'77',side:'BUY',volume:'.01',entry:'100.60',sl:'99.10',tp:'100.90',stage:0}],orders:[],history:[],candles:Array.from({length:60},(_,i)=>({time:60*i,open:100,high:100.8,low:99.8,close:100.5})),settings:{initial_sl:'1.5',initial_tp:'.25',offset:'.5',recovery_offset:'.5',recovery_sl:'1.5',sl_first:'.25',sl_second:'.40',sl_first_offset:'-.25',sl_second_offset:'0',profiles:{2:'.30,.70',3:'.30,.70,1.00'}},action_results:{}};
 await page.route('**/*',async route=>{const url=new URL(route.request().url());if(url.origin!=='http://native.test')throw Error('Unexpected network '+url);
  if(url.pathname==='/api/state')return route.fulfill({json:state});
  if(url.pathname==='/api/action'){const request=route.request().postDataJSON();actions.push(request);return route.fulfill({json:{status:'AWAITING_EA'}});}
  if(url.pathname.startsWith('/api/logs'))return route.fulfill({json:{text:'mock logs',page:0,has_more:false,enabled:false}});
  const file=path.join(root,url.pathname==='/'?'index.html':url.pathname.slice(1));return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'});
 });
 await page.goto('http://native.test');await page.waitForSelector('#workspace:not([hidden])');
 assert.deepEqual(errors,[]);assert.equal(await page.locator('#v3-entry-panel').count(),0);
 assert(await page.locator('#chart rect').count()>10);assert((await page.locator('#chart').textContent()).includes('BID'));assert((await page.locator('#chart').textContent()).includes('ASK'));
 const line=page.locator('#chart [data-line="77-sl"] line').first();const box=await line.boundingBox();assert(box);
 await page.mouse.move(box.x+100,box.y);await page.mouse.down();await page.waitForTimeout(550);await page.mouse.move(box.x+100,box.y-12);await page.mouse.up();await page.waitForSelector('#preview:not([hidden])');assert.equal(actions.length,0);await page.locator('#cancel-level').click();assert.equal(actions.length,0);
 await page.locator('#drawer-toggle').click();await page.locator('#cycle-keyboard').evaluate(e=>e.open=true);await page.locator('#cancel_triggers').click();await page.locator('#close_all').click();assert.equal(actions.length,2);assert.equal(actions[0].action.type,'cancel_triggers');assert.equal(actions[1].action.type,'close_all');
 await page.locator('#tab-settings').click();await page.locator('#monitor-first').evaluate(e=>{for(let p=e.parentElement;p;p=p.parentElement)if(p.tagName==='DETAILS')p.open=true;});await page.locator('#monitor-first').fill('.30');await page.locator('#monitor-save').click();assert.equal(actions.at(-1).action.type,'save_sl_monitor');
 assert.deepEqual(errors,[]);await page.screenshot({path:'/tmp/native-ui-smoke.png',fullPage:true});await browser.close();console.log('Existing panel smoke: candles, Bid/Ask, hold, confirmation, independent commands, monitor settings OK');
})().catch(e=>{console.error(e);process.exit(1)});
