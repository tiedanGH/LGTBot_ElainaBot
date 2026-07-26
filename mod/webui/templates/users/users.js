let usersCache = [];
let usersQueryTs = 0;
let usersTotal = 0;        // 主框架 users 表全量总数(独立于 usersCache 长度)
let usersPage = 1;
const usersWideMQ = window.matchMedia('(min-width: 1200px)');

function usersPageSize() { return usersWideMQ.matches ? 100 : 50; }

/* 当前搜索查询(空字符串 = 不过滤)。同时匹配 name 与 openid,大小写无关。 */
function usersFiltered() {
  const q = (document.getElementById('users-search').value || '').toLowerCase().trim();
  if (!q) return usersCache;
  return usersCache.filter(u =>
    (u.name || '').toLowerCase().includes(q) ||
    (u.openid || '').toLowerCase().includes(q)
  );
}
function usersTotalPages() {
  return Math.max(1, Math.ceil(usersFiltered().length / usersPageSize()));
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
    usersCache = data.users || [];
    usersQueryTs = data.query_time || 0;
    usersTotal = data.total || 0;
    usersPage = 1;
    usersRender();
  } catch (e) {
    document.getElementById('users-list-1').innerHTML =
      '<div class="user-row empty">用户数据解析失败: ' + escapeHtml(e.message) + '</div>';
  }
}

function usersRender() {
  /* 顶部汇总:查询时间 + 总用户数 */
  document.getElementById('users-query-time').textContent = usersFmtDateTime(usersQueryTs);
  document.getElementById('users-total').textContent = usersTotal;

  const filtered = usersFiltered();

  /* 分页:钳位、刷 UI(基于「过滤后」的列表) */
  const total = Math.max(1, Math.ceil(filtered.length / usersPageSize()));
  if (usersPage > total) usersPage = total;
  if (usersPage < 1) usersPage = 1;
  document.getElementById('users-page-input').value = usersPage;
  document.getElementById('users-page-total').textContent = total;
  document.getElementById('users-prev').disabled = usersPage <= 1;
  document.getElementById('users-next').disabled = usersPage >= total;

  const list1 = document.getElementById('users-list-1');
  const list2 = document.getElementById('users-list-2');

  if (!filtered.length) {
    const msg = usersCache.length ? '无匹配结果' : '暂无用户数据';
    list1.innerHTML = '<div class="user-row empty">' + msg + '</div>';
    list2.innerHTML = '';
    return;
  }

  const pageSize = usersPageSize();
  const start = (usersPage - 1) * pageSize;
  const slice = filtered.slice(start, start + pageSize);

  if (usersWideMQ.matches) {
    /* 2 列模式:先把左列填满前 50 个,剩下的再去右列(不是奇偶交错)。
       序号 = 在过滤后列表中的全局名次 = start + 本页内 index + 1。 */
    const half = Math.floor(pageSize / 2);  // 100 / 2 = 50
    const left = slice.slice(0, half);
    const right = slice.slice(half);
    list1.innerHTML = left.map((u, i) =>
      usersRowHtml(u, start + i + 1)).join('');
    list2.innerHTML = right.map((u, i) =>
      usersRowHtml(u, start + half + i + 1)).join('');
  } else {
    list1.innerHTML = slice.map((u, i) => usersRowHtml(u, start + i + 1)).join('');
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
      usersCache = data.users || [];
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

  /* 搜索:input 事件每次按键都触发,数据量 < 1000 时性能足够;每次输入回到
     第 1 页,免得「上一页之后又输入新关键字,结果空白」。 */
  document.getElementById('users-search').addEventListener('input', () => {
    usersPage = 1;
    usersRender();
  });

  /* 屏幕跨过 1200px 阈值时重排(2 列 ⇄ 1 列,每页容量也会变)。
     matchMedia 比 resize 监听更节流,只在阈值翻转时触发。 */
  usersWideMQ.addEventListener('change', usersRender);
});
