/* ──── 主模板提供的全局 ──── */
const PAGE_KEY = '__PAGE_KEY__';
const RESTART_KEY = '__RESTART_KEY__';
/* 计划重启走真路由(要带 ?reason= 维护原因;隐藏 action 的 provider 不接参数) */
const PLANNED_RESTART_ROUTE = '/api/ext/lgtbot/planned-restart';
/* 服务端渲染时注入的「计划重启」当前状态(1=维护模式开启) */
const PLANNED_RESTART_ON = '__PLANNED_ON__' === '1';
const REFRESH_MS = 3000;
const STORAGE_THEME = 'lgtbot-page-theme';

/* iframe 的 src 里带 ?token=... (auth.require_auth 只认 Bearer / ?token);
 * 内部 fetch 默认不带,要从 location.search 抠出来再拼回去。 */
const TOKEN_QS = (function () {
  const m = location.search.match(/[?&]token=([^&]+)/);
  return m ? ('?token=' + m[1]) : '';
})();
const apiUrl = (key) => '/api/web-pages/' + encodeURIComponent(key) + TOKEN_QS;

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

/* ──── 主题(顶部标题栏右侧的 #theme-toggle,整页通用)────
   图标显示「当前主题」: 浅色 ☀,深色 🌙(展示当前态而非目标态)。
   默认 light;localStorage 持久化用户选择。 */
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const btn = document.getElementById('theme-toggle');
  if (btn) btn.textContent = (theme === 'dark') ? '🌙' : '☀';
  try { localStorage.setItem(STORAGE_THEME, theme); } catch (e) {}
}
function initTheme() {
  let saved = 'light';
  try { saved = localStorage.getItem(STORAGE_THEME) || 'light'; } catch (e) {}
  applyTheme(saved);
}
document.getElementById('theme-toggle').addEventListener('click', () => {
  const cur = document.documentElement.getAttribute('data-theme');
  applyTheme(cur === 'dark' ? 'light' : 'dark');
});

/* ──── 全屏按钮──── 在新窗口打开本页面的独立全屏视图。
   脱离侧边栏 iframe 外壳整页铺满;token 透传保证新窗口里各 action fetch 仍鉴权通过。 */
const _fullscreenBtn = document.getElementById('fullscreen-btn');
if (_fullscreenBtn) {
  _fullscreenBtn.addEventListener('click', () => {
    window.open(apiUrl(PAGE_KEY), '_blank', 'noopener');
  });
}

/* ──── 标签切换 ──── */
document.querySelectorAll('.tabs .tab').forEach(btn => {
  btn.addEventListener('click', () => {
    const t = btn.dataset.tab;
    document.querySelectorAll('.tabs .tab').forEach(b => b.classList.toggle('active', b === btn));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id === 'tab-' + t));
  });
});

/* ──── 顶部全宽横幅(重启按钮反馈用)──── */
function showBanner(msg, isWarning) {
  const old = document.getElementById('lgtbot-banner');
  if (old) old.remove();
  const b = document.createElement('div');
  b.id = 'lgtbot-banner';
  b.style.cssText =
    'position:fixed;top:0;left:0;right:0;padding:14px 24px;text-align:center;' +
    'background:' + (isWarning ? '#dc3545' : '#1d6fdc') + ';' +
    'color:#fff;z-index:9999;box-shadow:0 2px 8px rgba(0,0,0,.18);' +
    'font-size:14px;font-weight:500;white-space:pre-wrap;line-height:1.6;';
  b.textContent = msg;
  document.body.appendChild(b);
  setTimeout(() => b.remove(), 8000);
}

/* ──── 计划重启按钮 ──── 切换维护模式:暂停创建新游戏。 */
function applyPlannedRestartUI(on) {
  const btn = document.getElementById('planned-restart-btn');
  if (!btn) return;
  btn.textContent = on ? '🚧 取消计划重启' : '🚧 计划重启';
  btn.classList.toggle('active', on);
}

