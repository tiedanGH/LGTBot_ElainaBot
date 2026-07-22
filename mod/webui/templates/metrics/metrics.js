/* ──── 指标面板 tab ────
 * 顶部标题栏的统一刷新按钮驱动三个区(数据统计 / 运行指标 / 游戏数据):
 * 一个端点(__lgtbot_metrics_refresh,fragment 协议同 audit)一次返回完整
 * payload,metricsApplyData 整体重渲染。
 * 所有来自数据库的文本(游戏名 / 昵称 / 脱敏 ID)一律 escapeHtml。
 */

const METRICS_REFRESH_KEY = '__lgtbot_metrics_refresh';

function metricsFmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.getFullYear() + '-' +
         String(d.getMonth() + 1).padStart(2, '0') + '-' +
         String(d.getDate()).padStart(2, '0') + ' ' +
         String(d.getHours()).padStart(2, '0') + ':' +
         String(d.getMinutes()).padStart(2, '0') + ':' +
         String(d.getSeconds()).padStart(2, '0');
}

const metricsFmtNum = (v) => (v == null ? '—' : String(v));

/* ──── ① 数据统计区 ──── */
function metricsRenderStats(stats) {
  const s = stats || {};
  document.getElementById('metrics-stat-user-cache').textContent = metricsFmtNum(s.user_cache_total);
  document.getElementById('metrics-stat-lgt-users').textContent = metricsFmtNum(s.lgtbot_users);
  document.getElementById('metrics-stat-matches').textContent = metricsFmtNum(s.lgtbot_matches);
  document.getElementById('metrics-stat-attendances').textContent = metricsFmtNum(s.lgtbot_match_attendances);
  document.getElementById('metrics-stat-achievements').textContent = metricsFmtNum(s.lgtbot_achievements);
}

/* ──── ② 运行指标区 ──── */
function metricsRenderRuntime(rt) {
  const r = rt || {};
  document.getElementById('metrics-path').textContent = r.metrics_path || '—';

  /* 图床成功率:总数 0 → "—" + 「暂无上传记录」 */
  const total = r.upload_total || 0;
  const fail = r.upload_fail || 0;
  document.getElementById('metrics-upload-rate').textContent =
    (r.upload_rate == null) ? '—' : (r.upload_rate + '%');
  document.getElementById('metrics-upload-sub').textContent =
    total > 0 ? ('总 ' + total + ' 次 · 失败 ' + fail + ' 次') : '暂无上传记录';

  /* 配额压力:大数字 = 等待超时次数,小字 = 耗尽次数 */
  document.getElementById('metrics-quota-timeout').textContent =
    (r.quota_wait_timeout || 0) + ' 次';
  document.getElementById('metrics-quota-sub').textContent =
    '配额耗尽 ' + (r.quota_exhausted || 0) + ' 次';

  /* 重启次数(面板 / 指令触发的主动重启):小字上次重启时间 */
  document.getElementById('metrics-restart-total').textContent =
    (r.restart_total || 0) + ' 次';
  document.getElementById('metrics-restart-sub').textContent =
    r.last_restart_ts ? ('上次重启 ' + metricsFmtTime(r.last_restart_ts)) : '暂无重启记录';

  /* 崩溃重启:分信号 + 最近一次 */
  const crashTotal = r.crash_total || 0;
  document.getElementById('metrics-crash-total').textContent = crashTotal + ' 次';
  const bySig = r.crash_by_sig || {};
  const parts = Object.entries(bySig).map(([sig, n]) => sig + ' ×' + n);
  if (crashTotal > 0 && r.last_crash_ts) {
    parts.push('最近 ' + metricsFmtTime(r.last_crash_ts));
  }
  document.getElementById('metrics-crash-sub').textContent =
      parts.length ? parts.join(' · ') : '暂无崩溃记录';
}

/* ──── ③ 游戏数据区 ──── */
function metricsRankRows(list, nameKey) {
  if (!list || !list.length) {
    return '<tr class="metrics-empty"><td colspan="3">暂无数据</td></tr>';
  }
  return list.map((item, i) =>
    '<tr><td class="metrics-col-rank">' + (i + 1) + '</td>' +
    '<td>' + escapeHtml(item[nameKey] || '') + '</td>' +
    '<td class="metrics-col-count">' + escapeHtml(String(item.count)) + '</td></tr>'
  ).join('');
}

