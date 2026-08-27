/* ──── 崩溃转储标签 ────
 * 列出 / 查看 / 下载 / 删除 LGTBot_CRASH_DUMPS/ 下的 crash_*.log。
 * 首屏读 #crash-data;刷新走隐藏 action(fragment);查看 / 下载 / 删除走带 ?name= 的真路由。
 * escapeHtml / apiUrl / TOKEN_QS / dashConfirm / dashAlert 由 main.js 提供。 */

const CRASH_LIST_KEY     = '__lgtbot_crash_list';
const CRASH_VIEW_ROUTE   = '/api/ext/lgtbot/crash/view';
const CRASH_DL_ROUTE     = '/api/ext/lgtbot/crash/download';
const CRASH_DELETE_ROUTE = '/api/ext/lgtbot/crash/delete';
/* 游戏子进程 core 的下载 / 批量删除(带 &d=<目录下标>) */
const CORE_DL_ROUTE     = '/api/ext/lgtbot/crash/core-download';
const CORE_DELETE_ROUTE = '/api/ext/lgtbot/crash/core-delete';

function crashFmtBytes(n) {
  if (n == null) return '—';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(2) + ' MB';
}
function crashFmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
         p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}
/* 路由带鉴权 token(iframe 内 fetch 不自动带,拼 TOKEN_QS;同 dashBindBot) */
function crashRouteUrl(route, name) {
  return route + TOKEN_QS + (TOKEN_QS ? '&' : '?') + 'name=' + encodeURIComponent(name);
}
function crashSigClass(sig) {
  if (sig === 11 || sig === 7) return 'seg';    // SIGSEGV / SIGBUS
  if (sig === 6) return 'abrt';                 // SIGABRT
  return 'misc';
}

/* 引擎崩溃重启卡片:累计次数 + 最近一次时间。不再列分信号明细(SIGSEGV ×N …)—— 下方转储列表每条都带信号列,一眼能看到,
   在卡片上重复一遍只是把这行挤长。payload 里的 crash_by_sig 仍保留给指标面板用。 */
function crashApplyRestart(r) {
  r = r || {};
  const total = r.crash_total || 0;
  document.getElementById('crash-restart-total').textContent = total + ' 次';
  document.getElementById('crash-restart-sub').textContent =
      (total > 0 && r.last_crash_ts) ? ('最近 ' + crashFmtTime(r.last_crash_ts))
                                     : '暂无崩溃记录';
}

/* 触发源分两列(用户 / 群聊)完整展示;有值则点击即可复制完整 ID */
function crashIdCell(v) {
  if (!v) return '<span class="crash-none">—</span>';
  const e = escapeHtml(v);
  return '<span class="crash-copy dash-mono" data-copy="' + e + '" title="点击复制">' + e + '</span>';
}
function crashUidCell(d) { return crashIdCell(d.uid); }
/* 群聊列:群聊显示完整 gid(可复制);私信(is_uid=1,无 gid)标「私信触发」;无则 — */
function crashGidCell(d) {
  if (d.is_uid === 1) return '<span class="crash-dm">私信触发</span>';
  return crashIdCell(d.gid);
}

