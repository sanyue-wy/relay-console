/* Relay Console 前端逻辑：所有动态内容一律经 esc() 转义；事件用 data-act 委托，无内联处理器。 */
let cur = null, platforms = [];
const cache = { sessions: [] };
const $ = id => document.getElementById(id);
const api = (p, o = {}) => fetch(p, { headers: { 'Content-Type': 'application/json' }, ...o })
  .then(async r => {
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      const d = j.detail;
      const msg = typeof d === 'string' ? d
        : (d && d.errors ? '请检查标红的输入项' : (j.error || ('HTTP ' + r.status)));
      const e = new Error(msg); e.detail = d; throw e;
    }
    return j;
  });
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
/* 字段级校验错误：显示在对应输入框旁边的 .ferr 元素里（前缀区分不同表单） */
function fieldErrors(prefix, e) {
  document.querySelectorAll('.ferr').forEach(x => { x.textContent = ''; });
  const errs = e && e.detail && e.detail.errors;
  if (!errs || !errs.length) { alert(e.message); return; }
  errs.forEach(x => { const el = $(prefix + x.field); if (el) el.textContent = esc(x.message); });
}

/* ---------------- 页面切换 ---------------- */
const PAGES = ['start', 'pool', 'accounts', 'settings'];
async function show(p) {
  PAGES.forEach(x => $('page-' + x).classList.toggle('on', x === p));
  document.querySelectorAll('#nav .tab').forEach(b => b.classList.toggle('on', b.dataset.page === p));
  try {
    if (p === 'start') { await Promise.all([loadPlatformsLite(), loadSessionsData()]); renderSessions(); if (cur) await openS(cur, true); }
    if (p === 'pool') { await loadPoolPage(); }
    if (p === 'accounts') { await loadAccounts(); }
    if (p === 'settings') { await loadSettings(); }
  } catch (e) { alert(e.message); }
}

/* ---------------- 开始页：会话 ---------------- */
async function loadPlatformsLite() {
  const d = await api('/api/platforms');
  cache.platforms = d.platforms;
  platforms = d.platforms.map(p => p.id);
  // 新建会话下拉只显示启用的平台（enabled 字段由 B2 提供，缺省视为启用）
  const enabled = d.platforms.filter(p => p.enabled !== false);
  $('newPlatform').innerHTML = enabled.map(p => `<option>${esc(p.id)}</option>`).join('');
}
async function loadSessionsData() { cache.sessions = await api('/api/sessions'); return cache.sessions; }
function renderSessions() {
  $('sessions').innerHTML = cache.sessions.map(x => `<div class="item ${x.id === cur ? 'on' : ''}" data-act="openS" data-id="${esc(x.id)}"><span class="t">${esc(x.title)}</span><div class="sub">${esc((x.chain || []).join(' → '))}</div></div>`).join('') || '<span class="empty">还没有会话</span>';
}
async function newSession() {
  const r = await api('/api/sessions', { method: 'POST', body: JSON.stringify({ title: $('newTitle').value || '新会话', platform: $('newPlatform').value }) });
  cur = r.id; await loadSessionsData(); renderSessions(); openS(cur);
}
async function openS(id, silent) {
  cur = id; const s = await api('/api/sessions/' + encodeURIComponent(id));
  cache.s = s;
  $('head').innerHTML = `<div class="t">${esc(s.title)}</div><div class="sub">当前 ${esc(s.platform)}:${esc(s.account || 'main')} · 接力链 ${esc((s.chain || []).join(' → '))}${s.pool_item_id ? ' · 已发布读取池' : ''}</div>
    <div class="row"><button class="ghost small" data-act="hoOpen" data-sid="${esc(s.id)}">🔁 总结并切换</button></div>`;
  $('chat').innerHTML = s.messages.map(m => `<div class="msg ${m.role === 'user' ? 'user' : 'assistant'}"><div class="who">${m.role === 'user' ? '你' : esc(m.platform)}</div>${esc(m.content)}</div>`).join('');
  $('chat').scrollTop = $('chat').scrollHeight; renderSessions();
}
async function ask() {
  if (!cur) return alert('先选会话');
  const t = $('q').value.trim(); if (!t) return; $('q').value = '';
  $('chat').innerHTML += `<div class="msg user">${esc(t)}</div><div class="empty" id="wait">等待回复…</div>`;
  $('chat').scrollTop = $('chat').scrollHeight;
  try {
    const r = await api(`/api/sessions/${encodeURIComponent(cur)}/ask`, { method: 'POST', body: JSON.stringify({ text: t }) });
    r.relays.forEach(x => $('chat').innerHTML += `<div class="relay">已接力：${esc(x.from)} → ${esc(x.to)}（${esc(x.reason)}）</div>`);
  } catch (e) { alert(e.message); }
  openS(cur);
}

