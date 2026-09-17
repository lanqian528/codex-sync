const $ = id => document.getElementById(id);
let csrf = '', revision = '', saved = null, saving = false;
const errors = {
  400: '配置格式不正确，请检查 HTTPS 地址、模式和 API Key。',
  401: '访问密钥不正确，或登录已过期。',
  403: '安全校验未通过，请刷新页面后重新登录。',
  409: '配置已在其他页面修改。请重新读取后再保存。',
  428: '页面版本或配置标识已失效，请刷新页面后重新读取配置。',
  503: '服务暂不可用。请确认 ACCESS_KEY 已设置、私有存储已连接，再重试。',
};
function message(text, error = false) { $('message').textContent = text; $('message').classList.toggle('error', error); $('message').hidden = false; }
function clearMessage() { $('message').hidden = true; }
function loggedOut() {
  csrf = ''; revision = ''; saved = null;
  $('api-key').value = ''; $('base-url').value = ''; $('access-key').value = '';
  $('access-key').type = 'password'; $('show-access').textContent = '显示';
  $('dashboard').hidden = true; $('login-view').hidden = false;
}
async function request(path, options = {}) {
  const res = await fetch(path, { credentials: 'same-origin', cache: 'no-store', ...options, headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, ...(options.headers || {}) } });
  if (!res.ok) {
    if (res.status === 401) loggedOut();
    throw Error(errors[res.status] || '操作未完成，请稍后重试。');
  }
  return { data: await res.json() };
}
function currentMode() { return document.querySelector('input[name=mode]:checked').value; }
function changed() { return saved && (currentMode() !== saved.mode || $('base-url').value !== saved.base_url || $('api-key').value !== ''); }
function updateState() {
  const dirty = changed();
  $('save').disabled = !dirty || saving || !saved?.storage_ready || !revision;
  $('change-label').textContent = dirty ? '有更改待保存' : '与云端配置一致';
  $('saved-badge').textContent = dirty ? '待保存' : '已读取';
}
function render(data) {
  if (typeof data.revision !== 'string' || !data.revision) throw Error('未读取到有效配置标识，请刷新页面后重试。');
  saved = data; revision = data.revision;
  document.querySelector(`input[name=mode][value="${data.mode === 'api' ? 'api' : 'pro'}"]`).checked = true;
  $('base-url').value = data.base_url;
  $('api-key').value = ''; $('api-key').type = 'password'; $('show-api').textContent = '显示';
  $('key-status').textContent = data.has_api_key ? 'Key 已保存' : '尚未设置 Key';
  $('storage-warning').hidden = data.storage_ready;
  $('login-view').hidden = true; $('dashboard').hidden = false;
  $('endpoint').textContent = location.origin + '/config.json';
  updateState();
}
async function load() {
  const result = await request('/api/admin');
  render(result.data);
}
$('login-form').addEventListener('submit', async event => {
  event.preventDefault(); clearMessage(); $('login-button').disabled = true;
  try {
    const result = await request('/api/session', { method: 'POST', body: JSON.stringify({ access_key: $('access-key').value }) });
    $('access-key').value = ''; csrf = result.data.csrf;
    await load();
  } catch (error) { message(error.message === 'Failed to fetch' ? '连接失败，请检查网络后重试。' : error.message, true); }
  finally { $('login-button').disabled = false; }
});
$('config-form').addEventListener('input', updateState);
$('config-form').addEventListener('submit', async event => {
  event.preventDefault(); if (saving || !saved?.storage_ready) return;
  clearMessage();
  const data = { mode: currentMode(), base_url: $('base-url').value.trim() };
  if ($('api-key').value) data.api_key = $('api-key').value;
  if (data.mode === 'api' && !data.api_key && !saved.has_api_key) { message('API 模式需要先填写 API Key。', true); return; }
  if (!data.base_url.startsWith('https://')) { message('API 地址必须使用 HTTPS。', true); return; }
  saving = true; $('save').textContent = '正在保存…'; updateState();
  try {
    const result = await request('/api/admin', { method: 'PUT', headers: { 'X-Config-Revision': revision }, body: JSON.stringify(data) });
    render(result.data);
    message('配置已保存。设备将在下一轮空闲同步时跟随；请重新打开 Codex 并使用新会话。');
  } catch (error) { message(error.message === 'Failed to fetch' ? '未能确认保存结果。请重新读取云端配置后再试。' : error.message, true); }
  finally { saving = false; $('save').textContent = '保存配置 ↗'; updateState(); }
});
$('reload').addEventListener('click', async () => {
  if (changed() && !window.confirm('重新读取会放弃当前尚未保存的输入，是否继续？')) return;
  clearMessage(); $('reload').disabled = true;
  try { await load(); message('已重新读取云端配置。'); } catch (error) { message(error.message, true); }
  finally { $('reload').disabled = false; }
});
$('logout').addEventListener('click', async () => {
  try { await request('/api/session', { method: 'DELETE' }); loggedOut(); clearMessage(); }
  catch (error) { message('退出未完成，请检查网络后重试。', true); }
});
for (const [button, field] of [['show-access', 'access-key'], ['show-api', 'api-key']]) {
  $(button).addEventListener('click', () => { const show = $(field).type === 'password'; $(field).type = show ? 'text' : 'password'; $(button).textContent = show ? '隐藏' : '显示'; });
}
$('copy-url').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('endpoint').textContent); message('客户端地址已复制。'); }
  catch { message('无法自动复制，请选中上方地址手动复制。', true); }
});
(async () => {
  try { const result = await request('/api/session'); csrf = result.data.csrf; await load(); }
  catch (error) { loggedOut(); if (!error.message.includes('登录已过期')) message(error.message, true); }
})();