function crashApplyData(data) {
  coreApplyData(data);      // core 区与 dump 区共用同一份 payload
  document.getElementById('crash-dir').textContent = data.crash_dir || '—';
  document.getElementById('crash-count').textContent = (data.count != null) ? data.count : '—';
  document.getElementById('crash-size').textContent = crashFmtBytes(data.total_bytes);
  crashApplyRestart(data.restart);
  const body = document.getElementById('crash-table-body');
  const dumps = data.dumps || [];
  /* 标题后的数量括号(必须在 dumps 声明之后 —— const 有 TDZ) */
  const cntBadge = document.getElementById('crash-count-badge');
  if (cntBadge) cntBadge.textContent = dumps.length ? ' (' + dumps.length + ')' : '';
  if (!dumps.length) {
    body.innerHTML = '<tr class="crash-empty"><td colspan="8">' +
      '暂无崩溃转储 —— 引擎未发生过 SIGSEGV / SIGBUS / SIGABRT</td></tr>';
    crashSyncSelection();
    return;
  }
  body.innerHTML = dumps.map(d => {
    const name = escapeHtml(d.name || '');
    const sigCls = crashSigClass(d.signal);
    const sig = '<span class="crash-sig ' + sigCls + '">' + escapeHtml(d.signal_name || '未知') + '</span>';
    return '<tr>' +
      '<td class="crash-col-chk"><input type="checkbox" class="crash-check" data-name="' + name + '"></td>' +
      '<td class="crash-col-time">' + escapeHtml(crashFmtTime(d.mtime)) + '</td>' +
      '<td class="crash-col-sig">' + sig + '</td>' +
      '<td class="crash-col-size dash-mono">' + crashFmtBytes(d.size) + '</td>' +
      '<td class="crash-col-name"><span class="dash-mono">' + name + '</span></td>' +
      '<td class="crash-col-uid">' + crashUidCell(d) + '</td>' +
      '<td class="crash-col-gid">' + crashGidCell(d) + '</td>' +
      '<td class="crash-col-act"><span class="crash-act-btns">' +
        '<button class="dash-btn dash-btn-small crash-view" data-name="' + name + '">👁 查看</button>' +
        '<button class="dash-btn dash-btn-small crash-dl" data-name="' + name + '">⬇ 下载</button>' +
      '</span></td></tr>';
  }).join('');
  body.querySelectorAll('.crash-view').forEach(b =>
    b.addEventListener('click', () => crashView(b.dataset.name)));
  body.querySelectorAll('.crash-dl').forEach(b =>
    b.addEventListener('click', () => crashDownload(b.dataset.name)));
  body.querySelectorAll('.crash-check').forEach(c =>
    c.addEventListener('change', crashSyncSelection));
  body.querySelectorAll('.crash-copy').forEach(el =>
    el.addEventListener('click', () => crashCopy(el.dataset.copy, el)));
  crashSyncSelection();   // 重渲染后重置全选框 + 删除按钮计数
}

/* 选中的文件名列表 */
function crashSelected() {
  return [...document.querySelectorAll('.crash-check:checked')].map(c => c.dataset.name);
}

/* 勾选变化时:更新「删除选中 (N)」按钮的计数 / 禁用态,以及全选框的半选 / 全选状态 */
function crashSyncSelection() {
  const all = [...document.querySelectorAll('.crash-check')];
  const sel = all.filter(c => c.checked);
  const btn = document.getElementById('crash-delete-btn');
  if (btn) {
    btn.textContent = '🗑 删除选中 (' + sel.length + ')';
    btn.disabled = sel.length === 0;
  }
  const master = document.getElementById('crash-check-all');
  if (master) {
    master.checked = all.length > 0 && sel.length === all.length;
    master.indeterminate = sel.length > 0 && sel.length < all.length;
  }
}

/* ──── 游戏崩溃 core 列表 ──── */
/* core 定位需要「目录下标 + 文件名」—— 同名 core 在本地 build/ 与预编译
   build_prebuilt/build/ 里都可能存在,只给名字无法区分。 */
function coreRouteUrl(route, name, dirIdx) {
  return route + TOKEN_QS + (TOKEN_QS ? '&' : '?') +
         'name=' + encodeURIComponent(name) + '&d=' + encodeURIComponent(dirIdx);
}

