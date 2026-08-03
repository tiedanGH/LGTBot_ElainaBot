/* ──── 引擎编译 标签 ────
 * 主要职责:
 *   · 状态条:轮询 build action 拉最新 state + log,渲染 badge / 当前命令 /
 *     已运行时间;running 时显示「终止编译」按钮
 *   · 10 个 action 按钮(8 个启动 + 1 个终止 + 1 个删 build/)。所有按钮都
 *     先 confirm 显示完整命令再发起请求
 *   · 自定义目标:前端 prompt + 严格白名单(同后端 _validate_target_name),
 *     合法后通过 framework /api/config-file/save 写参数文件,再调编译端点
 *
 * 安全:
 *   · 自定义目标名前端用 BUILD_TARGET_RE 校验后才允许通过 confirm;后端
 *     有独立校验,前端绕过(改 JS)也无法注入 shell —— 服务端用 argv
 *     list-form Popen,且 _validate_target_name 拒绝任何非白名单字符。
 *   · 没用 fetch JSON 任意命令的设计 —— 每条命令在后端写死 argv,前端只能
 *     选「哪个端点」+ 提供一个白名单 target 字符串。
 */

const BUILD_KEYS = {
  full:   '__lgtbot_dash_build_full',
  incr:   '__lgtbot_dash_build_incr',
  bridge: '__lgtbot_dash_build_bridge',
  list:   '__lgtbot_dash_build_list',
  custom: '__lgtbot_dash_build_custom',
  newTarget: '__lgtbot_dash_build_newtarget',
  kill:   '__lgtbot_dash_build_kill',
  clean:  '__lgtbot_dash_build_clean',
  remove: '__lgtbot_dash_build_remove',
  log:    '__lgtbot_dash_build_log',
  apiToken: '__lgtbot_build_api_token',
};

/* 自定义目标名白名单(同后端):字母/数字/下划线/连字符,1-63 字符,
   首字符必须是字母或下划线 */
const BUILD_TARGET_RE = /^[A-Za-z_][A-Za-z0-9_\-]{0,62}$/;

/* 从 build-data 取来的配置参数文件绝对路径,自定义目标时 POST 用 */
let buildParamsPath = '';
/* 上次「编译指定目标」用的 target 名,作为下一次 prompt 的预填值 */
let buildLastCustomTarget = '';
let buildPollTimer = null;
/* 上次挂载的日志段的 JSON 串,内容没变就跳过 DOM 重建 */
let buildLastLogKey = '';

function buildFmtBytes(n) {
  if (n == null) return '—';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(2) + ' MB';
  return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
}

function buildFmtElapsed(isoTime) {
  if (!isoTime) return '';
  const start = new Date(isoTime).getTime();
  if (isNaN(start)) return '';
  const sec = Math.max(0, Math.floor((Date.now() - start) / 1000));
  if (sec < 60) return '已运行 ' + sec + ' 秒';
  if (sec < 3600) return '已运行 ' + Math.floor(sec / 60) + ' 分 ' + (sec % 60) + ' 秒';
  return '已运行 ' + Math.floor(sec / 3600) + ' 时 ' +
         Math.floor((sec % 3600) / 60) + ' 分';
}

/* 把秒数格式化为「N 分 M 秒」/ 「H 时 M 分」,用于已完成任务的「用时」标签 */
function buildFmtDuration(sec) {
  if (sec == null || sec < 0) return '';
  if (sec < 60) return sec + ' 秒';
  if (sec < 3600) return Math.floor(sec / 60) + ' 分 ' + (sec % 60) + ' 秒';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return h + ' 时 ' + m + ' 分';
}

/* 服务端 _ansi_to_segments 把 ANSI 日志解析成 [{t,b,c}] 结构化段(文本 /
   粗体 / 前景色)。这里从数据直接建 DOM:文本一律 createTextNode / textContent,
   颜色经十六进制白名单校验后由 CSSOM 属性赋值 —— 日志内容全程不以 HTML
   字符串出现,也就不存在任何 HTML 解析 / innerHTML 环节 */
const BUILD_SAFE_COLOR_RE = /^#[0-9a-fA-F]{3,8}$/;