/* ---------------- Agent 池页 ---------------- */
async function loadPoolPage() {
  await loadSessionsData();
  $('pubSession').innerHTML = '<option value="">（不关联会话）</option>' + cache.sessions.map(x => `<option value="${esc(x.id)}">${esc(x.title)}</option>`).join('');
  await Promise.all([loadApprovals(), loadItems(), loadAgents(), loadArenas()]);
}
async function loadApprovals() {
  const d = await api('/api/approvals');
  $('apCount').textContent = d.pending.length;
  const what = r => r.scope_type === 'items' ? '条目：' + esc((r.titles || []).join('、'))
    : r.scope_type === 'turn' ? '对局 ' + esc(r.scope.arena_id) + ' 的本轮题目'
    : '会话 ' + esc(r.scope.session_id) + ' 的状态/摘要预览';
  $('approvals').innerHTML = d.pending.map(r => `<div class="item" style="border:1px solid rgba(245,158,11,.35);background:rgba(245,158,11,.08)"><div class="t">${esc(r.participant_id)} 申请读取</div><div class="sub">${what(r)} · 理由：${esc(r.reason)}</div><div class="row" style="margin-top:6px"><button class="btn small" data-act="decide" data-id="${esc(r.id)}" data-d="approve">批准一次</button>${d.allow_batch && r.scope_type === 'turn' ? `<button class="ghost small" data-act="approveRound" data-aid="${esc(r.scope.arena_id)}">本轮全部批准</button>` : ''}<button class="ghost small danger" data-act="decide" data-id="${esc(r.id)}" data-d="deny">拒绝</button></div></div>`).join('') || '<span class="empty">暂无</span>';
}
async function decide(id, d) { await api(`/api/approvals/${encodeURIComponent(id)}/${d}`, { method: 'POST' }); loadApprovals(); }
async function approveRound(aid) { try { await api(`/api/arenas/${encodeURIComponent(aid)}/approve-round`, { method: 'POST' }); } catch (e) { alert(e.message); } loadApprovals(); }
async function loadItems() {
  const [its, ses] = await Promise.all([api('/api/pool/items'), api('/api/sessions')]);
  const st = {}; ses.forEach(x => st[x.id] = x.title);
  $('items').innerHTML = its.map(i => `<div class="item" style="cursor:default"><div class="row spread"><span class="t">${esc(i.title)}</span><button class="ghost small danger" data-act="delItem" data-id="${esc(i.id)}">删除</button></div><div class="sub">${esc(i.id)}${i.session_id ? ' · 源会话：' + esc(st[i.session_id] || i.session_id) : ' · 独立条目'} · ${esc(i.audience)}</div></div>`).join('') || '<span class="empty">空</span>';
}
async function delItem(id) { await api('/api/pool/items/' + encodeURIComponent(id), { method: 'DELETE' }); loadItems(); }
async function publish() {
  const sid = $('pubSession').value, mode = $('pubMode').value;
  const title = $('pubTitle').value || (cache.sessions.find(x => x.id === sid) || {}).title || '发布条目';
  const body = { title, session_id: sid || null, mode, text: $('pubText').value || null, audience: $('pubAudience').value || '*' };
  try {
    const r = await api('/api/pool/publish', { method: 'POST', body: JSON.stringify(body) });
    alert(r.linked ? '已更新该会话对应的读取池条目（1:1）' : '已发布独立条目');
    $('pubText').value = ''; loadItems();
  } catch (e) { alert(e.message); }
}
async function loadAgents() {
  const a = await api('/api/agents');
  $('agents').innerHTML = a.map(x => `<div class="item" style="cursor:default"><div class="row spread"><span class="t">${esc(x.participant_id)}</span>${x.revoked ? '<span class="pill gray">已吊销</span>' : `<button class="ghost small danger" data-act="revoke" data-pid="${esc(x.participant_id)}">吊销</button>`}</div></div>`).join('') || '<span class="empty">还没有 Agent</span>';
}
async function newAgent() {
  const r = await api('/api/agents', { method: 'POST', body: JSON.stringify({ name: $('agentName').value }) });
  prompt('令牌只显示一次，复制到该 Agent 的 MCP 配置 RELAY_AGENT_TOKEN：', r.token); loadAgents();
}
async function revoke(pid) { await api(`/api/agents/${encodeURIComponent(pid)}/revoke`, { method: 'POST' }); loadAgents(); }
function seat(v, side) { v = v.trim(); return v.startsWith('agent:') ? { side, kind: 'agent', participant_id: v } : { side, kind: 'web', platform: v }; }
async function newArena() {
  await api('/api/arenas', { method: 'POST', body: JSON.stringify({ mode: $('mode').value, topic: $('topic').value, seats: [seat($('seatA').value, '正方'), seat($('seatB').value, '反方')] }) });
  loadArenas();
}
async function loadArenas() {
  const a = await api('/api/arenas');
  $('arenas').innerHTML = a.map(x => `<div class="item" style="cursor:default"><div class="row spread"><span class="t">${esc(x.topic)}</span><span class="pill violet">${esc(x.mode)}</span></div><div class="row" style="margin-top:5px"><button class="ghost small" data-act="round" data-aid="${esc(x.id)}">下一轮</button><button class="ghost small" data-act="viewArena" data-aid="${esc(x.id)}">查看</button></div><div id="ar_${esc(x.id)}"></div></div>`).join('') || '<span class="empty">还没有对局</span>';
}
async function round(id) { try { await api(`/api/arenas/${encodeURIComponent(id)}/round`, { method: 'POST' }); } catch (e) { alert(e.message); } viewArena(id); }
async function viewArena(id) {
  const d = await api('/api/arenas/' + encodeURIComponent(id));
  $('ar_' + id).innerHTML = `<div class="sub">状态：${esc(d.round_status)}</div>` + d.turns.map(t => `<div class="msg assistant"><div class="who">第${esc(t.round)}轮 · ${esc(t.side)}</div>${esc(t.content)}</div>`).join('');
}
async function showAudit() { const a = await api('/api/audit'); $('audit').textContent = a.map(x => `${new Date(x.ts * 1000).toLocaleString()} ${x.actor} ${x.action} ${x.detail}`).join('\n'); }