/* 解析结果 → 各列。解析不出就如实写「无法解析」—— core 本身仍可下载给 gdb 看,不因为面板读不懂就把它藏起来。 */
function coreGameCell(a) {
  if (!a || !a.ok) return '<span class="crash-core-unknown">无法解析</span>';
  if (!a.game) return '<span class="crash-core-unknown">未识别</span>';
  return '<b>' + escapeHtml(a.game) + '</b>';
}
function coreModuleCell(a) {
  if (!a || !a.ok || !a.crash_module) return '<span class="crash-core-unknown">—</span>';
  return '<span class="dash-mono">' + escapeHtml(a.crash_module) + '</span>';
}
/* 信号徽标。si_code 的人话解释放进 title 而不是另起一行 —— 信号列的宽度要和上方转储列表严格一致,塞不下一行中文说明。 */
function coreSigCell(a) {
  if (!a || a.signal == null) return '<span class="crash-core-unknown">—</span>';
  const tip = a.signal_detail ? ' title="' + escapeHtml(a.signal_detail) + '"' : '';
  return '<span class="crash-sig ' + crashSigClass(a.signal) + '"' + tip + '>' +
         escapeHtml(a.signal_name || '') + '</span>';
}

function coreApplyData(data) {
  const cores = data.cores || [];
  const cnt = document.getElementById('crash-core-count');
  if (cnt) cnt.textContent = (data.core_count != null) ? data.core_count : '—';
  const sz = document.getElementById('crash-core-size');
  if (sz) sz.textContent = crashFmtBytes(data.core_bytes);
  /* core 文件合计超阈值 → 本标签亮「清理」(core 动辄上百 MB,是这里的占用大头) */
  setTabCleanBadge('crash-clean-badge', data.core_bytes);
  const badge = document.getElementById('crash-core-badge');
  if (badge) badge.textContent = cores.length ? ' (' + cores.length + ')' : '';
  const body = document.getElementById('crash-core-body');
  if (!body) return;
  if (!cores.length) {
    body.innerHTML = '<tr class="crash-empty"><td colspan="8">' +
      '暂无 core 文件 —— 游戏子进程未发生过崩溃</td></tr>';
    coreSyncSelection();
    return;
  }
  body.innerHTML = cores.map(c => {
    const name = escapeHtml(c.name || '');
    const d = escapeHtml(String(c.dir_idx));
    const a = c.analysis || {};
    /* 出错地址 / 命令行 / 所在目录塞进 title,鼠标悬停即见,不占列宽 */
    /* 文件名列宽固定,长名会省略号 —— title 里先给完整文件名,再带诊断信息 */
    const tip = [
      c.name || '',
      (a.fault_addr != null) ? '出错地址 0x' + Number(a.fault_addr).toString(16) : '',
      a.cmdline ? '命令行 ' + a.cmdline : '',
      c.dir ? '目录 ' + c.dir : '',
    ].filter(Boolean).join(' | ');
    return '<tr>' +
      '<td class="crash-col-chk"><input type="checkbox" class="core-check" data-name="' +
        name + '" data-dir="' + d + '"></td>' +
      '<td class="crash-col-time">' + escapeHtml(crashFmtTime(c.crash_ts)) + '</td>' +
      '<td class="crash-col-sig">' + coreSigCell(a) + '</td>' +
      '<td class="crash-col-size dash-mono">' + crashFmtBytes(c.size) + '</td>' +
      '<td class="crash-col-name"><span class="dash-mono" title="' + escapeHtml(tip) + '">' +
        name + '</span></td>' +
      '<td class="crash-col-mod">' + coreModuleCell(a) + '</td>' +
      '<td class="crash-col-game">' + coreGameCell(a) + '</td>' +
      '<td class="crash-col-act"><span class="crash-act-btns">' +
        '<button class="dash-btn dash-btn-small core-dl" data-name="' + name +
        '" data-dir="' + d + '">⬇ 下载</button>' +
      '</span></td></tr>';
  }).join('');
  body.querySelectorAll('.core-dl').forEach(b =>
    b.addEventListener('click', () =>
      window.open(coreRouteUrl(CORE_DL_ROUTE, b.dataset.name, b.dataset.dir), '_blank')));
  body.querySelectorAll('.core-check').forEach(c =>
    c.addEventListener('change', coreSyncSelection));
  coreSyncSelection();
}