function buildLogFragment(segments) {
  const out = document.createDocumentFragment();
  (Array.isArray(segments) ? segments : []).forEach(seg => {
    const text = seg && typeof seg.t === 'string' ? seg.t : '';
    if (!text) return;
    const bold = !!(seg && seg.b);
    const color = seg && typeof seg.c === 'string' && BUILD_SAFE_COLOR_RE.test(seg.c)
      ? seg.c : '';
    if (!bold && !color) {
      out.appendChild(document.createTextNode(text));
      return;
    }
    const span = document.createElement('span');
    if (bold) span.style.fontWeight = 'bold';
    if (color) span.style.color = color;
    span.textContent = text;
    out.appendChild(span);
  });
  return out;
}

/* 把后端发来的状态对象 + 结构化日志段渲染到 UI */
function buildApplyState(state, logSegments, logSize) {
  const badge = document.getElementById('build-status-badge');
  const cmdEl = document.getElementById('build-status-cmd');
  const sinceEl = document.getElementById('build-status-since');
  const killBtn = document.getElementById('build-kill-btn');

  if (state && state.running) {
    badge.textContent = '编译中';
    badge.className = 'dash-badge dash-badge-warn';
    cmdEl.textContent = '$ ' + (state.cmd_argv || []).join(' ') +
                        '   (PID ' + state.pid + ')';
    sinceEl.textContent = buildFmtElapsed(state.started_iso);
    killBtn.style.display = '';
  } else if (state && state.finished && state.cmd_display) {
    /* 已完成:根据 kind + returncode 决定 badge 文案/颜色
       · kind='build' + rc=0  → 「✅ 编译成功」绿
       · kind='build' + rc≠0  → 「❌ 编译失败(退出码 N)」红
       · kind='build' + rc=null → 「⛔ 已终止」灰(被 SIGKILL 等)
       · kind='meta'           → 「已完成」灰(列出目标 / 删 build 等)
    */
    const kind = state.kind || 'build';
    const rc = state.returncode;
    if (kind === 'meta') {
      badge.textContent = '已完成';
      badge.className = 'dash-badge';
    } else if (rc === 0) {
      badge.textContent = '✅ 编译成功';
      badge.className = 'dash-badge dash-badge-ok';
    } else if (rc != null) {
      badge.textContent = '❌ 编译失败 (退出码 ' + rc + ')';
      badge.className = 'dash-badge dash-badge-err';
    } else {
      /* returncode 缺失:进程被 SIGKILL / wrapper 没机会写 status,
         同样视为失败/中断,用红 badge 与「编译失败」一致 */
      badge.textContent = '⛔ 已终止';
      badge.className = 'dash-badge dash-badge-err';
    }
    cmdEl.textContent = '上次任务：' + state.cmd_display;
    /* 有 elapsed_sec(编译类 / 终止)就在右侧显示用时;meta 类瞬时完成不展示 */
    const dur = buildFmtDuration(state.elapsed_sec);
    sinceEl.textContent = dur ? '用时 ' + dur : '';
    killBtn.style.display = 'none';
  } else {
    badge.textContent = '空闲';
    badge.className = 'dash-badge';
    cmdEl.textContent = '';
    sinceEl.textContent = '';
    killBtn.style.display = 'none';
  }

  /* 日志(结构化段 → 文本节点 / 白名单 span)*/
  const logEl = document.getElementById('build-log');
  /* 同样内容就跳过重建,避免 DOM 抖动 + 用户选区被破坏 */
  const logKey = JSON.stringify(logSegments || []);
  if (buildLastLogKey !== logKey) {
    logEl.replaceChildren(buildLogFragment(logSegments));
    buildLastLogKey = logKey;
    const autoscroll = document.getElementById('build-log-autoscroll');
    if (autoscroll && autoscroll.checked) {
      logEl.scrollTop = logEl.scrollHeight;
    }
  }
  document.getElementById('build-log-size').textContent = buildFmtBytes(logSize || 0);
}

function buildLoadInline() {
  try {
    const data = JSON.parse(document.getElementById('build-data').textContent);
    buildParamsPath = data.params_path || '';
    buildLastCustomTarget = data.last_custom_target || '';
    buildApplyState(data.state || {}, data.log_segments || [], data.log_size || 0);
    /* 进入页面时若有正在跑的编译,启动轮询 */
    if (data.state && data.state.running) buildStartPolling();
  } catch (e) {
    console.warn('[build] load failed:', e);
  }
}

/* 调隐藏 action 端点,解 <pre id="result"> 里的 JSON */
async function buildCallAction(key) {
  const r = await fetch(apiUrl(key), { cache: 'no-store' });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const text = await r.text();
  const doc = new DOMParser().parseFromString(text, 'text/html');
  const el = doc.getElementById('result');
  if (!el) throw new Error('响应不含 #result');
  return JSON.parse(el.textContent);
}

