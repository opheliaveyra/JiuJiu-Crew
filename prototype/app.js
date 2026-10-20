/* Deliberately does not talk to any model API. An existing agent must bring its own adapter. */
const $ = s => document.querySelector(s);
let token = localStorage.getItem('crewToken') || '';
let me = null;
let room = 'lobby';
let lastInbox = 0;
let refreshHandle = null;
let rooms = [];
let members = [];
async function api(path, opts = {}) {
  const headers = {'Content-Type': 'application/json'};
  if(token) headers.Authorization = 'Bearer ' + token;
  const response = await fetch(path, {...opts, headers});
  const data = await response.json();
  if(!response.ok) throw new Error(data.error || response.statusText);
  return data;
}
function status(msg, id='#notice'){ $(id).textContent = msg; }
function textNode(tag, text, cls) {const el=document.createElement(tag);el.textContent=text;if(cls)el.className=cls;return el;}
function clearSensitiveFields(){
  for(const selector of ['#credential-value','#token','#owner-secret','#invite-code','#invite-value']){
    const field=$(selector);
    if(field)field.value='';
  }
  const statusEl=$('#credential-copy-status');
  if(statusEl)statusEl.textContent='';
  const inviteStatus=$('#invite-copy-status');
  if(inviteStatus)inviteStatus.textContent='';
}
function showAuth(){
  clearInterval(refreshHandle);
  refreshHandle=null;
  clearSensitiveFields();
  $('#auth').hidden=false;
  $('#workspace').hidden=true;
  $('#identity').textContent='尚未登录';
  const cb=$('#credential-box');
  if(cb)cb.hidden=true;
  const inviteResult=$('#invite-result');
  if(inviteResult)inviteResult.hidden=true;
}
async function login(t){
  token=t; const info=await api('/api/me');
  me=info.member;
  localStorage.setItem('crewToken',token);$('#auth').hidden=true;$('#workspace').hidden=false;
  // Clear temporary login/bootstrap inputs, but preserve the new member token in its dedicated credential field.
  for(const selector of ['#token','#owner-secret','#invite-code']){const field=$(selector);if(field)field.value='';}
  $('#identity').textContent=me.name+' · '+(me.role==='owner'?'群主':'成员');
  $('#invite-button').hidden=me.role!=='owner';
  const cb=$('#credential-box');
  if(cb){$('#credential-value').value=token;$('#credential-copy-status').textContent='服务器只保存凭证的哈希，丢了无法找回。请复制到安全的地方。';cb.hidden=false;}
  lastInbox=0;
  await loadRooms();await loadMembers();await loadMessages();await loadInbox();
  clearInterval(refreshHandle);
  refreshHandle=setInterval(()=>Promise.all([loadMessages(),loadInbox()]).catch(e=>status(e.message)),5000);
}
async function loadRooms(){
  rooms=(await api('/api/rooms')).rooms;
  const box=$('#rooms');box.replaceChildren();
  if(!rooms.some(r=>r.id===room))room='lobby';
  for(const r of rooms){
    const b=textNode('button','# '+r.name,'room'+(room===r.id?' selected':''));
    b.onclick=async()=>{room=r.id;await loadRooms();await loadMessages();};box.append(b);
  }
  $('#room-title').textContent=rooms.find(r=>r.id===room)?.name||'讨论室';
}
async function loadMembers(){
  members=(await api('/api/members')).members;
  const box=$('#members');box.replaceChildren();
  for(const m of members)box.append(textNode('div',m.name+(m.role==='owner'?' 👑':''),'chip'));
}
async function loadMessages(){
  const list=(await api('/api/rooms/'+encodeURIComponent(room)+'/messages')).messages;
  const box=$('#messages');const follow=box.scrollTop+box.clientHeight>=box.scrollHeight-80;
  box.replaceChildren();
  for(const m of list){
    const bubble=textNode('div','', 'bubble'+(m.sender_id===me.id?' own':''));
    bubble.append(textNode('div',m.sender_name+(m.kind==='assembly'?' · 📣 集结':''),'by'));
    bubble.append(textNode('div',m.body));
    bubble.append(textNode('span',m.created_at,'meta'));
    box.append(bubble);
  }
  if(follow)box.scrollTop=box.scrollHeight;
}
async function loadInbox(){
  const got=(await api('/api/inbox?after='+lastInbox)).notifications;
  const box=$('#inbox');
  for(const n of got){box.prepend(textNode('div',n.sender_name+' 在 '+n.room_id+' '+(n.kind==='assembly'?'发起集结':'@了你')+'：'+n.body,'notification'));lastInbox=Math.max(lastInbox,n.seq);}
}
function bindForm(selector, callback){$(selector).addEventListener('submit',async e=>{e.preventDefault();try{await callback();}catch(error){status(error.message,selector==='#token-form'||selector==='#bootstrap-form'||selector==='#register-form'?'#auth-tip':'#notice');}});}
bindForm('#token-form',async()=>login($('#token').value.trim()));
bindForm('#bootstrap-form',async()=>{const data=await api('/api/bootstrap',{method:'POST',body:JSON.stringify({name:$('#owner-name').value,secret:$('#owner-secret').value})});await login(data.token);status('群主账号创建成功，已保存本机登录。请安全保存凭证。');});
bindForm('#register-form',async()=>{const data=await api('/api/register',{method:'POST',body:JSON.stringify({name:$('#new-name').value,invite:$('#invite-code').value.trim()})});await login(data.token);status('注册成功。请安全保存成员凭证。');});
bindForm('#room-form',async()=>{const info=await api('/api/rooms',{method:'POST',body:JSON.stringify({name:$('#room-name').value})});$('#room-name').value='';room=info.room.id;await loadRooms();await loadMessages();});
bindForm('#message-form',async()=>{const body=$('#message-input').value.trim();await api('/api/rooms/'+room+'/messages',{method:'POST',body:JSON.stringify({body})});$('#message-input').value='';await loadMessages();status('已发送。@提醒将写入对应成员的信箱。');});
async function copyInvite(){
  const input = $('#invite-value');
  if(!input.value)return;
  let copied = false;
  if(navigator.clipboard?.writeText){
    try{await navigator.clipboard.writeText(input.value);copied=true;}catch(error){/* Offer a fallback. */}
  }
  if(!copied){
    input.focus();input.select();
    try{copied=Boolean(document.execCommand('copy'));}catch(error){copied=false;}
  }
  $('#invite-copy-status').textContent = copied ? '✅ 邀请码已复制，可以交给需要注册的成员。' : '浏览器禁止自动复制，邀请码已选中，请按 Ctrl+C 复制。';
}
$('#invite-copy-button').onclick=copyInvite;
async function copyCredential(){
  const input = $('#credential-value');
  if(!input.value)return;
  let copied = false;
  if(navigator.clipboard?.writeText){
    try{await navigator.clipboard.writeText(input.value);copied=true;}catch(error){/* Offer a fallback. */}
  }
  if(!copied){
    input.focus();input.select();
    try{copied=Boolean(document.execCommand('copy'));}catch(error){copied=false;}
  }
  $('#credential-copy-status').textContent = copied ? '✅ 凭证已复制，请保存到密码管理器或安全位置。' : '浏览器禁止自动复制，凭证已选中，请按 Ctrl+C 复制。';
}
$('#credential-copy-button').onclick=copyCredential;
$('#invite-button').onclick=async()=>{
  try{
    const result=await api('/api/invites',{method:'POST',body:'{}'});
    $('#invite-value').value=result.invite;
    $('#invite-result').hidden=false;
    $('#invite-copy-status').textContent='邀请码仅能使用一次，请点击「复制邀请码」交给成员。';
  }catch(e){status(e.message);}
};
$('#assemble-button').onclick=async()=>{const topic=prompt('本次集结讨论什么主题？');if(!topic?.trim())return;try{const result=await api('/api/rooms/'+room+'/assemble',{method:'POST',body:JSON.stringify({body:topic})});await loadMessages();status('集结消息已写入 '+result.queued_for+' 位成员的信箱。未执行外部会话唤醒。');}catch(e){status(e.message);}};
$('#logout').onclick=()=>{localStorage.removeItem('crewToken');token='';me=null;showAuth();};
if(token) login(token).catch(e=>{status(e.message,'#auth-tip');showAuth();});