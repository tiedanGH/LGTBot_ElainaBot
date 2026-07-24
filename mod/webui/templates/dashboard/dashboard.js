/* ──── Dashboard:版本/统计/缓存/配置 ────
 * 隐藏 action 端点 key 与 webui/main.py 的 _DASH_* 常量一一对应。
 * 配置保存复用主框架 /api/config-file/save(接受 plugins/ 下绝对路径)。
 */

const DASH_KEYS = {
  check_update:       '__lgtbot_dash_check_update',
  do_update:          '__lgtbot_dash_do_update',
  do_update_force:    '__lgtbot_dash_do_update_force',
  update_submodule:   '__lgtbot_dash_update_submodule',
  clear_avatar:       '__lgtbot_dash_clear_avatar',
  clear_avatar_7d:    '__lgtbot_dash_clear_avatar_7d',
  clear_gen:          '__lgtbot_dash_clear_gen',
  clear_gen_7d:       '__lgtbot_dash_clear_gen_7d',
  clear_match_all:    '__lgtbot_dash_clear_match_all',
  clear_match_7d:     '__lgtbot_dash_clear_match_7d',
  init_repo:          '__lgtbot_dash_init_repo',
  matches:            '__lgtbot_dash_matches',
};

/* 机器人绑定换绑端点(register_route 真路由,带 ?appid= 参数) */
const BIND_BOT_ROUTE = '/api/ext/lgtbot/bind-bot';
/* 注:reload_config / dash-config-* / dash-reload-config 全部搬迁到「配置管理」
   tab,见 templates/config/config.js (CFG_KEYS.reload_config) */

/* 插件目录的 git 状态 ——
 *   'ok'     = .git 存在,「更新桥接层」按钮走 dashDoUpdate
 *   'no_git' = 市场下载场景,按钮文案切「📥 初始化为 git 仓库」,走 dashInitRepo */
let dashRepoStatus = 'ok';

/* 缓存最近一次拿到的 submodule info —— 给「更新子模块」按钮的 confirm
   弹窗决定文案(初始化 vs 更新),也用来拼完整 git 命令展示。 */
let dashLastSubmoduleInfo = {};

/* 桥接层(本插件)自身仓库跳转链接信息 {repo_url, repo_owner, repo_name} ——
   首屏 get_data 与「检查更新」结果都会填,渲染时拼成「· 仓库 <a>owner/repo</a>」。 */
let dashBridgeRepo = {};

function dashBridgeRepoLink() {
  const r = dashBridgeRepo || {};
  if (!r.repo_url || !r.repo_owner || !r.repo_name) return '';
  return ' · 仓库 <a href="' + escapeHtml(r.repo_url) +
         '" target="_blank" rel="noopener">' +
         escapeHtml(r.repo_owner + '/' + r.repo_name) + '</a>';
}

function dashFmtBytes(n) {
  if (n == null) return '—';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(2) + ' MB';
  return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
}

/* 版本号统一加 v 前缀:'1.5.0' → 'v1.5.0'(若已带 v/V 则不重复加)。
   __plugin_meta__ 里 version='1.5.0' 不带 v;GitHub tag 是 'v1.5.0' 带 v,
   规范化到同一形式后视觉对齐,避免「本地 1.5.0 / 远端 v1.5.0」混杂。 */
function dashFmtVersion(v) {
  if (!v) return '—';
  return /^v/i.test(v) ? v : 'v' + v;
}

/* ──── 进行中的对局 ──── */
/* 开局至今的时长文案(客户端按 since epoch 秒粗算,秒/分/时/天四档)。 */
function dashFmtSince(ts) {
  if (!ts) return '';
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (secs < 60) return secs + ' 秒';
  if (secs < 3600) return Math.floor(secs / 60) + ' 分钟';
  if (secs < 86400) return Math.floor(secs / 3600) + ' 小时';
  return Math.floor(secs / 86400) + ' 天';
}

