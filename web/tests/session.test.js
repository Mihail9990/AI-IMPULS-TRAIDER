'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const read=n=>fs.readFileSync(path.join(__dirname,'..',n),'utf8');
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const flush=()=>new Promise(r=>setImmediate(r));
const session=()=>Object.freeze({browserSession:true,csrf:'c'.repeat(43),mode:'native'});
function panel(auth){
 const handlers={},events=[],html=read('index.html');
 const make=id=>({id,value:'',checked:false,attrs:{},handlers:{},classList:{add(){},toggle(){}},setAttribute(k,v){this.attrs[k]=v;},getAttribute(k){return this.attrs[k];},addEventListener(k,v){this.handlers[k]=v;},replaceChildren(){},append(){},focus(){}});
 const ids=Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(([,id])=>[id,make(id)]));
 const tabs=['main','settings','history'].map(n=>ids['tab-'+n]);tabs.forEach((t,i)=>t.attrs['aria-controls']='view-'+['main','settings','history'][i]);
 const document={getElementById:id=>ids[id],querySelectorAll:()=>tabs,createElement:()=>make(''),addEventListener(){},hidden:false};
 const window={RecoverySession:auth,RecoveryModel:{spec:()=>({path:'/api/recovery/state',options:{}})},addEventListener(k,v){handlers[k]=v;},dispatchEvent(e){events.push(e.type);},scrollTo(){}};
 let fetched=0;
 vm.runInNewContext(read('panel.js'),{window,document,location:{protocol:'https:',host:'vps.example'},Event:class{constructor(type){this.type=type;}},CustomEvent:class{constructor(type){this.type=type;}},clearTimeout,setTimeout,AbortController,crypto:require('node:crypto').webcrypto,fetch:async()=>{fetched++;throw Error('offline state');}});
 return {window,ids,handlers,events,fetched:()=>fetched};
}
test('late restore cannot reauthenticate after explicit logout',async()=>{
 const first=deferred();let calls=0,revoked=0;
 const p=panel({restore:()=>++calls===1?first.promise:Promise.resolve(session()),logout:async()=>{revoked++;}});
 await p.ids.logout.handlers.click();first.resolve(session());await flush();
 assert.equal(await p.window.RecoveryView.getToken(),'');assert.equal(revoked,1);assert.equal(p.fetched(),0);
});
test('late login is waited then revoked, never installs UI session after logout',async()=>{
 const login=deferred();let restores=0,revoked=0;
 const p=panel({restore:async()=>{if(++restores===1)throw Error('none');return session();},login:()=>login.promise,logout:async()=>{revoked++;}});
 await flush();p.ids.key.value='a'.repeat(32);
 const submitted=p.ids.login.handlers.submit({preventDefault(){}});
 const exiting=p.ids.logout.handlers.click();login.resolve(session());await submitted;await exiting;await flush();
 assert.equal(await p.window.RecoveryView.getToken(),'');assert.equal(revoked,1);assert.equal(p.fetched(),0);
});
test('BFCache pagehide discards pending restore; pageshow restores view-only',async()=>{
 const old=deferred(),fresh=deferred();let calls=0;
 const p=panel({restore:()=>++calls===1?old.promise:fresh.promise});
 p.handlers.pagehide();old.resolve(session());await flush();assert.equal(await p.window.RecoveryView.getToken(),'');
 p.handlers.pageshow({persisted:true});fresh.resolve(session());await flush();
 assert.equal((await p.window.RecoveryView.getToken()).browserSession,true);
 assert.ok(p.events.filter(x=>x==='recovery-session-change').length>=3);
 assert.match(p.ids['session-label'].textContent,/Торговые кнопки выключены/);
});
test('session helper uses cookie not persisted credentials and restores without Bearer',async()=>{
 const calls=[];const window={};
 vm.runInNewContext(read('session.js'),{window,location:{protocol:'https:'},AbortController,setTimeout,clearTimeout,fetch:async(url,options)=>{calls.push([url,options]);return {ok:true,text:async()=>JSON.stringify({authenticated:true,csrf:'c'.repeat(43),mode:'native',expires_in:100})};}});
 await window.RecoverySession.restore();await window.RecoverySession.login('secret');
 assert.equal(calls[0][1].headers.Authorization,undefined);assert.equal(calls[0][1].credentials,'same-origin');assert.equal(calls[1][1].headers.Authorization,'Bearer secret');
 for(const word of ['localStorage','sessionStorage','document.cookie'])assert.ok(!read('session.js').includes(word));
});
test('double logout is single-flight and blocks login until revocation settles',async()=>{
 const revocation=deferred();let restores=0,revoked=0,logins=0;
 const p=panel({restore:async()=>{restores++;return session();},logout:()=>{revoked++;return revocation.promise;},login:async()=>{logins++;return session();}});
 await flush();const exiting=p.ids.logout.handlers.click();await flush();
 await p.ids.logout.handlers.click();p.ids.key.value='a'.repeat(32);await p.ids.login.handlers.submit({preventDefault(){}});
 assert.equal(revoked,1);assert.equal(logins,0);assert.equal(p.ids.connect.disabled,true);
 revocation.resolve();await exiting;assert.equal(p.ids.connect.disabled,false);
});