/* 与 dump 区的 crashSyncSelection 完全并行,两区选择互不干扰 */
function coreSyncSelection() {
  const all = [...document.querySelectorAll('.core-check')];
  const sel = all.filter(c => c.checked);
  const btn = document.getElementById('crash-core-delete');
  if (btn) {
    btn.textContent = '🗑 删除选中 (' + sel.length + ')';
    btn.disabled = sel.length === 0;
  }
  const master = document.getElementById('crash-core-check-all');
  if (master) {
    master.checked = all.length > 0 && sel.length === all.length;
    master.indeterminate = sel.length > 0 && sel.length < all.length;
  }
}

function coreShowMsg(text, kind) {
  const el = document.getElementById('crash-core-msg');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'crash-action-msg' + (kind ? ' ' + kind : '');
}

async function coreDeleteSelected() {
  const picked = [...document.querySelectorAll('.core-check:checked')]
    .map(c => ({ name: c.dataset.name, d: c.dataset.dir }));
  if (!picked.length) return;
  const ok = await dashConfirm(
    '确认删除选中的 ' + picked.length + ' 个 core 文件？\n\n' +
    (picked.length <= 8 ? picked.map(p => p.name).join('\n') + '\n\n' : '') +
    'core 是排查游戏崩溃的唯一现场（gdb 要用它）。删除后无法恢复，建议先下载留存。',
    { level: 'danger' }
  );
  if (!ok) return;
  /* name 与 d 按下标一一配对,后端 zip 起来用 */
  const qs = TOKEN_QS + (TOKEN_QS ? '&' : '?') +
             picked.map(p => 'name=' + encodeURIComponent(p.name) +
                             '&d=' + encodeURIComponent(p.d)).join('&');
  try {
    const r = await fetch(CORE_DELETE_ROUTE + qs, { cache: 'no-store' });
    const data = await r.json();
    coreShowMsg((data.success ? '✅ ' : '⚠️ ') + (data.message || ''),
                data.success ? 'info' : 'err');
    crashRefresh();
  } catch (e) {
    coreShowMsg('❌ 删除失败: ' + e.message, 'err');
  }
}


function crashLoadInline() {
  try {
    crashApplyData(JSON.parse(document.getElementById('crash-data').textContent));
  } catch (e) {
    crashShowMsg('转储数据解析失败: ' + e.message, 'err');
  }
}

async function crashCallAction(key) {
  const r = await fetch(apiUrl(key), { cache: 'no-store' });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const text = await r.text();
  const el = new DOMParser().parseFromString(text, 'text/html').getElementById('result');
  if (!el) throw new Error('响应不含 #result');
  return JSON.parse(el.textContent);
}

/* 两个区共用同一个端点、同一次刷新,刷新中要同时落在**两个**按钮。 */
const CRASH_REFRESH_BTNS = ['crash-refresh-btn', 'crash-core-refresh'];

function crashSetRefreshing(on) {
  CRASH_REFRESH_BTNS.forEach(id => {
    const btn = document.getElementById(id);
    if (!btn) return;
    if (on) {
      if (btn.dataset.label === undefined) btn.dataset.label = btn.textContent;
      btn.disabled = true;
      btn.textContent = '⏳ 刷新中……';
    } else {
      btn.disabled = false;
      if (btn.dataset.label !== undefined) {
        btn.textContent = btn.dataset.label;
        delete btn.dataset.label;
      }
    }
  });
}

async function crashRefresh() {
  crashSetRefreshing(true);
  try {
    crashApplyData(await crashCallAction(CRASH_LIST_KEY));
  } catch (e) {
    crashShowMsg('❌ 刷新失败: ' + e.message, 'err');
  } finally {
    crashSetRefreshing(false);
  }
}

