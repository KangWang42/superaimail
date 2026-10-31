'use strict';
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icons = {
  inbox:'<path d="M4 4h16v16H4z"/><path d="M4 14h5l2 3h2l2-3h5"/>',
  star:'<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9z"/>',
  task:'<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 3h6v4H9zM8 12h8m-8 4h5"/>',
  settings:'<path d="m9 3-1 3-3 1-2 3 2 3-1 3 3 2 3-1 3 2 3-2 1-3 3-1v-4l-3-1-1-3-3-2z"/><circle cx="11.5" cy="11.5" r="3"/>',
  archive:'<path d="M3 4h18v5H3zm2 5v12h14V9m-10 5h6"/>',
  send:'<path d="m3 3 19 9-19 9 4-9zm4 9h15"/>',
  mail:'<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 6 9 7 9-7"/>',
};
const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.mail}</svg>`;
const state = {session:null, overview:null, messages:[], more:false, message:null, route:0, filter:'', csrf:'', cache:new Map(), event:0};
let toastTimer;
function toast(text) { $('#toast').textContent = text; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').hidden = true, 4500); }
function applyTheme(value) {
  const theme = value || localStorage.getItem('supermail-theme') || 'system';
  document.documentElement.dataset.theme = theme === 'system' ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : theme;
}
applyTheme();
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => applyTheme());
async function api(path, body, {signal} = {}) {
  const options = {credentials:'same-origin', cache:'no-store', signal, headers:{}};
  if (body !== undefined) {
    options.method = 'POST'; options.headers = {'Content-Type':'application/json','X-Supermail-CSRF':state.csrf}; options.body = JSON.stringify(body);
  } else if (state.cache.has(path)) options.headers['If-None-Match'] = state.cache.get(path).etag;
  const response = await fetch(`/api/${path}`, options);
  if (response.status === 304) return state.cache.get(path).body;
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401) { state.cache.clear(); state.session = {authenticated:false}; loginPage(); }
    throw Error(result.error || `请求失败（${response.status}）`);
  }
  if (body !== undefined) state.cache.clear();
  else if (response.headers.get('ETag')) state.cache.set(path, {etag:response.headers.get('ETag'),body:result});
  return result;
}
const action = (name, payload={}) => api('action', {action:name,...payload});
const date = (stamp, full=false) => stamp ? new Date(stamp*1000).toLocaleString('zh-CN', full ? {month:'long',day:'numeric',hour:'2-digit',minute:'2-digit'} : {month:'numeric',day:'numeric'}) : '';
function routeInfo() { const [path, query=''] = (location.hash.slice(1) || '/inbox').split('?'); return {path, params:new URLSearchParams(query)}; }
function link(path, values={}) { const params = new URLSearchParams(Object.entries(values).filter(([,v]) => v !== '' && v != null)); return `#/${path}${params.size ? '?'+params : ''}`; }
const brand = () => '<a class="brand" href="#/inbox"><img src="icon.svg" alt="" width="31" height="31">SuperAIMail</a>';
function loginPage() {
  const setup = state.session?.setup;
  $('#app').innerHTML = `<main id="main" class="login-shell"><div class="login-card">${brand()}<div class="eyebrow">YOUR MAIL, YOUR SPACE</div><h1>${setup ? '先给邮箱一个家' : '欢迎回来'}</h1><p>${setup ? '设置工作台访问密码，然后添加你的邮箱。邮件和设置保存在运行本应用的设备上。' : '输入工作台访问密码，继续处理你的邮件。'}</p><form id="login-form"><label class="field">工作台访问密码<input name="password" type="password" minlength="10" maxlength="256" autocomplete="${setup ? 'new-password' : 'current-password'}" required autofocus><small>${setup ? '至少 10 个字符；这不是邮箱的登录密码。' : '使用首次打开 SuperAIMail 时设置的密码。'}</small></label>${setup ? '<label class="field">再次输入<input name="repeat" type="password" minlength="10" autocomplete="new-password" required></label>' : ''}<div class="form-error" role="alert"></div><button class="primary" type="submit">${setup ? '创建我的工作台' : '打开工作台'}</button></form><footer>多邮箱集中收发。连接 AI 后，还能分类、摘要、翻译和起草回复。<br>AI 默认关闭，可随时在设置中开启。</footer></div></main>`;
  $('#login-form').addEventListener('submit', async event => {
    event.preventDefault(); const form = event.currentTarget, values = Object.fromEntries(new FormData(form));
    await submit(form, async () => {
      if (setup && values.password !== values.repeat) throw Error('两次输入的密码不同');
      const result = await api(setup ? 'setup' : 'login', {password:values.password}); state.csrf = result.csrf;
      state.session = {authenticated:true}; await navigate();
    });
  });
}
function shell(title, subtitle, content, header='') {
  const {path,params} = routeInfo(), overview = state.overview;
  const navs = [['inbox','收件箱','inbox',overview.unread],['starred','星标邮件','star'],['tasks','收集任务','task'],['archive','归档','archive'],['settings','设置','settings']];
  const nav = navs.map(([id,label,glyph,count]) => `<a href="#/${id}" class="${path==='/'+id ? 'active':''}" ${path==='/'+id ? 'aria-current="page"':''}>${icon(glyph)}<span>${label}</span>${count ? `<span class="count">${count}</span>`:''}</a>`).join('');
  $('#app').innerHTML = `<div class="shell"><aside class="sidebar"><div>${brand()}<button class="primary compose" data-do="compose">＋ 写邮件</button></div><nav class="nav" aria-label="主导航">${nav}</nav><div><div class="sidebar-label eyebrow">我的邮箱</div><nav class="nav" aria-label="邮箱">${overview.accounts.map(a => `<a href="${link('inbox',{account:a.id})}" title="${esc(a.address)}" class="${params.get('account')===a.id?'active':''}"><span class="mailbox-dot"></span><span class="mailbox-name">${esc(a.label || a.address)}</span><span class="count">${a.counts.unread || ''}</span></a>`).join('') || '<p class="sidebar-label muted">还没有添加邮箱</p>'}<a href="#/settings" data-do="add-account">＋ 添加邮箱</a></nav></div><div class="sidebar-footer"><strong>${overview.ai.enabled ? 'AI 助手已开启':'基础邮件模式'}</strong><p>${overview.ai.enabled ? '帮你把注意力留给重要邮件':'在设置中连接 AI，开始智能整理'}</p><p>SuperAIMail ${esc(state.session.version || '0.1.1')}</p></div></aside><main class="main" id="main"><header class="page-header"><div><h1>${esc(title)}</h1><p class="subtitle">${esc(subtitle)}</p></div><div class="header-actions">${header}</div></header><div class="workspace">${content}</div></main><nav class="mobile-nav" aria-label="手机导航">${navs.filter(([id])=>id!=='archive').map(([id,label,glyph])=>`<a href="#/${id}" class="${path==='/'+id?'active':''}" ${path==='/'+id?'aria-current="page"':''}>${icon(glyph)}${label}</a>`).join('')}</nav></div>`;
}
async function navigate() {
  if (!state.session?.authenticated) return loginPage();
  const version = ++state.route;
  state.navigating = (state.navigating || 0) + 1;
  $('#app').setAttribute('aria-busy','true');
  try {
    state.overview = await api('overview');
    if (version !== state.route) return;
    const {path,params} = routeInfo();
    if (path === '/settings') return settingsPage();
    if (path === '/tasks') return tasksPage();
    if (path.startsWith('/task/')) return await taskPage(path.slice(6), version);
    if (path === '/compose') { inboxPage(); await loadMessages(version); compose(); return; }
    inboxPage();
    await loadMessages(version);
    if (params.get('id') && version === state.route) await openMessage(params.get('id'), version);
  } catch(error) { toast(error.message); if ($('#message-list')) $('#message-list').innerHTML = `<li class="empty-state"><h2>暂时无法载入</h2><p>${esc(error.message)}</p><button data-do="refresh">重试</button></li>`; }
  finally {state.navigating--;if(!state.navigating)$('#app').setAttribute('aria-busy','false');}
}
function mailFilters() {
  const {path,params} = routeInfo();
  const result = {view:path==='/archive'?'archive':'inbox',limit:50};
  for (const key of ['q','account','group','unread','attach','level','view','since','until','label','starred']) if (params.get(key)) result[key]=params.get(key);
  if (path==='/starred') result.starred='1';
  return result;
}
function inboxPage() {
  const {path,params}=routeInfo(), overview=state.overview;
  const title=path==='/starred'?'星标邮件':path==='/archive'?'归档':'收件箱';
  const subtitle=overview.accounts.length ? `${overview.unread} 封未读 · ${overview.accounts.length} 个邮箱${overview.ai.enabled?' · AI 正在帮你整理':''}` : '把不同邮箱里的重要消息，放在同一个地方';
  const filters=[['','全部'],['unread','未读'],['focus','待处理'],['attach','有附件']];
  shell(title,subtitle,`<div class="inbox-layout" id="inbox-layout"><section class="list-pane" aria-label="邮件列表"><form class="search-form" id="search-form"><input name="q" aria-label="搜索邮件" placeholder="搜索主题、发件人、正文…" value="${esc(params.get('q')||'')}"><button type="submit">搜索</button>${overview.ai.enabled?'<button type="button" data-do="ai-search" title="用自然语言描述要找的邮件">AI 查找</button>':''}</form><div class="filters">${filters.map(([id,label])=>`<button data-filter="${id}" class="${(id===''&&!params.has('unread')&&!params.has('attach')&&!params.has('level'))||params.get(id)==='1'||params.get('level')===id?'active':''}">${label}</button>`).join('')}<select id="mail-view" aria-label="邮件文件夹"><option value="inbox">收件箱</option><option value="sent">已发送</option><option value="archive">归档</option><option value="snoozed">稍后处理</option><option value="trash">回收站</option><option value="all">全部邮件</option></select></div><div class="filters"><select id="mail-account" aria-label="筛选邮箱"><option value="">所有邮箱</option>${overview.accounts.map(a=>`<option value="${a.id}">${esc(a.label||a.address)}</option>`).join('')}</select><select id="mail-group" aria-label="筛选分类"><option value="">所有分类</option>${overview.groups.map(g=>`<option>${esc(g.name)}</option>`).join('')}</select></div><div class="list-toolbar"><span id="result-count">正在读取…</span><button class="quiet" data-do="read-all">全部已读</button></div><ul class="message-list" id="message-list"></ul><div class="pagination" id="pagination"></div></section><article class="reader empty" id="reader"><div class="empty-state">${icon('mail')}<h2>给重要的邮件一点注意力</h2><p>选择一封邮件，开始阅读与处理。</p></div></article></div>`,`<button data-do="refresh">刷新</button><button class="primary" data-do="compose">写邮件</button>`);
  $('#mail-view').value=params.get('view')|| (path==='/archive'?'archive':'inbox');
  $('#mail-account').value=params.get('account')||''; $('#mail-group').value=params.get('group')||'';
  $('#search-form').addEventListener('submit', event=>{event.preventDefault(); updateQuery({q:new FormData(event.currentTarget).get('q'),id:''});});
  for (const [id,key] of [['mail-view','view'],['mail-account','account'],['mail-group','group']]) $('#'+id).addEventListener('change',event=>updateQuery({[key]:event.target.value,id:''}));
}
function updateQuery(updates) { const {path,params}=routeInfo(); for (const [key,value] of Object.entries(updates)) { if(value) params.set(key,value); else params.delete(key); } location.hash = path+(params.size?'?'+params:''); }
function messageRow(message) {
  const selected=routeInfo().params.get('id')===message.id;
  return `<li class="message-row ${message.read?'':'unread'} ${selected?'selected':''}" data-message="${esc(message.id)}"><button data-open="${esc(message.id)}" ${selected?'aria-current="true"':''}><div class="sender-line"><span class="sender">${esc(message.from_name || message.from_addr || '未知发件人')}</span><time datetime="${new Date(message.received*1000).toISOString()}">${date(message.received)}</time></div><div class="subject">${message.starred?'☆ ':''}${esc(message.subject || '（无主题）')}</div><p class="snippet">${esc(message.ai_summary || message.snippet || '')}</p><div class="row-meta">${message.group_name?`<span class="category">${esc(message.group_name)}</span>`:''}${message.has_attach?'<span>附件</span>':''}${message.code?`<span>验证码 ${esc(message.code)}</span>`:''}${message.attention==='act'?'<span>需要处理</span>':''}</div></button></li>`;
}
async function loadMessages(version=state.route,append=false) {
  const filters=mailFilters(); if(append && state.messages.length) {const last=state.messages.at(-1);filters.before=last.received;filters.before_id=last.id;}
  const result=await api('messages?'+new URLSearchParams(filters)); if(version!==state.route)return;
  state.messages=append?state.messages.concat(result.messages):result.messages;state.more=result.more;
  const list=$('#message-list'); if(!list)return;
  if(!state.messages.length) {
    list.innerHTML=`<li class="empty-state">${icon('inbox')}<h2>${state.overview.accounts.length?'这里已经处理好了':'从第一个邮箱开始'}</h2><p>${state.overview.accounts.length?'当前条件下没有邮件。可以更换筛选条件，或稍后刷新。':'支持 QQ、网易、Gmail、Outlook 和自定义 IMAP 邮箱。'}</p>${state.overview.accounts.length?'<button data-do="clear-filters">清除筛选</button>':'<button class="primary" data-do="add-account">添加邮箱</button>'}</li>`;
  } else {
    const existing=new Map([...list.children].filter(e=>e.dataset.message).map(e=>[e.dataset.message,e]));
    const fragment=document.createDocumentFragment();
    for(const m of state.messages){const html=messageRow(m);let row=existing.get(m.id);if(!row||row.outerHTML!==html){const template=document.createElement('template');template.innerHTML=html;row=template.content.firstElementChild;}fragment.append(row);}list.replaceChildren(fragment);
  }
  $('#result-count').textContent=`${state.messages.length} 封${result.more?'，还有更多':''}`;
  $('#pagination').innerHTML=result.more?'<button data-do="load-more">加载更多</button>':'';
}
async function openMessage(id,version=state.route) {
  const reader=$('#reader'); if(!reader)return;
  $('#inbox-layout').classList.add('reading');reader.classList.remove('empty');reader.innerHTML='<p class="muted">正在打开邮件…</p>';
  const message=await api('message?id='+encodeURIComponent(id)); if(version!==state.route)return;
  state.message=message;renderReader(message);
  const row=state.messages.find(m=>m.id===id);if(row){row.read=1;const item=$(`[data-message="${CSS.escape(id)}"]`);if(item)item.outerHTML=messageRow(row);}
}
function renderReader(message) {
  const ai=state.overview.ai.enabled;
  $('#reader').innerHTML=`<div class="reader-top"><button class="back-button quiet" data-do="back">← 列表</button><span class="muted">${esc(message.group_name||'未分类')}</span><div class="row-actions"><button data-do="star">${message.starred?'取消星标':'☆ 星标'}</button><button data-do="archive">${message.archived?'移回收件箱':'归档'}</button><button data-do="snooze">稍后</button><button data-do="trash" class="${message.deleted?'':'danger'}">${message.deleted?'恢复':'删除'}</button></div></div><h2>${esc(message.subject||'（无主题）')}</h2><div class="sender-detail"><span class="avatar">${esc((message.from_name||message.from_addr||'?').slice(0,1).toUpperCase())}</span><div class="meta"><strong>${esc(message.from_name||message.from_addr)}</strong> <span class="muted">&lt;${esc(message.from_addr)}&gt;</span><br><span class="muted">发给 ${esc(message.account?.address||message.to_addrs)} · ${date(message.received,true)}</span></div></div>${message.ai_summary?`<div class="ai-box"><strong>AI 摘要</strong>${esc(message.ai_summary)}</div>`:''}<div id="ai-result"></div><div class="reader-text">${esc(message.text||message.snippet||'这封邮件没有可显示的纯文本正文。')}</div>${message.text_error?`<p class="form-error">${esc(message.text_error)}</p>`:''}${message.has_html?`<details><summary>查看排版正文（远程图片默认关闭）</summary><iframe title="邮件排版正文" class="mail-frame" sandbox="allow-popups allow-popups-to-escape-sandbox" loading="lazy" src="/api/html?id=${encodeURIComponent(message.id)}&theme=${document.documentElement.dataset.theme}"></iframe></details>`:''}<div class="attachments">${(message.attachments||[]).filter(a=>!a.inline).map(a=>`<a class="attachment" href="/api/attachment?${new URLSearchParams({id:message.id,part:a.part})}" download>${esc(a.name)} ↓</a>`).join('')}</div><div class="ai-tools">${ai?'<button data-do="summarize">AI 提炼要点</button><button data-do="ai-reply">AI 起草回复</button><button data-do="translate">翻译为中文</button>':'<a href="#/settings">连接 AI，获取摘要与回复建议</a>'}</div><div class="row-actions"><button class="primary" data-do="reply">回复</button><button data-do="group">更改分类</button><button data-do="unread">标为未读</button></div>`;
}
function field(name,label,value='',options={}) {return `<label class="field ${options.full?'full':''}">${esc(label)}${options.textarea?`<textarea name="${name}" ${options.required?'required':''} class="${options.class||''}">${esc(value)}</textarea>`:`<input name="${name}" type="${options.type||'text'}" value="${esc(value)}" ${options.required?'required':''} ${options.placeholder?`placeholder="${esc(options.placeholder)}"`:''} ${options.type==='password'?'autocomplete="new-password"':''}>`}${options.help?`<small>${esc(options.help)}</small>`:''}</label>`;}
function selectField(name,label,options,value,full=false) {return `<label class="field ${full?'full':''}">${esc(label)}<select name="${name}">${options.map(([v,l])=>`<option value="${esc(v)}" ${String(value)===String(v)?'selected':''}>${esc(l)}</option>`).join('')}</select></label>`;}
function checkbox(name,label,checked=false) {return `<label class="check"><input name="${name}" type="checkbox" ${checked?'checked':''}>${esc(label)}</label>`;}
function modal(title,description,html) {$('#modal-content').innerHTML=`<div class="dialog-head"><h2 id="modal-title">${esc(title)}</h2><button class="quiet" data-do="close-modal" aria-label="关闭对话框">×</button></div>${description?`<p class="dialog-description">${esc(description)}</p>`:''}${html}`;if(!$('#modal').open)$('#modal').showModal();}
function formHtml(id,content,button='保存') {return `<form id="${id}"><div class="form-grid">${content}</div><div class="form-error" role="alert"></div><div class="form-footer"><button type="button" data-do="close-modal">取消</button><button class="primary" type="submit">${button}</button></div></form>`;}
function formValues(form){const values=Object.fromEntries(new FormData(form));for(const input of form.querySelectorAll('[type=checkbox]'))values[input.name]=input.checked;return values;}
async function submit(form,callback) {const buttons=[...form.querySelectorAll('button')], error=form.querySelector('.form-error'); if(error)error.textContent='';buttons.forEach(b=>b.disabled=true);try{await callback();}catch(e){if(error)error.textContent=e.message;else toast(e.message);}finally{buttons.forEach(b=>b.disabled=false);}}
function proxyOptions(){return [['','直连'],...state.overview.proxies.map(p=>[p.id,p.name])];}
function accountForm(id='') {
  const a=state.overview.accounts.find(a=>a.id===id)||{};
  modal(a.id?'编辑邮箱':'添加邮箱','使用客户端授权码或应用专用密码；服务地址可按邮箱服务商的说明修改。',formHtml('account-form',
    field('address','邮箱地址',a.address,{type:'email',required:true})+field('label','显示名称',a.label,{placeholder:'例如：工作邮箱'})+
    selectField('auth_type','登录方式',[['password','授权码 / 应用密码'],['microsoft','Microsoft OAuth']],a.auth_type||'password')+field('sender_name','发件人名称',a.sender_name)+
    field('secret',a.id?'授权码 / 密码（留空保留）':'授权码 / 应用密码','',{type:'password',required:!a.id&&a.auth_type!=='microsoft',full:true})+
    `<div class="field full" id="microsoft-login"><label class="field">Microsoft 应用 Client ID<input name="client_id" placeholder="你在 Microsoft Entra 注册的公共客户端 ID"><small>仅 Microsoft 登录需要；注册方法见项目的邮箱设置教程。</small></label><button type="button" data-do="oauth-begin">登录 Microsoft</button><p id="oauth-status" class="muted" aria-live="polite"></p></div>`+
    field('imap_host','IMAP 服务器',a.imap_host,{placeholder:'根据邮箱地址自动填写'})+field('imap_port','IMAP 端口',a.imap_port||993,{type:'number'})+
    field('smtp_host','SMTP 服务器',a.smtp_host,{placeholder:'根据邮箱地址自动填写'})+field('smtp_port','SMTP 端口',a.smtp_port||465,{type:'number'})+
    selectField('smtp_security','SMTP 加密',[['ssl','TLS（465 / 994）'],['starttls','STARTTLS（587）']],a.smtp_security||'ssl')+selectField('proxy_id','网络出口',proxyOptions(),a.proxy_id||'')+
    `<div class="field full">${checkbox('enabled','启用此邮箱',a.enabled!==false)}</div><div class="field full"><button type="button" data-do="test-account">测试收发连接</button><p id="test-result" class="muted"></p></div>`));
  const form=$('#account-form');form.dataset.id=id;
  const authChanged=()=>{const microsoft=form.elements.auth_type.value==='microsoft';$('#microsoft-login').hidden=!microsoft;form.elements.secret.closest('label').hidden=microsoft;form.elements.secret.required=!microsoft&&!a.id;};
  authChanged();form.elements.auth_type.addEventListener('change',authChanged);
  form.elements.address.addEventListener('change',()=>{const preset=state.overview.presets[form.elements.address.value.split('@').at(-1)?.toLowerCase()];if(preset){for(const key of ['imap_host','imap_port','smtp_host','smtp_port','smtp_security'])form.elements[key].value=preset[key];$('#test-result').textContent=preset.note||'';}});
  form.addEventListener('submit',event=>{event.preventDefault();submit(form,async()=>{const values=formValues(form);await action('account_save',{...values,id,oauth_ticket:form.dataset.oauthTicket});$('#modal').close();toast(values.enabled?'邮箱已保存，正在连接':'邮箱已保存，已停用');await navigate();});});
}
function statusText(account){const status=account.status;return status?.error||({watching:'已连接',online:'已连接',connected:'已连接',idle:'已连接',polling:'收取中',paused:'已暂停',stopped:'尚未连接',connecting:'连接中',error:'连接失败',backoff:'等待重连'}[status?.state]||status?.state||'正在连接');}
function settingsPage() {
  const o=state.overview;
  shell('设置','邮箱连接、AI 助手，以及你的工作方式',`<div class="settings-layout"><section class="panel"><h2>我的邮箱</h2><p>邮箱独立连接，邮件集中处理。读写状态保存在本应用，不改变原邮箱。</p>${o.accounts.map(a=>`<div class="setting-row"><div><strong>${esc(a.label||a.address)}</strong><p>${esc(a.address)} · ${a.counts.total} 封</p><p class="status ${a.status?.error?'error':''}">${esc(statusText(a))}</p></div><div class="row-actions"><button data-edit-account="${a.id}">编辑</button><button data-backfill="${a.id}">补收历史</button><button class="danger quiet" data-delete-account="${a.id}">移除</button></div></div>`).join('')}<div class="form-footer"><button class="primary" data-do="add-account">＋ 添加邮箱</button></div></section><section class="panel" id="ai-settings"><h2>AI 助手</h2><p>分类、关注程度、摘要、回复、翻译与自然语言搜索。开启后会将相关邮件内容发给你配置的模型服务。</p><form id="ai-form"><div class="form-grid"><div class="field full">${checkbox('enabled','启用 AI 助手',o.ai.enabled)}</div>${field('url','兼容接口地址',o.ai.url,{full:true,placeholder:'https://your-provider.example/v1',help:'填写服务商提供的基础地址，应用调用 /chat/completions。'})}${field('model','模型名称',o.ai.model,{placeholder:'填写服务商提供的模型 ID'})}${field('key',o.ai.has_key?'API Key（已保存，留空保留）':'API Key','',{type:'password'})}${selectField('mode','自动整理时机',[['each','新邮件到达时'],['daily','每天定时'],['every3','每三天定时']],o.ai.mode)}${field('night','定时整理时间（北京时间）',o.ai.night,{type:'time'})}${field('daily_limit','每日调用上限',o.ai.daily_limit,{type:'number',help:'包含手动和自动调用；设为 0 暂停调用。'})}${selectField('proxy_id','网络出口',proxyOptions(),o.ai.proxy_id||'')}</div><div class="form-error" role="alert"></div><div class="form-footer"><button type="button" data-do="ai-run">立即整理</button><button class="primary" type="submit">保存 AI 设置</button></div></form></section><section class="panel"><h2>分类与规则</h2><p>AI 按分类说明整理邮件；确定的发件人与关键词可使用规则。</p>${o.groups.map(g=>`<div class="setting-row"><div><strong>${esc(g.name)}</strong><p>${esc(g.description)}</p></div><div class="row-actions"><button data-edit-group="${g.id}">编辑</button><button class="quiet danger" data-delete-group="${g.id}">删除</button></div></div>`).join('')}<div class="form-footer"><button data-do="add-group">添加分类</button><button data-do="add-rule">添加匹配规则</button></div>${o.rules.length?`<div class="info">${o.rules.map(r=>`<div class="setting-row"><span>${esc(r.label)} · ${esc(r.from||r.subject||'附件匹配')}</span><div class="row-actions"><button data-edit-rule="${r.id}">编辑</button><button class="danger quiet" data-delete-rule="${r.id}">删除</button></div></div>`).join('')}</div>`:''}</section><section class="panel"><h2>网络出口</h2><p>为特定邮箱或 AI 配置 SOCKS5 代理，其他连接保持直连。</p>${o.proxies.map(p=>`<div class="setting-row"><div><strong>${esc(p.name)}</strong><p>${esc(p.host)}:${p.port}</p></div><div class="row-actions"><button data-edit-proxy="${p.id}">编辑</button><button class="danger quiet" data-delete-proxy="${p.id}">删除</button></div></div>`).join('')}<div class="form-footer"><button data-do="add-proxy">添加代理</button></div></section><section class="panel"><h2>外观与访问</h2><div class="setting-row"><div><strong>外观</strong><p>电脑与手机各自保存选择</p></div><select id="theme-select" aria-label="外观主题"><option value="system">跟随系统</option><option value="light">浅色</option><option value="dark">深色</option></select></div><div class="setting-row"><div><strong>打开邮件时标为已读</strong></div><input id="read-on-open" type="checkbox" aria-label="打开邮件时标为已读" ${o.mail_preferences.read_on_open?'checked':''}></div><div class="setting-row"><div><strong>退出工作台</strong><p>会同时退出其他设备上的浏览器会话，邮箱后台继续收取。</p></div><button data-do="logout">退出登录</button></div></section></div>`);
  $('#theme-select').value=localStorage.getItem('supermail-theme')||'system';
  $('#theme-select').addEventListener('change',e=>{localStorage.setItem('supermail-theme',e.target.value);applyTheme(e.target.value);});
  $('#read-on-open').addEventListener('change',e=>action('mail_preferences',{read_on_open:e.target.checked}).catch(error=>toast(error.message)));
  $('#ai-form').addEventListener('submit',event=>{event.preventDefault();submit(event.currentTarget,async()=>{await action('ai_settings_save',formValues(event.currentTarget));toast('AI 设置已保存');await navigate();});});
}
function tasksPage() {
  const tasks=state.overview.tasks;
  shell('收集任务','把附件按人归档，清楚看到谁已提交、谁还没交',tasks.length?`<div class="task-list">${tasks.map(t=>`<section class="task-card"><div class="eyebrow">${t.ended?'已结束':t.enabled?'收集中':'已暂停'}</div><h2><a href="#/task/${t.id}">${esc(t.name)}</a></h2><p class="muted">${t.deadline?'截止 '+date(t.deadline,true):'未设置截止时间'}</p><div class="task-stats"><div><strong>${t.counts.submitted}</strong><span>已提交</span></div><div><strong>${t.counts.missing}</strong><span>未提交</span></div><div><strong>${t.counts.pending}</strong><span>待确认</span></div></div><div class="progress"><span style="width:${t.counts.expected?Math.min(100,t.counts.submitted/t.counts.expected*100):0}%"></span></div><div class="row-actions"><a href="#/task/${t.id}">查看任务 →</a></div></section>`).join('')}</div>`:`<section class="panel empty-state">${icon('task')}<h2>附件收齐，不必逐封翻找</h2><p>设置主题关键词和人员名单，自动收集附件；AI 可以补充判断与整理文件名。</p><button class="primary" data-do="add-task">创建第一个任务</button></section>`,'<button class="primary" data-do="add-task">＋ 新建任务</button>');
}
async function taskPage(id,version) {
  const task=await api('task?id='+encodeURIComponent(id));if(version!==state.route)return;state.task=task;
  const fileLinks=sub=>(sub.files||[]).map(f=>f.error?`<span class="error-text">${esc(f.name)}：${esc(f.error)}</span>`:`<a class="attachment" href="/api/file?id=${encodeURIComponent(f.id)}" download>${esc(f.display_name||f.name)} ↓</a>`).join(' ');
  shell(task.config.name,`已提交 ${task.counts.submitted} · 未提交 ${task.counts.missing} · 待确认 ${task.counts.pending}`,`<section class="panel"><div class="row-actions"><a href="/api/task_zip?id=${id}&latest=1" download>下载最新附件 ZIP ↓</a><a href="/api/task_zip?id=${id}" download>包含历史版本</a><button data-do="task-rescan">重新扫描</button><button data-do="task-pause">${task.enabled?'暂停收集':'恢复收集'}</button><button data-do="task-end">${task.config.ended?'重新开启':'结束任务'}</button><button class="danger" data-do="task-delete">删除任务</button></div><p class="info">${esc(task.config.description||'匹配主题关键词：'+task.config.keywords.join('、'))}</p>${task.people.map(p=>`<div class="person-row"><h3>${esc(p.name)}<span class="status ${p.status==='submitted'?'ok':''}">${p.status==='submitted'?'已提交':'未提交'}${p.late?' · 逾期':''}</span></h3><p>${esc(p.email)}${p.latest?' · '+date(p.latest,true):''}${p.count>1?' · '+p.count+' 个版本':''}</p>${p.submissions.map(sub=>`<details><summary>${esc(sub.subject)} · ${date(sub.received,true)}</summary><div class="attachments">${fileLinks(sub)}</div><a href="${link('inbox',{id:sub.message_id})}">打开原邮件</a></details>`).join('')}</div>`).join('')||'<p class="muted">还没有明确归属的提交。</p>'}</section>${task.pending.length?`<section class="panel"><h2>待确认的邮件</h2>${task.pending.map(sub=>`<div class="person-row"><h3>${esc(sub.subject)}</h3><p>${esc(sub.from_name||sub.from_addr)} · ${esc(sub.reason)}</p><div class="attachments">${fileLinks(sub)}</div><div class="row-actions"><button data-assign="${sub.id}">指定提交人</button><button data-ignore="${sub.id}">忽略</button><a href="${link('inbox',{id:sub.message_id})}">原邮件</a></div></div>`).join('')}</section>`:''}`,'<button data-do="edit-task">编辑任务</button>');
}
function taskForm(task=null,draft={}) {
  if(!state.overview.accounts.length){toast('请先添加收件邮箱');return accountForm();}
  const t=task?.config||draft;
  const stamp=value=>value?new Date(value*1000-new Date(value*1000).getTimezoneOffset()*60000).toISOString().slice(0,16):'';
  modal(task?'编辑收集任务':'创建收集任务','按主题匹配邮件与附件，无法确认的人留待你处理。',formHtml('task-form',
    field('name','任务名称',t.name,{required:true})+selectField('account_id','收件邮箱',state.overview.accounts.map(a=>[a.id,a.label||a.address]),t.account_id)+
    field('keywords','主题关键词',(t.keywords||[]).join('，'),{required:true,help:'多个关键词用逗号隔开'})+field('extensions','附件类型',(t.extensions||['xlsx','pdf','docx','zip','jpg','png']).join('，'))+
    field('start','开始时间',stamp(t.start||Date.now()/1000-7*86400),{type:'datetime-local',required:true})+field('deadline','截止时间（可留空）',stamp(t.deadline),{type:'datetime-local'})+
    field('roster','人员名单',(t.roster||[]).map(p=>p.name+(p.email?' '+p.email:'')).join('\n'),{textarea:true,full:true,help:'每行一人，可写“姓名 邮箱”；不填写名单时按提交信息识别人。'})+
    field('description','任务说明',t.description,{textarea:true,full:true})+selectField('rename','文件命名',[['template','按模板'],['ai','AI 整理'],['off','保留原名']],t.rename||'template')+
    field('name_template','文件名模板',t.name_template||'{姓名}_{关键词}',{help:'支持 {姓名} {关键词} {任务} {原文件名} {日期}'})));
  if(!task&&state.overview.ai.enabled){const notice=document.createElement('section');notice.innerHTML='<details class="notice-draft"><summary>用 AI 从通知生成任务草稿</summary><label class="field">粘贴收集通知<textarea id="task-notice" placeholder="请在周五前提交项目材料，主题为姓名加项目名称…"></textarea><small>AI 按北京时间理解通知日期，请检查生成的截止时间和邮箱。</small></label><button type="button" id="draft-task">生成草稿</button><p class="form-error" role="alert"></p></details>';$('#task-form').before(notice);$('#draft-task').addEventListener('click',async event=>{const button=event.currentTarget,text=$('#task-notice').value;if(!text.trim())return;button.disabled=true;try{const result=await action('ai_task_draft',{text});taskForm(null,result.draft);}catch(error){notice.querySelector('.form-error').textContent=error.message;button.disabled=false;}});}
  $('#task-form').addEventListener('submit',event=>{event.preventDefault();submit(event.currentTarget,async()=>{const values=formValues(event.currentTarget);values.start=Date.parse(values.start)/1000;values.deadline=values.deadline?Date.parse(values.deadline)/1000:0;const result=await action('task_save',{...values,id:task?.id||''});$('#modal').close();toast('任务已保存');location.hash='#/task/'+result.id;if(task)await navigate();});});
}
function groupForm(id='') {const group=state.overview.groups.find(g=>g.id===id)||{};modal('邮件分类','说明越具体，AI 越容易判断。',formHtml('group-form',field('name','分类名称',group.name,{required:true})+field('description','分类说明',group.description,{full:true,textarea:true})+selectField('attention','默认关注程度',[['','由 AI 判断'],['act','需要处理'],['read','值得阅读'],['fyi','普通通知'],['low','低优先级']],group.attention||'')));$('#group-form').addEventListener('submit',event=>{event.preventDefault();submit(event.currentTarget,async()=>{await action('group_save',{...formValues(event.currentTarget),id});$('#modal').close();await navigate();});});}
function proxyForm(id=''){const p=state.overview.proxies.find(p=>p.id===id)||{};modal('SOCKS5 代理','只应用于选择了这个出口的连接。',formHtml('proxy-form',field('name','名称',p.name)+field('host','服务器地址',p.host,{required:true})+field('port','端口',p.port||1080,{type:'number'})+field('username','用户名',p.username)+field('password','密码（留空保留）','',{type:'password'})));$('#proxy-form').addEventListener('submit',event=>{event.preventDefault();submit(event.currentTarget,async()=>{await action('proxy_save',{...formValues(event.currentTarget),id});$('#modal').close();await navigate();});});}
function ruleForm(id=''){const r=state.overview.rules.find(r=>r.id===id)||{};modal('匹配规则','按发件人或主题关键词添加标签；规则在新邮件到达时执行。',formHtml('rule-form',field('label','添加标签',r.label,{required:true})+selectField('account_id','适用邮箱',[['','全部邮箱'],...state.overview.accounts.map(a=>[a.id,a.label||a.address])],r.account_id||'')+field('from','发件人关键词',r.from)+field('subject','主题关键词',r.subject)+selectField('notify','新邮件通知',[['notify','正常提醒'],['silent','静默']],r.notify||'notify')+`<div class="field">${checkbox('enabled','启用规则',r.enabled!==false)}</div>`));$('#rule-form').addEventListener('submit',event=>{event.preventDefault();submit(event.currentTarget,async()=>{await action('rule_save',{...formValues(event.currentTarget),id});$('#modal').close();await navigate();});});}
function compose(message=null,draft=''){
  if(!state.overview.accounts.length)return accountForm();
  modal(message?'回复邮件':'写邮件','先预览收件人和正文，再确认发送。',formHtml('compose-form',
    selectField('account_id','发信邮箱',state.overview.accounts.filter(a=>a.has_smtp).map(a=>[a.id,a.label||a.address]),message?.account_id,true)+
    field('to','收件人',message?.from_addr||'',{required:true,full:true,help:'多个地址用逗号分隔'})+field('cc','抄送（可选）','',{full:true})+
    field('subject','主题',message?'Re: '+message.subject:'',{required:true,full:true})+field('body','正文',draft,{required:true,textarea:true,full:true,class:'compose-body'}),'预览发送'));
  $('#compose-form').addEventListener('submit',event=>{event.preventDefault();submit(event.currentTarget,async()=>{
    const values=formValues(event.currentTarget); const preview=await action('send_preview',{...values,kind:message?'reply':'compose',id:message?.id});
    const saved=values;modal('确认发送',`将从 ${preview.from} 发送 ${preview.messages.length} 封邮件。`,`<div class="send-preview">${preview.messages.map(m=>`<p><strong>收件人</strong> ${esc(m.to.join(', '))}</p>${m.cc.length?`<p>抄送 ${esc(m.cc.join(', '))}</p>`:''}<p><strong>${esc(m.subject)}</strong></p><p>${esc(m.body)}</p>`).join('')}</div><div class="form-error" role="alert"></div><div class="form-footer"><button data-do="close-modal">取消</button><button id="send-confirm" class="primary">确认发送</button></div>`);
    $('#send-confirm').addEventListener('click',async event=>{event.currentTarget.disabled=true;try{const result=await action('send_confirm',{token:preview.token});if(result.failed.length)throw Error(`已发送 ${result.sent.length} 封；${result.failed.map(f=>f.to+'：'+f.error).join('；')}。请核对后重新撰写。`);$('#modal').close();toast(`已发送 ${result.sent.length} 封${result.copy_errors.length?'，本地副本保存失败，请在原邮箱核对':''}`);await navigate();}catch(error){$('#modal-content .form-error').textContent=error.message;event.currentTarget.textContent='预览已使用，请关闭后重新撰写';}});
  });});
}
async function confirmAction(title,text,callback){modal(title,text,`<div class="form-error" role="alert"></div><div class="form-footer"><button data-do="close-modal">取消</button><button id="confirm-action" class="primary">确认</button></div>`);$('#confirm-action').addEventListener('click',async event=>{event.currentTarget.disabled=true;try{await callback();$('#modal').close();await navigate();}catch(error){$('#modal-content .form-error').textContent=error.message;event.currentTarget.disabled=false;}});}
async function aiResult(kind){const result=$('#ai-result');result.innerHTML='<div class="ai-box"><strong>AI 助手</strong>正在阅读邮件…</div>';try{const data=await action(kind,{id:state.message.id});if(kind==='ai_reply') {result.innerHTML='';return compose(state.message,data.draft);}const text=kind==='ai_summarize'?[data.summary,...(data.points||[]).map(p=>'• '+p),...(data.todos||[]).map(p=>'待办：'+p),...(data.deadlines||[]).map(d=>d.when+' '+d.what)].join('\n'):data.text;result.innerHTML=`<div class="ai-box"><strong>${kind==='ai_translate'?'AI 翻译':'AI 要点'}</strong>${esc(text)}</div>`;}catch(error){result.innerHTML=`<div class="form-error" role="alert">${esc(error.message)}</div>`;}}
async function handleClick(event){
  const button=event.target.closest('button,a');if(!button)return;
  const d=button.dataset;
  if(d.open){updateQuery({id:d.open});return;}
  if(d.filter!==undefined){updateQuery({unread:d.filter==='unread'?'1':'',attach:d.filter==='attach'?'1':'',level:d.filter==='focus'?'focus':'',id:''});return;}
  if(d.editAccount!==undefined){event.preventDefault();return accountForm(d.editAccount);}
  if(d.editGroup!==undefined)return groupForm(d.editGroup);
  if(d.editProxy!==undefined)return proxyForm(d.editProxy);
  if(d.editRule!==undefined)return ruleForm(d.editRule);
  for(const [key,actionName,description] of [['deleteAccount','account_delete','移除后，本应用中的该邮箱索引、关联收集记录也会移除；原邮箱中的邮件不变。'],['deleteGroup','group_delete','删除分类后，已有邮件会取消这个分类。'],['deleteProxy','proxy_delete','删除前请确认没有邮箱或 AI 正在使用此出口。'],['deleteRule','rule_delete','新邮件将不再应用此规则。']])if(d[key])return confirmAction('确认移除',description,()=>action(actionName,{id:d[key]}));
  if(d.backfill)return confirmAction('补收历史邮件','读取最近 180 天的邮件，不修改原邮箱。',()=>action('account_backfill',{id:d.backfill,days:180}));
  if(d.assign){modal('指定提交人','确认后会记住发件人与提交人的对应关系。',formHtml('assign-form',field('person','姓名','',{required:true})));$('#assign-form').addEventListener('submit',e=>{e.preventDefault();submit(e.currentTarget,async()=>{await action('assign',{submission_id:d.assign,person:formValues(e.currentTarget).person});$('#modal').close();await navigate();});});return;}
  if(d.ignore){await action('ignore',{submission_id:d.ignore});return navigate();}
  if(!d.do)return;event.preventDefault();
  switch(d.do){
    case 'close-modal':return $('#modal').close();
    case 'add-account':return accountForm();
    case 'add-group':return groupForm();
    case 'add-proxy':return proxyForm();
    case 'add-rule':return ruleForm();
    case 'compose':return compose();
    case 'reply':return compose(state.message);
    case 'refresh':state.cache.clear();return navigate();
    case 'clear-filters':location.hash='#/inbox';return;
    case 'back':updateQuery({id:''});return;
    case 'load-more':button.disabled=true;try{await loadMessages(state.route,true);}finally{button.disabled=false;}return;
    case 'read-all':await action('messages_read',mailFilters());toast('当前列表中的邮件已标为已读');return navigate();
    case 'star':await action('message_mark',{id:state.message.id,starred:!state.message.starred});return navigate();
    case 'archive':await action('message_mark',{id:state.message.id,archived:!state.message.archived});updateQuery({id:''});return;
    case 'trash':await action('message_mark',{id:state.message.id,deleted:!state.message.deleted});updateQuery({id:''});return;
    case 'unread':await action('message_mark',{id:state.message.id,read:false});updateQuery({id:''});return;
    case 'snooze':await action('messages_mark',{ids:[state.message.id],snoozed_until:Math.floor(Date.now()/1000)+86400});toast('明天再处理，可在“稍后处理”中查看');updateQuery({id:''});return;
    case 'summarize':return aiResult('ai_summarize');
    case 'translate':return aiResult('ai_translate');
    case 'ai-reply':return aiResult('ai_reply');
    case 'group':modal('更改分类','这封邮件的手动分类将优先保留。',formHtml('message-group',selectField('group','分类',state.overview.groups.map(g=>[g.name,g.name]),state.message.group_name)));$('#message-group').addEventListener('submit',e=>{e.preventDefault();submit(e.currentTarget,async()=>{await action('message_group',{id:state.message.id,...formValues(e.currentTarget)});$('#modal').close();await navigate();});});return;
    case 'test-account':{button.disabled=true;$('#test-result').textContent='正在验证连接…';try{const form=$('#account-form');const result=await action('account_test',{...formValues(form),id:form.dataset.id,oauth_ticket:form.dataset.oauthTicket});$('#test-result').textContent=`收件连接成功${result.smtp?'，发信登录成功':result.smtp_error?'；发信：'+result.smtp_error:'（未设置发信）'}`;}catch(error){$('#test-result').textContent=error.message;}finally{button.disabled=false;}return;}
    case 'oauth-begin':{const form=$('#account-form'),status=$('#oauth-status');button.disabled=true;try{const flow=await action('oauth_begin',{client_id:form.elements.client_id.value});status.innerHTML=`打开 <a href="${esc(flow.url)}" target="_blank" rel="noopener noreferrer">Microsoft 登录页</a>，输入代码 <strong>${esc(flow.user_code)}</strong>，完成后回到这里。`;const end=Date.now()+flow.expires_in*1000;const check=async()=>{if(!form.isConnected||!$('#modal').open)return;if(Date.now()>end){status.textContent='登录已过期，请重试';button.disabled=false;return;}try{const result=await action('oauth_poll',{ticket:flow.ticket});if(result.ready){form.dataset.oauthTicket=flow.ticket;status.textContent='Microsoft 登录成功，可以保存邮箱。';button.disabled=false;}else setTimeout(check,(result.interval||flow.interval)*1000);}catch(error){status.textContent=error.message;button.disabled=false;}};setTimeout(check,flow.interval*1000);}catch(error){status.textContent=error.message;button.disabled=false;}return;}
    case 'ai-search':{const query=$('#search-form input').value;if(!query)return toast('先描述你想找的邮件');button.disabled=true;try{const result=await action('ai_search',{query});const valid={account:result.filters.account_id||''};for(const key of ['q','group','since','until','unread','attach','starred','label'])if(result.filters[key]!=null)valid[key]=typeof result.filters[key]==='boolean'?(result.filters[key]?'1':'0'):result.filters[key];updateQuery({...valid,q:valid.q||'',id:''});}finally{button.disabled=false;}return;}
    case 'ai-run':await action('ai_night_run');toast('整理已开始，可稍后刷新查看分类');return;
    case 'add-task':return taskForm();
    case 'edit-task':return taskForm(state.task);
    case 'task-rescan':await action('task_rescan',{id:state.task.id});toast('已加入重新扫描队列');return navigate();
    case 'task-pause':await action('task_pause',{id:state.task.id,enabled:!state.task.enabled});return navigate();
    case 'task-end':await action('task_end',{id:state.task.id,ended:!state.task.config.ended});return navigate();
    case 'task-delete':return confirmAction('删除收集任务','本应用的任务记录与归档附件将删除，原邮件不变。',async()=>{await action('task_delete',{id:state.task.id});location.hash='#/tasks';});
    case 'logout':await api('logout',{});state.csrf='';state.overview=null;state.messages=[];state.message=null;state.cache.clear();state.session={authenticated:false};return loginPage();
  }
}
document.addEventListener('click',event=>handleClick(event).catch(error=>toast(error.message)));
window.addEventListener('hashchange',()=>navigate());
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!$('#modal').open&&routeInfo().params.has('id'))updateQuery({id:''});});
async function poll(){
  if(!state.session?.authenticated||document.hidden||$('#modal').open||document.activeElement?.matches('input,textarea,select'))return;
  try{const data=await api('events?after='+state.event);if(data.last!==state.event){const initialized=state.event!==0;state.event=data.last;if(initialized){state.cache.clear();if($('#message-list'))await loadMessages();}}}catch(error){toast('邮件更新暂时中断：'+error.message);}
}
setInterval(poll,30000);
async function boot(){try{state.session=await api('session');state.csrf=state.session.csrf||'';await navigate();if('serviceWorker'in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>{});}catch(error){$('#app').innerHTML=`<main id="main" class="loading"><h1>暂时无法连接</h1><p>${esc(error.message)}</p><button data-do="refresh">重试</button></main>`;}}
boot();
