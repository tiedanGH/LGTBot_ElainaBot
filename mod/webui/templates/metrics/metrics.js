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
  document.getElementById('metrics-stat-user-cache').textContent = metricsFmtNum(s.users_total);
  document.getElementById('metrics-stat-lgt-users').textContent = metricsFmtNum(s.lgtbot_users);
  document.getElementById('metrics-stat-matches').textContent = metricsFmtNum(s.lgtbot_matches);
  document.getElementById('metrics-stat-attendances').textContent = metricsFmtNum(s.lgtbot_match_attendances);
  document.getElementById('metrics-stat-achievements').textContent = metricsFmtNum(s.lgtbot_achievements);

  /* 玩家转化(跨库:lgtbot 注册 ÷ 框架用户):任一缺失 → — */
  document.getElementById('metrics-stat-conversion').textContent =
    (s.player_conversion == null) ? '—' : (s.player_conversion + '%');

  /* 近 10 日私信用户(wakeup.db,日粒度),数据随 stats 下发,在此一并渲染 */
  document.getElementById('metrics-stat-dm10').textContent = metricsFmtNum(s.dm_active_10d);
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

  /* 图床可用性徽章(仅查配置 + 主框架 status(),非真实上传探测):
     ok=绿 / unset=黄 / 其余=红。文字带 backend 名,完整原因进 title tooltip。 */
  const badge = document.getElementById('metrics-hosting-badge');
  if (badge) {
    const h = r.hosting || {};
    const MAP = {
      ok:          {cls: 'ok',    text: '可用'},
      unset:       {cls: 'warn',  text: '未配置'},
      module_off:  {cls: 'err',   text: '模块未启用'},
      backend_off: {cls: 'err',   text: '未启用'},
      unknown:     {cls: 'err',   text: '未知图床'},
    };
    const info = MAP[h.state] || MAP.unset;
    const shown = h.display || h.backend;   // 中文显示名优先(any → 自动)
    badge.textContent = (shown ? shown + ' · ' : '') + info.text;
    badge.className = 'metrics-badge metrics-badge-' + info.cls;
    badge.title = h.label || '';
  }

  /* 配额压力:大数字 = 等待超时次数,小字 = 耗尽次数 */
  document.getElementById('metrics-quota-timeout').textContent =
    (r.quota_wait_timeout || 0) + ' 次';
  document.getElementById('metrics-quota-sub').textContent =
    '配额耗尽 ' + (r.quota_exhausted || 0) + ' 次';

  /* 异常发送失败:大数字 = 非预期的 API 拒绝(排除 40034105 配额超时的无权限主动拒绝),
  小字 = 全部失败次数(含预期拒绝)。按码分布只留存 metrics.json,不上 UI —— 码一多会把卡片挤破。 */
  document.getElementById('metrics-send-fail').textContent =
    (r.send_fail_total || 0) + ' 次';
  const failAll = r.send_fail_all || 0;
  document.getElementById('metrics-send-fail-sub').textContent =
    failAll > 0 ? ('全部发送失败 ' + failAll + ' 次') : '暂无失败记录';

  /* 重启次数(面板 / 指令触发的主动重启):小字上次重启时间 */
  document.getElementById('metrics-restart-total').textContent =
    (r.restart_total || 0) + ' 次';
  document.getElementById('metrics-restart-sub').textContent =
    r.last_restart_ts ? ('上次重启 ' + metricsFmtTime(r.last_restart_ts)) : '暂无重启记录';

  /* 崩溃重启:小字只给上次崩溃时间(与「重启次数」卡同格式)  */
  const crashTotal = r.crash_total || 0;
  document.getElementById('metrics-crash-total').textContent = crashTotal + ' 次';
  document.getElementById('metrics-crash-sub').textContent =
    (crashTotal > 0 && r.last_crash_ts)
      ? ('上次崩溃 ' + metricsFmtTime(r.last_crash_ts))
      : '暂无崩溃记录';
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

/* 涨跌标签(形态与配色对齐 dau 卡片:涨红跌绿)。前缀文案保持灰、只有标签着色,所以返回的是 HTML 片段而非纯文本。
   diff 一律先过 Number:调用方给的是两数相减,理论上必为数字,强制收敛一次杜绝畸形 payload 让字符串拼进 innerHTML。 */
function metricsDeltaTag(diff) {
  const n = Number(diff);
  if (!isFinite(n)) return '';
  if (n > 0) return '<span class="metrics-delta-tag metrics-delta-up">↑ ' + n + '</span>';
  if (n < 0) return '<span class="metrics-delta-tag metrics-delta-down">↓ ' + Math.abs(n) + '</span>';
  return '<span class="metrics-delta-tag">- 0</span>';
}

/* 「较昨日同时段」涨跌副标题(模仿 dau 面板的增减标识):有对比数据时替换
   卡片 sub 行,缺数据(旧库 / 查询失败)保留原说明文案。 */
function metricsDeltaSub(id, cur, yday, fallback) {
  const el = document.getElementById(id);
  if (!el) return;
  if (cur == null || yday == null) { el.textContent = fallback; return; }
  el.innerHTML = '较昨日同时段 ' + metricsDeltaTag(cur - yday);
}

function metricsRenderGame(game, activePush) {
  const g = game || {};
  document.getElementById('metrics-today-matches').textContent = metricsFmtNum(g.today_matches);
  document.getElementById('metrics-today-players').textContent = metricsFmtNum(g.today_players);
  document.getElementById('metrics-today-groups').textContent = metricsFmtNum(g.today_groups);
  metricsDeltaSub('metrics-today-matches-sub', g.today_matches,
                  g.yesterday_matches_same_span, '当日 00:00 起已结束的对局');
  metricsDeltaSub('metrics-today-players-sub', g.today_players,
                  g.yesterday_players_same_span, '今日参与游戏的玩家');
  metricsDeltaSub('metrics-today-groups-sub', g.today_groups,
                  g.yesterday_groups_same_span, '今日进行游戏的群聊');

  /* 「近 10 日私信用户」卡的 sub 行借位展示:
     近 10 日对局总数 + 对比上一个 10 日整期的涨跌(与数据统计图片卡同口径:trend 求和 vs prev10_matches)。
     任一缺数据回退原说明文案。卡片主数值(私信用户数)不动。 */
  const dm10Sub = document.getElementById('metrics-stat-dm10-sub');
  if (dm10Sub) {
    const trend10 = Array.isArray(g.trend_10d) ? g.trend_10d : [];
    const total10 = trend10.length
      ? trend10.reduce((a, t) => a + (Number(t.count) || 0), 0) : null;
    if (total10 == null || g.prev10_matches == null) {
      dm10Sub.textContent = '私信过机器人的活跃用户数';
    } else {
      dm10Sub.innerHTML = '近 10 日对局 ' + total10 + ' '
        + metricsDeltaTag(total10 - g.prev10_matches);
    }
  }

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
