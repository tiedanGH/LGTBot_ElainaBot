/* ──── 昵称审核 tab ────
 * 无参动作走隐藏 action(fragment 协议);带参的处置与设置保存走真路由 ——
 * 隐藏 action 的 provider 不接参数。
 */

const REVIEW_REFRESH_KEY = '__REVIEW_REFRESH_KEY__';
const REVIEW_TOGGLE_KEY = '__REVIEW_TOGGLE_KEY__';
const REVIEW_SCAN_START_KEY = '__REVIEW_SCAN_START_KEY__';
const REVIEW_SCAN_PAUSE_KEY = '__REVIEW_SCAN_PAUSE_KEY__';
const REVIEW_SCAN_RESET_KEY = '__REVIEW_SCAN_RESET_KEY__';
const REVIEW_VERDICT_ROUTE = '__REVIEW_VERDICT_ROUTE__';
const REVIEW_SETTINGS_ROUTE = '__REVIEW_SETTINGS_ROUTE__';

let reviewData = null;
let reviewScanTimer = null;

function reviewFmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.getFullYear() + '-' +
         String(d.getMonth() + 1).padStart(2, '0') + '-' +
         String(d.getDate()).padStart(2, '0') + ' ' +
         String(d.getHours()).padStart(2, '0') + ':' +
         String(d.getMinutes()).padStart(2, '0');
}

function reviewShowMsg(text, kind) {
  const el = document.getElementById('review-msg');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'review-msg dash-msg-' + (kind || 'info');
}

async function reviewCallAction(key) {
  const r = await fetch(apiUrl(key), { cache: 'no-store' });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const text = await r.text();
  const doc = new DOMParser().parseFromString(text, 'text/html');
  const el = doc.getElementById('result');
  if (!el) throw new Error('响应不含 #result');
  return JSON.parse(el.textContent);
}

/* 三档状态:未启用 / 已启用 / 已启用但中央 LLM 不可用。
   最后一档仍遮蔽已有结论,只是发不出新的审核请求。 */
function reviewStateView(d) {
  if (!d.enabled) {
    return {text: '未启用', cls: 'off',
            sub: d.llm_available ? '点击下方按钮启用' : d.llm_message};
  }
  if (!d.llm_available) {
    return {text: '已启用 · 降级', cls: 'degraded',
            sub: d.llm_message + '（已有结论仍然生效）'};
  }
  return {text: '已启用', cls: 'on',
          sub: d.fail_closed ? '未审核的昵称先显示匿名' : '未审核的昵称先按真名显示'};
}

function reviewApplyData(d) {
  reviewData = d;
  document.getElementById('review-db').textContent = d.db_path || '—';

  const sv = reviewStateView(d);
  const stateEl = document.getElementById('review-state');
  stateEl.textContent = sv.text;
  stateEl.className = 'review-status-value ' + sv.cls;
  document.getElementById('review-state-sub').textContent = sv.sub;

  const st = d.stats || {};
  document.getElementById('review-total').textContent = (st.total != null) ? st.total : '—';
  document.getElementById('review-total-sub').textContent = '永久缓存昵称结论';
  document.getElementById('review-flagged').textContent = (st.flagged != null) ? st.flagged : '—';
  document.getElementById('review-flagged-sub').textContent =
    '未处理 ' + (st.pending || 0) + ' 条';
  document.getElementById('review-pending').textContent = (st.pending != null) ? st.pending : '—';

  const scan = d.scan || {};
  document.getElementById('review-calls').textContent = scan.calls_today || 0;
  document.getElementById('review-calls-sub').textContent =
    '累计调用 ' + (scan.calls_total || 0);

  const toggle = document.getElementById('review-toggle');
  setBtnIcon(toggle, '#i-review',
             d.enabled ? '关闭昵称审核' : '启用昵称审核');
  toggle.classList.toggle('active', !!d.enabled);
  toggle.disabled = !d.enabled && !d.llm_available;
  toggle.title = toggle.disabled ? d.llm_message : '';

  /* 批量扫描的两个前置条件:总开关开着,且中央 LLM 可用 */
  document.getElementById('review-scan-section')
    .classList.toggle('disabled', !(d.enabled && d.llm_available));

  reviewApplySettings(d);
  reviewApplyScan(scan);
  reviewApplyEntries(d.entries || []);
  reviewApplyAllowed(d.allowed || []);
  reviewSyncBadge(st.pending || 0, !!d.enabled);
}

