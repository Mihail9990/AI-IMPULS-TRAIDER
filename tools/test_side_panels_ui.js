'use strict';
// Offline browser QA: intercept all HTTP, never contact a trading service.
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),assert=require('assert');
const root=path.resolve(__dirname,'../web/basket-panel'),paper=root.includes('Simylator'),profile={id:'six',name:'Six exact stages',stages:Array.from({length:6},(_,i)=>({volume:'0.01',tp_distance:String((i+1)*.25)})),total_volume:'0.06',sl_distance:'1.5'};
const state={contract:'ticket_basket_v1',mode:paper?'PAPER':'LIVE',scope:paper?'PAPER-fixture':'live-ui',revision:1,cycle_id:'c1',phase:'RECOVERY',debt:'5',realized:'0',stopped:true,read_only:false,settings:{INITIAL_SIZE:'0.09',INITIAL_ENTRY_DISTANCE:'1',INITIAL_SL_DISTANCE:'3',INITIAL_TP_DISTANCE:'.25',BASKET_COUNT:6,TICKET_SIZE:'.01',RECOVERY_SL_DISTANCE:'1.5',EXPANSION_DISTANCE:'1',CYCLE_COOLDOWN_SECONDS:'0'},feed:{status:'FRESH',timestamp_semantics:'VERIFIED',quote:{bid:'4000',ask:'4000.23'},age_seconds:0,candles:[]},levels:{},positions:[],orders:[],journal:[],action_results:{},recovery_mode:'V3',v3_settings:{entry_distance:'1',count_presets:[4,6,10]},v3_sl_distance:'1.5',v3_controls:{can_enter:true,can_link:true,can_cancel_link:false,can_adjust_sl:true},recovery_profiles:{selected_profile_id:'six',profiles:[profile,{id:'one',name:'One 0.10',stages:[{volume:'0.10',tp_distance:'1.25'}]}],recovery_sl_distance:'1.5'},selected_recovery_profile:profile,current_recovery_profiles:[{...profile,group_id:'frozen-1'}],paper:{history:[]}};
state.feed.candles=Array.from({length:300},(_,i)=>({utc_time:1700000000+i*60,open:'3999.90',close:'4000.10',high:'4000.25',low:'3999.80'}));
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',headless:true,args:['--no-sandbox']});
 try{for(const width of [320,390,1280]){
  const page=await browser.newPage({viewport:{width,height:844}}),errors=[],actions=[],dialogs=[];
  let current=structuredClone(state),receipt='APPLIED';
  page.on('pageerror',e=>errors.push(e.message));page.on('dialog',async d=>{dialogs.push(d.message());await d.dismiss();});
  await page.route('**/*',async route=>{
   const u=new URL(route.request().url());
   if(u.pathname.endsWith('/state'))return route.fulfill({json:current});
   if(u.pathname.endsWith('/action')){actions.push(route.request().postDataJSON());return route.fulfill({json:{status:receipt}});}
   const name=u.pathname==='/'?'index.html':path.basename(u.pathname);
   if(['index.html','panel.js','panel.css'].includes(name))return route.fulfill({body:fs.readFileSync(path.join(root,name),'utf8'),contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html'});
   return route.abort();
  });
  await page.goto('http://localhost:8765/');await page.locator('#workspace').waitFor({state:'visible'});
  assert(await page.locator('#v3-sl-panel').evaluate(n=>n.inert));
  const height=await page.locator('#chart').evaluate(n=>n.clientHeight);
  await page.locator('#toggle-sl-panel').click();await page.locator('#toggle-entry-panel').click();
  assert.equal(await page.locator('#v3-sl-panel').getAttribute('data-open'),'true');
  assert.equal(await page.locator('#v3-entry-panel').getAttribute('data-open'),'true');
  assert.equal(await page.locator('#chart').evaluate(n=>n.clientHeight),height);
  await page.keyboard.press('Escape');assert.equal(await page.locator('#v3-entry-panel').getAttribute('data-open'),'false');
  assert.equal(await page.locator('#v3-sl-panel').getAttribute('data-open'),'true');
  await page.locator('#close-sl-panel').click();assert(await page.locator('#v3-sl-panel').evaluate(n=>n.inert));
  await page.locator('#toggle-entry-panel').click();await page.locator('#v3-submit-entry').click();
  await page.waitForTimeout(200);assert.equal(actions.at(-1).action.type,'v3_entry');assert.equal(dialogs.length,0);
  assert.equal(await page.locator('#bot-activity-action').getAttribute('data-tone'),'success');
  current.v3_controls.can_cancel_entry=true;current.v3_controls.can_enter=false;await page.waitForTimeout(1200);
  await page.locator('#v3-cancel-entry').click();await page.waitForTimeout(100);assert.equal(actions.at(-1).action.type,'v3_cancel_entry');
  assert.deepEqual(Object.keys(actions.at(-1).action).sort(),['cycle_id','type']);
  current.v3_request={bar_id:1700000000,order_type:'STOP',recovery_profile:profile};await page.waitForTimeout(1200);
  await page.locator('#v3-cancel-request').click();assert.equal(actions.at(-1).action.type,'v3_cancel_request');assert.equal(dialogs.length,0);
  await page.locator('#close-entry-panel').click();await page.locator('#drawer-toggle').click();await page.locator('#v3-link-toggle').click();assert.equal(actions.at(-1).action.type,'v3_link');assert.equal(dialogs.length,0);
  current.manual_reason='TP1_OWN_ENTRY_SL_UNAVAILABLE';current.journal=['MANUAL: TP1_OWN_ENTRY_SL_UNAVAILABLE'];await page.waitForTimeout(1200);
  assert(!(await page.locator('#bot-activity').innerText()).includes('TP1_OWN_ENTRY_SL_UNAVAILABLE'));
  assert((await page.locator('#bot-activity-warning').innerText()).includes('не удалось перенести SL'));
  assert.equal(await page.locator('#safety').innerText(),'');await page.locator('#toggle-entry-panel').click();assert(await page.locator('#v3-cancel-entry').isDisabled());
  await page.locator('#tab-settings').click();assert(await page.locator('#v3-entry-panel').evaluate(n=>n.inert));
  await page.locator('#tab-chart').click();assert.equal(await page.locator('#v3-entry-panel').getAttribute('data-open'),'false');
  await page.setViewportSize({width,height:480});await page.locator('#toggle-entry-panel').click();
  const bounds=await page.locator('#v3-entry-panel').boundingBox();const chartBounds=await page.locator('#chart-page').boundingBox();assert(bounds.height>0&&bounds.height<=chartBounds.height*.42+1);assert(bounds.y>chartBounds.y,'compact drawer anchors below chart top');
  assert.deepEqual(errors,[]);await page.close();console.log('PASS side drawers, direct STOP, cancel, notices, settings and landscape at '+width);
 }}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