/* ---------------- 账户页 ---------------- */
async function loadAccounts() {
  const d = await api('/api/platforms');
  $('accList').innerHTML = d.platforms.map(p => `
    <div class="card">
      <h3>🌐 ${esc(p.id)}
        <span class="spacer" style="flex:1"></span>
        <label class="ck" style="margin:0" title="开启后打开平台未登录时用凭据库代填（凭据用 scripts/add_credential.py 录入）">
          <input type="checkbox" ${p.autofill ? 'checked' : ''} data-act="autofill" data-pid="${esc(p.id)}"> autofill
        </label>
      </h3>
      ${(p.accounts || []).map(a => `<div class="item" style="cursor:default"><div class="row spread">
          <span><span class="pill ${a.cooldown_until ? 'amber' : 'cyan'}">${esc(a.account)}</span>${a.cooldown_until ? ' <span class="sub">冷却至 ' + esc(String(a.cooldown_until).slice(11, 16)) + '</span>' : ''}</span>
          <span class="row">
            <button class="ghost small" data-act="openP" data-pid="${esc(p.id)}" data-account="${esc(a.account)}">打开 / 登录</button>
            <button class="ghost small danger" data-act="delAccount" data-pid="${esc(p.id)}" data-account="${esc(a.account)}">移除</button>
          </span></div></div>`).join('')}
      <div class="row" style="margin-top:8px">
        <input id="acc_${esc(p.id)}" placeholder="新账号名" style="flex:1">
        <button class="btn small" data-act="addAccount" data-pid="${esc(p.id)}">登记账号</button>
        <button class="ghost small" data-act="showChats" data-pid="${esc(p.id)}">平台历史会话</button>
      </div>
      <div id="chats_${esc(p.id)}"></div>
    </div>`).join('');
}
async function saveAccounts(pid, accounts) { return api(`/api/platforms/${encodeURIComponent(pid)}/accounts`, { method: 'POST', body: JSON.stringify({ accounts }) }); }
async function addAccount(pid) {
  const v = $('acc_' + pid).value.trim(); if (!v) return;
  const now = (await api(`/api/platforms/${encodeURIComponent(pid)}/accounts`)).accounts;
  await saveAccounts(pid, [...new Set([...now, v])]);
  $('acc_' + pid).value = ''; loadAccounts();
}
async function delAccount(pid, name) {
  const now = (await api(`/api/platforms/${encodeURIComponent(pid)}/accounts`)).accounts;
  const next = now.filter(x => x !== name);
  if (!next.length) return alert('至少保留一个账号');
  await saveAccounts(pid, next); loadAccounts();
}
async function toggleAutofill(pid, on) { await api(`/api/platforms/${encodeURIComponent(pid)}/accounts`, { method: 'POST', body: JSON.stringify({ autofill: on }) }); }
async function openP(pid, account) { try { await api(`/api/platforms/${encodeURIComponent(pid)}/open`, { method: 'POST', body: JSON.stringify({ account, autofill: true }) }); } catch (e) { alert(e.message); } }
async function showChats(pid) {
  const box = $('chats_' + pid);
  const d = await api(`/api/platforms/${encodeURIComponent(pid)}/chats`).catch(e => { alert(e.message); return { chats: [] }; });
  box.innerHTML = d.chats.map(c => `<div class="item" data-act="resumeChat" data-pid="${esc(pid)}" data-url="${esc(encodeURIComponent(c.url || ''))}" data-title="${esc(c.title)}"><span class="t">${esc(c.title)}</span></div>`).join('') || '<span class="empty">未读到（probe 修正 chat_list_item）</span>';
}
async function resumeChat(pid, url, title) {
  const r = await api('/api/sessions/resume', { method: 'POST', body: JSON.stringify({ platform: pid, url: decodeURIComponent(url), title }) });
  cur = r.id; await loadSessionsData(); show('start'); openS(cur);
}