document.getElementById('planned-restart-btn').addEventListener('click', async () => {
  const btn = document.getElementById('planned-restart-btn');
  const isOn = btn.classList.contains('active');
  let reason = '';
  let auto = false;
  if (isOn) {
    const ok = await dashConfirm('确认取消计划重启？\n\n将立即恢复玩家创建新游戏。',
                                 {level: 'info'});
    if (!ok) return;
  } else {
    /* 开启时用 prompt 收集维护原因(可留空)+「自动重启」勾选(默认不勾)。
       手动模式拦新建房间;自动模式不限制,对局清空并静默 30s 后自动重启。 */
    const input = await dashPrompt(
      '启用计划重启？\n\n手动模式（默认）：玩家无法创建新游戏（进行中的对局与已建房间不受影响），逐渐清空对局后手动重启。\n自动重启：不限制新游戏创建，全部对局结束并静默 30 秒后自动执行重启。\n\n可填写维护原因（将展示给玩家）：',
      {defaultValue: '', okText: '启用', level: 'warn',
       checkbox: '自动重启：全部对局结束后自动重启（不限制新游戏创建）'}
    );
    if (input === null) return;
    reason = (input.value || '').trim();
    auto = !!input.checked;
  }
  try {
    /* TOKEN_QS 可能为空 → 分隔符要动态判断(同 dashboard.js 的 BIND_BOT_ROUTE) */
    let url = PLANNED_RESTART_ROUTE + TOKEN_QS;
    let sep = TOKEN_QS ? '&' : '?';
    if (reason) { url += sep + 'reason=' + encodeURIComponent(reason); sep = '&'; }
    if (auto) { url += sep + 'auto=1'; }
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const text = await r.text();
    const doc = new DOMParser().parseFromString(text, 'text/html');
    const msgEl = doc.getElementById('msg');
    const stEl = doc.getElementById('state');
    const nowOn = !!(stEl && stEl.textContent.trim() === '1');
    applyPlannedRestartUI(nowOn);
    showBanner(msgEl ? msgEl.textContent.trim() : '已切换', nowOn);
  } catch (e) {
    showBanner('计划重启切换失败：' + e.message, true);
  }
});