/* 拉一次最新日志 + 状态,并让轮询与运行态保持同步:
 *   running     → 确保在轮询(幂等 start)
 *   非 running  → 确保停轮询(幂等 stop)
 * 关键:buildStartPolling / buildStopPolling **都不回调 buildPullOnce**。 */
async function buildPullOnce() {
  try {
    const data = await buildCallAction(BUILD_KEYS.log);
    const state = data.state || {};
    buildApplyState(state, data.log_segments || [], data.log_size || 0);
    if (state.running) buildStartPolling();
    else buildStopPolling();
  } catch (e) {
    /* 网络抖动不弹错,只 console */
    console.warn('[build] poll failed:', e);
  }
}

function buildStartPolling() {
  if (buildPollTimer) return;              // 已在轮询 —— 幂等,不重复起定时器
  buildPollTimer = setInterval(buildPullOnce, 2000);
  buildPullOnce();  // 立即拉一次,不等 2s(此刻 timer 已置位,再入 start 会直接返回)
}

function buildStopPolling() {
  if (!buildPollTimer) return;             // 本就没轮询 —— 直接返回,打破递归
  clearInterval(buildPollTimer);
  buildPollTimer = null;
}

/* 统一启动入口:弹 confirm → 调端点 → 启动轮询
   level 参数:第一次确认的视觉等级(info / warn / danger);
              二次确认 doubleConfirm 固定 danger,强调不可逆。 */
async function buildLaunch(key, displayCmd, promptText, doubleConfirm, level) {
  const finalPrompt = promptText + '\n\n命令：' + displayCmd;
  if (!(await dashConfirm(finalPrompt, {level: level || 'warn'}))) return;
  if (doubleConfirm && !(await dashConfirm(doubleConfirm, {level: 'danger'}))) return;
  try {
    const data = await buildCallAction(key);
    if (!data.success) {
      await dashAlert('❌ ' + (data.message || '启动失败'), {level: 'danger'});
      return;
    }
    buildStartPolling();
  } catch (e) {
    await dashAlert('❌ 请求失败：' + e.message, {level: 'danger'});
  }
}

/* ──── 完整编译 ──── */
function buildFull() {
  buildLaunch(
    BUILD_KEYS.full,
    'bash build.sh',
    '🚀 开始完整编译？\n\n将依次执行：\n  · 子模块 / 依赖自检\n  · CMake 配置 (Release 模式)\n' +
    '  · 编译 C++ 桥接层 + 全部游戏插件\n\n' +
    '⏱️ 预计耗时：2 核 CPU 约 20-30 分钟。\n' +
    '编译在子进程中运行，可关闭网页；过程中可点「🛑 终止编译」中止。',
    null, 'warn'
  );
}

/* ──── 增量编译 ──── */
function buildIncr() {
  buildLaunch(
    BUILD_KEYS.incr,
    'bash build.sh -i',
    '⚡ 开始增量编译？\n\n跳过 CMake 配置，只重编已变化的对象。\n' +
    '要求 build 目录已存在 (需先完整编译)。\n通常耗时数秒到几分钟。',
    null, 'info'
  );
}

/* ──── 增量编译桥接层 ──── */
function buildBridge() {
  buildLaunch(
    BUILD_KEYS.bridge,
    'bash build.sh -i -t LGTBot_ElainaBot',
    '🔌 增量编译桥接层 LGTBot_ElainaBot？\n\n' +
    '只重编桥接层 (改完 LGTBot_ElainaBot.cc 后最常用)，通常秒级完成。\n' +
    '要求 build 目录已存在。',
    null, 'info'
  );
}

/* ──── 列出可编译目标 ──── */
function buildList() {
  buildLaunch(
    BUILD_KEYS.list,
    'bash build.sh --list-targets',
    '📋 列出全部可编译目标？\n\n不执行任何编译，仅列出 CMake 已知 target，\n' +
    '用于「🎯 编译指定目标」按钮里填写。结果输出在下方日志框中。',
    null, 'info'
  );
}

/* ──── 目标名输入 → 校验 → 写参数文件 → 调端点 ────
   「🎯 编译指定目标」与「✨ 编译新游戏目标」共用:两者只差端点 key、命令
   展示与 confirm 文案(makeConfirm(target, cmd) 由调用方给)。 */
