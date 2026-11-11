'use strict';
const $ = id => document.getElementById(id);
let token = sessionStorage.getItem('voteAdminToken') || '';
let state = null;
let adminState = null;
let identity = null;
let verifiedCode = '';
const phases = {draft: '尚未开始', open: '投票进行中', closed: '投票已暂停'};

function message(text, error = false) {
  $('message').textContent = text;
  $('message').className = `notice ${error ? 'error' : 'success'}`;
}
async function api(path, body) {
  // Resolve beside this page so both / and /vote/ deployments work.
  const response = await fetch(path.replace(/^\//, ''), {
    method: body === undefined ? 'GET' : 'POST',
    headers: {'Content-Type': 'application/json', ...(token ? {Authorization: `Bearer ${token}`} : {})},
    ...(body === undefined ? {} : {body: JSON.stringify(body)}),
    cache: 'no-store'
  });
  let data;
  try { data = await response.json(); } catch { throw new Error('无法连接投票服务，请通过服务器地址打开页面'); }
  if (!response.ok) {
    if (response.status === 401 && path.startsWith('/api/admin/') && !path.endsWith('/login')) logoutLocal();
    throw new Error(data.error || '操作失败');
  }
  return data;
}
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
async function action(button, callback) {
  button.disabled = true;
  try { await callback(); } catch (error) { message(error.message || '网络连接失败，请重试', true); }
  finally { button.disabled = false; updateBallot(); }
}
function updateBallot() {
  $('ballot').hidden = !identity;
  if (!identity) return;
  $('identity').textContent = `${identity.name} · ${identity.team_name}${identity.voted ? ' · 已投票' : ''}`;
  const disabled = identity.voted || state?.phase !== 'open';
  $('submitButton').disabled = disabled;
  $('vote1').disabled = disabled;
  $('vote2').disabled = disabled;
}
function fillChoices(changed) {
  const selected = {vote1: $('vote1').value, vote2: $('vote2').value};
  if (selected.vote1 && selected.vote1 === selected.vote2) selected[changed === 'vote2' ? 'vote1' : 'vote2'] = '';
  for (const id of ['vote1', 'vote2']) {
    const other = id === 'vote1' ? 'vote2' : 'vote1';
    $(id).replaceChildren(new Option('请选择队伍', ''));
    for (const team of state.teams) {
      if (team.id !== identity.team_id && String(team.id) !== selected[other]) $(id).add(new Option(team.name, String(team.id)));
    }
    $(id).value = selected[id];
  }
}
for (const id of ['vote1', 'vote2']) $(id).addEventListener('change', () => { if (identity) fillChoices(id); });
async function refresh() {
  state = await api('/api/status');
  $('status').textContent = `${phases[state.phase]} · 已投 ${state.submitted} / ${state.total} 人`;
  if (identity) fillChoices();
  updateBallot();
}
function renderRanking() {
  $('rankList').replaceChildren();
  if (!adminState.ranking.length) $('rankList').append(el('p', '还没有录入队伍', 'muted'));
  let previous = null;
  let rank = 0;
  adminState.ranking.forEach((team, i) => {
    if (previous === null || Math.abs(team.score - previous) > 1e-10) rank = i + 1;
    previous = team.score;
    const row = el('div', undefined, 'item rank');
    row.append(el('span', `第 ${rank} 名 · ${team.name}`), el('strong', `${team.score.toFixed(4)}（${team.vote_count} 票）`));
    $('rankList').append(row);
  });
}
function logoutLocal() {
  token = '';
  adminState = null;
  sessionStorage.removeItem('voteAdminToken');
  $('teamList').replaceChildren();
  $('rankList').replaceChildren();
  $('adminPanel').hidden = true;
  $('loginCard').hidden = false;
}
async function refreshAdmin() {
  const requestedToken = token;
  const data = await api('/api/admin/state');
  if (!token || token !== requestedToken) return;
  adminState = data;
  renderRanking();
  $('adminPanel').hidden = false;
  $('loginCard').hidden = true;
  $('setupCard').hidden = adminState.phase === 'open';
  $('phaseButton').textContent = adminState.phase === 'open' ? '暂停投票' : '开放投票';
  $('teamList').replaceChildren();
  if (!adminState.teams.length) $('teamList').append(el('p', '请先添加至少三支队伍。', 'muted'));
  for (const team of adminState.teams) {
    const box = el('div', undefined, 'item');
    const heading = el('div', undefined, 'row');
    heading.append(el('h3', `${team.name} · ${team.n} 人`));
    if (adminState.phase === 'draft') {
      const remove = el('button', '删除队伍', 'danger');
      remove.addEventListener('click', () => {
        if (!confirm(`删除“${team.name}”及其成员投票码？`)) return;
        action(remove, async () => {
          await api('/api/admin/delete-team', {id: team.id});
          await refreshAdmin(); await refresh();
        });
      });
      heading.append(remove);
    }
    box.append(heading);
    for (const member of adminState.members.filter(m => m.team_id === team.id)) {
      const row = el('div');
      row.append(el('span', `${member.name} · ${member.voted ? '已投票' : '未投票'} · `), el('code', member.code));
      box.append(row);
    }
    $('teamList').append(box);
  }
}
document.querySelectorAll('[data-page]').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('[data-page]').forEach(b => b.classList.toggle('active', b === button));
  document.querySelectorAll('.page').forEach(p => { p.hidden = p.id !== button.dataset.page; });
  message('');
  action(button, async () => {
    await refresh();
    if (button.dataset.page === 'admin' && token) await refreshAdmin();
  });
}));
function resetBallot() {
  identity = null;
  verifiedCode = '';
  for (const id of ['vote1', 'vote2']) $(id).replaceChildren(new Option('请选择队伍', ''));
  updateBallot();
}
$('code').addEventListener('input', () => { resetBallot(); message(''); });
$('code').addEventListener('change', resetBallot);
window.addEventListener('pageshow', () => { resetBallot(); $('code').value = ''; });
$('identifyForm').addEventListener('submit', event => {
  event.preventDefault();
  action($('identifyButton'), async () => {
    resetBallot();
    const code = $('code').value.trim();
    const member = await api('/api/identify', {code});
    if ($('code').value.trim() !== code) return;
    await refresh();
    if ($('code').value.trim() !== code) return;
    verifiedCode = code; identity = member;
    fillChoices(); updateBallot();
    message(identity.voted ? '你已完成投票，无需重复提交。' : state.phase === 'open' ? '身份验证成功，请选择两支队伍。' : '身份验证成功，请等待组织者开放投票。');
  });
});
$('voteForm').addEventListener('submit', event => {
  event.preventDefault();
  if (!identity || identity.voted) return;
  if (!$('vote1').value || !$('vote2').value) return message('请完整选择两支队伍', true);
  const first = Number($('vote1').value), second = Number($('vote2').value);
  if (first === second || [first, second].includes(identity.team_id)) return message('请选择两支不同的其他队伍', true);
  const member = identity;
  const code = verifiedCode;
  action($('submitButton'), async () => {
    const result = await api('/api/vote', {code, vote1: first, vote2: second});
    member.voted = true;
    if (identity === member) {
      for (const id of ['vote1', 'vote2']) $(id).value = '';
    }
    message(result.message);
    updateBallot();
    await refresh();
  });
});
$('loginForm').addEventListener('submit', event => {
  event.preventDefault();
  action(event.submitter, async () => {
    const data = await api('/api/admin/login', {password: $('password').value});
    token = data.token; sessionStorage.setItem('voteAdminToken', token); $('password').value = '';
    await refreshAdmin(); message('管理员登录成功');
  });
});
$('logout').addEventListener('click', () => action($('logout'), async () => {
  try { await api('/api/admin/logout', {}); } finally { logoutLocal(); }
}));
$('teamForm').addEventListener('submit', event => {
  event.preventDefault();
  action(event.submitter, async () => {
    const members = $('memberNames').value.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
    await api('/api/admin/teams', {name: $('teamName').value.trim(), members});
    $('teamForm').reset(); await refreshAdmin(); await refresh(); message('队伍已添加，成员投票码已生成');
  });
});
$('phaseButton').addEventListener('click', () => {
  if (!adminState) return;
  const phase = adminState.phase === 'open' ? 'closed' : 'open';
  if (!confirm(phase === 'open' ? '确认开放投票？投票期间如需追加队伍，请先暂停。' : '确认暂停投票？暂停后可追加新队伍。')) return;
  action($('phaseButton'), async () => {
    await api('/api/admin/phase', {phase}); await refreshAdmin(); await refresh(); message(phases[phase]);
  });
});
$('refreshResults').addEventListener('click', () => action($('refreshResults'), refreshAdmin));
$('refreshAdmin').addEventListener('click', () => action($('refreshAdmin'), async () => { await refreshAdmin(); await refresh(); }));
$('exportCodes').addEventListener('click', () => action($('exportCodes'), async () => {
  await refreshAdmin();
  const cell = value => `"${String(value).replace(/^[=+@\-\t\r\n]/, "'$&").replace(/"/g, '""')}"`;
  const rows = [['队伍', '成员', '投票码', '投票状态']];
  for (const m of adminState.members) rows.push([adminState.teams.find(t => t.id === m.team_id).name, m.name, m.code, m.voted ? '已投票' : '未投票']);
  const blob = new Blob(['\ufeff' + rows.map(r => r.map(cell).join(',')).join('\r\n')], {type: 'text/csv;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const link = el('a'); link.href = url; link.download = '成员投票码.csv'; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}));
if (location.protocol === 'file:') {
  $('status').textContent = '请启动 server.py，并通过 http://服务器地址:8000 打开页面。';
  message('直接打开本地文件无法连接共享投票服务。', true);
} else {
  refresh().catch(error => { $('status').textContent = '连接服务器失败'; message(error.message, true); });
  setInterval(() => refresh().catch(() => {}), 15000);
}