/* ---------------- 设置页 ---------------- */
let sumPresets = {};
async function loadSettings() {
  const s = await api('/api/settings');
  $('setOrder').value = (s.relay_order || []).join(', ');
  $('setCooldown').value = s.default_cooldown_minutes;
  $('setNear').value = s.near_threshold_chars;
  $('setTrunc').checked = !!s.relay_on_truncation;
  $('setTurns').value = s.every_n_turns;
  $('setRecent').value = s.recent_rounds_raw;
  $('setArenaTimeout').value = s.agent_turn_timeout;
  $('setBatch').checked = !!s.allow_batch_approve;
  $('setInfo').innerHTML = `监听 <b>${esc(s.host)}:${esc(s.port)}</b> · 数据目录 <b>${esc(s.data_dir)}</b><br>提示：host/port/浏览器承载在 config.yaml 修改后重启生效`;
  await loadSummarySettings();
}

/* ---------------- 设置页：摘要模型（第三方 API，A1） ---------------- */
async function loadSummarySettings() {
  const s = await api('/api/settings/summary');
  sumPresets = s.presets || {};
  $('sumProvider').value = s.provider || 'custom';
  $('sumBaseUrl').value = s.base_url || '';
  $('sumModel').value = s.model || '';
  $('sumTimeout').value = s.timeout_seconds ?? 25;
  $('sumKey').value = '';
  $('sumKeyState').textContent = s.has_key ? '已保存（密钥存系统凭据库，不回显）' : '未保存';
  $('sumTestResult').textContent = '';
}
function sumPreset() {
  /* 预设只用来填默认值，用户仍可改 */
  const p = sumPresets[$('sumProvider').value] || {};
  if (p.base_url) $('sumBaseUrl').value = p.base_url;
  if (p.model) $('sumModel').value = p.model;
}
function sumBody() {
  const body = { provider: $('sumProvider').value, base_url: $('sumBaseUrl').value.trim(),
    model: $('sumModel').value.trim(), timeout_seconds: +$('sumTimeout').value || 25 };
  const k = $('sumKey').value.trim();
  if (k) body.api_key = k;          // 密钥只写不读
  return body;
}
async function sumSave() {
  try {
    const s = await api('/api/settings/summary', { method: 'POST', body: JSON.stringify(sumBody()) });
    $('sumKey').value = '';
    $('sumKeyState').textContent = s.has_key ? '已保存（密钥存系统凭据库，不回显）' : '未保存';
    $('sumTestResult').textContent = '已保存并热更新，无需重启。';
  } catch (e) { fieldErrors('ferr_', e); }
}
async function sumTest() {
  $('sumTestResult').textContent = '测试中…';
  try {
    const r = await api('/api/settings/summary/test', { method: 'POST', body: JSON.stringify(sumBody()) });
    $('sumTestResult').textContent = r.ok
      ? `连接正常（${r.latency_ms} ms，模型 ${esc(r.model)}）` : `失败：${esc(r.error || '')}`;
  } catch (e) { $('sumTestResult').textContent = `失败：${esc(e.message)}`; }
}
async function sumClearKey() {
  if (!confirm('确定删除凭据库里的摘要 API 密钥？删除后摘要将回退到缓存摘要。')) return;
  try {
    const cur = await api('/api/settings/summary');
    // 先取当前配置原样回传，避免只带 clear_key 时把 base_url/model 重置成预设
    const body = { provider: cur.provider, base_url: cur.base_url, model: cur.model,
      timeout_seconds: cur.timeout_seconds, clear_key: true };
    const s = await api('/api/settings/summary', { method: 'POST', body: JSON.stringify(body) });
    $('sumKey').value = '';
    $('sumKeyState').textContent = s.has_key ? '已保存（密钥存系统凭据库，不回显）' : '未保存';
    $('sumTestResult').textContent = '';
  } catch (e) { alert(e.message); }
}
async function saveSettings() {
  const body = {
    relay_order: $('setOrder').value.split(',').map(x => x.trim()).filter(Boolean),
    default_cooldown_minutes: +$('setCooldown').value,
    relay_on_truncation: $('setTrunc').checked,
    near_threshold_chars: +$('setNear').value,
    every_n_turns: +$('setTurns').value,
    recent_rounds_raw: +$('setRecent').value,
    agent_turn_timeout: +$('setArenaTimeout').value,
    allow_batch_approve: $('setBatch').checked,
  };
  try { await api('/api/settings', { method: 'POST', body: JSON.stringify(body) }); alert('已保存并写入 config.yaml'); }
  catch (e) { alert(e.message); }
}

