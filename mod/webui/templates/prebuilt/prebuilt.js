/* ──── 预编译部署 tab ────
 * 构建来源切换 / 镜像测速·选择 / 预编译包列表 + 下载(进度轮询)。
 * 依赖自检已移到仪表盘。无参端点走 fragment 协议;下载 / 测速 / 选镜像走 register_route。
 */

const PB_KEYS = {
  list:           '__lgtbot_prebuilt_list',
  state:          '__lgtbot_prebuilt_state',
  switchLocal:    '__lgtbot_prebuilt_switch_local',
  switchPrebuilt: '__lgtbot_prebuilt_switch_prebuilt',
};
const PB_DOWNLOAD_ROUTE = '/api/ext/lgtbot/prebuilt/download';
const PB_TESTMIRRORS_ROUTE = '/api/ext/lgtbot/prebuilt/test-mirrors';
const PB_MIRROR_ROUTE = '/api/ext/lgtbot/prebuilt/mirror';
const PB_UPLOAD_ROUTE = '/api/ext/lgtbot/prebuilt/upload';
const PB_POLL_MS = 1000;

let _pbPollTimer = null;
let _pbAssets = [];
let _pbMirrors = [];              // 测速结果 [{mirror, latency_ms, success}]
let _pbCustoms = [];              // 用户自定义镜像前缀
let _pbSelected = null;           // 选定镜像:null=未选 / ''=直连 / 'prefix'
let _pbMirrorExpanded = true;     // 镜像列表是否展开(未选默认展开,选后折叠)
let _pbListLoaded = false;

/* ──── 通用 ──── */
async function pbCallAction(key) {
  const r = await fetch(apiUrl(key), { cache: 'no-store' });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
  const el = doc.getElementById('result');
  if (!el) throw new Error('响应不含 #result');
  return JSON.parse(el.textContent);
}
async function pbCallRoute(path, { method = 'GET', body = null } = {}) {
  const opt = { method, cache: 'no-store' };
  if (body != null) { opt.headers = { 'Content-Type': 'application/json' }; opt.body = JSON.stringify(body); }
  const r = await fetch(path + (TOKEN_QS || ''), opt);
  let data = {};
  try { data = await r.json(); } catch (e) {}
  if (!r.ok && !data.message) throw new Error('HTTP ' + r.status);
  return data;
}
function pbFmtSize(b) {
  b = b || 0;
  if (b >= 1048576) return (b / 1048576).toFixed(1) + ' MB';
  if (b >= 1024) return (b / 1024).toFixed(0) + ' KB';
  return b + ' B';
}
function pbFmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return escapeHtml(iso);
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' +
         String(d.getDate()).padStart(2, '0') + ' ' +
         String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}
function pbShowMsg(text, kind) {
  const el = document.getElementById('pb-list-msg');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'pb-msg dash-msg-' + (kind || 'info');
}
const pbMirrorLabel = (m) => (m ? m : 'GitHub 直连');

/* ──── 构建来源 ──── */
function pbRenderMode(mode) {
  if (!mode) return;
  const badge = document.getElementById('pb-mode-badge');
  if (badge) {
    const running = mode.running === 'prebuilt' ? '预编译包' : '本地编译';
    badge.textContent = '当前运行：' + running;
    badge.className = 'dash-badge ' + (mode.running === 'prebuilt' ? 'dash-badge-ok' : 'dash-badge-warn');
  }
  const note = document.getElementById('pb-switch-note');
  if (note) {
    note.textContent = (mode.selected !== mode.running)
      ? ('已选「' + (mode.selected === 'prebuilt' ? '预编译包' : '本地编译') + '」，重启 LGTBot 后生效。')
      : '';
  }
  const usePb = document.getElementById('pb-use-prebuilt');
  if (usePb) usePb.disabled = !mode.prebuilt_installed;
}

async function pbSwitch(usePrebuilt) {
  const target = usePrebuilt ? '预编译包' : '本地编译';
  const ok = await dashConfirm('切换构建来源为「' + target + '」?\n\n切换后需重启 LGTBot 才会生效。', { level: 'warn' });
  if (!ok) return;
  try {
    const data = await pbCallAction(usePrebuilt ? PB_KEYS.switchPrebuilt : PB_KEYS.switchLocal);
    if (data.success) {
      pbRenderMode(data.mode_info);
      if (typeof dashAlert === 'function') dashAlert(data.message || '已切换，重启后生效');
    } else {
      if (typeof dashAlert === 'function') dashAlert(data.message || '切换失败');
    }
  } catch (e) { pbShowMsg('❌ ' + e.message, 'err'); }
}

