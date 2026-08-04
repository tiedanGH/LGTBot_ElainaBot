/* ──── 操作审计 tab ────
 * 只读视图:唯一后端交互是「刷新」(隐藏 action __lgtbot_audit_list,
 * fragment 协议同 backup)。所有渲染字段一律 escapeHtml。
 *
 * 类别筛选(多选切换):auditFilter 是 Set<cat>;
 *   · 空集 = 不过滤,显示全部(「全部」chip 呈激活态)
 *   · 点类别 chip 切换其选中状态,选中一个或多个 = 显示所选类别的并集
 *   · 点「全部」清空选中集回到全显
 * 纯客户端过滤(≤500 条),切换即时重渲染;状态不持久化,刷新页面回到全显。
 */

const AUDIT_LIST_KEY = '__lgtbot_audit_list';

let auditData = null;              /* 最近一次 payload(entries/categories/状态) */
const auditFilter = new Set();     /* 选中的 cat 短码集合;空 = 全部 */

function auditFmtBytes(n) {
  if (n == null) return '—';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(2) + ' MB';
}

function auditFmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.getFullYear() + '-' +
         String(d.getMonth() + 1).padStart(2, '0') + '-' +
         String(d.getDate()).padStart(2, '0') + ' ' +
         String(d.getHours()).padStart(2, '0') + ':' +
         String(d.getMinutes()).padStart(2, '0') + ':' +
         String(d.getSeconds()).padStart(2, '0');
}

/* 相对时间,同 backup tab 的规则(该函数非全局,这里各 tab 自带一份)。 */
function auditFmtRelative(ts) {
  if (!ts) return '—';
  const diff = Math.floor(Date.now() / 1000) - ts;
  if (diff < 90) return '刚刚';
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
  const d = new Date(ts * 1000);
  const today = new Date();
  const yesterday = new Date(today.getTime() - 86400000);
  const hhmm = String(d.getHours()).padStart(2, '0') + ':' +
               String(d.getMinutes()).padStart(2, '0');
  if (d.toDateString() === today.toDateString()) return `今天 ${hhmm}`;
  if (d.toDateString() === yesterday.toDateString()) return `昨天 ${hhmm}`;
  return d.getFullYear() + '-' +
         String(d.getMonth() + 1).padStart(2, '0') + '-' +
         String(d.getDate()).padStart(2, '0') + ' ' + hhmm;
}

/* cat 短码 → {emoji, label};未知短码回退 ❓ + 原文 */
function auditCatInfo(cat) {
  const info = (auditData && auditData.categories) ? auditData.categories[cat] : null;
  return info || { emoji: '❓', label: cat || '未知' };
}

/* 来源 → 徽标 class(复用 dash-badge 主题色 + audit 自有 API 色):
   面板 = 中性灰 / 指令 = 橙 / 自动 = 绿 / API = accent 蓝紫 —— 四色区分触发方 */
function auditSrcBadgeClass(src) {
  if (src === '指令') return 'dash-badge dash-badge-warn';
  if (src === '自动') return 'dash-badge dash-badge-ok';
  if (src === 'API') return 'dash-badge audit-badge-api';
  return 'dash-badge';
}

/* ──── 渲染:状态卡 ──── */
function auditApplyStatus(data) {
  document.getElementById('audit-path').textContent = data.audit_path || '—';
  const max = data.max_entries || 500;
  const count = data.count || 0;
  document.getElementById('audit-count').textContent = count;
  document.getElementById('audit-count-sub').textContent = '上限 ' + max + ' 条 · 滚动淘汰最旧';
  document.getElementById('audit-oldest').textContent =
    data.oldest_ts ? auditFmtRelative(data.oldest_ts) : '（暂无记录）';
  document.getElementById('audit-oldest-sub').textContent =
    data.oldest_ts
      ? (count >= max ? '更早的记录已滚动淘汰' : auditFmtTime(data.oldest_ts))
      : '状态变更操作触发后自动记录';
  document.getElementById('audit-size').textContent = auditFmtBytes(data.size_bytes || 0);
}

