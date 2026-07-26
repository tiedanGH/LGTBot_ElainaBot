/* ──── 用户数据标签:两段式懒加载 ────
 * 首屏 payload 只带前 1000 条(head)+ 真实 total;页数按 total 完整展示。
 * 翻/跳到 head 之外、或发起搜索时,一次拉取剩余全部(/api/ext/lgtbot/users/page
 * ?offset=head),此后本会话数据齐备:任意翻页零请求、搜索全量覆盖。
 * escapeHtml / apiUrl / PAGE_KEY / TOKEN_QS 由 main.js 提供(全局)。 */

const USERS_PAGE_ROUTE = '/api/ext/lgtbot/users/page';

let usersHead = [];        // 首屏内嵌的前 head 条
let usersRest = null;      // null=未加载 | 'loading' | 'failed' | 行数组(剩余全部)
let usersQueryTs = 0;
let usersTotal = 0;        // 框架 users 表全量总数(页数依据)
let usersPage = 1;
const usersWideMQ = window.matchMedia('(min-width: 1200px)');

function usersPageSize() { return usersWideMQ.matches ? 100 : 50; }

/* 已加载的全部行(head [+ rest],同一份服务端排序的连续切片) */
function usersAllRows() {
  return Array.isArray(usersRest) ? usersHead.concat(usersRest) : usersHead;
}
function usersFullyLoaded() {
  return Array.isArray(usersRest) || usersHead.length >= usersTotal;
}

function usersSearchQuery() {
  return (document.getElementById('users-search').value || '').toLowerCase().trim();
}

/* 当前搜索查询(空字符串 = 不过滤)。同时匹配 name 与 openid,大小写无关。 */
function usersFiltered() {
  const q = usersSearchQuery();
  const rows = usersAllRows();
  if (!q) return rows;
  return rows.filter(u =>
    (u.name || '').toLowerCase().includes(q) ||
    (u.openid || '').toLowerCase().includes(q)
  );
}

/* 总页数:非搜索按真实 total(完整可翻页);搜索按过滤结果 */
function usersTotalPages() {
  const base = usersSearchQuery() ? usersFiltered().length : usersTotal;
  return Math.max(1, Math.ceil(base / usersPageSize()));
}

function usersFmtDateTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.getFullYear() + '-' +
         String(d.getMonth() + 1).padStart(2, '0') + '-' +
         String(d.getDate()).padStart(2, '0') + ' ' +
         String(d.getHours()).padStart(2, '0') + ':' +
         String(d.getMinutes()).padStart(2, '0') + ':' +
         String(d.getSeconds()).padStart(2, '0');
}

function usersRowHtml(u, serial) {
  /* serial 是该用户在按最后活跃日期降序的完整列表中的全局名次(1-indexed,
     跨页累加),始终唯一,即便切换 1/2 列模式或翻页也对得上。 */
  const avatar = u.avatar
    ? '<img class="avatar-img" src="' + escapeHtml(u.avatar) + '" alt="" loading="lazy" referrerpolicy="no-referrer">'
    : '<div class="avatar-img"></div>';
  const name = escapeHtml(u.name || '—');
  /* 消息数:统计聚合截至昨日,无统计行(null)显 — */
  const msgs = (u.total_messages == null) ? '—' : String(u.total_messages);
  /* 最后活跃:日粒度('YYYY-MM-DD',三源取最新);空 = 无任何活跃记录 */
  const seen = u.last_active_date ? escapeHtml(u.last_active_date) : '从未';
  return '<div class="user-row">' +
    '<div class="col-idx">' + serial + '</div>' +
    '<div class="col-user">' + avatar + '<span class="user-name">' + name + '</span></div>' +
    '<div class="col-openid">' + escapeHtml(u.openid || '') + '</div>' +
    '<div class="col-msgs">' + msgs + '</div>' +
    '<div class="col-seen">' + seen + '</div>' +
  '</div>';
}

function usersLoadInline() {
  try {
    const data = JSON.parse(document.getElementById('user-data').textContent);
    usersHead = data.users || [];
    usersRest = null;
    usersQueryTs = data.query_time || 0;
    usersTotal = data.total || 0;
    usersPage = 1;
    usersRender();
  } catch (e) {
    document.getElementById('users-list-1').innerHTML =
      '<div class="user-row empty">用户数据解析失败: ' + escapeHtml(e.message) + '</div>';
  }
}

/* 一次拉取剩余全部(带 token;并发去重靠 'loading' 状态);完成后重渲染 */
async function usersEnsureRest() {
  if (usersFullyLoaded() || usersRest === 'loading') return;
  usersRest = 'loading';
  try {
    const url = USERS_PAGE_ROUTE + TOKEN_QS + (TOKEN_QS ? '&' : '?') +
                'offset=' + usersHead.length;
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    if (!data.success) throw new Error(data.message || '拉取失败');
    usersRest = data.users || [];
    if (data.total != null) usersTotal = data.total;   // 顺带校准总数/页数
  } catch (e) {
    console.warn('[users] 剩余数据加载失败:', e);
    usersRest = 'failed';
  }
  usersRender();
}

function usersRetryRest() {          // 失败提示里的「点此重试」入口
  usersRest = null;
  usersEnsureRest();
}

