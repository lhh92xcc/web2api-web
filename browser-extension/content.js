/* Runs only inside a tab explicitly opened by the bridge extension. */
(() => {
  if (globalThis.__web2apiAdapter) return;
  const state = {task: null};
  globalThis.__web2apiAdapter = state;
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  const fail = code => { throw new Error(code); };
  const visible = element => element && element.getClientRects().length && getComputedStyle(element).visibility !== "hidden";
  const all = selector => selector ? [...document.querySelectorAll(selector)] : [];
  const first = selector => all(selector).find(visible);
  const text = element => (element?.innerText ?? element?.textContent ?? "").trim();
  const draftText = editor => {
    if ("value" in editor) return editor.value.trim();
    // A website can preselect an image tool using a non-editable system hint pill.
    const copy = editor.cloneNode(true);
    copy.querySelectorAll('[data-system-hint-type]').forEach(node => node.remove());
    return copy.textContent.trim();
  };
  function responseText(node, adapter) {
    if (!node.hasAttribute("data-content-search-turn-key")) return text(node);
    // Some websites wrap a user bubble and assistant answer in one turn.
    // Extract only answer content, preserving block breaks and excluding controls.
    const parts = [];
    function walk(element) {
      if (element.nodeType === Node.TEXT_NODE) {parts.push(element.textContent); return;}
      if (!(element instanceof Element)) return;
      if (element.matches('button,svg,script,style,[hidden],[aria-hidden="true"],[data-user-message-bubble],[data-message-author-role="user"]')) return;
      if (/^H[1-6]$/.test(element.tagName) && /^(你说|ChatGPT\s*(说|said)|You said)[:：]?$/i.test(text(element))) return;
      const block = /^(P|DIV|LI|PRE|BLOCKQUOTE|H[1-6]|BR)$/.test(element.tagName);
      if (block) parts.push("\n");
      element.childNodes.forEach(walk);
      if (block) parts.push("\n");
    }
    walk(node);
    return parts.join("").replace(/\n{3,}/g,"\n\n").trim();
  }
  const blocked = () => !!document.querySelector('#challenge-running, iframe[src*="challenges.cloudflare.com"], #cf-challenge-running') || /Just a moment|Verify you are human/i.test(document.title);

  function probe(adapter) {
    try {
      if (blocked()) return "login_required";
      if (first('[data-testid="login-button"], a[href*="/auth/login"]')) return "login_required";
      if (!first(adapter.prompt_selector)) return "page_changed";
      if (state.task || first(adapter.busy_selector)) return "busy";
      return "ready";
    } catch { return "page_changed"; }
  }

  function setPrompt(editor, value) {
    if (editor instanceof HTMLTextAreaElement || editor instanceof HTMLInputElement) {
      const proto = editor instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(editor, value);
    } else if (editor.isContentEditable) {
      editor.replaceChildren(...value.split("\n").map(line => {
        const p = document.createElement("p");
        if (line) p.textContent = line; else p.append(document.createElement("br"));
        return p;
      }));
    } else { fail("page_changed"); }
    editor.dispatchEvent(new InputEvent("input", {bubbles:true, inputType:"insertText", data:value}));
    editor.dispatchEvent(new Event("change", {bubbles:true}));
  }

  async function attach(adapter, references) {
    if (!references.length) return;
    const inputs = all(adapter.file_selector).filter(element => element instanceof HTMLInputElement && element.type === "file");
    const input = inputs.find(element => element.multiple) || inputs[0];
    if (!input || (references.length > 1 && !input.multiple)) fail("upload_failed");
    const transfer = new DataTransfer();
    for (const [i, reference] of references.entries()) {
      const bytes = Uint8Array.from(atob(reference.data), char => char.charCodeAt(0));
      transfer.items.add(new File([bytes], `reference-${i + 1}.${reference.mime === 'image/jpeg' ? 'jpg' : reference.mime === 'image/webp' ? 'webp' : 'png'}`, {type:reference.mime}));
    }
    input.files = transfer.files;
    input.dispatchEvent(new Event("change", {bubbles:true}));
    await delay(1500);
  }

  async function readableImage(image) {
    const src = image.currentSrc || image.src;
    if (!/^(https?:|blob:|data:image\/(png|jpeg|webp);base64,)/.test(src)) fail("download_failed");
    try {
      const response = await fetch(src, {credentials:new URL(src, location.href).origin === location.origin ? "include" : "omit"});
      if (!response.ok) fail("download_failed");
      const blob = await response.blob();
      if (blob.size > 20 * 1024 * 1024) fail("download_failed");
      return await new Promise((resolve,reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",")[1]);
        reader.onerror = () => reject(new Error("download_failed"));
        reader.readAsDataURL(blob);
      });
    } catch { fail("download_failed"); }
  }

  async function run(task) {
    const adapter = task.adapter;
    const status = probe(adapter);
    if (status === "login_required") fail(status);
    if (status === "page_changed") fail(status);
    if (status === "busy") fail("page_busy");
    const editor = first(adapter.prompt_selector);
    if (draftText(editor)) fail("page_busy");
    const baseline = new Set(all(adapter.response_selector));
    const oldImages = new Set(all(adapter.response_selector).flatMap(node => [...node.querySelectorAll(adapter.image_selector)]).map(node => node.currentSrc || node.src));
    await attach(adapter, task.references || []);
    setPrompt(editor, task.prompt);
    let send;
    const deadline = Math.min(task.expires_at * 1000, Date.now() + 30000);
    while (Date.now() < deadline) {
      send = first(adapter.send_selector);
      if (send && !send.disabled && send.getAttribute("aria-disabled") !== "true") break;
      await delay(400);
    }
    if (!send || send.disabled || send.getAttribute("aria-disabled") === "true") fail("upload_failed");
    // No retry of click or prompt: a lost acknowledgment must not consume quota twice.
    send.click();
    const started = Date.now();
    let fingerprint = "", changedAt = Date.now(), sawResponse = false;
    while (Date.now() < task.expires_at * 1000) {
      if (blocked()) fail("login_required");
      const responses = all(adapter.response_selector).filter(node => !baseline.has(node));
      const last = responses.at(-1);
      if (last) {
        sawResponse = true;
        if (all('[role="alert"]').some(alert => last.contains(alert) && visible(alert))) fail("upstream_error");
        const value = responseText(last, adapter);
        const images = [...last.querySelectorAll(adapter.image_selector)].filter(image =>
          image instanceof HTMLImageElement && !image.closest('[data-user-message-bubble],[data-message-author-role="user"]') && image.complete && image.naturalWidth >= 128 && image.naturalHeight >= 128 && !oldImages.has(image.currentSrc || image.src));
        const fingerprintNow = task.kind === "chat" ? value : images.map(image => image.currentSrc || image.src).join("|");
        if (fingerprintNow !== fingerprint) {fingerprint = fingerprintNow; changedAt = Date.now();}
        const stable = Date.now() - changedAt;
        const busy = first(adapter.busy_selector);
        if (task.kind === "chat" && value && !busy && stable > 2500) return {text:value};
        if (task.kind === "image" && images.length && !busy && stable > 3500) {
          // One image per submission keeps extension messages bounded; the local queue handles n.
          return {images:[await readableImage(images[0])]};
        }
        if (task.kind === "image" && value && !images.length && !busy && Date.now() - started > 30000 && stable > 15000) fail("no_generated_image");
      }
      if (!sawResponse && Date.now() - started > 45000 && !first(adapter.busy_selector)) fail("send_failed");
      await delay(700);
    }
    fail("upstream_timeout");
  }

  chrome.runtime.onMessage.addListener((message, sender, respond) => {
    if (message.type === "web2api:probe") {
      respond({status:probe(message.adapter), task:state.task});
    }
    if (message.type === "web2api:run") {
      if (state.task) {respond({accepted:false}); return;}
      // probe must run before marking the adapter busy.
      const execution = run(message.task);
      state.task = message.task.id;
      respond({accepted:true});
      execution.then(result => chrome.runtime.sendMessage({type:"web2api:result", id:message.task.id, result}))
        .catch(error => chrome.runtime.sendMessage({type:"web2api:result", id:message.task.id, result:{error:error.message}}))
        .finally(() => {state.task = null;});
    }
  });
})();