function metricsRenderGame(game, activePush) {
  const g = game || {};
  document.getElementById('metrics-today-matches').textContent = metricsFmtNum(g.today_matches);
  document.getElementById('metrics-today-players').textContent = metricsFmtNum(g.today_players);
  document.getElementById('metrics-today-groups').textContent = metricsFmtNum(g.today_groups);

  /* 今日主动消息:大数字 = 总条数,小字 = 平均每群 / 每人(2 位小数) */
  const ap = activePush || {};
  const avg = (total, n) => (n > 0 ? (total / n).toFixed(2) : '0.00');
  document.getElementById('metrics-active-group').textContent = metricsFmtNum(ap.group_total) + ' 条';
  document.getElementById('metrics-active-group-sub').textContent =
    '平均每群 ' + avg(ap.group_total || 0, ap.group_targets_n || 0) + ' 条';
  document.getElementById('metrics-active-dm').textContent = metricsFmtNum(ap.dm_total) + ' 条';
  document.getElementById('metrics-active-dm-sub').textContent =
    '平均每人 ' + avg(ap.dm_total || 0, ap.dm_targets_n || 0) + ' 条';

  document.getElementById('metrics-top-all-body').innerHTML =
    metricsRankRows(g.top_games_all, 'game_name');
  document.getElementById('metrics-top-week-body').innerHTML =
    metricsRankRows(g.top_games_week, 'game_name');
  document.getElementById('metrics-top-players-body').innerHTML =
    metricsRankRows(g.top_players_week, 'display');

  /* 近10日趋势:日期 / 局数 / 活跃玩家 三列 */
  const trendBody = document.getElementById('metrics-trend-body');
  const trend = g.trend_10d || [];
  trendBody.innerHTML = trend.length
    ? trend.map(t => '<tr><td>' + escapeHtml(t.date || '') + '</td>' +
                     '<td class="metrics-col-count">' + escapeHtml(String(t.count)) + '</td>' +
                     '<td class="metrics-col-count">' + escapeHtml(String(t.players)) + '</td></tr>').join('')
    : '<tr class="metrics-empty"><td colspan="3">暂无数据</td></tr>';

  /* lgtbot.db 缺失 / 表缺失时展示 errors */
  const errsBox = document.getElementById('metrics-game-errors');
  const errs = g.errors || [];
  if (errs.length) {
    errsBox.innerHTML = errs.map(e => escapeHtml(e)).join('<br>');
    errsBox.style.display = 'block';
  } else {
    errsBox.style.display = 'none';
  }
}

function metricsApplyData(data) {
  metricsRenderStats(data.stats);
  metricsRenderRuntime(data.runtime);
  metricsRenderGame(data.game, data.active_push);
  document.getElementById('metrics-query-time').textContent = metricsFmtTime(data.query_time);
}

function metricsLoadInline() {
  try {
    metricsApplyData(JSON.parse(document.getElementById('metrics-data').textContent));
  } catch (e) {
    console.warn('[metrics] load failed:', e);
  }
}

/* 调隐藏 action 端点,解 <pre id="result"> 里的 JSON(同 audit) */
async function metricsCallAction(key) {
  const r = await fetch(apiUrl(key), { cache: 'no-store' });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const text = await r.text();
  const doc = new DOMParser().parseFromString(text, 'text/html');
  const el = doc.getElementById('result');
  if (!el) throw new Error('响应不含 #result');
  return JSON.parse(el.textContent);
}

function metricsShowMsg(text, kind) {
  const el = document.getElementById('metrics-action-msg');
  if (!el) return;
  el.textContent = text;
  el.className = 'metrics-action-msg dash-msg-' + (kind || 'info');
}

/* ──── 统一刷新(三个区一起) ──── */
async function metricsRefresh() {
  const btn = document.getElementById('metrics-refresh-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ ...'; }
  try {
    const data = await metricsCallAction(METRICS_REFRESH_KEY);
    if (data.success) {
      metricsApplyData(data);
      metricsShowMsg('');
    } else {
      metricsShowMsg('❌ 刷新失败', 'err');
    }
  } catch (e) {
    metricsShowMsg('❌ ' + e.message, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🔄 刷新'; }
  }
}

window.addEventListener('DOMContentLoaded', () => {
  metricsLoadInline();
  const refreshBtn = document.getElementById('metrics-refresh-btn');
  if (refreshBtn) refreshBtn.addEventListener('click', metricsRefresh);
});
