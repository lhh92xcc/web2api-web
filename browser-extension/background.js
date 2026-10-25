"use strict";
let settings, tabs = {}, inflight = null, outbox = null, polling = false, delivering = null;
let timer;
const safeCodes = new Set(["login_required","page_changed","page_busy","upload_failed","send_failed","download_failed","no_generated_image","upstream_timeout","browser_interrupted","upstream_error"]);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function load() {
  const saved = await chrome.storage.local.get(["settings","sessionId"]);
  settings = saved.settings;
  if (!saved.sessionId) {saved.sessionId = crypto.randomUUID(); await chrome.storage.local.set({sessionId:saved.sessionId});}
  if (settings) settings.sessionId = saved.sessionId;
  const session = await chrome.storage.session.get(["managedTabs","inflight"]);
  tabs = session.managedTabs || {};
  inflight = session.inflight || null;
}
async function persist() {await chrome.storage.session.set({managedTabs:tabs, inflight});}

async function api(path, body) {
  const response = await fetch(settings.url + "/bridge/v1/" + path, {
    method: body === undefined ? "GET" : "POST",
    headers:{"Authorization":"Bearer " + settings.token, "Content-Type":"application/json"},
    body:body === undefined ? undefined : JSON.stringify(body),
    signal:AbortSignal.timeout(15000),
  });
  if (!response.ok) {const error = new Error(`本地服务返回 ${response.status}，请检查配对和服务状态`); error.status = response.status; throw error;}
  return response.json();
}
const leaseBody = task => ({session_id:settings.sessionId, lease:task.lease});
async function deliver() {
  if (delivering) return delivering;
  if (!inflight || !outbox) return;
  const task = inflight, result = outbox;
  delivering = (async () => {
    try {
      await api(`tasks/${task.id}/result`, {...leaseBody(task), ...result});
    } catch (error) {
      if (error.status === 413 || error.status === 415 || error.status === 422) {
        outbox = {error:"download_failed"};
        return;
      }
      if (error.status !== 409) throw error;
    }
    if (inflight?.id === task.id) {inflight = null; outbox = null; await persist();}
  })();
  try {await delivering;} finally {delivering = null;}
}

async function inject(tabId) {
  await chrome.scripting.executeScript({target:{tabId}, files:["content.js"]});
}
async function probe(item) {
  try {
    const tab = await chrome.tabs.get(item.tabId);
    if (new URL(tab.url).origin !== new URL(item.adapter.url).origin) return {provider:item.adapter.id, status:"disconnected"};
    if (tab.status !== "complete") return {provider:item.adapter.id, status:"busy"};
    await inject(item.tabId);
    const result = await chrome.tabs.sendMessage(item.tabId, {type:"web2api:probe", adapter:item.adapter});
    if (inflight?.tabId === item.tabId && !outbox && result.task !== inflight.id && !inflight.preparing) outbox = {error:"browser_interrupted"};
    return {provider:item.adapter.id, status:result.status};
  } catch {
    if (inflight?.tabId === item.tabId && !outbox && !inflight.preparing) outbox = {error:"browser_interrupted"};
    return {provider:item.adapter.id, status:"disconnected"};
  }
}

async function prepare(task) {
  const item = tabs[task.provider];
  inflight = {...task, tabId:item?.tabId, preparing:true};
  await persist();
  if (!item) {inflight.preparing=false; outbox = {error:"browser_interrupted"}; return;}
  try {
    // Each task starts from the configured new-chat entry, isolating project contexts.
    await chrome.tabs.update(item.tabId, {url:task.adapter.url});
    const deadline = Math.min(task.expires_at * 1000, Date.now() + 30000);
    while ((await chrome.tabs.get(item.tabId)).status !== "complete") {
      if (Date.now() > deadline) throw new Error("page_changed");
      await sleep(300);
    }
    await inject(item.tabId);
    // Browser load completion can precede the app's hydrated composer.
    // Only inspect readiness here; the prompt is still submitted once below.
    let pageStatus;
    while (Date.now() < deadline) {
      const result = await chrome.tabs.sendMessage(item.tabId, {type:"web2api:probe", adapter:task.adapter});
      pageStatus = result?.status;
      if (pageStatus === "ready") break;
      if (pageStatus === "login_required") throw new Error("login_required");
      if (pageStatus === "busy") throw new Error("page_busy");
      if (pageStatus !== "page_changed") throw new Error("browser_interrupted");
      await sleep(300);
    }
    if (pageStatus !== "ready") throw new Error(Date.now() >= task.expires_at * 1000 ? "upstream_timeout" : "page_changed");
    const references = [];
    for (let i=0; i<task.reference_count; i++) {
      const response = await fetch(settings.url + `/bridge/v1/tasks/${task.id}/references/${i}`, {
        method:"POST", headers:{"Authorization":"Bearer " + settings.token,"Content-Type":"application/json"},
        body:JSON.stringify(leaseBody(task)), signal:AbortSignal.timeout(20000),
      });
      if (!response.ok) throw new Error("upload_failed");
      const bytes = new Uint8Array(await response.arrayBuffer());
      let binary = "";
      for (let start=0; start<bytes.length; start+=16384) binary += String.fromCharCode(...bytes.subarray(start,start+16384));
      references.push({mime:response.headers.get("content-type").split(";")[0], data:btoa(binary)});
    }
    const response = await chrome.tabs.sendMessage(item.tabId, {type:"web2api:run", task:{...task,references}});
    if (inflight?.id === task.id) inflight.preparing = false;
    await persist();
    if (!response?.accepted) outbox = {error:"page_busy"};
  } catch (error) {
    if (inflight) inflight.preparing = false;
    outbox = {error:safeCodes.has(error.message) ? error.message : "browser_interrupted"};
    await persist();
  }
}