/* ──── 重启按钮(整页通用,标题栏右侧)──── */
document.getElementById('restart-btn').addEventListener('click', async () => {
  const ok = await dashConfirm(
    '确认重启 LGTBot？\n\n将以新进程重新加载 C++ 引擎、bridge 与全部游戏插件。\n若存在进行中的对局会自动拒绝重启。',
    {level: 'warn'}
  );
  if (!ok) return;
  try {
    const r = await fetch(apiUrl(RESTART_KEY), { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const text = await r.text();
    const doc = new DOMParser().parseFromString(text, 'text/html');
    const msgEl = doc.getElementById('msg');
    const msg = msgEl ? msgEl.textContent.trim() : '已请求重启';
    const isWarn = msg.includes('⚠️') || msg.includes('❌') || msg.includes('ℹ️');
    showBanner(msg, isWarn);
  } catch (e) {
    showBanner('重启请求失败：' + e.message, true);
  }
});

/* ═══════════════════════════════════════════════════════════════════════
 * 全局自定义弹窗 —— 替代浏览器 confirm / alert / prompt
 * ───────────────────────────────────────────────────────────────────────
 * API(都返回 Promise,务必 await):
 *   const ok    = await dashConfirm('确认删除?', {level: 'danger'});
 *   await dashAlert('操作失败', {level: 'danger'});
 *   const value = await dashPrompt('输入名称', {defaultValue: 'foo'});
 *
 * level:
 *   · 'info'   常规操作(默认),OK 按钮 primary 蓝
 *   · 'warn'   常规风险(橙黄色顶 banner + 橙字),OK 按钮 warn 橙
 *   · 'danger' 严重风险 / 不可逆(红顶 banner + 粗体红字),OK 按钮 danger 红
 *
 * 键盘:Enter = OK,Esc = Cancel(prompt 里 input 聚焦,Enter 提交输入值)
 * 鼠标:点 backdrop 空白 = 取消(不会穿透到 modal 内部)
 *
 * 视觉骨架在 main.html 末尾的 #dash-modal-backdrop,样式在 main.css。
 * ═══════════════════════════════════════════════════════════════════════ */
let _dashModalResolve = null;
let _dashModalKind = 'confirm';
let _dashModalHasCheckbox = false;
const _DASH_OK_TEXT = { confirm: '确定', alert: '我知道了', prompt: '确认' };

function _dashOpenModal(opts) {
  const { message, level, kind, defaultValue, okText, cancelText, checkbox } = opts;
  return new Promise(resolve => {
    /* 上一个 modal 还未结算就被新的覆盖时,旧 promise resolve 取消值,防止上层 await 永远 stuck */
    if (_dashModalResolve) {
      _dashModalResolve(_dashModalKind === 'prompt' ? null : false);
      _dashModalResolve = null;
    }
    _dashModalResolve = resolve;
    _dashModalKind = kind;
    _dashModalHasCheckbox = !!checkbox;

    const backdrop = document.getElementById('dash-modal-backdrop');
    const modal    = document.getElementById('dash-modal');
    const msgEl    = document.getElementById('dash-modal-message');
    const inputEl  = document.getElementById('dash-modal-input');
    const cancelBtn = document.getElementById('dash-modal-cancel');
    const okBtn     = document.getElementById('dash-modal-ok');
    const cbRow    = document.getElementById('dash-modal-checkbox-row');
    const cbEl     = document.getElementById('dash-modal-checkbox');
    const cbLabel  = document.getElementById('dash-modal-checkbox-label');

    msgEl.textContent = message || '';
    modal.className = 'dash-modal level-' + (level || 'info');

    if (kind === 'prompt') {
      inputEl.style.display = '';
      inputEl.value = defaultValue == null ? '' : String(defaultValue);
    } else {
      inputEl.style.display = 'none';
      inputEl.value = '';
    }
    /* 可选勾选项(如计划重启的「自动重启」):默认不勾选,每次打开都复位 */
    if (cbRow) {
      cbRow.style.display = checkbox ? '' : 'none';
      if (cbEl) cbEl.checked = false;
      if (cbLabel) cbLabel.textContent = checkbox || '';
    }
    /* alert 只要一个「我知道了」,不显示取消 */
    cancelBtn.style.display = (kind === 'alert') ? 'none' : '';
    okBtn.textContent     = okText     || _DASH_OK_TEXT[kind] || '确定';
    cancelBtn.textContent = cancelText || '取消';
    /* OK 按钮颜色随 level 切换 */
    okBtn.className = 'dash-btn ' + (
      level === 'danger' ? 'dash-btn-danger'
      : level === 'warn' ? 'dash-btn-warn'
      : 'dash-btn-primary'
    );

    backdrop.classList.remove('hidden');
    backdrop.setAttribute('aria-hidden', 'false');
    /* prompt 聚焦输入框便于直接打字;其它聚焦 OK 让 Enter 立刻生效 */
    setTimeout(() => {
      if (kind === 'prompt') { inputEl.focus(); inputEl.select(); }
      else okBtn.focus();
    }, 0);
  });
}

function _dashCloseModal(value) {
  const backdrop = document.getElementById('dash-modal-backdrop');
  backdrop.classList.add('hidden');
  backdrop.setAttribute('aria-hidden', 'true');
  const r = _dashModalResolve;
  _dashModalResolve = null;
  if (r) r(value);
}

function dashConfirm(message, opts) {
  const o = opts || {};
  return _dashOpenModal({
    message, kind: 'confirm', level: o.level,
    okText: o.okText, cancelText: o.cancelText,
  });
}
function dashAlert(message, opts) {
  const o = opts || {};
  return _dashOpenModal({
    message, kind: 'alert', level: o.level, okText: o.okText,
  });
}
function dashPrompt(message, opts) {
  /* opts.checkbox 非空时弹窗底部多一个勾选项(默认不勾),返回值变为 {value, checked}|null。 */
  const o = opts || {};
  return _dashOpenModal({
    message, kind: 'prompt', level: o.level,
    defaultValue: o.defaultValue, checkbox: o.checkbox,
    okText: o.okText, cancelText: o.cancelText,
  });
}

function _dashBindModalEvents() {
  const backdrop  = document.getElementById('dash-modal-backdrop');
  if (!backdrop) return;
  const modal     = document.getElementById('dash-modal');
  const inputEl   = document.getElementById('dash-modal-input');
  const okBtn     = document.getElementById('dash-modal-ok');
  const cancelBtn = document.getElementById('dash-modal-cancel');

  const doOk = () => {
    if (_dashModalKind === 'prompt') {
      const cbEl = document.getElementById('dash-modal-checkbox');
      _dashCloseModal(_dashModalHasCheckbox
        ? { value: inputEl.value, checked: !!(cbEl && cbEl.checked) }
        : inputEl.value);
    } else {
      _dashCloseModal(true);
    }
  };
  const doCancel = () => {
    _dashCloseModal(_dashModalKind === 'prompt' ? null : false);
  };

  okBtn.addEventListener('click', doOk);
  cancelBtn.addEventListener('click', doCancel);

  /* 点 backdrop 空白 = 取消;modal 内部点击 stopPropagation 不冒泡 */
  backdrop.addEventListener('mousedown', (e) => {
    if (e.target === backdrop) doCancel();
  });
  modal.addEventListener('mousedown', e => e.stopPropagation());

  /* 全局键盘:仅 modal 显示时拦截 Enter/Esc */
  document.addEventListener('keydown', (e) => {
    if (backdrop.classList.contains('hidden')) return;
    if (e.key === 'Enter') {
      e.preventDefault();
      doOk();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      doCancel();
    }
  });
}

/* ──── 启动 ──── */
window.addEventListener('DOMContentLoaded', () => {
  initTheme();
  applyPlannedRestartUI(PLANNED_RESTART_ON);
  _dashBindModalEvents();
  logsLoadInline();
  usersLoadInline();
  setInterval(logsRefresh, REFRESH_MS);
  setInterval(dashMatchesRefresh, REFRESH_MS);
});