/* ──── 镜像:测速 / 自定义 / 选择 / 折叠 ──── */
function pbRenderMirrorUI() {
  const summary = document.getElementById('pb-mirror-summary');
  const list = document.getElementById('pb-mirror-list');
  const collapsed = (_pbSelected !== null) && !_pbMirrorExpanded;
  if (collapsed) {
    summary.style.display = '';
    document.getElementById('pb-mirror-summary-name').textContent = pbMirrorLabel(_pbSelected);
    list.style.display = 'none';
  } else {
    summary.style.display = 'none';
    list.style.display = '';
    pbRenderMirrorList();
  }
}
function pbRenderMirrorList() {
  const list = document.getElementById('pb-mirror-list');
  if (!_pbMirrors.length) {
    list.innerHTML = '<div class="pb-mirror-empty">点「📶 测速」按延迟排序后进行选择，或「➕ 自定义镜像」添加自己的代理。</div>';
    return;
  }
  list.innerHTML = _pbMirrors.map(m => {
    const chosen = (m.mirror === _pbSelected);
    const lat = m.success ? (m.latency_ms + ' ms') : '超时';
    return '<div class="pb-mirror-row' + (chosen ? ' pb-mirror-chosen' : '') + '">' +
      '<span class="pb-mirror-name">' + escapeHtml(pbMirrorLabel(m.mirror)) + '</span>' +
      (chosen ? '<span class="pb-mirror-chosen-tag">已选</span>' : '') +
      '<span class="pb-mirror-lat ' + (m.success ? 'pb-mirror-ok' : 'pb-mirror-bad') + '">' + lat + '</span>' +
      '<button class="dash-btn dash-btn-small pb-pick" data-m="' + escapeHtml(m.mirror) + '"' +
        (chosen ? ' disabled' : '') + '>选择</button></div>';
  }).join('');
  list.querySelectorAll('.pb-pick').forEach(b => b.addEventListener('click', () => pbSelectMirror(b.dataset.m || '')));
}
async function pbTestMirrors() {
  const btn = document.getElementById('pb-test-mirrors');
  if (btn) { btn.disabled = true; btn.textContent = '📶 测速中…'; }
  try {
    const data = await pbCallRoute(PB_TESTMIRRORS_ROUTE, { method: 'POST', body: { customs: _pbCustoms } });
    if (data.success) {
      _pbMirrors = data.mirrors || [];
      if (data.selected != null) _pbSelected = data.selected;
      _pbMirrorExpanded = true;         // 测速完展开让用户挑
      pbRenderMirrorUI();
    } else {
      pbShowMsg('❌ 测速失败：' + (data.message || ''), 'err');
    }
  } catch (e) { pbShowMsg('❌ 测速失败：' + e.message, 'err'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = '📶 测速'; } }
}
async function pbAddCustomMirror() {
  if (typeof dashPrompt !== 'function') return;
  const v = await dashPrompt('输入自定义镜像前缀（拼在 github URL 前），例如：\nhttps://ghproxy.example/', {});
  if (v == null) return;
  const m = v.trim();
  if (!m) return;
  if (!_pbCustoms.includes(m)) _pbCustoms.push(m);
  pbTestMirrors();                      // 重新测速,把自定义一起纳入
}
async function pbSelectMirror(m) {
  try {
    const data = await pbCallRoute(PB_MIRROR_ROUTE, { method: 'POST', body: { mirror: m } });
    _pbSelected = (data.selected != null) ? data.selected : m;
    _pbMirrorExpanded = false;          // 选完自动折叠,只显示已选
    pbRenderMirrorUI();
  } catch (e) { pbShowMsg('❌ ' + e.message, 'err'); }
}