/* ──── 查看大弹窗 ──── */
function crashOpenModal() {
  const bd = document.getElementById('crash-modal-backdrop');
  bd.classList.remove('hidden');
  bd.setAttribute('aria-hidden', 'false');
}
function crashCloseModal() {
  const bd = document.getElementById('crash-modal-backdrop');
  bd.classList.add('hidden');
  bd.setAttribute('aria-hidden', 'true');
}
function crashMetaItem(label, valueHtml) {
  return '<div class="crash-meta-item"><span class="crash-meta-label">' + label +
         '</span><span class="crash-meta-value dash-mono">' + valueHtml + '</span></div>';
}
function crashRenderMeta(meta) {
  const el = document.getElementById('crash-view-meta');
  meta = meta || {};
  const rows = [];
  const sigName = meta.signal_name || (meta.signal != null ? ('sig' + meta.signal) : '未知');
  rows.push(crashMetaItem('信号', escapeHtml(sigName) + (meta.signal != null ? ' (' + meta.signal + ')' : '')));
  rows.push(crashMetaItem('时间', escapeHtml(crashFmtTime(meta.mtime))));
  rows.push(crashMetaItem('大小', crashFmtBytes(meta.size)));
  if (meta.si_addr) rows.push(crashMetaItem('si_addr', escapeHtml(meta.si_addr)));
  if (meta.pid != null) rows.push(crashMetaItem('pid', meta.pid));
  if (meta.tid != null) rows.push(crashMetaItem('tid', meta.tid));
  // 触发源分「用户」「群」两项独立展示;私信在群项标「私信触发」
  rows.push(crashMetaItem('用户', meta.uid ? escapeHtml(meta.uid) : '—'));
  let gidVal;
  if (meta.is_uid === 1) gidVal = '<span class="crash-dm">私信触发</span>';
  else if (meta.gid) gidVal = escapeHtml(meta.gid);
  else gidVal = '—';
  rows.push(crashMetaItem('群聊', gidVal));
  el.innerHTML = rows.join('');
  const wrap = document.getElementById('crash-view-msg-wrap');
  const msgEl = document.getElementById('crash-view-msg');
  if (meta.msg && meta.msg.length) { msgEl.textContent = meta.msg; wrap.style.display = ''; }
  else { wrap.style.display = 'none'; }
}
/* content 含头部 + backtrace,弹窗里 backtrace 区只展示分隔线之后的栈 */
function crashBacktrace(content) {
  const raw = content || '';
  const idx = raw.indexOf('--- backtrace ---');
  return (idx >= 0) ? raw.slice(idx + '--- backtrace ---'.length).replace(/^\n+/, '') : raw;
}

async function crashView(name) {
  if (!name) return;
  document.getElementById('crash-view-name').textContent = name;
  document.getElementById('crash-view-meta').innerHTML = '';
  document.getElementById('crash-view-msg-wrap').style.display = 'none';
  const bodyEl = document.getElementById('crash-view-body');
  bodyEl.textContent = '加载中……';
  document.getElementById('crash-view-download').onclick = () => crashDownload(name);
  crashOpenModal();
  try {
    const r = await fetch(crashRouteUrl(CRASH_VIEW_ROUTE, name), { cache: 'no-store' });
    const data = await r.json();
    if (!data.success) { bodyEl.textContent = '❌ ' + (data.message || '读取失败'); return; }
    crashRenderMeta(data.meta);
    bodyEl.textContent = (crashBacktrace(data.content) || '(空)') +
      (data.truncated ? '\n\n……（文件过大，仅显示前 256 KB，完整内容请下载）' : '');
  } catch (e) {
    bodyEl.textContent = '❌ 请求失败: ' + e.message;
  }
}

function crashDownload(name) {
  if (!name) return;
  /* Content-Disposition: attachment → 浏览器直接下载;新窗口触发不影响面板 */
  window.open(crashRouteUrl(CRASH_DL_ROUTE, name), '_blank', 'noopener');
}