function dashRenderMatches(data) {
  const wrap = document.getElementById('dash-matches-list');
  const countEl = document.getElementById('dash-matches-count');
  if (!wrap) return;
  const matches = data.matches || [];
  if (countEl) countEl.textContent = matches.length ? ' (' + matches.length + ')' : '';
  if (!matches.length) {
    wrap.innerHTML = '<div class="dash-matches-empty">当前没有进行中的对局</div>';
    return;
  }
  wrap.innerHTML = matches.map(m => {
    const isUid = !!m.is_uid;
    const idText = escapeHtml(String(m.id || ''));
    /* 昵称(私信)/ 备注名(群聊)存在就只显示它;否则回退 openid(灰字 mono)。 */
    const loc = m.name
      ? escapeHtml(m.name)
      : '<span class="dash-mono dash-match-id">' + idText + '</span>';
    const game = m.game ? escapeHtml(m.game) : '未知游戏';
    const since = dashFmtSince(m.since);
    return '<div class="dash-match-row">' +
      '<span class="dash-match-type ' + (isUid ? 'dm' : 'grp') + '">' +
        (isUid ? '私信' : '群聊') + '</span>' +
      '<span class="dash-match-game" title="游戏">' + game + '</span>' +
      '<span class="dash-match-loc" title="' + (isUid ? '用户' : '群') + '">' + loc + '</span>' +
      (since ? '<span class="dash-match-since" title="已进行时长">🕒 ' + since + '</span>' : '') +
      '</div>';
  }).join('');
}

/* 进行中对局实时刷新 —— 由 main.js 的 setInterval 每几秒调一次。走只读轻量端点
 * (只返回对局列表,不跑缓存 os.walk 等重活),失败静默不打扰。与日志页同理。 */
async function dashMatchesRefresh() {
  try {
    const data = await dashCallAction(DASH_KEYS.matches);
    dashRenderMatches(data);
  } catch (e) {
    /* 静默:偶发网络抖动不干扰,下个周期自愈 */
  }
}

/* ──── 机器人绑定 ──── */
function dashRenderBots(data) {
  const wrap = document.getElementById('dash-bot-list');
  if (!wrap) return;
  const bots = data.bots || [];
  const bound = data.bound_appid || '';
  if (!bots.length) {
    wrap.innerHTML = '<span class="dash-msg-warn">未在主框架配置中发现任何机器人</span>';
    return;
  }
  wrap.innerHTML = bots.map(b => {
    const isBound = b.appid === bound;
    const qq = b.qq ? ('QQ：' + b.qq) : 'QQ 未配置';
    const vol = (b.full_volume == null) ? '—' : b.full_volume;
    return '<div class="dash-bot-row' + (isBound ? ' bound' : '') + '">' +
      '<span class="dash-mono">' + escapeHtml(b.appid) + '</span>' +
      '<span class="dash-bot-qq">' + escapeHtml(qq) + '</span>' +
      '<span class="dash-bot-vol" title="该机器人的全量群数量">🌐 全量群 ' +
        escapeHtml(String(vol)) + '</span>' +
      (isBound
        ? '<span class="dash-badge dash-badge-ok">当前绑定</span>'
        : '<button class="dash-btn dash-btn-small" data-bind-appid="' +
          escapeHtml(b.appid) + '">绑定</button>') +
      '</div>';
  }).join('');
  wrap.querySelectorAll('[data-bind-appid]').forEach(btn => {
    btn.addEventListener('click', () => dashBindBot(btn.dataset.bindAppid));
  });
}

