"use strict";
const $ = id => document.getElementById(id);
const message = value => {$('message').textContent=value;};
async function list() {
  const result = await chrome.runtime.sendMessage({type:"providers"});
  if (!result?.ok) {message(result?.message || "请先配对"); return;}
  $('providers').replaceChildren();
  for (const adapter of result.data) {
    const row = document.createElement('div'); row.className='site';
    const name = document.createElement('strong'); name.textContent=adapter.name;
    const open = document.createElement('button'); open.textContent='连接网站';
    open.addEventListener('click', async () => {
      try {
        const granted = await chrome.permissions.request({origins:[new URL(adapter.url).origin+'/*']});
        if (!granted) {message('未获得此网站的访问权限'); return;}
        open.disabled=true;
        const response = await chrome.runtime.sendMessage({type:'open-provider',id:adapter.id});
        message(response?.ok ? '已打开专用标签；请完成登录，工作台会自动显示连接状态。' : response?.message || '连接失败');
      } catch {message('连接失败，请检查扩展和本机服务');}
      finally {open.disabled=false;}
    });
    const stop = document.createElement('button'); stop.className='secondary'; stop.textContent='断开';
    stop.addEventListener('click',async()=>{const response=await chrome.runtime.sendMessage({type:'stop-provider',id:adapter.id});message(response?.ok?'已停止接管此平台标签':response?.message||'操作失败');});
    row.append(name,open,stop);$('providers').append(row);
  }
}
$('pair-form').addEventListener('submit',async event=>{
  event.preventDefault();$('save').disabled=true;
  try {
    const response=await chrome.runtime.sendMessage({type:'configure',url:'http://127.0.0.1:8765',token:$('token').value.trim()});
    if (!response?.ok) throw new Error(response?.message||'保存失败');
    $('token').value='';message('配对成功。选择平台并打开专用标签。');await list();
  } catch(error){message(error.message);} finally{$('save').disabled=false;}
});
list();