async function crashDeleteSelected() {
  const names = crashSelected();
  if (!names.length) return;
  const ok = await dashConfirm(
    '确认删除选中的 ' + names.length + ' 个崩溃转储？\n\n' +
    (names.length <= 8 ? names.join('\n') + '\n\n' : '') +
    '删除后无法恢复。建议先「⬇ 下载」需要留存的 dump 再删。',
    { level: 'danger' }
  );
  if (!ok) return;
  /* 多选:route?name=a&name=b…(后端 getall('name') 收全部) */
  const qs = TOKEN_QS + (TOKEN_QS ? '&' : '?') +
             names.map(n => 'name=' + encodeURIComponent(n)).join('&');
  try {
    const r = await fetch(CRASH_DELETE_ROUTE + qs, { cache: 'no-store' });
    const data = await r.json();
    crashShowMsg((data.success ? '✅ ' : '⚠️ ') + (data.message || ''), data.success ? 'info' : 'err');
    crashRefresh();
  } catch (e) {
    crashShowMsg('❌ 删除失败: ' + e.message, 'err');
  }
}

/* 点击复制完整 ID。优先 Clipboard API;iframe 内被拦时回退 execCommand。短暂反馈。 */
async function crashCopy(text, el) {
  if (!text) return;
  let ok = false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch (e) { ok = false; }
  if (!ok) {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed'; ta.style.top = '-1000px'; ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.focus(); ta.select();
      ok = document.execCommand('copy');
      document.body.removeChild(ta);
    } catch (e) { ok = false; }
  }
  if (el) {
    el.classList.add('crash-copied');
    setTimeout(() => el.classList.remove('crash-copied'), 900);
  }
  crashShowMsg(ok ? '✅ 已复制完整 ID' : '❌ 复制失败，请手动选择', ok ? 'info' : 'err');
  clearTimeout(crashCopy._t);
  crashCopy._t = setTimeout(() => crashShowMsg('', 'info'), 1500);
}

function crashShowMsg(text, kind) {
  const el = document.getElementById('crash-action-msg');
  if (!el) return;
  el.textContent = text || '';
  el.style.color = (kind === 'err') ? '#dc3545' : 'var(--text-muted)';
}

window.addEventListener('DOMContentLoaded', () => {
  crashLoadInline();
  const btn = document.getElementById('crash-refresh-btn');
  if (btn) btn.addEventListener('click', crashRefresh);
  const del = document.getElementById('crash-delete-btn');
  if (del) del.addEventListener('click', crashDeleteSelected);
  const coreRef = document.getElementById('crash-core-refresh');
  if (coreRef) coreRef.addEventListener('click', crashRefresh);
  const coreDel = document.getElementById('crash-core-delete');
  if (coreDel) coreDel.addEventListener('click', coreDeleteSelected);
  const coreMaster = document.getElementById('crash-core-check-all');
  if (coreMaster) coreMaster.addEventListener('change', () => {
    document.querySelectorAll('.core-check')
      .forEach(c => { c.checked = coreMaster.checked; });
    coreSyncSelection();
  });
  const master = document.getElementById('crash-check-all');
  if (master) master.addEventListener('change', () => {
    document.querySelectorAll('.crash-check').forEach(c => { c.checked = master.checked; });
    crashSyncSelection();
  });
  /* 弹窗:关闭按钮 / 点 backdrop 空白 / Esc 均关闭;点 modal 内部不冒泡到 backdrop */
  const close = document.getElementById('crash-view-close');
  if (close) close.addEventListener('click', crashCloseModal);
  const backdrop = document.getElementById('crash-modal-backdrop');
  if (backdrop) {
    backdrop.addEventListener('mousedown', (e) => { if (e.target === backdrop) crashCloseModal(); });
    const modal = document.getElementById('crash-modal');
    if (modal) modal.addEventListener('mousedown', e => e.stopPropagation());
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !backdrop.classList.contains('hidden')) crashCloseModal();
    });
  }
});
