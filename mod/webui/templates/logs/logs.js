let logsFilter = 'all';
let logsAutoRefresh = true;
let logsCache = [];

function logsFmtTime(ts) {
  const d = new Date(ts * 1000);
  return String(d.getHours()).padStart(2, '0') + ':' +
         String(d.getMinutes()).padStart(2, '0') + ':' +
         String(d.getSeconds()).padStart(2, '0');
}

function logsLoadInline() {
  try {
    logsCache = JSON.parse(document.getElementById('log-data').textContent);
    logsRender();
  } catch (e) {
    document.getElementById('log-list').innerHTML =
      '<div class="empty">日志解析失败: ' + escapeHtml(e.message) + '</div>';
  }
}

function logsRender() {
  const filtered = logsCache.filter(l => {
    if (logsFilter === 'all') return true;
    if (logsFilter === 'in') return l.direction === 'in';
    if (logsFilter === 'out') return l.direction === 'out';
    if (logsFilter === 'group') return l.kind === 'group';
    if (logsFilter === 'private') return l.kind === 'private';
    return true;
  });
  const list = document.getElementById('log-list');
  const stats = document.getElementById('logs-stats');
  stats.textContent = '共 ' + logsCache.length + ' / 显示 ' + filtered.length +
    '  ·  ' + (logsAutoRefresh ? '自动刷新中' : '已暂停');
  if (!filtered.length) {
    list.innerHTML = '<div class="empty">暂无日志</div>';
    return;
  }
  const rows = filtered.slice().reverse().map(l => {
    const cls = 'log-row ' + (l.direction === 'in' ? 'in' : 'out');
    const dir = l.direction === 'in' ? '⬇ 收' : '⬆ 发';
    const kindTag = '<span class="kind-tag">' + (l.kind === 'group' ? '群' : '私') + '</span>';
    const who = l.gid
      ? kindTag + 'g:' + escapeHtml(l.gid.slice(0, 16))
      : kindTag + 'u:' + escapeHtml((l.uid || '').slice(0, 16));
    const imgMark = l.image ? '<span class="img-mark">[图]</span> ' : '';
    const content = imgMark + escapeHtml(l.content || '');
    return '<div class="' + cls + '">' +
      '<div class="time">' + logsFmtTime(l.time) + '</div>' +
      '<div class="dir">' + dir + '</div>' +
      '<div class="who">' + who + '</div>' +
      '<div class="content">' + content + '</div>' +
    '</div>';
  }).join('');
  list.innerHTML = rows;
}

async function logsRefresh() {
  if (!logsAutoRefresh) return;
  try {
    const r = await fetch(apiUrl(PAGE_KEY), { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const text = await r.text();
    const m = text.match(/<script id="log-data"[^>]*>([\s\S]*?)<\/script>/);
    if (m) {
      logsCache = JSON.parse(m[1]);
      logsRender();
    }
  } catch (e) {
    console.warn('[logs] refresh failed:', e);
  }
}

window.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.filters button[data-f]').forEach(btn => {
    btn.addEventListener('click', () => {
      logsFilter = btn.dataset.f;
      document.querySelectorAll('.filters button[data-f]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      logsRender();
    });
  });

  document.getElementById('auto-toggle').addEventListener('click', () => {
    logsAutoRefresh = !logsAutoRefresh;
    setBtnIcon(document.getElementById('auto-toggle'),
           logsAutoRefresh ? '#i-pause' : '#i-play', '自动');
    const badge = document.getElementById('live-badge');
    if (badge) badge.style.opacity = logsAutoRefresh ? '1' : '.4';
    logsRender();
  });
});