/* ──── 渲染:类别筛选 chips ──── */
function auditRenderChips() {
  const wrap = document.getElementById('audit-filter-chips');
  const cats = (auditData && auditData.categories) || {};
  const chips = ['<button class="audit-chip' + (auditFilter.size === 0 ? ' active' : '') +
                 '" data-cat="">全部</button>'];
  for (const [cat, info] of Object.entries(cats)) {
    const active = auditFilter.has(cat) ? ' active' : '';
    chips.push('<button class="audit-chip' + active + '" data-cat="' + escapeHtml(cat) + '">' +
               escapeHtml((info.emoji || '') + ' ' + (info.label || cat)) + '</button>');
  }
  wrap.innerHTML = chips.join('');
  wrap.querySelectorAll('.audit-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const cat = chip.dataset.cat;
      if (!cat) {
        auditFilter.clear();                 /* 「全部」:清空选中集 → 全显 */
      } else if (auditFilter.has(cat)) {
        auditFilter.delete(cat);             /* 再点已选中的 chip:取消该类 */
      } else {
        auditFilter.add(cat);                /* 选中该类(可多选并集) */
      }
      auditRenderChips();
      auditRenderTable();
    });
  });
}

/* ──── 渲染:记录表 ──── */
function auditRenderTable() {
  const tbody = document.getElementById('audit-table-body');
  const all = (auditData && auditData.entries) || [];
  const rows = auditFilter.size === 0 ? all : all.filter(e => auditFilter.has(e.cat));

  if (rows.length === 0) {
    const text = all.length === 0
      ? '暂无审计记录 —— 面板或指令触发的状态变更操作会自动记录在这里'
      : '所选类别下暂无记录（点「全部」恢复全部显示）';
    tbody.innerHTML = '<tr class="audit-empty"><td colspan="6">' + escapeHtml(text) + '</td></tr>';
    return;
  }

  tbody.innerHTML = rows.map(e => {
    const cat = auditCatInfo(e.cat);
    const fullTime = escapeHtml(auditFmtTime(e.ts));
    const detail = e.detail || '';
    return `<tr>
      <td class="audit-col-time" title="${fullTime}">${escapeHtml(auditFmtRelative(e.ts))}</td>
      <td class="audit-col-cat">${escapeHtml(cat.emoji + ' ' + cat.label)}</td>
      <td class="audit-col-action">${escapeHtml(e.action || '')}</td>
      <td class="audit-col-detail">${detail
        ? '<span class="audit-detail-text" title="' + escapeHtml(detail) + '">' + escapeHtml(detail) + '</span>'
        : '<span class="audit-detail-text">—</span>'}</td>
      <td class="audit-col-src"><span class="${auditSrcBadgeClass(e.src)}">${escapeHtml(e.src || '—')}</span></td>
      <td class="audit-col-result">${e.ok
        ? '<span class="dash-msg-ok">✅ 成功</span>'
        : '<span class="dash-msg-err">❌ 失败</span>'}</td>
    </tr>`;
  }).join('');

  /* 详情点击展开 / 收起(innerHTML 重写后重新绑) */
  tbody.querySelectorAll('.audit-detail-text').forEach(el => {
    el.addEventListener('click', () => el.classList.toggle('expanded'));
  });
}

function auditApplyData(data) {
  auditData = data;
  auditApplyStatus(data);
  auditRenderChips();
  auditRenderTable();
}

function auditLoadInline() {
  try {
    auditApplyData(JSON.parse(document.getElementById('audit-data').textContent));
  } catch (e) {
    console.warn('[audit] load failed:', e);
  }
}

/* 调隐藏 action 端点,解 <pre id="result"> 里的 JSON(同 backup) */
async function auditCallAction(key) {
  const r = await fetch(apiUrl(key), { cache: 'no-store' });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const text = await r.text();
  const doc = new DOMParser().parseFromString(text, 'text/html');
  const el = doc.getElementById('result');
  if (!el) throw new Error('响应不含 #result');
  return JSON.parse(el.textContent);
}

function auditShowMsg(text, kind) {
  const el = document.getElementById('audit-action-msg');
  if (!el) return;
  el.textContent = text;
  el.className = 'audit-action-msg dash-msg-' + (kind || 'info');
}

/* ──── 刷新 ──── */
async function auditRefresh() {
  const btn = document.getElementById('audit-refresh-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ ...'; }
  try {
    const data = await auditCallAction(AUDIT_LIST_KEY);
    if (data.success) {
      auditApplyData(data);
      auditShowMsg('');
    } else {
      auditShowMsg('❌ 刷新失败', 'err');
    }
  } catch (e) {
    auditShowMsg('❌ ' + e.message, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🔄 刷新'; }
  }
}

window.addEventListener('DOMContentLoaded', () => {
  auditLoadInline();
  const refreshBtn = document.getElementById('audit-refresh-btn');
  if (refreshBtn) refreshBtn.addEventListener('click', auditRefresh);
});
