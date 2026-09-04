/* ──── 数据备份 tab ────
 * 隐藏 action endpoint key 与 webui/main.py 的 _BACKUP_* 常量一一对应。
 * restore / delete 走主框架 register_route 注册的 /api/ext/lgtbot/backup/*
 * 真实 HTTP 路由,带 ?name=<zip_name> query 参数。
 */

const BACKUP_KEYS = {
  create:    '__lgtbot_backup_create',
  list:      '__lgtbot_backup_list',
};

/* register_route 注册的真实路由前缀(参见 webui/main.py register_backup_routes) */
const BACKUP_ROUTE_BASE = '/api/ext/lgtbot/backup';


function backupFmtBytes(n) {
  if (n == null) return '—';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(2) + ' MB';
  return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
}

function backupFmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.getFullYear() + '-' +
         String(d.getMonth() + 1).padStart(2, '0') + '-' +
         String(d.getDate()).padStart(2, '0') + ' ' +
         String(d.getHours()).padStart(2, '0') + ':' +
         String(d.getMinutes()).padStart(2, '0') + ':' +
         String(d.getSeconds()).padStart(2, '0');
}

/* 相对时间("3 分钟前" / "昨天 15:30" / "2026-06-22 03:00:15"):
   ≤ 90s → 「刚刚」
   ≤ 60min → 「N 分钟前」
   ≤ 24h 且同一天 → 「今天 HH:MM」
   昨天 → 「昨天 HH:MM」
   其它 → 完整 YYYY-MM-DD HH:MM:SS */
function backupFmtRelative(ts) {
  if (!ts) return '—';
  const now = Math.floor(Date.now() / 1000);
  const diff = now - ts;
  if (diff < 90) return '刚刚';
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
  const d = new Date(ts * 1000);
  const today = new Date();
  const isToday = d.toDateString() === today.toDateString();
  const yesterday = new Date(today.getTime() - 86400000);
  const isYesterday = d.toDateString() === yesterday.toDateString();
  const hhmm = String(d.getHours()).padStart(2, '0') + ':' +
               String(d.getMinutes()).padStart(2, '0');
  if (isToday) return `今天 ${hhmm}`;
  if (isYesterday) return `昨天 ${hhmm}`;
  return backupFmtTime(ts);
}


function backupApplyData(data) {
  const dirEl = document.getElementById('backup-dir');
  if (dirEl) dirEl.textContent = data.backup_dir || '—';

  const backups = data.backups || [];

  /* 状态卡 */
  if (backups.length > 0) {
    const latest = backups[0];
    document.getElementById('backup-latest-time').textContent = backupFmtRelative(latest.mtime_ts);
    document.getElementById('backup-latest-meta').textContent = backupFmtBytes(latest.size_bytes) + ' · ' + latest.name;
  } else {
    document.getElementById('backup-latest-time').textContent = '（从未备份）';
    document.getElementById('backup-latest-meta').textContent = '点「立即备份」开始';
  }

  document.getElementById('backup-total-count').textContent = backups.length;
  document.getElementById('backup-total-meta').textContent = '保留上限 ' + (data.retention_count || 7);
  document.getElementById('backup-total-size').textContent = backupFmtBytes(data.total_size_bytes || 0);
  document.getElementById('backup-auto-interval').textContent = '每 ' + (data.auto_interval_h || 24) + ' 小时';

  /* 备份列表表格 */
  const tbody = document.getElementById('backup-table-body');
  if (backups.length === 0) {
    tbody.innerHTML = '<tr class="backup-empty"><td colspan="4">尚未创建任何备份</td></tr>';
    return;
  }
  tbody.innerHTML = backups.map(b => {
    const safeName = escapeHtml(b.name);
    return `<tr>
      <td class="backup-col-name"><code class="dash-mono">${safeName}</code></td>
      <td class="backup-col-time">${escapeHtml(backupFmtRelative(b.mtime_ts))}</td>
      <td class="backup-col-size">${escapeHtml(backupFmtBytes(b.size_bytes))}</td>
      <td class="backup-col-ops">
        <button class="dash-btn dash-btn-small backup-btn-download" data-name="${safeName}"><svg class="ui-icon btn-icon"><use href="#i-download"/></svg><span class="btn-label">下载</span></button>
        <button class="dash-btn dash-btn-small backup-btn-restore" data-name="${safeName}"><svg class="ui-icon btn-icon"><use href="#i-undo"/></svg><span class="btn-label">恢复</span></button>
        <button class="dash-btn dash-btn-small dash-btn-warn backup-btn-delete" data-name="${safeName}"><svg class="ui-icon btn-icon"><use href="#i-trash"/></svg><span class="btn-label">删除</span></button>
      </td>
    </tr>`;
  }).join('');

  /* 绑事件(每次渲染重新绑,因为 innerHTML 重写了节点) */
  tbody.querySelectorAll('.backup-btn-download').forEach(btn => {
    btn.addEventListener('click', () => backupDownload(btn.dataset.name));
  });
  tbody.querySelectorAll('.backup-btn-restore').forEach(btn => {
    btn.addEventListener('click', () => backupRestore(btn.dataset.name));
  });
  tbody.querySelectorAll('.backup-btn-delete').forEach(btn => {
    btn.addEventListener('click', () => backupDelete(btn.dataset.name));
  });
}


