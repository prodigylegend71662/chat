/* C.S.P. Chat client: one small vanilla-JS file handles Socket.IO, UI state,
   rooms, DMs, reactions, moderation actions, calendar, settings and notifications. */
(() => {
  "use strict";
  const app = document.getElementById("app");
  if (!app) return;

  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const meId = Number(app.dataset.meId);
  const meName = app.dataset.meName;
  const $ = (id) => document.getElementById(id);
  const messagesEl = $("messages");
  const input = $("message-input");
  const connection = $("connection-status");
  const modalBackdrop = $("modal-backdrop");
  const modal = $("modal");
  let currentRoom = null;
  let currentRoomName = "general";
  let oldestId = null;
  let hasMore = false;
  let replyTo = null;
  let unread = 0;
  let focused = document.hasFocus();
  let socket = null;
  let lastTypingSent = 0;
  let typingClearTimer = null;
  const unreadRooms = new Map();
  let people = Array.from(document.querySelectorAll("#user-list .user-item"));

  // A tiny self-contained WAV avoids another network asset for notification audio.
  const BEEP = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA=";
  function beep() { try { const a = new Audio(BEEP); a.volume = 0.12; a.play().catch(() => {}); } catch (_) {} }

  function toast(message, error = false) {
    const el = document.createElement("div"); el.className = "toast";
    if (error) el.style.borderColor = "rgba(239,68,68,.5)";
    el.textContent = message; $("toast-region").appendChild(el);
    setTimeout(() => el.remove(), 3500);
  }

  function escapeHTML(text) {
    const d = document.createElement("div"); d.textContent = text || ""; return d.innerHTML;
  }

  // Safe lightweight formatting: only transform already-escaped text.
  function safeFormat(text) {
    let s = escapeHTML(text);
    s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
    return s;
  }

  function nearBottom() {
    return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 90;
  }

  function scrollBottom() { messagesEl.scrollTop = messagesEl.scrollHeight; }

  function formatTime(iso) {
    return new Date(iso).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", hour12: false});
  }

  function messageNode(msg) {
    const row = document.createElement("article");
    row.className = `message-row ${msg.user_id === meId ? "mine" : ""} ${msg.role === "bot" ? "bot" : ""}`;
    row.dataset.id = msg.id;
    const bubble = document.createElement("div"); bubble.className = "message-bubble";
    const meta = document.createElement("div"); meta.className = "meta";
    const avatar = document.createElement("span"); avatar.textContent = msg.avatar;
    const name = document.createElement("strong"); name.textContent = msg.display_name || msg.username;
    const time = document.createElement("span"); time.textContent = formatTime(msg.created_at);
    meta.append(avatar, name, time);
    if (msg.role === "admin") { const badge = document.createElement("span"); badge.textContent = "ADMIN"; meta.append(badge); }
    bubble.appendChild(meta);
    if (msg.reply) {
      const q = document.createElement("div"); q.className = "reply-quote"; q.dataset.jump = msg.reply.id;
      q.textContent = `↪ ${msg.reply.username}: ${msg.reply.text}`; bubble.appendChild(q);
    }
    const txt = document.createElement("div"); txt.className = "text";
    if (msg.deleted) txt.textContent = "[message deleted]"; else txt.innerHTML = safeFormat(msg.text);
    bubble.appendChild(txt);
    if (msg.edited && !msg.deleted) { const e = document.createElement("small"); e.textContent = " (edited)"; e.style.opacity = ".7"; txt.appendChild(e); }
    const actions = document.createElement("div"); actions.className = "actions";
    addAction(actions, "↩", "reply", msg.id);
    addAction(actions, "😊", "react", msg.id);
    if (msg.user_id === meId && !msg.deleted) addAction(actions, "✎", "edit", msg.id);
    if ((msg.user_id === meId || window.cspRole === "admin") && !msg.deleted) addAction(actions, "×", "delete", msg.id);
    if (window.cspRole === "admin") addAction(actions, msg.pinned ? "📌" : "📍", "pin", msg.id);
    bubble.appendChild(actions);
    const reacts = document.createElement("div"); reacts.className = "reactions";
    Object.entries(msg.reactions || {}).forEach(([emoji, data]) => {
      const b = document.createElement("button"); b.className = "reaction"; b.dataset.action = "reaction"; b.dataset.messageId = msg.id; b.dataset.emoji = emoji; b.title = (data.users || []).join(", "); b.textContent = `${emoji} ${data.count}`; reacts.appendChild(b);
    });
    if (Object.keys(msg.reactions || {}).length) bubble.appendChild(reacts);
    row.appendChild(document.createElement("span")); row.appendChild(bubble);
    return row;
  }

  function addAction(parent, label, action, id) { const b = document.createElement("button"); b.className = "action-btn"; b.textContent = label; b.dataset.action = action; b.dataset.messageId = id; b.title = action; parent.appendChild(b); }

  function renderMessages(list, append = false) {
    const shouldScroll = nearBottom();
    if (!append) messagesEl.replaceChildren();
    list.forEach(m => messagesEl.appendChild(messageNode(m)));
    oldestId = list.length ? list[0].id : oldestId;
    if (!append || shouldScroll) scrollBottom();
    document.querySelectorAll(".reaction").forEach(() => {});
  }

  async function loadMessages(before = null) {
    const url = new URL("/api/messages", location.origin);
    url.searchParams.set("room", currentRoom);
    if (before) url.searchParams.set("before_id", before);
    const res = await fetch(url, {credentials: "same-origin"});
    const data = await res.json(); if (!res.ok) { toast(data.error || "Could not load messages.", true); return; }
    hasMore = data.has_more; $("load-more").classList.toggle("hidden", !hasMore);
    renderMessages(data.messages, Boolean(before));
    renderPinned(data.messages);
  }

  function renderPinned(list) {
    const pins = list.filter(m => m.pinned && !m.deleted).slice(-3);
    const bar = $("pinned-bar"); bar.replaceChildren();
    if (!pins.length) return bar.classList.add("hidden");
    bar.classList.remove("hidden"); const title = document.createElement("strong"); title.textContent = "Pinned: "; bar.appendChild(title);
    pins.forEach(p => { const b = document.createElement("button"); b.className = "reaction"; b.textContent = `${p.username}: ${p.text.slice(0, 70)}`; b.onclick = () => jumpTo(p.id); bar.appendChild(b); });
  }

  function jumpTo(id) { const el = document.querySelector(`[data-id="${CSS.escape(String(id))}"]`); if (el) { el.scrollIntoView({behavior:"smooth",block:"center"}); el.style.outline = "2px solid #f59e0b"; setTimeout(() => el.style.outline = "", 2000); } }

  async function selectRoom(room, name) {
    if (!room || room === currentRoom) return;
    if (currentRoom) socket?.emit("leave_room", {room: currentRoom, csrf_token: csrf});
    currentRoom = room; currentRoomName = name; oldestId = null; replyTo = null; $("reply-preview").classList.add("hidden");
    $("chat-title").textContent = name; $("typing-indicator").textContent = ""; $("message-search").value = ""; $("search-results").classList.add("hidden");
    document.querySelectorAll(".room-item,.user-item").forEach(e => e.classList.remove("active"));
    document.querySelector(`[data-room="${CSS.escape(room)}"]`)?.classList.add("active");
    socket?.emit("join_room", {room, csrf_token: csrf});
    await loadMessages();
    unreadRooms.delete(room);
  }

  function buildSidebar() {
    const roomList = $("room-list"); roomList.replaceChildren();
    const defaults = window.cspRooms || [];
    defaults.forEach(r => {
      const b = document.createElement("button"); b.className = "room-item"; b.dataset.room = `room:${r.id}`; b.dataset.roomName = r.name; b.textContent = `# ${r.name}`;
      b.onclick = () => selectRoom(`room:${r.id}`, r.name); roomList.appendChild(b);
      if (r.created_by === meId && !r.default) { const d = document.createElement("span"); d.textContent = "×"; d.title = "Delete room"; d.style.marginLeft="auto"; d.onclick = e => {e.stopPropagation(); deleteRoom(r.id);}; b.appendChild(d); }
    });
    buildUsers(window.cspUsers || []);
  }

  function buildUsers(users) {
    people = []; const list = $("user-list"); list.replaceChildren();
    users.filter(u => u.id !== meId).forEach(u => {
      const b = document.createElement("button"); b.className = "user-item"; b.dataset.userId = u.id;
      const dot = document.createElement("span"); dot.className = `status-dot ${u.status}`; const av = document.createElement("span"); av.textContent=u.avatar; const n=document.createElement("span"); n.textContent=u.display_name || u.username;
      b.append(dot,av,n); b.onclick = async () => { const r = await fetch(`/api/dm/${u.id}`); const d=await r.json(); if(!r.ok)return toast(d.error,true); await selectRoom(d.room, `@${u.display_name || u.username}`); };
      list.appendChild(b); people.push(b);
    });
  }

  function openModal(html) { modal.innerHTML = html; modalBackdrop.classList.remove("hidden"); }
  function closeModal() { modalBackdrop.classList.add("hidden"); modal.replaceChildren(); }
  modalBackdrop.addEventListener("click", e => { if(e.target === modalBackdrop) closeModal(); });

  function openSettings() {
    const avatars = window.cspAvatars || []; const currentAvatar = window.cspMe.avatar;
    openModal(`<h3>Settings</h3><form id="settings-form"><label>Display name<input class="field" name="display_name" maxlength="30" value="${escapeHTML(window.cspMe.display_name)}"></label><label>Theme<select class="field" name="theme"><option value="dark">Dark</option><option value="light">Light</option><option value="system">System</option></select></label><div class="avatar-grid">${avatars.map(a=>`<label class="avatar-option"><input type="radio" name="avatar" value="${escapeHTML(a)}" ${a===currentAvatar?'checked':''}><span>${escapeHTML(a)}</span></label>`).join("")}</div><label><input type="checkbox" name="notify_mentions" checked> Notify mentions</label><label><input type="checkbox" name="notify_dms" checked> Notify DMs</label><label><input type="checkbox" name="notify_room" checked> Notify room messages</label><label><input type="checkbox" name="profanity_filter" checked> Profanity filter</label><div class="modal-actions"><button type="button" class="icon-btn" id="modal-cancel">Cancel</button><button class="primary-btn">Save</button></div></form>`);
    const theme = window.cspSettings?.theme || "dark"; document.querySelector("#settings-form select").value=theme;
    $("modal-cancel").onclick=closeModal; $("settings-form").onsubmit=async e=>{e.preventDefault();const fd=new FormData(e.target);["notify_mentions","notify_dms","notify_room","profanity_filter"].forEach(k=>{fd.set(k,e.target.elements[k].checked?"1":"0")});const r=await fetch("/api/settings",{method:"POST",body:fd,headers:{"X-CSRFToken":csrf}});const d=await r.json();if(!r.ok)return toast(d.error,true);window.cspMe=d.user;document.querySelector(".profile-card strong").textContent=d.user.display_name;applyTheme(fd.get("theme"));toast("Settings saved");closeModal();};
  }

  function openCalendar() {
    const now = new Date(); let y=now.getFullYear(), m=now.getMonth(); let events=[];
    const render=()=>{const first=new Date(y,m,1), days=new Date(y,m+1,0).getDate(), offset=first.getDay(); openModal(`<div style="display:flex;justify-content:space-between;align-items:center"><button class="icon-btn" id="cal-prev">‹</button><h3>${new Intl.DateTimeFormat([], {month:'long',year:'numeric'}).format(first)}</h3><button class="icon-btn" id="cal-next">›</button></div><div id="calendar" class="calendar-grid"></div><hr><form id="event-form"><h3>Add global event</h3><div class="form-row"><input class="field" name="title" placeholder="Event title" maxlength="120" required><input class="field" name="location" placeholder="Location" maxlength="180"></div><div class="form-row"><input class="field" type="datetime-local" name="starts_at" required><input class="field" type="datetime-local" name="ends_at"></div><textarea class="field" name="description" maxlength="1000" placeholder="Description"></textarea><div class="modal-actions"><button type="button" id="cal-close" class="icon-btn">Close</button><button class="primary-btn">Add event</button></div></form>`);
      $("cal-prev").onclick=()=>{m--;if(m<0){m=11;y--;}render();loadCal();}; $("cal-next").onclick=()=>{m++;if(m>11){m=0;y++;}render();loadCal();}; $("cal-close").onclick=closeModal;
      $("event-form").onsubmit=async e=>{e.preventDefault();const fd=new FormData(e.target);const r=await fetch('/api/calendar',{method:'POST',body:fd,headers:{'X-CSRFToken':csrf}});const d=await r.json();if(!r.ok)return toast(d.error,true);toast('Event added');loadCal();};
      drawCalendar(y,m,events);
    };
    const loadCal=async()=>{const start=new Date(y,m,1).getTime()/1000;const end=new Date(y,m+1,0,23,59,59).getTime()/1000;const r=await fetch(`/api/calendar?start=${start}&end=${end}`);const d=await r.json();events=d.events||[];drawCalendar(y,m,events)};
    render(); loadCal();
  }

  function drawCalendar(y,m,events){const grid=$("calendar");if(!grid)return;grid.replaceChildren();for(let i=0;i<new Date(y,m,1).getDay();i++)grid.appendChild(document.createElement('div'));const count=new Date(y,m+1,0).getDate();for(let day=1;day<=count;day++){const c=document.createElement('div');c.className='calendar-cell';const d=document.createElement('div');d.className='day';d.textContent=day;c.appendChild(d);events.filter(e=>{const dt=new Date(e.starts_at);return dt.getFullYear()===y&&dt.getMonth()===m&&dt.getDate()===day}).forEach(e=>{const p=document.createElement('div');p.className='event-pill';p.textContent=`${new Date(e.starts_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',hour12:false})} ${e.title}`;p.title=`${e.title} — ${e.description}`;c.appendChild(p)});grid.appendChild(c);}}

  function applyTheme(theme) { if(theme==="system"){document.body.classList.toggle("light",matchMedia("(prefers-color-scheme: light)").matches);}else document.body.classList.toggle("light",theme==="light"); localStorage.setItem("csp-theme",theme); $("theme-toggle").textContent=document.body.classList.contains("light")?"☀":"☾"; }

  async function updateHealth(){try{const r=await fetch('/health');const d=await r.json();const badge=$("db-status");badge.className='status-badge '+(d.database==='render_postgres'||d.database==='supabase_postgres'?'pg':d.database==='sqlite_local'?'local':'memory');badge.textContent=d.database.replaceAll('_',' ');$("temporary-banner").classList.toggle('hidden',d.database!=='in_memory');}catch(_){}}

  async function deleteRoom(id){const r=await fetch(`/api/rooms/${id}`,{method:'DELETE',headers:{'X-CSRFToken':csrf}});const d=await r.json();if(!r.ok)return toast(d.error,true);toast('Room deleted');}

  function send(){const text=input.value;if(!text.trim())return;socket.emit('send_message',{text,room:currentRoom,reply_to:replyTo,csrf_token:csrf});input.value='';resizeInput();replyTo=null;$("reply-preview").classList.add('hidden');}
  function resizeInput(){input.style.height='auto';input.style.height=Math.min(input.scrollHeight,160)+'px';}

  $("send-btn").onclick=send; input.addEventListener('input',()=>{resizeInput();const now=Date.now();if(now-lastTypingSent>2000){lastTypingSent=now;socket?.emit('typing',{room:currentRoom,csrf_token:csrf});}});input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});
  $("load-more").onclick=()=>{if(oldestId)loadMessages(oldestId)}; $("settings-open").onclick=openSettings; $("calendar-open").onclick=openCalendar;
  $("new-room").onclick=()=>openModal(`<h3>Create room</h3><form id="room-form"><input class="field" name="name" minlength="3" maxlength="30" placeholder="Room name" required><div class="modal-actions"><button type="button" class="icon-btn" id="modal-cancel">Cancel</button><button class="primary-btn">Create</button></div></form>`);
  modal.addEventListener('submit',async e=>{if(e.target.id!=='room-form')return;e.preventDefault();const fd=new FormData(e.target);const r=await fetch('/api/rooms',{method:'POST',body:fd,headers:{'X-CSRFToken':csrf}});const d=await r.json();if(!r.ok)return toast(d.error,true);closeModal();toast(`Room #${d.name} created`);socket.emit('request_rooms');});
  $("user-search").addEventListener('input',e=>{const q=e.target.value.toLowerCase();people.forEach(p=>p.classList.toggle('hidden',!p.textContent.toLowerCase().includes(q)));});

  $("message-search").addEventListener('input',()=>{
    clearTimeout(window.cspSearchTimer);window.cspSearchTimer=setTimeout(async()=>{const q=$("message-search").value.trim();if(!q)return $("search-results").classList.add('hidden');const r=await fetch(`/api/search?q=${encodeURIComponent(q)}&room=${encodeURIComponent(currentRoom)}`);const d=await r.json();const box=$("search-results");box.replaceChildren();box.classList.remove('hidden');if(!d.results.length){box.textContent='No results found';return;}d.results.forEach(x=>{const b=document.createElement('div');b.className='search-result';b.textContent=`${x.username} · ${formatTime(x.created_at)} — ${x.text}`;b.onclick=()=>jumpTo(x.id);box.appendChild(b);});},300);
  });

  messagesEl.addEventListener('click',e=>{const action=e.target.closest('[data-action]')?.dataset.action;if(!action)return;const btn=e.target.closest('[data-action]'), id=Number(btn.dataset.messageId);
    if(action==='reaction'){socket.emit('react',{message_id:id,emoji:btn.dataset.emoji,csrf_token:csrf});return;}
    if(action==='reply'){const row=document.querySelector(`[data-id="${id}"]`);const text=row?.querySelector('.text')?.textContent||'';replyTo=id;const p=$("reply-preview");p.classList.remove('hidden');p.textContent=`Replying to: ${text.slice(0,100)}`;input.focus();return;}
    if(action==='delete'){socket.emit('delete_message',{message_id:id,csrf_token:csrf});return;}
    if(action==='pin'){socket.emit('pin_message',{message_id:id,csrf_token:csrf});return;}
    if(action==='edit'){const row=document.querySelector(`[data-id="${id}"]`), text=row?.querySelector('.text')?.textContent||'';const next=prompt('Edit message',text.replace(/ \(edited\)$/,''));if(next!==null)socket.emit('edit_message',{message_id:id,text:next,csrf_token:csrf});}
  });
  messagesEl.addEventListener('click',e=>{const q=e.target.closest('[data-jump]');if(q)jumpTo(q.dataset.jump)});

  window.addEventListener('focus',()=>{focused=true;unread=0;document.title='C.S.P. Chat'});window.addEventListener('blur',()=>focused=false);

  // Socket.IO connection lifecycle.
  socket = io({transports:['websocket','polling'],auth:{csrf_token:csrf}});
  socket.on('connect',()=>{connection.textContent='Connected';connection.className='connection connected';updateHealth();if(currentRoom)socket.emit('join_room',{room:currentRoom,csrf_token:csrf});});
  socket.on('disconnect',()=>{connection.textContent='Reconnecting…';connection.className='connection connecting';});
  socket.on('connect_error',()=>{connection.textContent='Reconnecting…';connection.className='connection connecting';});
  socket.on('message',msg=>{if(msg.room!==currentRoom){const n=(unreadRooms.get(msg.room)||0)+1;unreadRooms.set(msg.room,n);return;}const bottom=nearBottom();messagesEl.appendChild(messageNode(msg));if(bottom)scrollBottom();if(!focused&&msg.user_id!==meId){unread++;document.title=`(${unread}) C.S.P. Chat`;beep();}});
  socket.on('message_updated',msg=>{const old=document.querySelector(`[data-id="${msg.id}"]`);if(old)old.replaceWith(messageNode(msg));});
  socket.on('reaction_update',data=>{const old=document.querySelector(`[data-id="${data.message.id}"]`);if(old)old.replaceWith(messageNode(data.message));});
  socket.on('presence',data=>{window.cspUsers=data.users;buildUsers(data.users);});
  socket.on('rooms',data=>{window.cspRooms=data.rooms;buildSidebar();});
  socket.on('typing',data=>{if(data.room!==currentRoom)return;$("typing-indicator").textContent=`${data.user.display_name} is typing…`;clearTimeout(typingClearTimer);typingClearTimer=setTimeout(()=>$("typing-indicator").textContent='',3000);});
  socket.on('error_message',data=>toast(data.message,true));
  socket.on('mention',data=>{const msg=data.message;if(!focused)beep();if('Notification'in window&&Notification.permission==='granted'&&window.cspSettings?.notify_mentions)new Notification(`@${meName} was mentioned`,{body:msg.text.slice(0,100)});});
  socket.on('profile_updated',data=>{if(data.user.id===meId)window.cspMe=data.user;});
  socket.on('calendar_event',()=>{});

  const inviteBtn = $("invite-open");
  if (inviteBtn) inviteBtn.onclick = async () => {
    if (window.cspRole !== "admin") return toast("Admin access required.", true);
    openModal(`<h3>Invite people</h3><form id="invite-form"><label>How many codes?<input class="field" type="number" name="count" min="1" max="10" value="1"></label><div class="modal-actions"><button type="button" class="icon-btn" id="modal-cancel">Cancel</button><button class="primary-btn">Generate</button></div></form><div id="invite-results"></div>`);
    $("modal-cancel").onclick=closeModal;
    $("invite-form").onsubmit=async e=>{e.preventDefault();const fd=new FormData(e.target);const r=await fetch('/api/invites',{method:'POST',body:fd,headers:{'X-CSRFToken':csrf}});const d=await r.json();if(!r.ok)return toast(d.error,true);const out=$("invite-results");out.replaceChildren();(d.codes||[]).forEach(code=>{const p=document.createElement('p');const link=`${location.origin}/invite/${code}`;p.textContent=`${code} — ${link}`;out.appendChild(p);});toast('Invite codes generated');};
  };

  $("theme-toggle").onclick=()=>applyTheme(document.body.classList.contains('light')?'dark':'light');
  const storedTheme=localStorage.getItem('csp-theme')||window.cspSettings?.theme||'dark';applyTheme(storedTheme);
  updateHealth();setInterval(updateHealth,60000);

  // Ask for notification permission only after a user gesture rather than on page load.
  document.addEventListener('click',()=>{if('Notification'in window&&Notification.permission==='default')Notification.requestPermission().catch(()=>{});},{once:true});

  // Server-rendered data is injected as JSON-safe strings below by the template script.
  window.cspRooms = [];
  try { window.cspRooms = JSON.parse(document.body.dataset.rooms || '[]'); } catch (_) {}
  // Rehydrate room/user information from template globals.
  window.cspRooms = window.__CSP_ROOMS__ || window.cspRooms;
  window.cspUsers = window.__CSP_USERS__ || [];
  window.cspMe = window.__CSP_ME__ || {id:meId,username:meName,display_name:meName,avatar:'😀'};
  window.cspSettings = window.__CSP_SETTINGS__ || {theme:'dark',notify_mentions:true,notify_dms:true,notify_room:true,profanity_filter:true};
  window.cspAvatars = window.__CSP_AVATARS__ || [];
  window.cspRole = window.cspMe.role;
  document.querySelectorAll(".admin-only").forEach(el => el.classList.toggle("hidden-admin", window.cspRole !== "admin"));
  buildSidebar();
  const first = window.cspRooms.find(r=>r.name==='general') || window.cspRooms[0];
  if(first) selectRoom(`room:${first.id}`,first.name);
  else toast('No chat rooms are available.',true);
})();
