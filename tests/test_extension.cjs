/* Run with node --test tests/test_extension.cjs. No real browser or network. */
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const {webcrypto} = require('node:crypto');

async function flush() {for(let i=0;i<12;i++)await new Promise(resolve=>setImmediate(resolve));}
function worker({recovering=false,failResult=false,composerDelay=0,composerStatus='ready'}={}) {
  const adapter={id:'chatgpt',url:'https://chatgpt.com/',prompt_selector:'#prompt'};
  const task={id:'web_synthetic',provider:'chatgpt',adapter,lease:'synthetic-lease-long-enough',kind:'image',reference_count:1,remaining:1,prompt:'synthetic prompt',expires_at:Date.now()/1000+120};
  const local={settings:{url:'http://127.0.0.1:8765',token:'br_synthetic_test_not_real_abcdefghijklmnopqrstuvwxyz'},sessionId:'synthetic_session_1'};
  const session={managedTabs:{chatgpt:{tabId:12,adapter}},inflight:recovering?{...task,tabId:12,preparing:true}:null};
  let listener,alarm,timer,claimed=false,running=false;
  const calls={runs:0,updates:0,composerProbes:0,results:[],requests:[]};
  const area=object=>({get:async keys=>Object.fromEntries((Array.isArray(keys)?keys:[keys]).filter(key=>key in object).map(key=>[key,object[key]])),set:async values=>Object.assign(object,structuredClone(values)),setAccessLevel:async()=>{}});
  const chrome={storage:{local:area(local),session:area(session)},tabs:{get:async()=>({id:12,url:adapter.url,status:'complete'}),update:async()=>{calls.updates++;return {id:12};},sendMessage:async(id,message)=>{if(message.type==='web2api:probe'){let status='ready';if(calls.updates){calls.composerProbes++;status=calls.composerProbes<=composerDelay?'page_changed':composerStatus;}return {status,task:running?task.id:null};}if(message.type==='web2api:run'){calls.runs++;running=true;assert.equal(message.task.references.length,1);return {accepted:true};}}},scripting:{executeScript:async()=>{}},alarms:{create:()=>{},onAlarm:{addListener:fn=>{alarm=fn;}}},runtime:{id:'synthetic-extension',onMessage:{addListener:fn=>{listener=fn;}},onStartup:{addListener:()=>{}}}};
  const response=(body,status=200)=>({ok:status<400,status,json:async()=>body});
  const fetch=async(url,options={})=>{
    calls.requests.push(url);
    const body=options.body?JSON.parse(options.body):null;
    if(url.endsWith('/heartbeat'))return response({ok:true});
    if(url.endsWith('/claim')){if(claimed||recovering)return response({task:null});claimed=true;return response({task});}
    if(url.includes('/references/'))return {ok:true,headers:{get:()=> 'image/png'},arrayBuffer:async()=>new Uint8Array([1,2,3]).buffer};
    if(url.endsWith('/result')){calls.results.push(body);if(failResult&&calls.results.length===1)throw new Error('synthetic offline');running=false;return response({ok:true});}
    throw new Error('Unexpected fetch '+url);
  };
  const context=vm.createContext({chrome,fetch,crypto:webcrypto,URL,AbortSignal,Uint8Array,btoa:value=>Buffer.from(value,'binary').toString('base64'),setTimeout:(fn,ms)=>{if(ms<1000)setImmediate(fn);else timer=fn;return 1;},clearTimeout:()=>{},Date,console});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../browser-extension/background.js'),'utf8'),context);
  return {calls,local,session,task,get listener(){return listener;},get alarm(){return alarm;},runTimer:async()=>{await timer();await flush();}};
}

test('network recovery retries result delivery and never resends the prompt',async()=>{
  const state=worker({failResult:true});await flush();
  assert.equal(state.calls.runs,1);assert.equal(state.calls.updates,1);
  const ignored=state.listener({type:'web2api:result',id:state.task.id,result:{images:['ignored']}},{tab:{id:999}},()=>{});
  assert.equal(ignored,undefined);assert.equal(state.calls.results.length,0);
  await new Promise(resolve=>state.listener({type:'web2api:result',id:state.task.id,result:{images:['synthetic-png']}},{tab:{id:12}},resolve));
  assert.equal(state.calls.results.length,1);assert.equal(state.session.inflight.id,state.task.id);
  await state.runTimer();
  assert.equal(state.calls.results.length,2);assert.equal(state.calls.runs,1);
  assert.equal(state.session.inflight,null);
  assert.equal(state.calls.results[1].lease,state.task.lease);
});

test('worker restart during preparation reports uncertainty without executing again',async()=>{
  const state=worker({recovering:true});await flush();
  assert.equal(state.calls.runs,0);assert.equal(state.calls.updates,0);
  assert.equal(state.calls.results[0].error,'browser_interrupted');
  assert.equal(state.session.inflight,null);
});

test('content script cannot change pairing configuration',async()=>{
  const state=worker();await flush();
  const before=structuredClone(state.local.settings);
  state.listener({type:'configure',url:'http://attacker.invalid',token:'stolen'},{tab:{id:12},id:'synthetic-extension'},()=>{});
  await flush();assert.deepEqual(state.local.settings,before);
});

test('navigation waits for the composer to render before sending one prompt',async()=>{
  const state=worker({composerDelay:2});await flush();
  assert.equal(state.calls.composerProbes,3);
  assert.equal(state.calls.updates,1);
  assert.equal(state.calls.runs,1);
  assert.equal(state.calls.results.length,0);
});

test('login needed after navigation fails before uploading or sending a prompt',async()=>{
  const state=worker({composerStatus:'login_required'});await flush();
  assert.equal(state.calls.runs,0);
  assert.equal(state.calls.requests.some(url=>url.includes('/references/')),false);
  await state.runTimer();
  assert.equal(state.calls.results[0].error,'login_required');
  assert.equal(state.calls.runs,0);
});