function usersRender() {
  /* 顶部汇总:查询时间 + 总用户数(页数按 total 完整计算,数据两段式懒加载) */
  document.getElementById('users-query-time').textContent = usersFmtDateTime(usersQueryTs);
  document.getElementById('users-total').textContent = usersTotal;

  const searching = !!usersSearchQuery();
  const pageSize = usersPageSize();
  const total = usersTotalPages();
  if (usersPage > total) usersPage = total;
  if (usersPage < 1) usersPage = 1;
  document.getElementById('users-page-input').value = usersPage;
  document.getElementById('users-page-total').textContent = total;
  document.getElementById('users-prev').disabled = usersPage <= 1;
  document.getElementById('users-next').disabled = usersPage >= total;

  const list1 = document.getElementById('users-list-1');
  const list2 = document.getElementById('users-list-2');
  const start = (usersPage - 1) * pageSize;

  /* 需要 head 之外的数据(深翻页 / 搜索需全量)而尚未齐备 → 占位 + 触发拉取 */
  const needRest = (searching || start + pageSize > usersHead.length) && !usersFullyLoaded();
  if (needRest) {
    if (usersRest === 'failed') {
      list1.innerHTML = '<div class="user-row empty">❌ 剩余数据加载失败，' +
        '<a href="javascript:void(0)" onclick="usersRetryRest()">点此重试</a></div>';
      list2.innerHTML = '';
      return;
    }
    /* 搜索时 head 内的临时结果照常显示,底部提示加载中;纯翻页则整页占位 */
    if (!searching) {
      list1.innerHTML = '<div class="user-row empty">⏳ 正在加载全部用户数据……</div>';
      list2.innerHTML = '';
      usersEnsureRest();
      return;
    }
    usersEnsureRest();
  }

  const rows = searching ? usersFiltered() : usersAllRows();
  if (!rows.length) {
    list1.innerHTML = '<div class="user-row empty">' +
      (searching && usersAllRows().length ? '无匹配结果' : '暂无用户数据') + '</div>';
    list2.innerHTML = '';
    return;
  }
  const slice = rows.slice(start, start + pageSize);
  if (!slice.length) {
    list1.innerHTML = '<div class="user-row empty">（本页无数据）</div>';
    list2.innerHTML = '';
    return;
  }
  const tailHint = (searching && !usersFullyLoaded())
    ? '<div class="user-row empty">⏳ 正在加载全部数据以完成搜索……</div>' : '';

  if (usersWideMQ.matches) {
    /* 2 列模式:先把左列填满前 50 个,剩下的再去右列(不是奇偶交错)。 */
    const half = Math.floor(pageSize / 2);  // 100 / 2 = 50
    const left = slice.slice(0, half);
    const right = slice.slice(half);
    list1.innerHTML = left.map((u, i) => usersRowHtml(u, start + i + 1)).join('') + tailHint;
    list2.innerHTML = right.map((u, i) => usersRowHtml(u, start + half + i + 1)).join('');
  } else {
    list1.innerHTML = slice.map((u, i) => usersRowHtml(u, start + i + 1)).join('') + tailHint;
    list2.innerHTML = '';
  }
}

async function usersRefresh() {
  const btn = document.getElementById('users-refresh');
  btn.disabled = true;
  try {
    const r = await fetch(apiUrl(PAGE_KEY), { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const text = await r.text();
    const m = text.match(/<script id="user-data"[^>]*>([\s\S]*?)<\/script>/);
    if (m) {
      const data = JSON.parse(m[1]);
      usersHead = data.users || [];   // 刷新丢弃旧数据,回到首段
      usersRest = null;
      usersQueryTs = data.query_time || 0;
      usersTotal = data.total || 0;
      /* 刷新数据后回到第 1 页,语义上「重新查询」就应该看到最新顶部 */
      usersPage = 1;
      usersRender();
    }
  } catch (e) {
    console.warn('[users] refresh failed:', e);
  } finally {
    btn.disabled = false;
  }
}

window.addEventListener('DOMContentLoaded', () => {
  /* 分页事件 */
  document.getElementById('users-prev').addEventListener('click', () => {
    if (usersPage > 1) { usersPage--; usersRender(); }
  });
  document.getElementById('users-next').addEventListener('click', () => {
    if (usersPage < usersTotalPages()) { usersPage++; usersRender(); }
  });
  document.getElementById('users-page-input').addEventListener('change', (e) => {
    const v = parseInt(e.target.value, 10);
    if (!isNaN(v)) { usersPage = v; usersRender(); }
  });
  document.getElementById('users-refresh').addEventListener('click', usersRefresh);

  /* 搜索:input 事件每次按键都触发,全量行的 Array.filter 是亚毫秒级;首次搜索
     会触发剩余数据补全(usersRender 内)。每次输入回到第 1 页。 */
  document.getElementById('users-search').addEventListener('input', () => {
    usersPage = 1;
    usersRender();
  });

  /* 屏幕跨过 1200px 阈值时重排(2 列 ⇄ 1 列,每页容量也会变)。
     matchMedia 比 resize 监听更节流,只在阈值翻转时触发。 */
  usersWideMQ.addEventListener('change', usersRender);
});