/* 选项来自中央模块的公开配置,值是本地保存的选择;已失效的选择回落到
   「自动选择」,与后端 resolve_selection 一致。 */
function reviewApplySettings(d) {
  const providers = d.providers || [];
  const state = document.getElementById('review-llm-state');
  state.textContent = d.llm_available
    ? ('中央 AI LLM 已就绪，共 ' + providers.length + ' 个可用接口')
    : (d.llm_message || '中央 AI LLM 不可用');
  state.style.color = d.llm_available ? '' : 'var(--warn)';

  const ps = document.getElementById('review-provider');
  ps.innerHTML = '<option value="">自动选择（按接口优先级）</option>' +
    providers.map(p => '<option value="' + escapeHtml(p.id) + '">' +
                       escapeHtml(p.name) + '</option>').join('');
  ps.value = providers.some(p => p.id === d.provider_id) ? d.provider_id : '';
  reviewFillModels(providers, d.model || '');

  document.getElementById('review-batch').value = d.batch_size || 40;
  document.getElementById('review-failmode').value = d.fail_closed ? '1' : '0';
}

function reviewFillModels(providers, want) {
  const pid = document.getElementById('review-provider').value;
  const p = providers.find(x => x.id === pid);
  const list = p ? p.models : [...new Set(providers.flatMap(x => x.models || []))];
  const el = document.getElementById('review-model');
  el.innerHTML = '<option value="">自动选择（按模型优先级）</option>' +
    list.map(m => '<option value="' + escapeHtml(m) + '">' + escapeHtml(m) + '</option>').join('');
  el.value = list.includes(want) ? want : '';
}

async function reviewSaveSettings(btn) {
  const q = new URLSearchParams({
    provider_id: document.getElementById('review-provider').value,
    model: document.getElementById('review-model').value,
    batch_size: document.getElementById('review-batch').value,
    fail_closed: document.getElementById('review-failmode').value === '1' ? '1' : '0',
  });
  const old = btnLabel(btn);
  btn.disabled = true;
  setBtnIcon(btn, '#i-hourglass', '...');
  try {
    const url = REVIEW_SETTINGS_ROUTE + TOKEN_QS + (TOKEN_QS ? '&' : '?') + q.toString();
    const data = await (await fetch(url, { cache: 'no-store' })).json();
    reviewShowMsg((data.success ? '✅ ' : '❌ ') + (data.message || ''),
                  data.success ? 'ok' : 'err');
  } catch (e) {
    reviewShowMsg('❌ ' + e.message, 'err');
  } finally {
    btn.disabled = false;
    setBtnIcon(btn, '#i-save', old);
  }
  await reviewRefresh();
}

function reviewApplyScan(scan) {
  const total = scan.total || 0;
  const done = Math.min(scan.scanned || 0, total || (scan.scanned || 0));
  const pct = total > 0 ? Math.min(100, Math.round(done * 100 / total)) : 0;
  document.getElementById('review-scan-fill').style.width = pct + '%';
  /* 「取到昵称」与「新送审」必须单列:换过 bot 之后老玩家在框架里查不到昵称,
     进度会跑满却一次都没送审,只看百分比看不出是这个原因 */
  document.getElementById('review-scan-text').textContent =
    (scan.running ? '扫描中 ' : '') + done + ' / ' + total +
    (total > 0 ? '（' + pct + '%）' : '') +
    '　取到昵称 ' + (scan.resolved || 0) +
    '　新送审 ' + (scan.queued || 0);

  const errEl = document.getElementById('review-scan-error');
  const err = scan.last_error || {};
  /* 指定了模型时中央不做故障切换,这类错误只能靠改选择解决,把话说到位 */
  const hint = (err.permanent && (reviewData || {}).model)
    ? '　指定模型时中央不会故障切换，请在上方改选接口 / 模型。' : '';
  errEl.textContent = err.message
    ? ((err.permanent ? '⚠️ 需要处理：' : '⚠️ ') + err.message + hint)
    : '';
  errEl.className = 'review-scan-error' + (err.permanent ? ' permanent' : '');

  /* 扫描进行中才轮询 */
  if (scan.running && reviewScanTimer === null) {
    reviewScanTimer = setInterval(reviewRefresh, 5000);
  } else if (!scan.running && reviewScanTimer !== null) {
    clearInterval(reviewScanTimer);
    reviewScanTimer = null;
  }
}