async function buildTargetFlow(key, cmdOf, makeConfirm) {
  /* 1. prompt 拿 target —— defaultValue 用上次编译的 target 名 */
  const raw = await dashPrompt(
    '请输入要编译的目标名称 (参考「📋 列出可编译目标」结果)：\n\n' +
    '允许字符：字母 / 数字 / 下划线 / 连字符\n' +
    '长度：1-63，首字符必须是字母或下划线',
    {level: 'info', defaultValue: buildLastCustomTarget}
  );
  if (raw == null) return;  /* 用户取消 */
  const target = raw.trim();

  /* 2. 前端白名单(后端有第二道闸) */
  if (!BUILD_TARGET_RE.test(target)) {
    await dashAlert(
      '❌ 目标名称非法：' + JSON.stringify(target) + '\n' +
      '只允许字母 / 数字 / 下划线 / 连字符，长度 1-63，首字符为字母或下划线。',
      {level: 'danger'}
    );
    return;
  }

  /* 3. 二次 confirm 显示完整命令 */
  const ok = await dashConfirm(makeConfirm(target, cmdOf(target)), {level: 'warn'});
  if (!ok) return;

  /* 4. 写参数文件 —— framework /api/config-file/save 接受 plugins/ 下绝对路径 */
  if (!buildParamsPath) {
    await dashAlert('❌ 参数文件路径未注入，无法继续 (请刷新页面)', {level: 'danger'});
    return;
  }
  try {
    const r = await fetch('/api/config-file/save' + TOKEN_QS, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        path: buildParamsPath,
        content: JSON.stringify({target: target}),
        format: 'json',
      }),
    });
    const wj = await r.json();
    if (!wj.success) {
      await dashAlert('❌ 写入参数文件失败：' + (wj.message || ''), {level: 'danger'});
      return;
    }
    /* 写盘成功:本地缓存同步更新,本次会话不重刷页面也能让下一次 prompt 预填 */
    buildLastCustomTarget = target;
  } catch (e) {
    await dashAlert('❌ 写参数请求失败：' + e.message, {level: 'danger'});
    return;
  }

  /* 5. 调编译端点 */
  try {
    const data = await buildCallAction(key);
    if (!data.success) {
      await dashAlert('❌ ' + (data.message || '启动失败'), {level: 'danger'});
      return;
    }
    buildStartPolling();
  } catch (e) {
    await dashAlert('❌ 请求失败：' + e.message, {level: 'danger'});
  }
}

/* ──── 编译指定目标(增量,-i) ──── */
function buildCustom() {
  return buildTargetFlow(
    BUILD_KEYS.custom,
    t => 'bash build.sh -i -t ' + t,
    (t, cmd) =>
      '🎯 确认编译目标「' + t + '」？\n\n命令：' + cmd +
      '\n\n如果该 target 在 CMake 中不存在，bash 会报错"No rule to make target"。'
  );
}

/* ──── 编译新游戏目标(cmake 重配置 + make,不带 -i) ──── */
function buildNewTarget() {
  return buildTargetFlow(
    BUILD_KEYS.newTarget,
    t => 'bash build.sh -t ' + t,
    (t, cmd) =>
      '✨ 确认编译新游戏目标「' + t + '」？\n\n命令：' + cmd +
      '\n\n将重新运行 CMake 配置以发现新增目标，再单独构建它 —— 用于刚放进\n' +
      'lgtbot/games/ 的新游戏（增量编译的缓存里没有它）。\n' +
      '比增量编译多一个配置阶段，通常 1-3 分钟。'
  );
}

/* ──── 终止编译 ──── */
async function buildKill() {
  const ok = await dashConfirm(
    '🛑 终止当前编译？\n\n将向编译进程组发送 SIGTERM(2 秒不响应升级 SIGKILL)。\n' +
    '已编译的 .o 中间产物会保留 (保留至下次增量编译)。',
    {level: 'warn'}
  );
  if (!ok) return;
  try {
    const data = await buildCallAction(BUILD_KEYS.kill);
    if (!data.success) {
      await dashAlert('❌ ' + (data.message || '终止失败'), {level: 'danger'});
      return;
    }
    /* 等 1s 让后端补完 finished 状态,再拉一次 */
    setTimeout(buildPullOnce, 1000);
  } catch (e) {
    await dashAlert('❌ 请求失败：' + e.message, {level: 'danger'});
  }
}