async function tick() {
  if (polling) return;
  polling = true;
  try {
    if (!settings) await load();
    if (!settings?.token) return;
    // Extension API activity keeps the worker available; an alarm also recovers idle shutdown.
    const stored = await chrome.storage.session.get("managedTabs");
    tabs = stored.managedTabs || tabs;
    const states = await Promise.all(Object.values(tabs).map(probe));
    await api("heartbeat", {session_id:settings.sessionId, tabs:states});
    if (inflight && Date.now() > inflight.expires_at * 1000) outbox = {error:"upstream_timeout"};
    if (outbox) await deliver();
    if (!inflight) {
      const {task} = await api("claim", {session_id:settings.sessionId});
      if (task) await prepare(task);
    }
    await chrome.storage.local.set({lastStatus:{ok:true, at:Date.now(), tabs:states}});
  } catch (error) {
    await chrome.storage.local.set({lastStatus:{ok:false, at:Date.now(), message:error.message}});
  } finally {
    polling = false;
    clearTimeout(timer); timer = setTimeout(tick,2000);
  }
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (message.type === "web2api:result") {
    if (!inflight || sender.tab?.id !== inflight.tabId || message.id !== inflight.id) return;
    const result = message.result || {};
    outbox = result.error ? {error:safeCodes.has(result.error) ? result.error : "upstream_error"} : {text:result.text || "", images:result.images || []};
    deliver().then(() => respond({ok:true})).catch(() => respond({ok:false}));
    return true;
  }
  // Content scripts cannot reconfigure the bridge, open tabs or access its token.
  if (sender.tab || sender.id !== chrome.runtime.id) return;
  if (message.type === "configure") {
    (async () => {
      if (inflight) throw new Error("请等待当前任务结束后再修改配对");
      if (message.url !== "http://127.0.0.1:8765" || !/^br_[A-Za-z0-9_-]{30,}$/.test(message.token)) throw new Error("请填写工作台生成的配对码");
      settings = {url:message.url, token:message.token};
      await chrome.storage.local.set({settings}); await load();
      const providers = await api("providers"); tick(); return providers;
    })().then(data => respond({ok:true,...data})).catch(error => respond({ok:false,message:error.message}));
    return true;
  }
  if (message.type === "providers") {
    (async () => {await load(); return await api("providers");})().then(data => respond({ok:true,...data})).catch(() => respond({ok:false,message:"请先保存有效的配对码"}));
    return true;
  }
  if (message.type === "open-provider") {
    (async () => {
      if (inflight) throw new Error("请等待当前任务结束");
      const {data} = await api("providers");
      const adapter = data.find(item => item.id === message.id);
      if (!adapter) throw new Error("此平台未启用");
      const origin = new URL(adapter.url).origin + "/*";
      if (!await chrome.permissions.contains({origins:[origin]})) throw new Error("请先允许扩展访问此网站");
      const current = tabs[adapter.id];
      let tab;
      try {if (current) tab = await chrome.tabs.get(current.tabId);} catch { /* Closed managed tab. */ }
      if (tab) await chrome.tabs.update(tab.id, {active:true});
      else tab = await chrome.tabs.create({url:adapter.url, active:true});
      tabs[adapter.id] = {tabId:tab.id, adapter}; await persist(); tick();
    })().then(() => respond({ok:true})).catch(error => respond({ok:false,message:error.message}));
    return true;
  }
  if (message.type === "stop-provider") {
    (async () => {
      if (inflight?.provider === message.id) throw new Error("当前网页任务尚未结束");
      delete tabs[message.id]; await persist(); tick();
    })().then(() => respond({ok:true})).catch(error => respond({ok:false,message:error.message}));
    return true;
  }
});
chrome.alarms.onAlarm.addListener(() => tick());
chrome.runtime.onStartup.addListener(() => load().then(tick));
chrome.storage.local.setAccessLevel({accessLevel:"TRUSTED_CONTEXTS"}).catch(() => {});
chrome.alarms.create("web2api-poll", {periodInMinutes:0.5});
load().then(() => {
  if (inflight?.preparing) {inflight.preparing=false; outbox={error:"browser_interrupted"};}
  tick();
});