/* ---------------- 手动“总结并切换”三步弹窗（A4） ---------------- */
let hoFrom = '';
function hoStep(n) {
  [1, 2, 3].forEach(i => {
    $('hoStep' + i).classList.toggle('on', i === n);
    $('hoDot' + i).classList.toggle('on', i === n);
  });
}
async function hoOpen() {
  if (!cur) return alert('先选会话');
  hoFrom = `${(cache.s || {}).platform || '?'}:${(cache.s || {}).account || 'main'}`;
  $('hoModal').classList.add('on');
  hoStep(1); hoRefresh();
}
function hoClose() { $('hoModal').classList.remove('on'); }
async function hoRefresh() {
  $('hoGen').textContent = '正在生成摘要…';
  $('hoMeta').textContent = '';
  try {
    const r = await api(`/api/sessions/${encodeURIComponent(cur)}/summary/refresh`, { method: 'POST' });
    $('hoSummary').value = r.summary || '';
    const parts = [];
    if (r.source === 'api') parts.push('来源：刚刚由摘要 API 生成');
    if (r.source === 'cached') parts.push('来源：缓存摘要');
    if (r.source === 'none') parts.push('来源：无摘要');
    if (r.warning) parts.push('提示：' + esc(r.warning));
    $('hoMeta').innerHTML = parts.join(' · ');
    // 没配提供者时给一个去设置页的入口
    if (r.source === 'none') {
      $('hoMeta').innerHTML += ' <button class="ghost small" data-act="hoGoSettings" style="margin-left:6px">去设置页配置 →</button>';
    }
  } catch (e) {
    $('hoGen').textContent = '';
    $('hoMeta').innerHTML = `<span style="color:#f9a8d4">${esc(e.message)}</span>`;
  }
}
async function hoSaveSummary() {
  try {
    await api(`/api/sessions/${encodeURIComponent(cur)}/summary`, { method: 'PUT', body: JSON.stringify({ summary: $('hoSummary').value }) });
    $('hoMeta').innerHTML = '已保存修改。';
  } catch (e) { alert(e.message); }
}
function hoFillTargets() {
  const list = (cache.platforms || []).filter(p => p.enabled !== false);
  $('hoPlatform').innerHTML = list.map(p => `<option value="${esc(p.id)}">${esc(p.id)}</option>`).join('');
  hoFillAccounts();
}
function hoTo2() { hoFillTargets(); hoStep(2); }
function hoFillAccounts() {
  const pid = $('hoPlatform').value;
  const p = (cache.platforms || []).find(x => x.id === pid) || {};
  const curAcc = (cache.s || {}).account || 'main';
  const accs = (p.accounts || []).filter(a => !(pid === (cache.s || {}).platform && a.account === curAcc));
  $('hoAccount').innerHTML = accs.map(a =>
    `<option value="${esc(a.account)}">${esc(a.account)}${a.cooldown_until ? '（冷却至 ' + esc(String(a.cooldown_until).slice(11, 16)) + '）' : ''}</option>`).join('')
    || '<option value="">（无可用账号）</option>';
  hoProbeStatus();
}
/* 状态徽标：B2 的状态接口；A4 阶段接口尚未上线时静默降级 */
async function hoProbeStatus() {
  const pid = $('hoPlatform').value, acc = $('hoAccount').value;
  if (!pid || !acc) { $('hoStatus').textContent = ''; return; }
  $('hoStatus').textContent = '检测状态中…';
  try {
    const r = await api(`/api/platforms/${encodeURIComponent(pid)}/status?account=${encodeURIComponent(acc)}`);
    const bits = [];
    bits.push(r.launched ? '<span class="badge ok">已打开</span>' : '<span class="badge off">未启动</span>');
    bits.push(r.logged_in ? '<span class="badge ok">已登录</span>' : (r.launched ? '<span class="badge warn">已打开未登录</span>' : ''));
    if (r.cooldown_until) bits.push(`<span class="badge warn">冷却到 ${esc(String(r.cooldown_until).slice(11, 16))}</span>`);
    $('hoStatus').innerHTML = bits.filter(Boolean).join('');
  } catch (e) { $('hoStatus').innerHTML = '<span class="badge off">状态未知（接口未就绪）</span>'; }
}
async function hoTo3() {
  const acc = $('hoAccount').value;
  if (!acc) return alert('目标平台没有可用账号');
  $('hoPreview').textContent = '正在生成预览…';
  $('hoPkg').textContent = '';
  try {
    const p = new URLSearchParams({ platform: $('hoPlatform').value, account: acc, recent_rounds: $('hoRounds').value });
    const r = await api(`/api/sessions/${encodeURIComponent(cur)}/handoff/preview?` + p.toString());
    const modeText = { inline: '直接发送', upload_md: '上传 .md 附件', trimmed: '会被裁剪（保留摘要与问题，回放裁短）' };
    const warn = $('hoSummary').value.includes('暂无摘要') || $('hoSummary').value === '' ? '（当前无摘要，交接时只附带最近原文）' : '';
    $('hoPreview').innerHTML = `共 <b>${r.chars}</b> 字 · 投递方式：<b>${modeText[r.mode] || esc(r.mode)}</b>${r.target_limit ? '（目标上限 ' + r.target_limit + ' 字）' : ''}${warn}`;
    $('hoPkg').textContent = r.package;
    hoStep(3);
  } catch (e) { alert(e.message); }
}
async function hoGo() {
  const body = { platform: $('hoPlatform').value, account: $('hoAccount').value,
    question: $('hoQuestion').value.trim() || null, recent_rounds: +$('hoRounds').value };
  const btn = document.querySelector('[data-act="hoGo"]'); btn.disabled = true;
  try {
    const r = await api(`/api/sessions/${encodeURIComponent(cur)}/handoff`, { method: 'POST', body: JSON.stringify(body) });
    hoClose();
    if (r.warning) alert(r.warning);
    $('chat').innerHTML += `<div class="relay manual">已手动切换：${esc(hoFrom)} → ${esc(r.platform)}:${esc(r.account)}${r.warning ? '（' + esc(r.warning) + '）' : ''}</div>`;
    $('chat').scrollTop = $('chat').scrollHeight;
    openS(cur);
  } catch (e) { alert(e.message); }
  finally { btn.disabled = false; }
}
function hoGoSettings() { hoClose(); show('settings'); }