let reviewShowHandled = false;

/* 一行记录。昵称绝不换行,点开才看全名。 */
function reviewRow(e, acts, extraClass) {
  const key = escapeHtml(e.key || '');
  const name = escapeHtml(e.sample || '');
  return '<div class="review-row' + (extraClass ? ' ' + extraClass : '') + '">' +
    '<span class="review-name" data-name="' + name + '" title="点击查看完整昵称">' +
      name + '</span>' +
    '<span class="review-time">' + escapeHtml(reviewFmtTime(e.ts)) + '</span>' +
    acts.map(a =>
      '<button class="dash-btn dash-btn-small ' + a.cls + '" data-key="' + key +
      '" data-op="' + a.op + '">' +
      '<svg class="ui-icon btn-icon"><use href="' + a.icon + '"/></svg>' +
      '<span class="btn-label">' + a.text + '</span></button>').join('') +
    '</div>';
}

function reviewApplyEntries(entries) {
  const pending = entries.filter(e => !e.handled);
  const handled = entries.filter(e => e.handled);
  const shown = reviewShowHandled ? pending.concat(handled) : pending;

  const btn = document.getElementById('review-toggle-handled');
  btn.textContent = (reviewShowHandled ? '隐藏已处理' : '展开已处理') +
                    (handled.length ? '（' + handled.length + '）' : '');
  btn.classList.toggle('active', reviewShowHandled);
  btn.disabled = !handled.length;

  const body = document.getElementById('review-list');
  if (!shown.length) {
    body.innerHTML = '<div class="review-empty-row">' +
      (handled.length ? '没有待处理的违规记录' : '暂无违规记录') + '</div>';
  } else {
    body.innerHTML = shown.map(e => reviewRow(e, e.handled
      ? [{op: 'acquit', cls: '', icon: '#i-undo', text: '翻案'},
         {op: 'reopen', cls: 'dash-btn-success', icon: '#i-rotate-ccw', text: '撤销处理'}]
      : [{op: 'acquit', cls: '', icon: '#i-undo', text: '翻案'},
         {op: 'handled', cls: '', icon: '#i-check', text: '已处理'}],
      e.handled ? 'handled' : '')).join('');
  }
  reviewBindRow(body);
}

function reviewApplyAllowed(allowed) {
  document.getElementById('review-allowed-count').textContent = allowed.length;
  const body = document.getElementById('review-allow-list');
  body.innerHTML = allowed.length
    ? allowed.map(e => reviewRow(e, [
        {op: 'revoke', cls: '', icon: '#i-undo', text: '撤销白名单'},
        {op: 'condemn', cls: 'review-btn-quiet', icon: '#i-alert', text: '判定违规'},
      ])).join('')
    : '<div class="review-empty-row">暂无白名单记录</div>';
  reviewBindRow(body);
}

function reviewBindRow(root) {
  root.querySelectorAll('.review-name').forEach(el =>
    el.addEventListener('click', ev => reviewPopName(ev, el.dataset.name)));
  root.querySelectorAll('button[data-op]').forEach(b =>
    b.addEventListener('click', () => reviewVerdict(
      b.dataset.key, b.dataset.op,
      b.closest('.review-row').querySelector('.review-name').dataset.name)));
}

/* 气泡贴着点击位置浮在上方,不锁整页 */
function reviewPopName(ev, name) {
  const el = document.getElementById('review-pop');
  if (!el) return;
  el.textContent = name || '';
  el.hidden = false;
  const left = Math.min(Math.max(8, ev.clientX - el.offsetWidth / 2),
                        window.innerWidth - el.offsetWidth - 8);
  const above = ev.clientY - el.offsetHeight - 10;
  el.style.left = left + 'px';
  el.style.top = (above >= 8 ? above : ev.clientY + 16) + 'px';
}

/* 有未处理的违规就亮角标,不论当前停在哪个标签 */
function reviewSyncBadge(pending, enabled) {
  const el = document.getElementById('review-pending-badge');
  if (!el) return;
  const on = enabled && pending > 0;
  el.textContent = on ? String(pending) : '';
  el.classList.toggle('on', on);
}

