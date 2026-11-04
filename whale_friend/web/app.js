"use strict";
const $ = id => document.getElementById(id);
const node = (tag, className = "", text = "") => { const n = document.createElement(tag); n.className = className; n.textContent = text; return n; };
const storage = {get(key) {try {return JSON.parse(sessionStorage.getItem(key));} catch {return null;}}, set(key, value) {try {sessionStorage.setItem(key, JSON.stringify(value));} catch { /* Private-mode storage can be unavailable. */ }}};
let session = storage.get("session") || "", tab = "chat", state = null, paired = false;
let avatarEvent = "", avatarSequence = 0, avatarFront = 0;
let avatarPaused = storage.get('avatar-paused') ?? window.matchMedia('(prefers-reduced-motion: reduce)').matches;
function avatarVideo(index) {return $(index ? 'avatar-video-b' : 'avatar-video-a');}
function playAvatar(snapshot) {
  if (!snapshot?.enabled || avatarPaused || document.hidden || tab !== 'chat') return;
  const sequence = ++avatarSequence, index = 1-avatarFront, video = avatarVideo(index);
  video.pause(); video.loop = !!snapshot.loop; video.muted = true;
  video.onloadeddata = () => {
    if (sequence !== avatarSequence || avatarPaused || document.hidden || tab !== 'chat') return;
    avatarVideo(avatarFront).pause(); avatarVideo(avatarFront).hidden = true;
    avatarFront = index; video.hidden = false; $('avatar-fallback').hidden = true;
    $('avatar-caption').textContent = '本鱼现在 · ' + snapshot.mood;
    video.play().catch(() => {$('avatar-fallback').hidden = false; video.hidden = true;});
  };
  video.onerror = () => {if (sequence===avatarSequence) {$('avatar-fallback').hidden = false; video.hidden = true;}};
  video.onended = () => {if (sequence===avatarSequence) playAvatar(state?.animation_idle);};
  video.src = snapshot.url; video.load();
}
function syncAvatar() {
  $('toggle-avatar').textContent = avatarPaused ? '播放动画' : '暂停动画';
  $('toggle-avatar').setAttribute('aria-label',avatarPaused ? '播放角色动画' : '暂停角色动画');
  if (!paired || !state?.animation?.enabled || avatarPaused || document.hidden || tab !== 'chat') {
    ++avatarSequence; avatarVideo(0).pause(); avatarVideo(1).pause(); avatarEvent = '';
    if (!state?.animation?.enabled) {$('avatar-fallback').hidden = false; avatarVideo(0).hidden=true; avatarVideo(1).hidden=true;}
    return;
  }
  const event = session + ':' + state.animation.event;
  if (event !== avatarEvent) {avatarEvent = event; playAvatar(state.animation);}
}
$('toggle-avatar').onclick = () => {avatarPaused = !avatarPaused; storage.set('avatar-paused',avatarPaused); syncAvatar();};
document.addEventListener('visibilitychange',syncAvatar);
let pending = [], sendAttempt = storage.get("sendAttempt"), sending = false, uploading = false;
let rendered = "", older = [], memoryRows = [], imageRows = [], editingMemory = null, editingImage = null;
let memoryHub = null, editingTopic = null;
let toastTimer, lastDataRefresh = 0, lastAssistant = 0, fetchInProgress = false, uploadToChat = true;
function toast(message) {$("toast").textContent = message; $("toast").hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $("toast").hidden = true, 4500);}
function notice(message = "") {$("notice").textContent = message; $("notice").hidden = !message;}
function pairedView(value) {paired = value; $("pair-screen").hidden = value; $("app").hidden = !value; if (!value) {syncAvatar(); document.querySelectorAll("dialog[open]").forEach(d => d.close());}}
async function api(path, options = {}) {
  const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 25000);
  try {
    const response = await fetch(path, {...options, credentials: "same-origin", cache: "no-store", signal: controller.signal});
    const data = await response.json();
    if (!response.ok) {if (response.status === 401 && path !== "/api/pair") pairedView(false); throw new Error(typeof data.detail === "string" ? data.detail : "操作未完成，请稍后重试");}
    return data;
  } catch (error) {
    if (error.name === "AbortError" || error instanceof TypeError) throw new Error("暂时连接不上电脑。请检查同一 Wi-Fi 和电脑小屋是否仍在运行；草稿会保留。");
    throw error;
  } finally {clearTimeout(timer);}
}
const post = (path, data = {}, method = "POST") => api(path, {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)});
function stashDraft() {storage.set("draft:" + session, {text: $("message").value, pending});}
function restoreDraft() {const draft = storage.get("draft:" + session); $("message").value = draft?.text || ""; pending = draft?.pending || []; renderPending();}
function renderPending() {
  $("pending-images").replaceChildren();
  for (const asset of pending) {const chip = node("div", "chip"); chip.append(node("span", "", asset.title)); const remove = node("button", "", "×"); remove.type = "button"; remove.setAttribute("aria-label", "移除附件 " + asset.title); remove.onclick = () => {pending = pending.filter(a => a.id !== asset.id); renderPending(); stashDraft();}; chip.append(remove); $("pending-images").append(chip);}
}
function switchTab(value) {
  tab = value; document.querySelectorAll("[data-tab]").forEach(b => b.classList.toggle("selected", b.dataset.tab === tab));
  for (const name of ["chat", "memories", "journals", "images"]) $("panel-" + name).hidden = name !== tab;
  if (tab === "chat") $("unread").hidden = true;
  syncAvatar();
  loadCollection().catch(error => toast(error.message));
}
function renderChat(data) {
  const signature = JSON.stringify([data.session, data.messages, older, data.busy, data.active_session, data.last_error]);
  if (signature === rendered) return;
  rendered = signature;
  const list = $("timeline"), previousTop = list.scrollTop, atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 110;
  list.replaceChildren();
  if (older.length || data.has_older) {const load = node("button", "text-button wide", "查看更早的来信"); load.onclick = async () => {try {const first = older[0]?.id || data.messages[0]?.id; const page = await api("/api/state?session=" + session + "&before=" + first); if (!page.messages.length) {toast("已经是第一封来信啦"); return;} older = [...page.messages, ...older]; rendered = ""; renderChat(state);} catch (e) {toast(e.message);}}; list.append(load);}
  const all = [...new Map([...older, ...data.messages].map(m => [m.id, m])).values()].sort((a,b) => a.id - b.id);
  if (!all.length) {const welcome = node("div", "welcome"); welcome.append(node("div", "wave", "～"), node("h2", "", "本鱼也装进口袋啦。"), node("p", "", "电脑里说过的话，本鱼都带着呢。\n聊聊今天的小事，或继续上次的话题？")); list.append(welcome);}
  let day = "";
  for (const message of all) {
    if (message.day !== day) {day = message.day; list.append(node("div", "day-label", day));}
    const item = node("article", "message " + message.role);
    item.append(node("div", "message-meta", `${message.role === "user" ? data.user : data.character}${message.proactive ? " · 主动来信" : ""}  ${message.created_at.slice(11,16)}`));
    if (message.content) item.append(node("div", "bubble", message.content));
    if (message.memory_action?.status === "saved") {
      const action = message.memory_action;
      item.append(node("div", "message-status", "已写入记忆收藏夹" + (action.memory_id ? " · #" + action.memory_id : "")));
    }
    for (const asset of message.attachments) {const button = node("button", "image-button"); const img = node("img", "chat-image"); img.src = "/api/images/" + asset.id; img.alt = asset.title; img.loading = "lazy"; button.append(img, node("div", "image-caption", asset.title + " · 查看")); button.onclick = () => openImage(asset); item.append(button);}
    if (message.status !== "complete") item.append(node("span", "message-status", {pending: "本鱼正在写回信…", interrupted: "已停止，可重试", failed: "回复未完成，可重试", erased: "已删除"}[message.status] || "未完成"));
    list.append(item);
  }
  if (data.busy && data.active_session === session) list.append(node("div", "typing", "小尾巴晃呀晃，本鱼正在想…"));
  if (atBottom || !all.length) list.scrollTop = list.scrollHeight; else list.scrollTop = previousTop;
  const incoming = data.messages.filter(m => m.role === "assistant" && m.status === "complete").at(-1)?.id || 0;
  if (lastAssistant && incoming > lastAssistant && tab !== "chat") $("unread").hidden = false;
  lastAssistant = Math.max(lastAssistant, incoming);
}
async function refresh() {
  if (fetchInProgress) return; fetchInProgress = true;
  try {
    let data;
    try {data = await api("/api/state" + (session ? "?session=" + encodeURIComponent(session) : ""));}
    catch (error) {if (error.message.includes("会话不存在")) {session = ""; storage.set("session", ""); return;} throw error;}
    if (!data.session) {session = (await post("/api/sessions")).id; storage.set("session", session); data = await api("/api/state?session=" + session); restoreDraft();}
    if (session !== data.session) {session = data.session; storage.set("session", session); restoreDraft();}
    state = data; pairedView(true); syncAvatar(); $("character-name").textContent = data.character + "的口袋小屋";
    $("image-vision-note").textContent = (data.vision_enabled ? "已开启直接识图，聊天附图不需要先填写说明。" : data.vision_mode === "off" ? "当前仅发送图片说明，可在电脑设置中开启识图。" : data.vision_status || "收到图片后会自动检测视觉输入能力。") + " 表情包填写用途并允许复用后，本鱼可以选来回应你。";
    $("presence").textContent = "本鱼现在 · " + data.mood; $("online").textContent = "● 已连接 · 同一份记忆";
    $("backup").textContent = data.backup_at ? "备份 " + data.backup_at.slice(11,16) : "本机保存";
    $("send").disabled = data.busy || sending || uploading; $("stop").hidden = !data.busy || data.active_session !== session;
    $("retry").disabled = data.busy; notice(data.busy && data.active_session !== session ? "本鱼正在另一个会话回信，请稍等一下。" : data.last_error && !data.busy && data.active_session === session ? data.last_error : ""); renderChat(data);
    if (tab !== "chat" && Date.now() - lastDataRefresh > 5000) await loadCollection();
  } catch (error) {
    if (paired) {$("online").textContent = "○ 等待电脑重新连接"; notice(error.message);}
  } finally {fetchInProgress = false;}
}
function memoryEditor(row = null) {
  editingMemory = row; $("memory-kind").value = row?.kind || "事实"; $("memory-content").value = row?.content || "";
  $("memory-pin").checked = !!row?.pinned; $("memory-active").checked = row?.active !== 0;
  $('memory-importance').value=row?.importance_manual ? String(row.importance) : '';
  $("memory-topic").replaceChildren(new Option("自动分类", ""));
  for (const topic of memoryHub?.topics || []) $("memory-topic").append(new Option(topic.title, topic.id));
  $("memory-topic").value = row?.topics_override?.[0] || ""; $("memory-versions").hidden = !row;
  $("memory-source").textContent = row ? "来源消息编号：" + row.source_ids + "。修正会同步到电脑，并保留旧版供核对。" : "手动记录你希望本鱼长期记住的事情。";
  $("memory-dialog").showModal();
}
function memoryDetail(title, text) {$("memory-detail-title").textContent = title; $("memory-detail-content").textContent = text; $("memory-detail-dialog").showModal();}
function topicEditor(row = null) {editingTopic = row; $("topic-title").value = row?.title || ""; $("topic-aliases").value = row?.aliases.join("，") || ""; $("topic-dialog").showModal();}
function renderMemories() {
  const list = $("memory-list"), view = $("memory-view").value; list.replaceChildren(); $("topic-tools").hidden = view !== "topics";
  const names = new Map(memoryHub.topics.map(t => [t.id, t.title]));
  const levels=['随口 / 玩笑','普通','重要','长期关键'];
  if (view==='changes') {
    const labels={pending:'待核对',applied:'已更新',rejected:'不视为替代',undone:'已恢复旧状态'};
    for (const change of memoryHub.changes || []) {
      const card=node('article','card');
      card.append(node('div','card-meta',labels[change.status] || change.status),node('p','',`以前 #${change.previous_id}：${change.previous_content}\n后来 #${change.newer_id}：${change.newer_content}`),node('p','section-note',`整理说明：${change.explanation}\n依据消息：${change.evidence_ids.map(id=>'#'+id).join('、')} · ${change.created_at}`));
      const evidence=node('button','soft-button','查看变化依据');
      evidence.onclick=()=>memoryDetail('状态变化 · 用户原文依据',(change.evidence || []).map(row=>`消息#${row.id} · ${row.created_at}\n${row.content}`).join('\n\n') || '未找到当前有效来源，请先核对。');
      card.append(evidence);
      for (const [decision,label] of change.status==='pending' ? [['accept','确认更新'],['reject','不视为替代']] : change.status==='applied' ? [['undo','恢复旧状态']] : []) {
        const button=node('button','soft-button',label);
        button.onclick=async()=>{try {await post('/api/memory/changes/'+change.id,{decision}); await loadCollection(); toast('状态已同步到电脑。');} catch(e) {toast(e.message);}};
        card.append(button);
      }
      list.append(card);
    }
    if (!(memoryHub.changes || []).length) list.append(node('p','empty','还没有状态变化候选；明确的变化会在后台归纳时检查，原记忆保留。'));
    return;
  }
  if (view === "activation") {
    const trace = memoryHub.activation;
    if (!trace) {list.append(node("p", "empty", "这段会话还没有加载记录，聊一条消息后再来看看。")); return;}
    const card = node("article", "card"); card.append(node("div", "card-meta", trace.created_at), node("p", "", "核心编号：" + trace.core_ids.join("、") + "\n自动激活：" + trace.topic_ids.map(id => names.get(id) || id).join("、") + "\n实际放入上下文的记忆：" + trace.memory_ids.map(id => "#" + id).join("、")), node("p", "section-note", "这是实际加载记录。"+(trace.recall_reason ? '\n语义关联线索（不是当前原因）：'+trace.recall_reason : '')+"\n下方显示这些编号目前的内容。")); list.append(card);
  }
  if (view === "topics") {
    if (memoryHub.organizing_error) list.append(node("p", "section-note", memoryHub.organizing_error));
    for (const topic of memoryHub.topics) {
      const card = node("article", "card"), meta = node("div", "card-meta"), edit = node("button", "", "编辑别名"); edit.onclick = () => topicEditor(topic);
      meta.append(node("span", "", topic.title + " · " + topic.count + " 条"), edit);
      const refs = node("button", "", "查看原始记忆 " + topic.memory_ids.map(id => "#" + id).join("、"));
      refs.onclick = () => memoryDetail(topic.title + " · 原始记忆", memoryRows.filter(r => topic.memory_ids.includes(r.id)).map(r => `#${r.id} [${r.kind}] ${r.content}`).join("\n\n") || "暂无关联记忆");
      refs.disabled = !topic.count;
      card.append(meta, node("p", "", topic.summary || (topic.count ? "摘要待归纳，聊天时仍能自动读取原始记忆。" : "还没有关联记忆，可补充别名或在记忆条目中选择这个主题。")), node("p", "section-note", topic.count ? (topic.dirty ? "待继续归纳 · " : "已归纳 · ") + "摘要来源：" + topic.source_ids.map(id => "#" + id).join("、") : "暂无来源"), refs); list.append(card);
    } return;
  }
  let rows = memoryRows;
  if (view === "core") rows = memoryHub.core;
  if (view === "history") rows = rows.filter(r => !r.active);
  if (view === 'important') rows=rows.filter(r=>r.active && r.importance>=2).sort((a,b)=>b.importance-a.importance);
  if (view === "activation") rows = rows.filter(r => memoryHub.activation.memory_ids.includes(r.id));
  for (const brief of rows) {const row = memoryRows.find(r => r.id === brief.id) || brief; const card = node("article", "card"), meta = node("div", "card-meta"), edit = node("button", "", "修正"); edit.onclick = () => memoryEditor(row); meta.append(node("span", "", `${row.pinned ? "★ 固定 · " : !row.active ? "历史 · " : ""}#${row.id} ${row.kind} · ${levels[row.importance ?? 1]}`), edit); card.append(meta, node("p", "", row.content), node("p", "section-note", row.topics.map(id => names.get(id)).filter(Boolean).join(" · ")+(row.pending_change ? '\n状态变化待核对' : '')+(row.recall_cues?.length ? '\n联想线索：'+row.recall_cues.join('、') : ''))); list.append(card);}
  if (!rows.length) list.append(node("p", "empty", "这里会慢慢装满我们熟悉的小事。"));
}
function openImage(row) {editingImage = row; $("image-preview").src = "/api/images/" + row.id; $("image-title").value = row.title; $("image-description").value = row.description; $("image-tags").value = row.tags.join("，"); $("image-reuse").checked = !!row.allow_reuse; $("image-form").hidden = !!row.archived; $("image-dialog").showModal();}
async function loadCollection() {
  if (!paired || tab === "chat") return; lastDataRefresh = Date.now();
  if (tab === "memories") {
    memoryHub = await api("/api/memory/overview?session=" + encodeURIComponent(session)); memoryRows = memoryHub.records; renderMemories();
  } else if (tab === "journals") {
    const rows = await api("/api/journals?query=" + encodeURIComponent($("journal-query").value)); $("journal-list").replaceChildren();
    for (const row of rows) {const card = node("article", "card"); card.append(node("div", "card-meta", row.day + " · 第 " + row.part + " 段"), node("h3", "", row.title), node("pre", "", row.content)); $("journal-list").append(card);}
    if (!rows.length) $("journal-list").append(node("p", "empty", "还没有相关日志。对话会在电脑空闲后整理。"));
  } else if (tab === "images") {
    imageRows = await api("/api/images?query=" + encodeURIComponent($("image-query").value)); $("image-list").replaceChildren();
    for (const row of imageRows) {const card = node("button", "image-card"), img = node("img"); img.src = "/api/images/" + row.id; img.alt = row.title; img.loading = "lazy"; card.append(img, node("strong", "", row.title), node("small", "", row.allow_reuse ? "本鱼可以使用" : "仅收藏")); card.onclick = () => openImage(row); $("image-list").append(card);}
    if (!imageRows.length) $("image-list").append(node("p", "empty", "还没有收藏，发一张小表情给本鱼吧。"));
  }
}
$("pair-form").onsubmit = async event => {event.preventDefault(); const button = event.submitter; button.disabled = true; try {await post("/api/pair", {code: $("pair-code").value}); $("pair-code").value = ""; $("pair-error").textContent = ""; await refresh();} catch (e) {$("pair-error").textContent = e.message;} finally {button.disabled = false;}};
$("chat-form").onsubmit = async event => {
  event.preventDefault(); if (sending || uploading || state?.busy || !session) return;
  const text = $("message").value.trim(), images = pending.map(row => row.id); if (!text && !images.length) return;
  if (!sendAttempt || sendAttempt.session !== session || sendAttempt.text !== text || JSON.stringify(sendAttempt.images) !== JSON.stringify(images)) sendAttempt = {session, text, images, request_id: crypto.randomUUID ? crypto.randomUUID() : "10000000-1000-4000-8000-100000000000".replace(/[018]/g, c => (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16))};
  storage.set("sendAttempt", sendAttempt); sending = true; $("send").disabled = true;
  try {await post("/api/send", sendAttempt); $("message").value = ""; pending = []; sendAttempt = null; storage.set("sendAttempt", null); stashDraft(); renderPending(); await refresh(); const list = $("timeline"); list.scrollTop = list.scrollHeight;}
  catch (e) {toast(e.message);} finally {sending = false; $("send").disabled = !!state?.busy || uploading;}
};
$("message").oninput = () => {stashDraft(); if (paired) post("/api/activity", {composing: !!$("message").value.trim() || !!pending.length}).catch(() => {});};
$("retry").onclick = async () => {try {await post("/api/reply/retry", {session}); await refresh();} catch(e) {toast(e.message);}};
$("stop").onclick = async () => {try {await post("/api/reply/stop", {session});} catch(e) {toast(e.message);}};
$("choose-file").onclick = () => {uploadToChat = true; $("image-file").click();};
$("import-file").onclick = () => {uploadToChat = false; $("image-file").click();};
$("image-file").onchange = async () => {
  const files = [...$("image-file").files]; $("image-file").value = "";
  if (uploadToChat && files.length + pending.length > 4) {toast("每条消息最多 4 张图片，更多图片可先收藏到图库。"); return;}
  uploading = true; $("send").disabled = true;
  try {for (const file of files) {if (file.size > 20 * 1024 * 1024) throw new Error("单张图片不能超过 20 MB"); const row = await api("/api/images", {method:"POST", headers:{"X-File-Name":encodeURIComponent(file.name)}, body:file}); if (uploadToChat && !pending.some(a => a.id === row.id)) pending.push(row);} renderPending(); stashDraft(); if (!uploadToChat) {await loadCollection(); toast("图片收藏好啦，点开可以填写说明。");}}
  catch(e) {renderPending(); stashDraft(); toast(e.message);} finally {uploading = false; $("send").disabled = !!state?.busy;}
};
$("open-menu").onclick = () => {$("session-list").replaceChildren(); for (const row of state?.sessions || []) {const button = node("button", "card", (row.id === session ? "◉ " : "") + row.title); button.onclick = async () => {stashDraft(); session = row.id; older = []; rendered = ""; lastAssistant = 0; storage.set("session", session); restoreDraft(); $("menu-dialog").close(); switchTab("chat"); await refresh();}; $("session-list").append(button);} $("menu-dialog").showModal();};
$("new-session").onclick = async () => {try {stashDraft(); session = (await post("/api/sessions")).id; storage.set("session", session); older = []; rendered = ""; restoreDraft(); $("menu-dialog").close(); switchTab("chat"); await refresh();} catch(e) {toast(e.message);}};
$("logout").onclick = async () => {try {await post("/api/logout"); pairedView(false);} catch(e) {toast(e.message);}};
$("add-memory").onclick = () => memoryEditor();
$("memory-active").onchange = () => {if (!$("memory-active").checked) $("memory-pin").checked = false;};
$("memory-form").onsubmit = async event => {event.preventDefault(); try {const topic = $("memory-topic").value; const previous = editingMemory?.topics_override; const topics = topic && previous?.includes(topic) ? previous : topic ? [topic] : null; await post("/api/memories" + (editingMemory ? "/" + editingMemory.id : ""), {kind:$("memory-kind").value,content:$("memory-content").value,pinned:$("memory-pin").checked,active:$("memory-active").checked,topics_override:topics,importance:$('memory-importance').value==='' ? null : Number($('memory-importance').value)}, editingMemory ? "PATCH" : "POST"); $("memory-dialog").close(); await loadCollection(); toast("已经和电脑一起记住了。");} catch(e) {toast(e.message);}};
$("memory-view").onchange = () => {if (memoryHub) renderMemories();};
$("add-topic").onclick = () => topicEditor();
$("topic-form").onsubmit = async event => {event.preventDefault(); try {await post("/api/memory/topics" + (editingTopic ? "/" + editingTopic.id : ""), {title:$("topic-title").value,aliases:$("topic-aliases").value}, editingTopic ? "PATCH" : "POST"); $("topic-dialog").close(); await loadCollection(); toast("主题已同步，新的相关说法会自动生效。");} catch(e) {toast(e.message);}};
$("organize-memory").onclick = async () => {try {await post("/api/memory/organize"); toast("本鱼开始后台归纳啦，完成后这里会自动更新。");} catch(e) {toast(e.message);}};
$("memory-versions").onclick = async () => {try {const rows = await api("/api/memory/versions/" + editingMemory.id); memoryDetail("记忆修订历史", rows.map(r => `${r.valid_from} → ${r.valid_to}\n[${r.kind}] ${r.content}\n来源消息：${r.source_ids}`).join("\n\n") || "这条记忆还没有修订历史。");} catch(e) {toast(e.message);}};
$("image-form").onsubmit = async event => {event.preventDefault(); try {await post("/api/images/" + editingImage.id, {title:$("image-title").value,description:$("image-description").value,tags:$("image-tags").value,allow_reuse:$("image-reuse").checked}, "PATCH"); $("image-dialog").close(); await loadCollection(); toast("图片说明已同步。");} catch(e) {toast(e.message);}};
$("attach-image").onclick = () => {if (pending.length >= 4) {toast("一条消息最多 4 张图片。"); return;} if (!pending.some(a => a.id === editingImage.id)) pending.push(editingImage); renderPending(); stashDraft(); $("image-dialog").close(); switchTab("chat"); $("message").focus();};
$("journal-search").onsubmit = event => {event.preventDefault(); loadCollection().catch(e => toast(e.message));};
let searchTimer; $("image-query").oninput = () => {clearTimeout(searchTimer); searchTimer = setTimeout(() => loadCollection().catch(e => toast(e.message)), 300);};
document.querySelectorAll("[data-tab]").forEach(button => button.onclick = () => switchTab(button.dataset.tab));
document.querySelectorAll(".close-dialog").forEach(button => button.onclick = () => button.closest("dialog").close());
document.addEventListener("visibilitychange", () => {if (!document.hidden) refresh();});
restoreDraft(); refresh();
setInterval(() => {if (paired && !document.hidden) refresh();}, 1200);
setInterval(() => {if (paired && !document.hidden && ($("message").value.trim() || pending.length)) post("/api/activity", {composing:true}).catch(() => {});}, 15000);