async function dashBindBot(appid) {
  const ok = await dashConfirm(
    '确认绑定机器人 ' + appid + '？\n\n' +
    '绑定后仅该 bot 的消息会被处理，全量群等数据切换为该 bot 的数据库，' +
    '其他 bot 的事件将被静默忽略。',
    {level: 'warn'}
  );
  if (!ok) return;
  const msgEl = document.getElementById('dash-bot-msg');
  try {
    const url = BIND_BOT_ROUTE + TOKEN_QS + (TOKEN_QS ? '&' : '?') +
                'appid=' + encodeURIComponent(appid);
    const r = await fetch(url, { cache: 'no-store' });
    const data = await r.json();
    if (msgEl) msgEl.textContent = (data.success ? '✅ ' : '❌ ') + (data.message || '');
    if (data.success) dashRefreshAll();
  } catch (e) {
    if (msgEl) msgEl.textContent = '❌ 请求失败：' + e.message;
  }
}

/* ──── 新版本徽章(启动自检 / 手动检查共用) ──── */
function dashRenderUpdateHint(hint) {
  const el = document.getElementById('dash-update-hint');
  if (!el) return;
  if (hint && hint.has_update) {
    el.textContent = '✨ 有新版本';
    el.style.display = '';
  } else {
    el.style.display = 'none';
  }
}

/* ──── 极简 markdown 渲染(Release 正文用) ────
   支持:标题 #~####(降级渲染)、**加粗**、`行内代码`、``` 代码块、
   [文字](http 链接)、- / * 无序列表、--- 分隔线;其余按行输出。
   先 escapeHtml 再做替换,正文里的任何 HTML 都不会被执行。 */
