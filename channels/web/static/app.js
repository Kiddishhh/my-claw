/* 浏览器只保存会话 ID、标题和上传路径；消息正文从服务端 checkpoint 读取。
 * 模型文本一律 textContent，不能把模型生成的 HTML 当页面执行。
 */
const $ = id => document.getElementById(id);
const storageKey = 'myclaw.sessions.v1';
let sessions = [];
try { sessions = JSON.parse(localStorage.getItem(storageKey) || '[]').filter(s => /^[A-Za-z0-9_-]{1,128}$/.test(s.id)); } catch { sessions = []; }
let current = sessions[0]?.id;
let busy = false;
let loadVersion = 0;
function save() { try { localStorage.setItem(storageKey, JSON.stringify(sessions)); } catch { notice('浏览器无法保存会话列表，请记下会话 ID。', true); } }
function selected() { return sessions.find(s => s.id === current); }
function notice(text, error = false) { $('notice').textContent = text; $('notice').className = error ? 'error' : ''; }
function setBusy(value) { busy = value; for (const id of ['send','new','delete','files','prompt']) $(id).disabled = value; renderList(); }
function renderList() {
  $('sessions').replaceChildren();
  for (const item of sessions) { const b = document.createElement('button'); b.textContent = item.title; b.title = item.id; b.className = item.id === current ? 'active' : ''; b.disabled = busy; b.onclick = () => switchTo(item.id); $('sessions').append(b); }
  $('title').textContent = selected()?.title || '新会话';
  $('attachments').replaceChildren();
  for (const file of selected()?.files || []) { const span = document.createElement('span'); span.textContent = file.name; span.title = file.path; $('attachments').append(span); }
}
function addMessage(role, text) { const item = document.createElement('article'); item.className = `bubble ${role}`; const label = document.createElement('div'); label.className = 'role'; label.textContent = role === 'user' ? '你' : 'MYCLAW'; const body = document.createElement('div'); body.textContent = text; item.append(label, body); $('messages').append(item); $('messages').scrollTop = $('messages').scrollHeight; }
function emptyState() { const section = document.createElement('div'); section.className = 'empty'; const h = document.createElement('h2'); h.textContent = '从一个想法开始'; const p = document.createElement('p'); p.textContent = '一起写代码、分析文件，把想法变成可运行的项目。'; section.append(h,p); $('messages').append(section); }
async function api(path, options = {}) {
  const response = await fetch('/v1/agent' + path, options);
  if (!response.ok) { let body = {}; try { body = await response.json(); } catch {} throw new Error(response.status === 409 ? '会话正在执行任务，请结束后再试。' : response.status === 413 ? '文件超过 20 MiB。' : body.message || `请求失败（${response.status}）`); }
  return response.json();
}
async function switchTo(id) {
  if (busy) return;
  current = id; renderList(); $('messages').replaceChildren(); notice('正在读取历史…');
  const version = ++loadVersion;
  try { const data = await api(`/sessions/${id}/messages`); if (version !== loadVersion) return; for (const m of data.messages) addMessage(m.role, m.text); if (!data.messages.length) emptyState(); notice(''); }
  catch (error) { if (version === loadVersion) notice(error.message, true); }
}
function newSession() { if (busy) return; const item = {id: crypto.randomUUID(), title:'新会话', files:[]}; sessions.unshift(item); save(); switchTo(item.id); }
$('new').onclick = newSession;
$('chat').onsubmit = async event => {
  event.preventDefault(); if (busy) return; const text = $('prompt').value.trim(); if (!text) return;
  ++loadVersion; const item = selected(); const files = item.files || [];
  const message = text + (files.length ? '\n\n本会话用户已上传的文件（沙箱绝对路径，请按需读取）：\n' + files.map(f => f.path).join('\n') : '');
  if (message.length > 50000) { notice('文本和文件路径总长超过限制，请缩短消息。', true); return; }
  if (item.title === '新会话') item.title = text.slice(0,24);
  save(); setBusy(true); document.querySelector('.empty')?.remove(); addMessage('user',text); $('prompt').value = ''; notice('Agent 正在处理，请稍候…');
  try { const result = await api('/channels/web/tasks', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({thread_id:current,message})}); addMessage('assistant', result.reply || '本次没有可展示的文字回复。'); notice(result.workspace_saved ? '工作区已保存' : '回复已生成，但工作区保存失败。', !result.workspace_saved); }
  catch (error) { notice(error.message + ' 任务可能已执行部分操作，请先查看会话再决定是否重试。', true); $('prompt').value = text; }
  finally { setBusy(false); }
};
$('files').onchange = async event => {
  const files = [...event.target.files]; if (!files.length || busy) return; setBusy(true); ++loadVersion;
  try { for (const file of files) { if (file.size > 20*1024*1024) throw new Error(`${file.name} 超过 20 MiB`); notice(`正在上传 ${file.name} 并保存工作区…`); const data = await api(`/sessions/${current}/files?filename=${encodeURIComponent(file.name)}`, {method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file}); (selected().files ||= []).push({name:file.name,path:data.path}); save(); renderList(); } notice('文件已进入沙箱。发送任务后，Agent 会收到文件路径。'); }
  catch (error) { notice(error.message + ' 未完成的文件请重试；此前成功上传的文件会保留。', true); }
  finally { $('files').value = ''; setBusy(false); }
};
$('delete').onclick = () => { if (!busy) $('confirm').showModal(); };
$('cancel-delete').onclick = () => $('confirm').close();
$('confirm-delete').onclick = async () => {
  if (busy) return; $('confirm').close(); ++loadVersion; setBusy(true); notice('正在清理会话和沙箱…'); const id = current;
  try { await api(`/sessions/${id}`, {method:'DELETE'}); sessions = sessions.filter(s => s.id !== id); save(); setBusy(false); if (sessions.length) await switchTo(sessions[0].id); else newSession(); }
  catch (error) { notice(error.message + ' 删除尚未完成，可再次点击删除重试。', true); }
  finally { setBusy(false); }
};
if (current) switchTo(current); else newSession();