async function reviewVerdict(key, op, name) {
  const ask = {
    acquit: '确认翻案？\n\n昵称：%s\n\n该昵称会立即恢复真名显示，并转入白名单 —— 后续批量重扫不会再把它判为违规。',
    revoke: '确认撤销白名单？\n\n昵称：%s\n\n该昵称会退回违规记录的待处理，并重新按匿名显示。',
    condemn: '确认判定违规？\n\n昵称：%s\n\n该昵称会立即按匿名显示，并直接标记为已处理。',
  }[op];
  /* 替换值走函数形式:昵称是用户可控文本,字符串形式会把里面的 $& 当成替换模式 */
  if (ask && !await dashConfirm(ask.replace('%s', () => name || key),
                                {level: 'warn'})) return;
  try {
    const url = REVIEW_VERDICT_ROUTE + TOKEN_QS + (TOKEN_QS ? '&' : '?') +
                'key=' + encodeURIComponent(key) + '&op=' + encodeURIComponent(op);
    const r = await fetch(url, { cache: 'no-store' });
    const data = await r.json();
    reviewShowMsg((data.success ? '✅ ' : '❌ ') + (data.message || ''),
                  data.success ? 'ok' : 'err');
  } catch (e) {
    reviewShowMsg('❌ ' + e.message, 'err');
  }
  await reviewRefresh();
}

async function reviewRunAction(key, btn) {
  setBtnBusy(btn, '...');
  try {
    const data = await reviewCallAction(key);
    reviewApplyData(data);
    if (data.message) reviewShowMsg(data.message, data.success ? 'ok' : 'err');
  } catch (e) {
    reviewShowMsg('❌ ' + e.message, 'err');
  } finally {
    clearBtnBusy(btn);
  }
}

async function reviewRefresh() {
  try {
    reviewApplyData(await reviewCallAction(REVIEW_REFRESH_KEY));
  } catch (e) {
    console.warn('[review] refresh failed:', e);
  }
}

function reviewLoadInline() {
  try {
    reviewApplyData(JSON.parse(document.getElementById('review-data').textContent));
  } catch (e) {
    console.warn('[review] load failed:', e);
  }
}

window.addEventListener('DOMContentLoaded', () => {
  reviewLoadInline();
  const bind = (id, key, confirmText) => {
    const btn = document.getElementById(id);
    if (!btn) return;
    btn.addEventListener('click', async () => {
      if (confirmText && !await dashConfirm(confirmText, {level: 'warn'})) return;
      await reviewRunAction(key, btn);
    });
  };
  const toggle = document.getElementById('review-toggle');
  if (toggle) {
    toggle.addEventListener('click', async () => {
      const on = reviewData && reviewData.enabled;
      const text = on
        ? '确认关闭昵称审核？\n\n关闭后所有昵称按原样显示（已有结论保留，重新启用即刻恢复）。'
        : '确认启用昵称审核？\n\n违规昵称将在对局图片 / 播报 / 排行榜 / 面板里显示为匿名。';
      if (!await dashConfirm(text, {level: 'warn'})) return;
      await reviewRunAction(REVIEW_TOGGLE_KEY, toggle);
    });
  }
  const provider = document.getElementById('review-provider');
  if (provider) {
    provider.addEventListener('change', () =>
      reviewFillModels((reviewData || {}).providers || [], ''));
  }
  const save = document.getElementById('review-save');
  if (save) save.addEventListener('click', () => reviewSaveSettings(save));
  const handledBtn = document.getElementById('review-toggle-handled');
  if (handledBtn) {
    handledBtn.addEventListener('click', () => {
      reviewShowHandled = !reviewShowHandled;
      reviewApplyEntries((reviewData || {}).entries || []);
    });
  }
  const pop = document.getElementById('review-pop');
  if (pop) {
    const hide = () => { pop.hidden = true; };
    /* 捕获阶段:点另一个昵称时先于它自己的 handler 跑,别把刚要重定位的气泡关掉 */
    document.addEventListener('click', ev => {
      if (!ev.target.classList.contains('review-name')) hide();
    }, true);
    document.addEventListener('keydown', ev => { if (ev.key === 'Escape') hide(); });
    window.addEventListener('scroll', hide, true);
  }
  bind('review-refresh', REVIEW_REFRESH_KEY);
  bind('review-scan-start', REVIEW_SCAN_START_KEY);
  bind('review-scan-pause', REVIEW_SCAN_PAUSE_KEY);
  bind('review-scan-reset', REVIEW_SCAN_RESET_KEY,
       '确认重置扫描游标？\n\n下次将从头遍历玩家表。已有结论仍会被跳过，不会重复消耗调用。');
});
