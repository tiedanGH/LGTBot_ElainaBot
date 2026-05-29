/* ──── Dashboard:版本/统计/缓存/配置 ────
 * 隐藏 action 端点 key 与 webui/main.py 的 _DASH_* 常量一一对应。
 * 配置保存复用主框架 /api/config-file/save(接受 plugins/ 下绝对路径)。
 */

const DASH_KEYS = {
  check_update:       '__lgtbot_dash_check_update',
  do_update:          '__lgtbot_dash_do_update',
  update_submodule:   '__lgtbot_dash_update_submodule',
  clear_avatar:       '__lgtbot_dash_clear_avatar',
  clear_avatar_7d:    '__lgtbot_dash_clear_avatar_7d',
  clear_gen:          '__lgtbot_dash_clear_gen',
  clear_gen_7d:       '__lgtbot_dash_clear_gen_7d',
  clear_match_all:    '__lgtbot_dash_clear_match_all',
  clear_match_7d:     '__lgtbot_dash_clear_match_7d',
  reload_config:      '__lgtbot_dash_reload_config',
};

/* 引擎配置绝对路径(由 dashboard-data 注入),保存请求里要原样回传 */
let dashConfigAbsPath = '';
let dashConfigOriginal = '';

/* 缓存最近一次拿到的 submodule info —— 给「更新子模块」按钮的 confirm
   弹窗决定文案(初始化 vs 更新),也用来拼完整 git 命令展示。 */
let dashLastSubmoduleInfo = {};

function dashFmtBytes(n) {
  if (n == null) return '—';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(2) + ' MB';
  return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
}

function dashFmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.getFullYear() + '-' +
         String(d.getMonth() + 1).padStart(2, '0') + '-' +
         String(d.getDate()).padStart(2, '0') + ' ' +
         String(d.getHours()).padStart(2, '0') + ':' +
         String(d.getMinutes()).padStart(2, '0') + ':' +
         String(d.getSeconds()).padStart(2, '0');
}

/* 版本号统一加 v 前缀:'1.5.0' → 'v1.5.0'(若已带 v/V 则不重复加)。
   __plugin_meta__ 里 version='1.5.0' 不带 v;GitHub tag 是 'v1.5.0' 带 v,
   规范化到同一形式后视觉对齐,避免「本地 1.5.0 / 远端 v1.5.0」混杂。 */
function dashFmtVersion(v) {
  if (!v) return '—';
  return /^v/i.test(v) ? v : 'v' + v;
}

function dashApplyData(data) {
  /* 版本号 + 引擎状态 */
  document.getElementById('dash-current-version').textContent = data.version || '—';
  const statusEl = document.getElementById('dash-engine-status');
  if (data.engine_running) {
    statusEl.textContent = '运行中';
    statusEl.className = 'dash-badge dash-badge-ok';
  } else {
    statusEl.textContent = '未运行';
    statusEl.className = 'dash-badge dash-badge-warn';
  }

  /* 子模块初始状态(get_data 只填本地 commit,远端留给检查更新按钮)*/
  if (data.submodule) {
    dashRenderSubmoduleStatus(data.submodule);
  }

  /* 统计 */
  document.getElementById('dash-stats-time').textContent = dashFmtTime(data.query_time);
  const stats = data.stats || {};
  const fmt = (v) => (v == null ? '—' : String(v));
  document.getElementById('dash-stat-user-cache').textContent = fmt(stats.user_cache_total);
  document.getElementById('dash-stat-lgt-users').textContent = fmt(stats.lgtbot_users);
  document.getElementById('dash-stat-matches').textContent = fmt(stats.lgtbot_matches);
  document.getElementById('dash-stat-attendances').textContent = fmt(stats.lgtbot_match_attendances);
  document.getElementById('dash-stat-achievements').textContent = fmt(stats.lgtbot_achievements);

  const errsBox = document.getElementById('dash-stats-errors');
  const errs = stats.errors || [];
  if (errs.length) {
    errsBox.innerHTML = errs.map(e => escapeHtml(e)).join('<br>');
    errsBox.style.display = 'block';
  } else {
    errsBox.style.display = 'none';
  }

  /* 引擎配置(只在没有未保存改动时刷新编辑器内容) */
  const cfg = data.config || {};
  dashConfigAbsPath = cfg.abs_path || '';
  dashConfigOriginal = cfg.content || '';
  document.getElementById('dash-config-path').textContent = dashConfigAbsPath || '—';
  const editor = document.getElementById('dash-config-editor');
  if (!editor.dataset.dirty) {
    editor.value = dashConfigOriginal;
  }
  if (cfg.read_error) {
    dashShowConfigMsg('读取失败：' + cfg.read_error, 'err');
  }

  /* 缓存尺寸 */
  const cache = data.cache || {};
  ['avatar', 'gen', 'match'].forEach(k => {
    const c = cache[k] || {};
    document.getElementById('dash-cache-' + k + '-size').textContent = dashFmtBytes(c.bytes || 0);
    const countEl = document.getElementById('dash-cache-' + k + '-count');
    countEl.textContent = c.exists ? '(' + (c.count || 0) + ' 文件)' : '(目录不存在)';
  });
}