function dashMdToHtml(md) {
  const lines = escapeHtml(md || '').split(/\r?\n/);
  const out = [];
  let inCode = false, inList = false;
  const closeList = () => { if (inList) { out.push('</ul>'); inList = false; } };
  for (const raw of lines) {
    if (/^\s*```/.test(raw)) {
      closeList();
      out.push(inCode ? '</code></pre>' : '<pre><code>');
      inCode = !inCode;
      continue;
    }
    if (inCode) { out.push(raw + '\n'); continue; }
    if (/^\s*(---+|\*\*\*+)\s*$/.test(raw)) { closeList(); out.push('<hr>'); continue; }
    const line = raw
      .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
               '<a href="$2" target="_blank" rel="noopener">$1</a>');
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      closeList();
      const lv = Math.min(h[1].length + 1, 6);
      out.push('<h' + lv + '>' + h[2] + '</h' + lv + '>');
      continue;
    }
    const li = line.match(/^\s*[-*+]\s+(.*)$/);
    if (li) {
      if (!inList) { out.push('<ul>'); inList = true; }
      out.push('<li>' + li[1] + '</li>');
      continue;
    }
    closeList();
    if (line.trim() === '') continue;
    out.push('<div>' + line + '</div>');
  }
  if (inCode) out.push('</code></pre>');
  closeList();
  return out.join('');
}

/* ──── Release 折叠卡(检查更新后出现,默认折叠) ────
 * data.release = {releases: [...]}:后端已筛成「比本地新的全部版本」(新→旧);
 * 已是最新时退化为只含最新一个。跨多个大版本升级(如 2.2.x → 2.4.0)会渲染多张卡片,各自独立展开查看。 */
function dashReleaseCard(r, latestLabel) {
  const dateStr = r.published_at ? r.published_at.slice(0, 10) : '';
  const title = (r.name || r.tag_name || '') + (dateStr ? '（' + dateStr + '）' : '');
  const link = r.html_url
    ? '<a class="dash-release-link" href="' + escapeHtml(r.html_url) +
      '" target="_blank" rel="noopener">↗ 在 GitHub 查看</a>'
    : '';
  /* 单个(已最新)沿用「最新 Release：」前缀,与旧版一致;多个时每卡按版本自述 */
  const prefix = latestLabel ? '最新 Release：' : '';
  return '<details class="dash-release">' +
      '<summary>📄 ' + prefix + escapeHtml(title) + '</summary>' +
      '<div class="dash-release-body">' + dashMdToHtml(r.body || '（无正文）') + link + '</div>' +
    '</details>';
}

function dashRenderRelease(rel) {
  const box = document.getElementById('dash-release-notes');
  if (!box) return;
  const list = (rel && Array.isArray(rel.releases)) ? rel.releases : [];
  if (!list.length) { box.innerHTML = ''; return; }
  const single = list.length === 1;
  box.innerHTML = list.map(r => dashReleaseCard(r, single)).join('');
}

/* ──── 运行环境自检(数据来自 prebuilt.self_check,随 dashboard-data 下发) ──── */
function dashRenderCheckItems(id, items, isCompile) {
  const box = document.getElementById(id);
  if (!box) return;
  if (!items || !items.length) { box.innerHTML = '<div class="dash-check-empty">无</div>'; return; }
  box.innerHTML = items.map(it => {
    /* 编译依赖缺失但可用预编译规避 → 灰点 +「预编译无需」标,不算错误(红); 运行时 warn 项缺失 → 黄点,计警告不计严重异常 */
    let dotCls, skipTag = '';
    if (it.ok) dotCls = 'dot-ok';
    else if (isCompile) { dotCls = 'dot-skip'; skipTag = '<span class="dash-check-skip">预编译无需</span>'; }
    else if (it.warn) dotCls = 'dot-warn';
    else dotCls = 'dot-bad';
    return '<div class="dash-check-item"><span class="dash-check-dot ' + dotCls + '"></span>' +
      '<span class="dash-check-name">' + escapeHtml(it.name || '') + '</span>' + skipTag +
      '<span class="dash-check-detail">' + escapeHtml(it.detail || '') + '</span></div>';
  }).join('');
}
function dashSetSelfcheckCollapsed(collapsed) {
  const body = document.getElementById('dash-selfcheck-body');
  const caret = document.getElementById('dash-selfcheck-caret');
  const section = document.getElementById('dash-selfcheck-section');
  if (body) body.classList.toggle('collapsed', collapsed);
  if (caret) caret.textContent = collapsed ? '▸' : '▾';
  if (section) section.classList.toggle('is-collapsed', collapsed);
}

/* 折叠态只在首屏按「有无异常 / 警告」自动决定一次(有红色异常或黄色警告都展开,
   全绿才折叠);之后(重新检测 / 换绑 / 清缓存等任何刷新)只更新检查项与红黄计数,
   **不改变折叠态** —— 此后折叠与否只由用户点标题控制。 */
let dashSelfcheckInited = false;

function dashRenderSelfCheck(sc) {
  if (!sc) return;
  dashRenderCheckItems('dash-runtime-list', sc.runtime, false);
  dashRenderCheckItems('dash-compile-list', sc.compile, true);
  /* 严重异常 = 运行时硬依赖缺失(红点);warn 项缺失只计警告(黄点);
     编译依赖缺失走「预编译无需」灰点,两者都不计 */
  const critical = (sc.runtime || []).filter(it => !it.ok && !it.warn).length;
  const warns    = (sc.runtime || []).filter(it => !it.ok && it.warn).length;
  const badge = document.getElementById('dash-selfcheck-badge');
  const warnBadge = document.getElementById('dash-selfcheck-badge-warn');
  if (badge) {
    if (critical > 0) {
      badge.textContent = '⚠ ' + critical + ' 项异常';
      badge.className = 'dash-selfcheck-badge bad';   // 红字
    } else if (warns > 0) {
      badge.textContent = '';                          // 有警告 → 不显示「环境正常」
      badge.className = 'dash-selfcheck-badge';
    } else {
      badge.textContent = '✓ 环境正常';                // 无异常且无警告才算正常
      badge.className = 'dash-selfcheck-badge ok';    // 绿字
    }
  }
  if (warnBadge) warnBadge.textContent = warns > 0 ? ('⚠ ' + warns + ' 项警告') : '';
  if (!dashSelfcheckInited) {
    dashSetSelfcheckCollapsed(critical === 0 && warns === 0);   // 首屏:有异常或警告都展开,全绿才折叠
    dashSelfcheckInited = true;
  }
}

function dashApplyData(data) {
  /* 进行中的对局 */
  dashRenderMatches(data);

  /* 运行环境自检 */
  dashRenderSelfCheck(data.self_check);

  /* 机器人绑定列表 */
  dashRenderBots(data);

  /* 启动自检的新版本 */
  dashRenderUpdateHint(data.update_hint || null);

  /* 版本号 + 引擎状态 */
  document.getElementById('dash-current-version').textContent = data.version || '—';
  const statusEl = document.getElementById('dash-engine-status');
  if (data.engine_running) {
    statusEl.textContent = '运行中';
    statusEl.className = 'dash-badge dash-badge-ok';
  } else {
    statusEl.textContent = '引擎未运行';
    statusEl.className = 'dash-badge dash-badge-err';
  }
  /* 引擎未运行 → 亮出「📦 预编译部署」跳转按钮(免编译快速部署引导) */
  const jumpBtn = document.getElementById('dash-prebuilt-jump');
  if (jumpBtn) jumpBtn.style.display = data.engine_running ? 'none' : '';

  /* 子模块初始状态(get_data 只填本地 commit,远端留给检查更新按钮)*/
  if (data.submodule) {
    dashRepoStatus = data.submodule.repo_status || 'ok';
    dashRenderSubmoduleStatus(data.submodule);
  }
  /* 桥接层仓库链接(首屏 / 刷新都渲染,同子模块的本地态渲染)*/
  if (data.bridge) dashBridgeRepo = data.bridge;
  const bDetail = document.getElementById('dash-bridge-detail');
  const bBtn = document.getElementById('dash-do-update');
  if (dashRepoStatus === 'no_git') {
    /* no_git 时主动把按钮切到「初始化」状态 */
    bDetail.innerHTML = '<span class="dash-msg-warn">⚠️ 未检测到 .git/ '
                      + '(可能从插件市场安装)。点击右侧按钮把当前目录'
                      + '初始化为 git 仓库，方可使用更新功能。</span>'
                      + dashBridgeRepoLink();
    bBtn.textContent = '📥 初始化为 git 仓库';
    bBtn.style.display = '';
  } else {
    /* ok 时初始文案「点击检查更新查看版本」+ 仓库链接;检查更新后由 dashRenderBridgeStatus 覆盖成版本对比 */
    bDetail.innerHTML = '点击「检查更新」查看版本' + dashBridgeRepoLink();
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

/* 整页刷新 → 抠出新的 dashboard-data JSON,只刷新本标签的状态(用户的标签切换 / 编辑器脏标记都不会被破坏)。
 * 无专属刷新按钮 —— 由换绑 / 清缓存 / 初始化仓库等操作成功后程序化调用。 */
async function dashRefreshAll() {
  try {
    const r = await fetch(apiUrl(PAGE_KEY), { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const text = await r.text();
    const m = text.match(/<script id="dashboard-data"[^>]*>([\s\S]*?)<\/script>/);
    if (m) dashApplyData(JSON.parse(m[1]));
  } catch (e) {
    console.warn('[dashboard] refresh failed:', e);
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
    // 检查更新结果里也带 repo_status (submodule info 自带);同步给桥接层渲染
    if (data.submodule) {
      dashRepoStatus = data.submodule.repo_status || 'ok';
    }
    dashRenderBridgeStatus(data.bridge || {});
    dashRenderSubmoduleStatus(data.submodule || {});
    dashRenderRelease(data.release || null);
    const b = data.bridge || {};
    dashRenderUpdateHint({
      has_update: !!(b.success && b.has_update),
      remote_version: b.remote_version || '',
    });
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

  /* 检查更新结果里带仓库链接字段,存起来供本行各分支拼接(同 get_data) */
  if (bridge && (bridge.repo_url || bridge.repo_owner)) dashBridgeRepo = bridge;
  const repoLink = dashBridgeRepoLink();

  /* 最高优先级: 插件目录不是 git 仓库 → 把这一行整个切到「初始化」分支,
     不展示版本对比信息(也对比不了,本地没 git history) */
  if (dashRepoStatus === 'no_git') {
    detail.innerHTML = '<span class="dash-msg-warn">⚠️ 未检测到 .git/ '
                     + '(可能从插件市场安装)。点击右侧按钮把当前目录'
                     + '初始化为 git 仓库，方可使用更新功能。</span>' + repoLink;
    btn.textContent = '📥 初始化为 git 仓库';
    btn.style.display = '';
    return;
  }

  /* 正常路径: 显式还原按钮文案(防止用户之前看到过初始化按钮文案残留) */
  btn.textContent = '⬇ 更新桥接层';

  if (!bridge || !bridge.success) {
    detail.innerHTML = '<span class="dash-msg-err">❌ ' +
      escapeHtml(bridge && bridge.error ? bridge.error : '检查失败') + '</span>' + repoLink;
    btn.style.display = 'none';
    return;
  }
  const local = dashFmtVersion(bridge.local_version);
  const remote = dashFmtVersion(bridge.remote_version);
  if (bridge.has_update) {
    detail.innerHTML = '<span class="dash-msg-warn">✨ 本地 <b>' +
      escapeHtml(local) + '</b> → 远端 <b>' + escapeHtml(remote) + '</b></span>' + repoLink;
    btn.style.display = '';
  } else {
    detail.innerHTML = '<span class="dash-msg-ok">✅ 已是最新版本 (' + escapeHtml(remote) + ')</span>' + repoLink;
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

/* ──── 更新桥接层(git pull --ff-only origin main) ──── */
async function dashDoUpdate() {
  const cmd = 'git pull --ff-only origin main';
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
    /* 失败时附加「💥 强制更新」按钮 —— 典型场景:工作区脏 (`would be
       overwritten by merge`) / 本地与远端分叉无法 ff 等。本插件多数用户
       不会主动 commit,远端有新版时本地几乎一定脏,所以兜底按钮是常用路径。 */
    if (!data.success) {
      html += '<div class="dash-update-force-row" style="margin-top:10px">' +
              '<button id="dash-do-update-force" class="dash-btn dash-btn-warn">' +
              '💥 强制更新 (丢弃本地修改)' +
              '</button>' +
              '<span class="dash-msg-warn" style="margin-left:8px;font-size:12px">' +
              '会执行 git reset --hard origin/main，本地未提交的代码改动将丢失' +
              '</span></div>';
    }
    resEl.innerHTML = html;
    /* 把刚渲染进 DOM 的强制更新按钮绑事件(每次失败都会重新注入,所以
       不能用一次性 addEventListener 注册到固定 id) */
    const forceBtn = document.getElementById('dash-do-update-force');
    if (forceBtn) forceBtn.addEventListener('click', dashDoUpdateForce);
  } catch (e) {
    resEl.innerHTML = '<span class="dash-msg-err">❌ ' + escapeHtml(e.message) + '</span>';
  } finally {
    btn.disabled = false;
    btn.textContent = '⬇ 更新桥接层';
  }
}

/* ──── 强制更新桥接层(丢弃本地修改) ────
 * 普通 git pull 失败的兜底:fetch + reset --hard origin/main。会覆盖工作区
 * 已 tracked 的文件;data/、build/、lgtbot/ 因 .gitignore 排除不受影响。
 * danger 级双 confirm,避免误触丢失代码改动。 */
async function dashDoUpdateForce() {
  const cmd = 'git fetch origin && git reset --hard origin/main';
  const ok1 = await dashConfirm(
    '⚠️ 确认强制更新桥接层？\n\n将执行：\n  ' + cmd +
    '\n\n会**丢弃所有未提交的本地修改** (已 tracked 文件)。\n' +
    'data/、build/、lgtbot/ 等运行时目录因被 .gitignore 排除不受影响。',
    {level: 'warn'}
  );
  if (!ok1) return;
  const ok2 = await dashConfirm(
    '再次确认：已 tracked 文件的本地改动将永久丢失，无法恢复。\n\n' +
    '若不确定，请先手动 git stash 备份你的改动后再执行。',
    {level: 'danger'}
  );
  if (!ok2) return;

  const forceBtn = document.getElementById('dash-do-update-force');
  const resEl = document.getElementById('dash-update-result');
  if (forceBtn) {
    forceBtn.disabled = true;
    forceBtn.textContent = '⏳ 强制更新中……';
  }
  try {
    const data = await dashCallAction(DASH_KEYS.do_update_force);
    let html = '<div class="dash-msg-info">执行命令：<code class="dash-mono">' +
               escapeHtml(cmd) + '</code></div>';
    if (data.success) {
      html += '<div class="dash-msg-ok">' + escapeHtml(data.message) + '</div>';
    } else {
      html += '<div class="dash-msg-err">' + escapeHtml(data.message || '强制更新失败') + '</div>';
    }
    /* 后端返回的 stages 是 (label, rc, stdout, stderr) 元组列表 —— 同 init-repo */
    if (Array.isArray(data.stages) && data.stages.length) {
      const items = data.stages.map(s => {
        const label = escapeHtml(s[0] || '');
        const rc = s[1];
        const out = (s[2] || '') + (s[3] ? (s[2] ? '\n' : '') + s[3] : '');
        return '<li><code class="dash-mono">' + label + '</code> (rc=' +
               escapeHtml(String(rc)) + ')' +
               (out ? '<pre class="dash-pre">' + escapeHtml(out) + '</pre>' : '') +
               '</li>';
      });
      html += '<ul class="dash-pluginconf-changes">' + items.join('') + '</ul>';
    }
    resEl.innerHTML = html;
  } catch (e) {
    resEl.innerHTML = '<span class="dash-msg-err">❌ ' + escapeHtml(e.message) + '</span>';
  } finally {
    if (forceBtn) {
      forceBtn.disabled = false;
      forceBtn.textContent = '💥 强制更新 (丢弃本地修改)';
    }
  }
}

/* ──── 把插件目录初始化为 git 仓库(市场用户专用) ──── */
async function dashInitRepo() {
  const ok = await dashConfirm(
    '把当前插件目录初始化为 git 仓库?\n\n' +
    '将在插件目录下依次执行:\n' +
    '  · git init -b main\n' +
    '  · git remote add origin <插件 GitHub URL>\n' +
    '  · git fetch origin --tags --depth 50\n' +
    '  · git reset --mixed v<当前版本> (失败则 fallback origin/main)\n\n' +
    '只动 index，不动工作区文件 —— data/、build/、lgtbot/ 全部保留。\n\n' +
    '需要本机已安装 git 客户端。',
    {level: 'danger'}
  );
  if (!ok) return;
  const btn = document.getElementById('dash-do-update');
  const resEl = document.getElementById('dash-update-result');
  btn.disabled = true;
  btn.textContent = '⏳ 初始化中……';
  resEl.innerHTML = '';
  try {
    const data = await dashCallAction(DASH_KEYS.init_repo);
    const parts = [];
    if (data.success) {
      parts.push('<div class="dash-msg-ok">' + escapeHtml(data.message) + '</div>');
      if (data.fallback_to_main) {
        parts.push('<div class="dash-msg-warn">ℹ️ 当前版本 tag 在远端不存在，' +
                   '已 fallback 到 origin/main</div>');
      }
    } else {
      parts.push('<div class="dash-msg-err">❌ ' +
                 escapeHtml(data.message || '初始化失败') + '</div>');
    }
    if (Array.isArray(data.stages) && data.stages.length) {
      const items = data.stages.map(s => {
        const label = escapeHtml(s[0] || '');
        const rc = s[1];
        const out = (s[2] || '') + (s[3] ? (s[2] ? '\n' : '') + s[3] : '');
        return '<li><code class="dash-mono">' + label + '</code> (rc=' +
               escapeHtml(String(rc)) + ')' +
               (out ? '<pre class="dash-pre">' + escapeHtml(out) + '</pre>' : '') +
               '</li>';
      });
      parts.push('<ul class="dash-pluginconf-changes">' + items.join('') + '</ul>');
    }
    resEl.innerHTML = parts.join('');
    /* 成功 → 整页刷新,dashRepoStatus 会回到 'ok',按钮自动切回「检查更新」流程 */
    if (data.success) dashRefreshAll();
  } catch (e) {
    resEl.innerHTML = '<span class="dash-msg-err">❌ ' + escapeHtml(e.message) + '</span>';
  } finally {
    btn.disabled = false;
    /* 按当前状态决定文案: 成功 → 走 'ok' 文案;失败 → 保持 no_git 文案让用户重试 */
    btn.textContent = (dashRepoStatus === 'no_git'
                        ? '📥 初始化为 git 仓库'
                        : '⬇ 更新桥接层');
  }
}

/* 桥接层行按钮的分发器: 按 dashRepoStatus 决定调哪个 handler。
   一个 button 元素 + 一个 click listener,handler 根据状态分支。 */
async function dashBridgeButtonClick() {
  if (dashRepoStatus === 'no_git') return dashInitRepo();
  return dashDoUpdate();
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

/* 注:dashShowConfigMsg / dashSaveConfig / dashRevertConfig 已搬迁至
   templates/config/config.js (cfgSave / cfgRevert / cfgShowMsg) —— 那里支持
   4 块编辑器(yaml / notice / trouble / engine),不再受限于"引擎配置"一项。 */

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
      msgEl.textContent = '✅ ' + (data.message || '清理完成') + ' (删除 ' + (data.removed || 0) + ' 项)';
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

/* 注:dashReloadConfig 已搬到 templates/config/config.js 的 cfgReloadConfig,
   action key 不变(__lgtbot_dash_reload_config),webui/main.py 中 provider
   现在是 page_config.render_reload_config。 */

window.addEventListener('DOMContentLoaded', () => {
  dashLoadInline();

  document.getElementById('dash-check-update').addEventListener('click', dashCheckUpdate);
  /* 桥接层行按钮: dashRepoStatus 决定调 dashInitRepo 还是 dashDoUpdate */
  document.getElementById('dash-do-update').addEventListener('click', dashBridgeButtonClick);
  document.getElementById('dash-update-submodule').addEventListener('click', dashDoUpdateSubmodule);

  /* 缓存清理按钮(委托) */
  document.querySelectorAll('[data-clear]').forEach(btn => {
    btn.addEventListener('click', () => dashClearCache(btn.dataset.clear));
  });

  /* 引擎未运行时的「📦 预编译部署」跳转 → 切到该标签 */
  const jumpBtn = document.getElementById('dash-prebuilt-jump');
  if (jumpBtn) jumpBtn.addEventListener('click', () => {
    const t = document.querySelector('.tabs .tab[data-tab="prebuilt"]');
    if (t) t.click();
  });
  /* 标题点击 → 展开 / 折叠(折叠态此后完全由用户控制) */
  const scToggle = document.getElementById('dash-selfcheck-toggle');
  if (scToggle) scToggle.addEventListener('click', () => {
    const body = document.getElementById('dash-selfcheck-body');
    dashSetSelfcheckCollapsed(!(body && body.classList.contains('collapsed')));
  });
  /* 运行环境自检「重新检测」→ 只重新拉 dashboard-data(含最新 self_check)刷新
     检查项与红字计数,**不改变折叠态**(见 dashRenderSelfCheck 的首屏一次性规则) */
  const scBtn = document.getElementById('dash-selfcheck-refresh');
  if (scBtn) scBtn.addEventListener('click', dashRefreshAll);
});