function backupLoadInline() {
  try {
    const data = JSON.parse(document.getElementById('backup-data').textContent);
    backupApplyData(data);
  } catch (e) {
    console.warn('[backup] load failed:', e);
  }
}


async function backupCallAction(key) {
  const r = await fetch(apiUrl(key), { cache: 'no-store' });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const text = await r.text();
  const doc = new DOMParser().parseFromString(text, 'text/html');
  const el = doc.getElementById('result');
  if (!el) throw new Error('响应不含 #result');
  return JSON.parse(el.textContent);
}


/* register_route 路由是真实 HTTP 路由,直接返 json_response,不需要 DOMParser 解析 fragment。
   但需要带框架 token(全局 TOKEN_QS,与 dash-config-save 同款)。 */
async function backupCallRoute(path, params) {
  const search = new URLSearchParams(params);
  if (TOKEN_QS) {
    const tokenSearch = new URLSearchParams(TOKEN_QS.slice(1));
    for (const [k, v] of tokenSearch) search.set(k, v);
  }
  const qs = search.toString();
  const url = path + (qs ? '?' + qs : '');
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) {
    /* 4xx / 5xx 时仍尝试解 json 拿后端 message */
    try {
      const d = await r.json();
      throw new Error(d.message || ('HTTP ' + r.status));
    } catch (_) {
      throw new Error('HTTP ' + r.status);
    }
  }
  return await r.json();
}


function backupShowMsg(text, kind) {
  const el = document.getElementById('backup-action-msg');
  if (!el) return;
  el.textContent = text;
  el.className = 'backup-action-msg dash-msg-' + (kind || 'info');
}


/* ──── 立即备份 ──── */
async function backupCreate() {
  const btn = document.getElementById('backup-create-btn');
  btn.disabled = true;
  const oldText = btnLabel(btn);
  setBtnIcon(btn, '#i-hourglass', '备份中……');
  backupShowMsg('正在打包数据库与配置……', 'info');
  try {
    const data = await backupCallAction(BACKUP_KEYS.create);
    if (data.success) {
      backupShowMsg('✅ ' + (data.message || '已生成备份') + (data.zip_name ? ' (' + data.zip_name + ')' : ''), 'ok');
      if (data.skipped && data.skipped.length > 0) {
        console.warn('[backup] 跳过的文件:', data.skipped);
      }
      await backupRefresh();   /* 自动刷新列表 */
    } else {
      backupShowMsg('❌ ' + (data.message || '备份失败'), 'err');
    }
  } catch (e) {
    backupShowMsg('❌ ' + e.message, 'err');
  } finally {
    btn.disabled = false;
    setBtnIcon(btn, '#i-save', oldText);
  }
}