/* ──── 包列表 ──── */
function pbRenderList(data) {
  const body = document.getElementById('pb-table-body');
  const plat = document.getElementById('pb-local-plat');
  if (plat && data.local) plat.textContent = (data.local.os || '?') + ' · py' + (data.local.python || '?');
  if (!body) return;
  if (!data.success) {
    body.innerHTML = '<tr class="pb-empty"><td colspan="6">' + escapeHtml(data.error || data.message || '获取失败') + '</td></tr>';
    return;
  }
  _pbAssets = data.assets || [];
  if (!_pbAssets.length) {
    body.innerHTML = '<tr class="pb-empty"><td colspan="6">远程暂无预编译包</td></tr>';
    return;
  }
  body.innerHTML = _pbAssets.map(a => {
    const tags = [];
    if (a.is_latest) tags.push('<span class="pb-tag pb-tag-latest">最新</span>');
    tags.push(a.matches_local
      ? '<span class="pb-tag pb-tag-match">本机匹配</span>'
      : '<span class="pb-tag pb-tag-mismatch">不匹配</span>');
    if (a.installed) tags.push('<span class="pb-tag pb-tag-installed">已安装</span>');
    const btn = '<button class="dash-btn dash-btn-small pb-dl" data-name="' + escapeHtml(a.name) + '">⬇ 下载</button>';
    return '<tr class="' + (a.matches_local ? 'pb-row-match' : '') + '">' +
           '<td>' + escapeHtml(a.os || '') + '</td>' +
           '<td class="dash-mono">' + escapeHtml(a.python_tag || '') + '</td>' +
           '<td class="dash-mono">' + pbFmtSize(a.size) + '</td>' +
           '<td class="dash-mono">' + pbFmtTime(a.updated_at) + '</td>' +
           '<td>' + tags.join(' ') + '</td>' +
           '<td>' + btn + '</td></tr>';
  }).join('');
  body.querySelectorAll('.pb-dl').forEach(b => b.addEventListener('click', () => pbDownload(b.dataset.name)));
}
async function pbRefreshList() {
  pbShowMsg('获取远程列表……', 'info');
  try {
    const data = await pbCallAction(PB_KEYS.list);
    pbRenderList(data);
    pbShowMsg(data.success ? '' : ('❌ ' + (data.message || data.error || '获取失败')), data.success ? 'info' : 'err');
  } catch (e) { pbShowMsg('❌ ' + e.message, 'err'); }
}

/* ──── 下载进度 ──── */
function pbRenderProgress(st) {
  const wrap = document.getElementById('pb-progress-wrap');
  if (!wrap) return;
  if (!st || (!st.running && st.stage !== 'done' && st.stage !== 'error')) { wrap.style.display = 'none'; return; }
  wrap.style.display = 'block';
  const pct = st.progress || 0;
  document.getElementById('pb-progress-fill').style.width = pct + '%';
  document.getElementById('pb-progress-pct').textContent = pct + '%';
  const label = { download: '下载中……', verify: '校验并安装中……', done: '✅ 完成', error: '❌ 失败' }[st.stage] || '处理中……';
  document.getElementById('pb-progress-label').textContent = (st.asset ? st.asset + ' — ' : '') + label;
  let sub = '';
  if (st.stage === 'download' && st.total) sub = pbFmtSize(st.downloaded) + ' / ' + pbFmtSize(st.total);
  else if (st.stage === 'error') sub = st.error || '';
  document.getElementById('pb-progress-sub').textContent = sub;
}
function pbStartPoll() { if (!_pbPollTimer) _pbPollTimer = setInterval(pbPollOnce, PB_POLL_MS); }
function pbStopPoll() { if (_pbPollTimer) { clearInterval(_pbPollTimer); _pbPollTimer = null; } }
async function pbPollOnce() {
  let st;
  try { st = (await pbCallAction(PB_KEYS.state)).state || {}; } catch (e) { return; }
  pbRenderProgress(st);
  if (!st.running) {
    pbStopPoll();
    if (st.stage === 'done') {
      pbShowMsg('✅ 已下载安装。到「构建来源」点「📦 用预编译包」并重启 LGTBot 生效。', 'ok');
      pbRefreshList();
      if (typeof dashAlert === 'function')
        dashAlert('预编译包已下载安装完成。\n到本页「🔀 构建来源」点「📦 用预编译包」切换，再点右上角「🔁 重启 LGTBot」生效。');
    } else if (st.stage === 'error') {
      pbShowMsg('❌ 下载失败：' + (st.error || ''), 'err');
    }
  }
}
async function pbDownload(name) {
  if (!name) return;
  const asset = _pbAssets.find(a => a.name === name) || {};
  let warn = '确认下载预编译包「' + name + '」?\n\n';
  if (!asset.matches_local) warn += '⚠️ 该包与本机发行版 / Python 不匹配，安装后可能无法加载。\n';
  warn += '包较大，下载期间请勿关闭面板；完成后需切换到预编译并重启生效。';
  const ok = await dashConfirm(warn, { level: asset.matches_local ? 'warn' : 'danger' });
  if (!ok) return;
  const body = { name };
  if (_pbSelected !== null) body.mirror = _pbSelected;
  try {
    const data = await pbCallRoute(PB_DOWNLOAD_ROUTE, { method: 'POST', body });
    if (data.started || data.success) {
      pbShowMsg('已开始下载……', 'info');
      pbRenderProgress({ running: true, stage: 'download', asset: name, progress: 0 });
      pbStartPoll();
    } else {
      pbShowMsg('❌ ' + (data.message || '启动下载失败'), 'err');
    }
  } catch (e) { pbShowMsg('❌ ' + e.message, 'err'); }
}

