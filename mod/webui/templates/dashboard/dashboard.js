/* ──── Dashboard:版本/统计/缓存/配置 ────
 * 隐藏 action 端点 key 与 webui/main.py 的 _DASH_* 常量一一对应。
 * 配置保存复用主框架 /api/config-file/save(接受 plugins/ 下绝对路径)。
 *
 * 全部 confirm / alert / 状态文案统一中文全角标点(,。:;()「」?!)。
 * 状态用 emoji + .dash-msg-ok / dash-msg-warn / dash-msg-err 颜色双重表达。
 */

const DASH_KEYS = {
  check_update:     '__lgtbot_dash_check_update',
  do_update:        '__lgtbot_dash_do_update',
  clear_avatar:     '__lgtbot_dash_clear_avatar',
  clear_avatar_7d:  '__lgtbot_dash_clear_avatar_7d',
  clear_gen:        '__lgtbot_dash_clear_gen',
  clear_gen_7d:     '__lgtbot_dash_clear_gen_7d',
  clear_match_all:  '__lgtbot_dash_clear_match_all',
  clear_match_7d:   '__lgtbot_dash_clear_match_7d',
};

/* 引擎配置绝对路径(由 dashboard-data 注入),保存请求里要原样回传 */
let dashConfigAbsPath = '';
let dashConfigOriginal = '';

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
  document.getElementById('dash-current-version').textContent = dashFmtVersion(data.version);
  const statusEl = document.getElementById('dash-engine-status');
  if (data.engine_running) {
    statusEl.textContent = '引擎运行中';
    statusEl.className = 'dash-badge dash-badge-ok';
  } else {
    statusEl.textContent = '引擎未运行';
    statusEl.className = 'dash-badge dash-badge-warn';
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

/* ──── 检查更新 ──── */
async function dashCheckUpdate() {
  const btn = document.getElementById('dash-check-update');
  const updBtn = document.getElementById('dash-do-update');
  const resEl = document.getElementById('dash-update-result');
  btn.disabled = true;
  btn.textContent = '⏳ 检查中……';
  resEl.innerHTML = '';
  try {
    const data = await dashCallAction(DASH_KEYS.check_update);
    if (!data.success) {
      resEl.innerHTML = '<span class="dash-msg-err">❌ ' + escapeHtml(data.message || '检查失败') + '</span>';
      updBtn.style.display = 'none';
      return;
    }
    const local = dashFmtVersion(data.local_version);
    const remote = dashFmtVersion(data.remote_version);
    if (data.has_update) {
      resEl.innerHTML = '<span class="dash-msg-warn">🆕 发现新版本：' +
        '<b>' + escapeHtml(local) + '</b> → <b>' + escapeHtml(remote) + '</b></span>';
      updBtn.style.display = '';
    } else {
      resEl.innerHTML = '<span class="dash-msg-ok">✅ 已是最新版本（' + escapeHtml(remote) + '）</span>';
      updBtn.style.display = 'none';
    }
  } catch (e) {
    resEl.innerHTML = '<span class="dash-msg-err">❌ ' + escapeHtml(e.message) + '</span>';
  } finally {
    btn.disabled = false;
    btn.textContent = '🔍 检查更新';
  }
}

/* ──── 一键更新(git pull --ff-only) ──── */
async function dashDoUpdate() {
  if (!confirm('确认执行 git pull --ff-only？\n更新完成后需要重启 LGTBot 引擎或重启进程才能加载新版本。')) {
    return;
  }
  const btn = document.getElementById('dash-do-update');
  const resEl = document.getElementById('dash-update-result');
  btn.disabled = true;
  btn.textContent = '⏳ 更新中……';
  try {
    const data = await dashCallAction(DASH_KEYS.do_update);
    let html = '';
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
    btn.textContent = '⬇ 立即更新';
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
/* DASH_CLEAR_PROMPTS:每个 which → confirm 文案数组,
 *   长度 1 = 单次确认(用于图片缓存:bot 发送一次后就没用了,不易误删)
 *   长度 2 = 双次确认(头像 / 赛况:误删会有可视影响,需要二次确认)
 */
const DASH_CLEAR_PROMPTS = {
  avatar: [
    '清理「头像缓存」(engine/images/avatar)？\n所有头像 PNG 将被删除，引擎下次需要时会自动重新下载。',
    '再次确认：删除头像缓存，无法恢复？',
  ],
  avatar_7d: [
    '仅保留最近 7 天的「头像缓存」，删除其它文件？',
    '再次确认：删除 7 天前的头像缓存，无法恢复？',
  ],
  gen: [
    '清理「图片缓存」(engine/images/gen)？\n图片在 bot 发送一次后就没用了，本次清理不影响已发出的消息。',
  ],
  gen_7d: [
    '仅保留最近 7 天的「图片缓存」，删除其它文件？',
  ],
  match_all: [
    '清理「赛况缓存」全部(engine/images/match)？\n所有对局的渲染图都将被删除，历史战绩页面里的图片链接会失效。',
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
  /* 依次弹出每条 prompt,任一取消即中断 */
  for (let i = 0; i < prompts.length; i++) {
    if (!confirm(prompts[i])) return;
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

window.addEventListener('DOMContentLoaded', () => {
  dashLoadInline();

  document.getElementById('dash-check-update').addEventListener('click', dashCheckUpdate);
  document.getElementById('dash-do-update').addEventListener('click', dashDoUpdate);
  document.getElementById('dash-stats-refresh').addEventListener('click', dashRefreshAll);
  document.getElementById('dash-config-save').addEventListener('click', dashSaveConfig);
  document.getElementById('dash-config-revert').addEventListener('click', dashRevertConfig);

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