/* ---------------- 事件委托（无内联 handler，CSP 可只允许同源脚本） ---------------- */
const ACTIONS = {
  nav: d => show(d.nav),
  newSession, ask, publish, newAgent, newArena, showAudit, saveSettings,
  sumSave, sumTest, sumClearKey,
  hoOpen, hoClose, hoRefresh, hoSaveSummary, hoTo2, hoBack1: () => hoStep(1), hoBack2: () => hoStep(2),
  hoTo3, hoGo, hoGoSettings,
  openS: d => openS(d.id),
  decide: d => decide(d.id, d.d),
  approveRound: d => approveRound(d.aid),
  delItem: d => delItem(d.id),
  revoke: d => revoke(d.pid),
  round: d => round(d.aid),
  viewArena: d => viewArena(d.aid),
  openP: d => openP(d.pid, d.account),
  delAccount: d => delAccount(d.pid, d.account),
  addAccount: d => addAccount(d.pid),
  showChats: d => showChats(d.pid),
  resumeChat: d => resumeChat(d.pid, d.url, d.title),
};
document.addEventListener('click', e => {
  const el = e.target.closest('[data-act]');
  if (!el) return;
  const fn = ACTIONS[el.dataset.act];
  if (fn) { e.preventDefault(); fn(el.dataset); }
});
document.addEventListener('change', e => {
  const el = e.target.closest('[data-act="autofill"]');
  if (el) { toggleAutofill(el.dataset.pid, el.checked); return; }
  if (e.target.closest('[data-act="sumPreset"]')) sumPreset();
  if (e.target.id === 'hoPlatform') hoFillAccounts();
  if (e.target.id === 'hoAccount') hoProbeStatus();
});
$('q').addEventListener('keydown', e => { if (e.key === 'Enter' && e.ctrlKey) ask(); });

/* ---------------- 启动 ---------------- */
show('start');
fetch('/api/platforms').then(r => r.json()).then(() => { $('connInfo').textContent = '已连接'; }).catch(() => { $('connInfo').textContent = '控制台未连接'; });
setInterval(() => { if ($('page-pool').classList.contains('on')) loadApprovals(); }, 3000);
