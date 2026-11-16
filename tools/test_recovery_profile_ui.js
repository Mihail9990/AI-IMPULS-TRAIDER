'use strict';
// Offline browser QA: intercept all HTTP, never contact a trading service.
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),assert=require('assert');
const root=path.resolve(__dirname,'../web/basket-panel'),paper=root.includes('Simylator'),profile={id:'six',name:'Six exact stages',stages:Array.from({length:6},(_,i)=>({volume:'0.01',tp_distance:String((i+1)*.25)})),total_volume:'0.06',sl_distance:'1.5'};
const state={contract:'ticket_basket_v1',mode:paper?'PAPER':'LIVE',scope:paper?'PAPER-fixture':'live-ui',revision:1,cycle_id:'c1',phase:'RECOVERY',debt:'5',realized:'0',stopped:true,read_only:false,settings:{INITIAL_SIZE:'0.09',INITIAL_ENTRY_DISTANCE:'1',INITIAL_SL_DISTANCE:'3',INITIAL_TP_DISTANCE:'.25',BASKET_COUNT:6,TICKET_SIZE:'.01',RECOVERY_SL_DISTANCE:'1.5',EXPANSION_DISTANCE:'1',CYCLE_COOLDOWN_SECONDS:'0'},feed:{status:'FRESH',timestamp_semantics:'VERIFIED',quote:{bid:'4000',ask:'4000.23'},age_seconds:0,candles:[]},levels:{},positions:[],orders:[],journal:[],action_results:{},recovery_mode:'V3',v3_settings:{entry_distance:'1',count_presets:[4,6,10]},v3_sl_distance:'1.5',v3_controls:{can_enter:true,can_link:true,can_cancel_link:false,can_adjust_sl:true},recovery_profiles:{selected_profile_id:'six',profiles:[profile,{id:'one',name:'One 0.10',stages:[{volume:'0.10',tp_distance:'1.25'}]}],recovery_sl_distance:'1.5'},selected_recovery_profile:profile,current_recovery_profiles:[{...profile,group_id:'frozen-1'}],paper:{history:[]}};
state.feed.candles=Array.from({length:300},(_,i)=>({utc_time:1700000000+i*60,open:'3999.90',close:'4000.10',high:'4000.25',low:'3999.80'}));
async function assertQuoteGutter(page){
 const geometry=await page.evaluate(()=>{
  const svg=document.querySelector('#chart');
  const candles=[...svg.querySelectorAll(':scope > rect')].map(n=>n.getBBox());
  const badges=[...svg.querySelectorAll('[data-quote] rect')].map(n=>n.getBBox());
  const texts=[...svg.querySelectorAll('[data-quote] text')].map(n=>n.getBBox());
  return {right:Math.max(...candles.map(n=>n.x+n.width)),badges:badges.map(n=>({x:n.x,y:n.y,width:n.width,height:n.height})),texts:texts.map(n=>({x:n.x,y:n.y,width:n.width,height:n.height}))};
 });
 assert.equal(geometry.badges.length,2);
 for(let i=0;i<geometry.badges.length;i++){
  const badge=geometry.badges[i],text=geometry.texts[i];
  assert(geometry.right+18<=badge.x,'candles never enter the reserved quote gutter');
  assert(text.x>=badge.x&&text.x+text.width<=badge.x+badge.width,'price text fits its badge');
 }
 const [a,b]=geometry.badges;assert(a.y+a.height<=b.y||b.y+b.height<=a.y,'Bid/Ask badges do not overlap');
}
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',headless:true,args:['--no-sandbox']});
 try{
  for(const width of [320,390,1280]){
   const page=await browser.newPage({viewport:{width,height:844}}),errors=[],actions=[];
   page.on('pageerror',e=>errors.push(e.message));
   await page.route('**/*',async route=>{
    const u=new URL(route.request().url());
    if(u.pathname.endsWith('/state'))return route.fulfill({json:state});
    if(u.pathname.endsWith('/action')){actions.push(route.request().postDataJSON());return route.fulfill({json:{status:'APPLIED'}});}
    const name=u.pathname==='/'?'index.html':path.basename(u.pathname);
    if(['index.html','diagnostics.js','panel.js','panel.css'].includes(name)){
     let body=fs.readFileSync(path.join(root,name),'utf8');
     if(name==='index.html')body=body.replace('__PANEL_ROLE__',paper?'PAPER':'LIVE');
     return route.fulfill({body,contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html'});
    }
    return route.abort();
   });
   await page.goto('http://localhost:8765/');await page.locator('#workspace').waitFor({state:'visible'});
   await assertQuoteGutter(page);
   await page.locator('#chart').hover();
   for(let i=0;i<16;i++)await page.mouse.wheel(0,-200);
   await page.waitForTimeout(100);await assertQuoteGutter(page);
   for(let i=0;i<32;i++)await page.mouse.wheel(0,200);
   await page.waitForTimeout(100);await assertQuoteGutter(page);
   await page.setViewportSize({width:width===1280?640:width,height:480});await page.waitForTimeout(100);
   await assertQuoteGutter(page);await page.setViewportSize({width,height:844});
   await page.locator('#tab-settings').click();
   await page.locator('summary').filter({hasText:'Параметры торговли'}).click();
   await page.locator('#recovery-profile-settings>summary').click();
   assert(!await page.locator('#recovery-profile-name').isVisible(),'editor begins collapsed');
   await page.locator('#recovery-profile-new').click();
   assert(await page.locator('#recovery-profile-name').isVisible(),'New reveals editor');
   assert(await page.locator('#recovery-profile-name').evaluate(n=>n===document.activeElement),'New focuses name');
   assert.equal(await page.locator('.recovery-stage-row').count(),1);
   await page.locator('#recovery-profile-new').click();
   await page.locator('#recovery-profile-name').fill('Unsaved draft');
   await page.getByRole('textbox',{name:'Объём · ступень 1',exact:true}).fill('0.101');
   await page.getByRole('textbox',{name:'TP от входа · ступень 1',exact:true}).fill('0.251');
   for(const id of ['recovery-profile-new','recovery-profile-edit']){
    await page.locator('#recovery-profile-editor>summary').click();
    page.once('dialog',dialog=>dialog.dismiss());await page.locator('#'+id).click();
    assert(await page.locator('#recovery-profile-name').isVisible());
    assert.equal(await page.locator('#recovery-profile-name').inputValue(),'Unsaved draft');
    assert.equal(await page.getByRole('textbox',{name:'Объём · ступень 1',exact:true}).inputValue(),'0.101');
   }
   page.once('dialog',dialog=>dialog.dismiss());await page.locator('#recovery-profile-save').click();
   assert.equal(actions.length,0,'cancelled save never sends a command');
   await page.locator('#tab-chart').click();await page.locator('#tab-settings').click();
   assert.equal(await page.locator('#recovery-profile-name').inputValue(),'Unsaved draft','navigation preserves draft');
   await page.locator('#recovery-profile-select').selectOption('one');
   page.once('dialog',dialog=>dialog.accept());await page.locator('#recovery-profile-edit').click();
   assert.equal(await page.locator('#recovery-profile-name').inputValue(),'One 0.10');
   await page.locator('#recovery-add-stage').click();assert.equal(await page.locator('.recovery-stage-row').count(),2);
   await page.locator('.recovery-stage-row').last().getByRole('button',{name:'Удалить'}).click();
   assert.equal(await page.locator('.recovery-stage-row').count(),1);
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'no horizontal overflow');
   assert.equal(errors.length,0,errors.join('\n'));assert.equal(actions.length,0,'editing/navigation never send trading commands');
   await page.screenshot({path:'/tmp/recovery-profile-'+(paper?'paper':'live')+'-'+width+'.png',fullPage:true});await page.close();
  }
  console.log('PASS offline Chromium: profile reveal/focus, repeat/cancel/replace, exact draft retention; quote gutter/zoom/resize at 320/390/1280px. No service contacted.');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