function dashLoadInline() {
  try {
    const data = JSON.parse(document.getElementById('dashboard-data').textContent);
    dashApplyData(data);
  } catch (e) {
    console.warn('[dashboard] load failed:', e);
  }
}

/* 整页刷新 → 抠出新的 dashboard-data JSON,只刷新本标签的状态(用户的标签
 * 切换 / 编辑器脏标记都不会被破坏) */
async function dashRefreshAll() {
  const btn = document.getElementById('dash-stats-refresh');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 刷新中……'; }
  try {
    const r = await fetch(apiUrl(PAGE_KEY), { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const text = await r.text();
    const m = text.match(/<script id="dashboard-data"[^>]*>([\s\S]*?)<\/script>/);
    if (m) dashApplyData(JSON.parse(m[1]));
  } catch (e) {
    console.warn('[dashboard] refresh failed:', e);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🔄 刷新'; }
  }
}

/* 调隐藏 action 端点,从 <pre id="result"> 抠出 JSON */
async function dashCallAction(key) {
  const r = await fetch(apiUrl(key), { cache: 'no-store' });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const text = await r.text();
  const doc = new DOMParser().parseFromString(text, 'text/html');
  const el = doc.getElementById('result');
  if (!el) throw new Error('响应不含 #result');
  return JSON.parse(el.textContent);
}

/* ──── 检查更新 —— 同时拉桥接层 tags 和子模块上游 commit ──── */
async function dashCheckUpdate() {
  const btn = document.getElementById('dash-check-update');
  const resEl = document.getElementById('dash-update-result');
  btn.disabled = true;
  btn.textContent = '⏳ 检查中……';
  resEl.innerHTML = '';
  /* 隐藏所有更新按钮,等结果回来再按需重显 */
  document.getElementById('dash-do-update').style.display = 'none';
  /* 注意:dash-update-submodule 不一定隐藏 —— 如果 status=missing/empty,
     在 dashApplyData 阶段就显示了「初始化子模块」按钮。
     这里也先隐藏,新结果回来时 dashRenderSubmoduleStatus 会重新决定。 */
  document.getElementById('dash-update-submodule').style.display = 'none';
  try {
    const data = await dashCallAction(DASH_KEYS.check_update);
    dashRenderBridgeStatus(data.bridge || {});
    dashRenderSubmoduleStatus(data.submodule || {});
  } catch (e) {
    resEl.innerHTML = '<span class="dash-msg-err">❌ ' + escapeHtml(e.message) + '</span>';
  } finally {
    btn.disabled = false;
    btn.textContent = '🔍 检查更新';
  }
}

/* ──── 桥接层(本插件)的检测结果渲染 ──── */
function dashRenderBridgeStatus(bridge) {
  const detail = document.getElementById('dash-bridge-detail');
  const btn = document.getElementById('dash-do-update');
  if (!bridge || !bridge.success) {
    detail.innerHTML = '<span class="dash-msg-err">❌ ' +
      escapeHtml(bridge && bridge.error ? bridge.error : '检查失败') + '</span>';
    btn.style.display = 'none';
    return;
  }
  const local = dashFmtVersion(bridge.local_version);
  const remote = dashFmtVersion(bridge.remote_version);
  if (bridge.has_update) {
    detail.innerHTML = '<span class="dash-msg-warn">✨ 本地 <b>' +
      escapeHtml(local) + '</b> → 远端 <b>' + escapeHtml(remote) + '</b></span>';
    btn.style.display = '';
  } else {
    detail.innerHTML = '<span class="dash-msg-ok">✅ 已是最新版本 (' + escapeHtml(remote) + ')</span>';
    btn.style.display = 'none';
  }
}

/* ──── 子模块的检测结果渲染 ────
 * 三种情况:
 *   1. status=missing / empty —— 红字「未初始化」,按钮文案「初始化子模块」
 *   2. status=ok,但远端查询失败 —— 显示本地 commit + 远端错误
 *   3. status=ok,远端查询成功 —— 本地 / 远端 commit 对比
 *      · has_update=true  → 显示「更新子模块」按钮
 *      · has_update=false → 隐藏按钮,绿字「已是最新」
 * 同时把 sub 缓存到 dashLastSubmoduleInfo 给 confirm 弹窗用。
 */
function dashRenderSubmoduleStatus(sub) {
  dashLastSubmoduleInfo = sub || {};
  const detail = document.getElementById('dash-submodule-detail');
  const btn = document.getElementById('dash-update-submodule');
  const pathEl = document.getElementById('dash-submodule-path');
  if (sub.path) pathEl.textContent = sub.path;

  /* 未初始化:红叉 + 「初始化子模块」按钮 */
  if (sub.status === 'missing' || sub.status === 'empty') {
    const reason = sub.status === 'missing' ? '文件夹不存在' : '文件夹为空';
    let html = '<span class="dash-msg-err">❌ 子模块未初始化 (' + reason + ')</span>';
    if (sub.upstream_url) {
      html += ' · 上游 <a href="' + escapeHtml(sub.upstream_url) +
              '" target="_blank" rel="noopener">' +
              escapeHtml((sub.upstream_owner || '') + '/' + (sub.upstream_repo || '')) +
              '</a>';
    }
    detail.innerHTML = html;
    btn.textContent = '⬇ 初始化子模块';
    btn.style.display = '';
    return;
  }

  /* status=ok */
  const local = sub.local_commit || '—';
  const upstreamLink = sub.upstream_url
    ? ' · 上游 <a href="' + escapeHtml(sub.upstream_url) +
      '" target="_blank" rel="noopener">' +
      escapeHtml((sub.upstream_owner || '') + '/' + (sub.upstream_repo || '')) +
      '</a>'
    : '';

  /* 远端没查(初始页 get_data 不查) */
  if (!sub.remote_commit && !sub.error) {
    detail.innerHTML = '本地 <b class="dash-mono">' + escapeHtml(local) +
      '</b> · 点击「检查更新」查看远端' + upstreamLink;
    btn.style.display = 'none';
    return;
  }

  /* 远端查询失败 */
  if (sub.error) {
    detail.innerHTML = '本地 <b class="dash-mono">' + escapeHtml(local) +
      '</b> · <span class="dash-msg-err">远端查询失败：' +
      escapeHtml(sub.error) + '</span>' + upstreamLink;
    btn.style.display = 'none';
    return;
  }

  /* 远端查询成功:对比 */
  const remote = sub.remote_commit || '—';
  if (sub.has_update) {
    detail.innerHTML = '<span class="dash-msg-warn">✨ 本地 <b class="dash-mono">' +
      escapeHtml(local) + '</b> → 远端 <b class="dash-mono">' +
      escapeHtml(remote) + '</b></span>' + upstreamLink;
    btn.textContent = '⬇ 更新子模块';
    btn.style.display = '';
  } else {
    detail.innerHTML = '<span class="dash-msg-ok">✅ 已是最新 (本地 ' +
      escapeHtml(local) + ' = 远端 ' + escapeHtml(remote) + ')</span>' + upstreamLink;
    btn.style.display = 'none';
  }
}

/* ──── 更新桥接层(git pull --ff-only) ──── */
async function dashDoUpdate() {
  const cmd = 'git pull --ff-only';
  const ok = await dashConfirm(
    '确认更新桥接层？\n\n将在插件目录下执行命令：\n  ' + cmd +
    '\n\n更新完成后需要重启 LGTBot 引擎或重启进程才能加载新版本。',
    {level: 'warn'}
  );
  if (!ok) return;
  const btn = document.getElementById('dash-do-update');
  const resEl = document.getElementById('dash-update-result');
  btn.disabled = true;
  btn.textContent = '⏳ 更新中……';
  try {
    const data = await dashCallAction(DASH_KEYS.do_update);
    let html = '<div class="dash-msg-info">执行命令：<code class="dash-mono">' +
               escapeHtml(cmd) + '</code></div>';
    if (data.success) {
      html += '<div class="dash-msg-ok">' + escapeHtml(data.message) + '</div>';
    } else {
      html += '<div class="dash-msg-err">' + escapeHtml(data.message || '更新失败') + '</div>';
    }
    if (data.stdout) html += '<pre class="dash-pre">stdout:\n' + escapeHtml(data.stdout) + '</pre>';
    if (data.stderr) html += '<pre class="dash-pre">stderr:\n' + escapeHtml(data.stderr) + '</pre>';
    resEl.innerHTML = html;
  } catch (e) {
    resEl.innerHTML = '<span class="dash-msg-err">❌ ' + escapeHtml(e.message) + '</span>';
  } finally {
    btn.disabled = false;
    btn.textContent = '⬇ 更新桥接层';
  }
}

/* ──── 更新 / 初始化 lgtbot 子模块 ──── */
async function dashDoUpdateSubmodule() {
  const sub = dashLastSubmoduleInfo || {};
  const path = sub.path || 'lgtbot';
  const isInit = (sub.status === 'missing' || sub.status === 'empty');
  const verb = isInit ? '初始化' : '更新';
  const cmd = 'git submodule update --init --recursive --force ' + path;
  const tail = isInit
    ? '\n\n首次初始化会克隆完整的 lgtbot 仓库 (含 50+ 游戏插件)，通常需要 30 秒至几分钟。'
    : '\n\n该命令会强制把本地子模块对齐到父仓库 gitlink，清除子模块内的本地修改。';

  /* 第一次 confirm:warn 等级,展示完整命令 + 影响说明 */
  const ok1 = await dashConfirm(
    '确认' + verb + '子模块「' + path + '」？\n\n将在插件目录下执行命令：\n  ' + cmd + tail,
    {level: 'warn'}
  );
  if (!ok1) return;

  /* 第二次 confirm:danger 等级,强调不可逆。
     · init 场景:首次克隆,容量大,中断需手动清理
     · update 场景:强制丢弃 lgtbot/ 内的本地修改 */
  const dangerText = isInit
    ? '再次确认：初始化子模块「' + path + '」?\n\n' +
      '将从远端克隆完整 lgtbot 仓库 (含 50+ 游戏子模块) 到本地。\n' +
      '若网络中断，需手动删除半残的 lgtbot/ 目录后重试。'
    : '再次确认：更新子模块「' + path + '」?\n\n' +
      '此操作会强制清除 lgtbot/ 内所有未提交的本地修改，无法恢复！\n' +
      '请确认你不需要保留 lgtbot 子模块内的任何工作区改动。';
  const ok2 = await dashConfirm(dangerText, {level: 'danger'});
  if (!ok2) return;

  const btn = document.getElementById('dash-update-submodule');
  const resEl = document.getElementById('dash-update-result');
  const originalLabel = btn.textContent;
  btn.disabled = true;
  btn.textContent = isInit ? '⏳ 初始化中……' : '⏳ 更新中……';
  try {
    const data = await dashCallAction(DASH_KEYS.update_submodule);
    let html = '';
    if (data.command) {
      html += '<div class="dash-msg-info">执行命令：<code class="dash-mono">' +
              escapeHtml(data.command) + '</code></div>';
    }
    if (data.success) {
      html += '<div class="dash-msg-ok">' + escapeHtml(data.message) + '</div>';
    } else {
      html += '<div class="dash-msg-err">' +
              escapeHtml(data.message || (verb + '失败')) + '</div>';
    }
    if (data.stdout) html += '<pre class="dash-pre">stdout:\n' + escapeHtml(data.stdout) + '</pre>';
    if (data.stderr) html += '<pre class="dash-pre">stderr:\n' + escapeHtml(data.stderr) + '</pre>';
    resEl.innerHTML = html;
    /* 成功后刷新整页 —— 本地 commit / 子模块 status 会跟着变 */
    if (data.success) dashRefreshAll();
  } catch (e) {
    resEl.innerHTML = '<span class="dash-msg-err">❌ ' + escapeHtml(e.message) + '</span>';
  } finally {
    btn.disabled = false;
    btn.textContent = originalLabel;
  }
}

/* ──── 保存引擎配置 ──── */
function dashShowConfigMsg(msg, kind) {
  const el = document.getElementById('dash-config-msg');
  el.textContent = msg;
  el.className = 'dash-config-msg dash-msg-' + (kind || 'info');
}

async function dashSaveConfig() {
  const editor = document.getElementById('dash-config-editor');
  const text = editor.value;
  /* 前置 JSON 语法校验 —— 失败直接红字,不发请求 */
  try {
    JSON.parse(text);
  } catch (e) {
    dashShowConfigMsg('JSON 格式错误：' + e.message, 'err');
    return;
  }
  if (!dashConfigAbsPath) {
    dashShowConfigMsg('配置文件路径未知，无法保存', 'err');
    return;
  }
  const btn = document.getElementById('dash-config-save');
  btn.disabled = true;
  try {
    const r = await fetch('/api/config-file/save' + TOKEN_QS, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        path: dashConfigAbsPath,
        content: text,
        format: 'json',
      }),
    });
    const data = await r.json();
    if (data.success) {
      dashShowConfigMsg('✅ 配置已保存，需重启 LGTBot 引擎后才能生效', 'ok');
      dashConfigOriginal = text;
      delete editor.dataset.dirty;
    } else {
      dashShowConfigMsg('❌ ' + (data.message || '保存失败'), 'err');
    }
  } catch (e) {
    dashShowConfigMsg('❌ 请求失败：' + e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

function dashRevertConfig() {
  const editor = document.getElementById('dash-config-editor');
  editor.value = dashConfigOriginal;
  delete editor.dataset.dirty;
  dashShowConfigMsg('已恢复至上次加载内容', 'info');
}

/* ──── 缓存清理 ──── */
/* DASH_CLEAR_PROMPTS:每个 which → confirm 文案数组,统一双次确认。
 *
 * 安全准则:所有缓存清理一律走双次确认 —— 第一次 warn 等级展示范围 + 影响,
 * 第二次 danger 等级强调不可逆。没有任何自动清理 / 定时清理 / 后台调度;
 * 此前图片缓存是单次确认,因有误触风险已统一改为双次。
 */
const DASH_CLEAR_PROMPTS = {
  avatar: [
    '清理「头像缓存」(engine/images/avatar)？\n所有用户头像图片将被删除，引擎下次需要时会自动重新下载。',
    '再次确认：删除头像缓存，无法恢复？',
  ],
  avatar_7d: [
    '仅保留最近 7 天的「头像缓存」，删除其它文件？',
    '再次确认：删除 7 天前的头像缓存，无法恢复？',
  ],
  gen: [
    '清理「图片缓存」(engine/images/gen)？\n所有已发送过的渲染图片缓存将被删除，不影响已发出的消息。',
    '再次确认：删除全部图片缓存，无法恢复？',
  ],
  gen_7d: [
    '仅保留最近 7 天的「图片缓存」，删除其它文件？',
    '再次确认：删除 7 天前的图片缓存，无法恢复？',
  ],
  match_all: [
    '清理「赛况缓存」全部 (engine/images/matches)？\n保存在 matches 的对局的记录将被删除，不会影响玩家战绩数据库。',
    '再次确认：删除全部赛况图，无法恢复？',
  ],
  match_7d: [
    '仅保留最近 7 天的「赛况缓存」，删除其它子目录？',
    '再次确认：删除 7 天前的赛况图，无法恢复？',
  ],
};

const DASH_CLEAR_KEYS = {
  avatar:    DASH_KEYS.clear_avatar,
  avatar_7d: DASH_KEYS.clear_avatar_7d,
  gen:       DASH_KEYS.clear_gen,
  gen_7d:    DASH_KEYS.clear_gen_7d,
  match_all: DASH_KEYS.clear_match_all,
  match_7d:  DASH_KEYS.clear_match_7d,
};

async function dashClearCache(which) {
  const prompts = DASH_CLEAR_PROMPTS[which];
  if (!prompts) return;
  /* 依次弹出每条 prompt,任一取消即中断。
     双次确认场景下最后一条用 danger(强调不可逆),前几条 warn;
     单次确认场景直接 warn(常规风险)。 */
  for (let i = 0; i < prompts.length; i++) {
    const isLast = (i === prompts.length - 1);
    const level = (isLast && prompts.length > 1) ? 'danger' : 'warn';
    const ok = await dashConfirm(prompts[i], {level});
    if (!ok) return;
  }

  const msgEl = document.getElementById('dash-cache-msg');
  msgEl.textContent = '⏳ 清理中……';
  msgEl.className = 'dash-cache-msg dash-msg-info';
  try {
    const data = await dashCallAction(DASH_CLEAR_KEYS[which]);
    if (data.success) {
      msgEl.textContent = '✅ ' + (data.message || '清理完成') + '(删除 ' + (data.removed || 0) + ' 项)';
      msgEl.className = 'dash-cache-msg dash-msg-ok';
      dashRefreshAll();
    } else {
      msgEl.textContent = '❌ ' + (data.message || '清理失败');
      msgEl.className = 'dash-cache-msg dash-msg-err';
    }
  } catch (e) {
    msgEl.textContent = '❌ ' + e.message;
    msgEl.className = 'dash-cache-msg dash-msg-err';
  }
}

/* ──── 插件配置热重载 ────
 * 按当前 data/config.yaml 重新下发到运行时(不重启插件、不重启引擎)。
 * admin_uids 改动需重启 LGTBot 引擎才能生效,UI 上单独显示一条警示。
 */
async function dashReloadConfig() {
  const ok = await dashConfirm(
    '确认按当前 data/config.yaml 热重载？\n\n' +
    '会立即把 yaml 里的运行时可调字段 (配额秒数、图床、菜单游戏、崩溃通知群、' +
    '沙箱白名单) 重新下发，**不重启插件、不重启引擎**。\n\n' +
    '注意：admin_uids 变更需重启 LGTBot 引擎才能生效 (C++ 侧只在 start() 时读一次)。',
    {level: 'warn'}
  );
  if (!ok) return;
  const btn = document.getElementById('dash-reload-config');
  const msgEl = document.getElementById('dash-reload-config-msg');
  btn.disabled = true;
  btn.textContent = '⏳ 重载中……';
  msgEl.innerHTML = '';
  try {
    const data = await dashCallAction(DASH_KEYS.reload_config);
    if (!data.success) {
      msgEl.innerHTML = '<div class="dash-msg-err">❌ ' +
                        escapeHtml(data.message || '热重载失败') + '</div>';
      return;
    }
    const parts = [];
    parts.push('<div class="dash-msg-ok">' +
               escapeHtml(data.message || '已重载') + '</div>');
    if (data.changes && data.changes.length) {
      const items = data.changes.map(c =>
        '<li><code class="dash-mono">' + escapeHtml(c.field) + '</code>: ' +
        '<span class="dash-msg-info">' +
        escapeHtml(JSON.stringify(c.before)) + '</span> → ' +
        '<b>' + escapeHtml(JSON.stringify(c.after)) + '</b></li>'
      );
      parts.push('<ul class="dash-pluginconf-changes">' + items.join('') + '</ul>');
    } else {
      parts.push('<div class="dash-msg-info">(运行时参数与 yaml 一致，无变化)</div>');
    }
    parts.push('<div class="dash-msg-info">📋 当前 admin_uids: ' +
               (data.admin_count || 0) + ' 人</div>');
    if (data.note) {
      parts.push('<div class="dash-msg-warn">⚠️ ' + escapeHtml(data.note) + '</div>');
    }
    msgEl.innerHTML = parts.join('');
    /* 引擎配置编辑器(下方的 ⚙️ 引擎配置)与本次热重载无关 —— 那是 lgtbot.json,
       不是 config.yaml;但页面顶部数据(版本/统计/缓存)可能因 menu_game_buttons
       变化而需要刷新。保险起见整页刷一次。 */
    dashRefreshAll();
  } catch (e) {
    msgEl.innerHTML = '<div class="dash-msg-err">❌ ' + escapeHtml(e.message) + '</div>';
  } finally {
    btn.disabled = false;
    btn.textContent = '🔁 热重载配置';
  }
}

window.addEventListener('DOMContentLoaded', () => {
  dashLoadInline();

  document.getElementById('dash-check-update').addEventListener('click', dashCheckUpdate);
  document.getElementById('dash-do-update').addEventListener('click', dashDoUpdate);
  document.getElementById('dash-update-submodule').addEventListener('click', dashDoUpdateSubmodule);
  document.getElementById('dash-stats-refresh').addEventListener('click', dashRefreshAll);
  document.getElementById('dash-config-save').addEventListener('click', dashSaveConfig);
  document.getElementById('dash-config-revert').addEventListener('click', dashRevertConfig);
  document.getElementById('dash-reload-config').addEventListener('click', dashReloadConfig);

  /* 编辑器脏标记:用户改过且与原文不同 → dirty;复位即清除。
     dashRefreshAll 据此判定是否覆盖编辑器内容,避免刷新统计时把编辑中的
     文本擦掉。 */
  document.getElementById('dash-config-editor').addEventListener('input', (e) => {
    if (e.target.value !== dashConfigOriginal) {
      e.target.dataset.dirty = '1';
    } else {
      delete e.target.dataset.dirty;
    }
  });

  /* 缓存清理按钮(委托) */
  document.querySelectorAll('[data-clear]').forEach(btn => {
    btn.addEventListener('click', () => dashClearCache(btn.dataset.clear));
  });
});