/* ──── 刷新列表 ──── */
async function backupRefresh() {
  const btn = document.getElementById('backup-refresh-btn');
  if (btn) { btn.disabled = true; setBtnIcon(btn, '#i-hourglass', '...'); }
  try {
    const data = await backupCallAction(BACKUP_KEYS.list);
    if (data.success) {
      /* 复用 get_data 的渲染逻辑,只需要补 backup_dir / retention_count /
         auto_interval_h 这几个 list 不返的字段 —— 用现有 inline data 补 */
      const inlineData = JSON.parse(document.getElementById('backup-data').textContent);
      backupApplyData({
        backup_dir: inlineData.backup_dir,
        retention_count: inlineData.retention_count,
        auto_interval_h: inlineData.auto_interval_h,
        backups: data.backups,
        total_size_bytes: data.total_size_bytes,
      });
    }
  } catch (e) {
    console.warn('[backup] refresh failed:', e);
  } finally {
    if (btn) { btn.disabled = false; setBtnIcon(btn, '#i-refresh', '刷新列表'); }
  }
}


/* ──── 下载 ────
 * Content-Disposition: attachment → 浏览器直接下载;不能用 fetch(会读进内存),
 * 拼好带鉴权 token 的 URL 后 window.open 到新窗口触发下载(不影响面板)。 */
function backupDownload(name) {
  if (!name) return;
  const search = new URLSearchParams({name});
  if (TOKEN_QS) {
    const tokenSearch = new URLSearchParams(TOKEN_QS.slice(1));
    for (const [k, v] of tokenSearch) search.set(k, v);
  }
  const url = BACKUP_ROUTE_BASE + '/download?' + search.toString();
  window.open(url, '_blank', 'noopener');
}


/* ──── 恢复 ──── */
async function backupRestore(name) {
  const ok1 = await dashConfirm(
    '确认恢复备份「' + name + '」？\n\n' +
    '将用此备份原子覆盖磁盘上的当前数据，此操作不可逆。\n' +
    '战绩、成就等数据恢复将立即生效。引擎配置需重启才能重新加载。',
    {level: 'danger'}
  );
  if (!ok1) return;
  const ok2 = await dashConfirm(
    '再次确认：这是不可逆操作！\n\n' +
    '现有的对局战绩、成就、配置将被备份时的快照覆盖。\n' +
    '如有疑虑，请先点「💾 立即备份」抓取当前数据状态后再恢复。',
    {level: 'danger'}
  );
  if (!ok2) return;

  backupShowMsg('正在恢复……', 'info');
  try {
    const data = await backupCallRoute(BACKUP_ROUTE_BASE + '/restore', {name});
    if (data.success) {
      backupShowMsg('✅ ' + (data.message || '已恢复'), 'ok');
      /* 仅引擎配置 lgtbot.json 需重启才能重新加载。 */
      dashAlert
        ? await dashAlert('恢复完成！ —— 战绩、成就、公告等数据已立即生效。\n'
              + '若本次恢复涉及引擎配置 lgtbot.json，请点击「🔁 重启 LGTBot」让引擎重新加载。')
        : alert('恢复完成！战绩、成就、公告等数据已立即生效，如涉及引擎配置请重启 LGTBot');
    } else {
      backupShowMsg('❌ ' + (data.message || '恢复失败'), 'err');
    }
  } catch (e) {
    backupShowMsg('❌ ' + e.message, 'err');
  }
}


/* ──── 删除 ──── */
async function backupDelete(name) {
  const ok = await dashConfirm(
    '确认删除备份「' + name + '」?\n\n' +
    '删除后无法找回。其他备份不受影响。',
    {level: 'warn'}
  );
  if (!ok) return;

  backupShowMsg('正在删除……', 'info');
  try {
    const data = await backupCallRoute(BACKUP_ROUTE_BASE + '/delete', {name});
    if (data.success) {
      backupShowMsg('✅ ' + (data.message || '已删除'), 'ok');
      await backupRefresh();
    } else {
      backupShowMsg('❌ ' + (data.message || '删除失败'), 'err');
    }
  } catch (e) {
    backupShowMsg('❌ ' + e.message, 'err');
  }
}


window.addEventListener('DOMContentLoaded', () => {
  backupLoadInline();

  const createBtn = document.getElementById('backup-create-btn');
  if (createBtn) createBtn.addEventListener('click', backupCreate);

  const refreshBtn = document.getElementById('backup-refresh-btn');
  if (refreshBtn) refreshBtn.addEventListener('click', backupRefresh);
});