/* ──── 手动上传本地包 ──── */
/* 上传用独立的「只渲染进度」轮询器:上传是单个长 POST,完成与否以该请求的响应
   为准,轮询只负责在上传期间刷新进度条,不做 done/error 收尾(避免与响应重复)。 */
let _pbUploadPoll = null;
function pbUploadStartPoll() {
  if (_pbUploadPoll) return;
  _pbUploadPoll = setInterval(async () => {
    try { pbRenderProgress((await pbCallAction(PB_KEYS.state)).state || {}); } catch (e) {}
  }, PB_POLL_MS);
}
function pbUploadStopPoll() { if (_pbUploadPoll) { clearInterval(_pbUploadPoll); _pbUploadPoll = null; } }

async function pbUpload(file) {
  if (!file) return;
  const ok = await dashConfirm(
    '确认上传并安装「' + file.name + '」？\n\n' +
    '请确保它是与本机发行版 / Python 匹配的预编译包 zip（含 manifest.json）。\n' +
    '安装后需到「🔀 构建来源」切换「📦 用预编译包」并重启 LGTBot 生效。',
    { level: 'warn' }
  );
  if (!ok) return;
  const fd = new FormData();
  fd.append('file', file);
  pbShowMsg('上传中……', 'info');
  pbRenderProgress({ running: true, stage: 'upload', asset: file.name, progress: 0 });
  pbUploadStartPoll();
  try {
    const r = await fetch(PB_UPLOAD_ROUTE + (TOKEN_QS || ''), { method: 'POST', body: fd, cache: 'no-store' });
    let data = {};
    try { data = await r.json(); } catch (e) {}
    pbUploadStopPoll();
    if (data.success) {
      pbRenderProgress({ stage: 'done', progress: 100 });
      pbShowMsg('✅ ' + (data.message || '上传包已安装'), 'ok');
      pbRefreshList();
      if (typeof dashAlert === 'function')
        dashAlert('上传包已安装。\n到「🔀 构建来源」点「📦 用预编译包」，再点右上角「🔁 重启 LGTBot」生效。');
    } else {
      pbRenderProgress({ stage: 'error', error: data.message || '安装失败' });
      pbShowMsg('❌ ' + (data.message || '上传失败'), 'err');
    }
  } catch (e) {
    pbUploadStopPoll();
    pbRenderProgress({ stage: 'error', error: e.message });
    pbShowMsg('❌ 上传失败：' + e.message, 'err');
  }
}

/* ──── 首屏 + 绑定 ──── */
function pbLoadInline() {
  try {
    const data = JSON.parse(document.getElementById('prebuilt-data').textContent);
    pbRenderMode(data.mode);
    pbRenderProgress(data.state || {});
    if (data.selected_mirror !== undefined && data.selected_mirror !== null) {
      _pbSelected = data.selected_mirror;
      _pbMirrorExpanded = false;
    }
    pbRenderMirrorUI();
    if ((data.state || {}).running) pbStartPoll();
  } catch (e) { console.warn('[prebuilt] load failed:', e); }
}

window.addEventListener('DOMContentLoaded', () => {
  pbLoadInline();
  const bind = (id, fn) => { const el = document.getElementById(id); if (el) el.addEventListener('click', fn); };
  bind('pb-use-local', () => pbSwitch(false));
  bind('pb-use-prebuilt', () => pbSwitch(true));
  bind('pb-test-mirrors', pbTestMirrors);
  bind('pb-add-mirror', pbAddCustomMirror);
  bind('pb-list-refresh', pbRefreshList);
  bind('pb-mirror-expand', () => { _pbMirrorExpanded = true; pbRenderMirrorUI(); });

  // 手动上传:按钮触发隐藏 file input;选文件后上传并清空 input(便于重复上传同名文件)
  const upBtn = document.getElementById('pb-upload-btn');
  const upInput = document.getElementById('pb-upload-input');
  if (upBtn && upInput) {
    upBtn.addEventListener('click', () => upInput.click());
    upInput.addEventListener('change', () => {
      const f = upInput.files && upInput.files[0];
      upInput.value = '';
      if (f) pbUpload(f);
    });
  }

  // 首次切到本 tab 时自动拉一次远程列表
  const tabBtn = document.querySelector('.tabs .tab[data-tab="prebuilt"]');
  if (tabBtn) tabBtn.addEventListener('click', () => {
    if (!_pbListLoaded) { _pbListLoaded = true; pbRefreshList(); }
  });
});