/* ──── 复制编译 API Token ──── */
async function buildCopyApiToken() {
  try {
    const data = await buildCallAction(BUILD_KEYS.apiToken);
    if (!data.success || !data.token) {
      await dashAlert('❌ 获取 token 失败', {level: 'danger'});
      return;
    }
    /* clipboard API 需要 https/localhost 安全上下文;HTTP 局域网面板走 textarea + execCommand 兜底 */
    let copied = false;
    if (navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(data.token); copied = true; } catch (e) {}
    }
    if (!copied) {
      const ta = document.createElement('textarea');
      ta.value = data.token;
      ta.style.cssText = 'position:fixed;opacity:0';
      document.body.appendChild(ta);
      ta.select();
      try { copied = document.execCommand('copy'); } catch (e) {}
      ta.remove();
    }
    const masked = data.token.slice(0, 8) + '…' + data.token.slice(-4);
    const ep = data.endpoints || {};
    await dashAlert(
      (copied ? '✅ API Token 已复制到剪贴板\n\n' : '⚠️ 复制失败，请手动记录：\n' + data.token + '\n\n') +
      'Token：' + masked + '\n存储文件：' + (data.path || '') +
      '\n\n编译端点：POST ' + (ep.compile || '') +
      '\n中断端点：POST ' + (ep.terminate || '') +
      '\n认证方式：Authorization: Bearer <token>',
      {level: 'info'}
    );
  } catch (e) {
    await dashAlert('❌ 请求失败：' + e.message, {level: 'danger'});
  }
}

/* ──── 清理重编(--clean) ──── */
function buildClean() {
  buildLaunch(
    BUILD_KEYS.clean,
    'bash build.sh --clean',
    '🧹 清理重编 (--clean)？\n\n' +
    '⚠️ 会先 rm -rf build/ (删除所有 CMake 缓存和编译产物)，然后从头完整编译。\n' +
    '等同于「🚀 完整编译」的总耗时，2 核 CPU 约 20-30 分钟。',
    '再次确认：删除 build 后重新完整编译？\n此操作不可撤销，所有现有 .so 都会被覆盖。',
    'warn'
  );
}

/* ──── 删除 build/ 目录 ──── */
async function buildRemove() {
  const ok1 = await dashConfirm(
    '🗑️ 删除 build 目录？\n\n命令：rm -rf <plugin_dir>/build\n\n' +
    '⚠️ 仅删除目录，不重新编译。删除后引擎将无法启动，\n' +
    '直到下次「🚀 完整编译」。',
    {level: 'warn'}
  );
  if (!ok1) return;
  const ok2 = await dashConfirm(
    '再次确认：永久删除 build 目录？\n此操作不可撤销。',
    {level: 'danger'}
  );
  if (!ok2) return;
  try {
    const data = await buildCallAction(BUILD_KEYS.remove);
    if (data.success) {
      await dashAlert('✅ ' + (data.message || 'build/ 已删除'), {level: 'info'});
    } else {
      await dashAlert('❌ ' + (data.message || '删除失败'), {level: 'danger'});
    }
    buildPullOnce();
  } catch (e) {
    await dashAlert('❌ 请求失败：' + e.message, {level: 'danger'});
  }
}

window.addEventListener('DOMContentLoaded', () => {
  buildLoadInline();

  document.getElementById('build-full').addEventListener('click', buildFull);
  document.getElementById('build-incr').addEventListener('click', buildIncr);
  document.getElementById('build-bridge').addEventListener('click', buildBridge);
  document.getElementById('build-list').addEventListener('click', buildList);
  document.getElementById('build-custom').addEventListener('click', buildCustom);
  document.getElementById('build-newtarget').addEventListener('click', buildNewTarget);
  document.getElementById('build-api-token').addEventListener('click', buildCopyApiToken);
  document.getElementById('build-kill-btn').addEventListener('click', buildKill);
  document.getElementById('build-clean').addEventListener('click', buildClean);
  document.getElementById('build-remove').addEventListener('click', buildRemove);

  /* 切换到「引擎编译」tab 时启动轮询(若有进行中的任务);
     切走时停轮询 —— 减少无效流量。 */
  document.querySelectorAll('.tabs .tab').forEach(tabBtn => {
    tabBtn.addEventListener('click', () => {
      if (tabBtn.dataset.tab === 'build') {
        /* 进 build tab —— 强制拉一次,如果在跑就开始轮询 */
        buildPullOnce();
      } else if (buildPollTimer) {
        /* 离开 build tab —— 停轮询(节省请求) */
        buildStopPolling();
      }
    });
  });
});
