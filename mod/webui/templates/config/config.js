/* ──── 配置管理 tab ────
 * 编辑器共享同一套读 / dirty 跟踪 / 保存 / revert 逻辑,通过 cfgEditors 表驱动。
 * 保存一律走本插件端点 /api/ext/lgtbot/config/save:config.yaml / lgtbot.json 要过服务端
 * 语法 + schema 校验,失败返回 errors 列表、文件保持原样;纯文本编辑器没有可校验的语法。
 * config.yaml 另有可视化编辑(见下方 pcfg 一节),保存由 pcfgSave 接管。
 * 热重载按钮调 __lgtbot_dash_reload_config。
 */

const CFG_KEYS = {
  reload_config: '__lgtbot_dash_reload_config',
  urgent_toggle: '__lgtbot_urgent_toggle',
  urgent_reset: '__lgtbot_urgent_reset',
};
const CFG_VALIDATED_SAVE_ROUTE = '/api/ext/lgtbot/config/save';

/* 每个编辑器一份状态:
 *   dataKey  —— get_data() 返回的 JSON 里对应哪个 key(缺失 = 后端没渲染该区块)
 *   absPath  —— 文件绝对路径,只用于展示(保存路径由服务端按 target 解析)
 *   original —— 最近一次从后端拉到的内容,用来做 dirty 检测 + 「恢复」
 *   format   —— yaml / json / text,json 保存前先在前端过一遍语法
 *   editorId / pathId / msgId / saveBtnId —— 对应 DOM 元素 id
 *
 * 表项 → 数据的对应全靠 dataKey,cfgApplyData 直接遍历本表 —— 服务端按运行时
 * 开关裁掉某一段(见 page_config.render_tab_js)时这里少一项即可,不用改别处。 */
const cfgEditors = {
  yaml: {
    dataKey: 'config_yaml',
    absPath: '', original: '', format: 'yaml', target: 'config_yaml',
    editorId: 'cfg-yaml-editor', pathId: 'cfg-yaml-path',
    msgId: 'cfg-yaml-msg', saveBtnId: 'cfg-yaml-save', revertBtnId: 'cfg-yaml-revert',
    saveHint: '，请点「🔁 热重载配置」即时下发到运行时',
  },
  important: {
    dataKey: 'important_update',
    absPath: '', original: '', format: 'text', target: 'important_update',
    editorId: 'cfg-important-editor', pathId: 'cfg-important-path',
    msgId: 'cfg-important-msg', saveBtnId: 'cfg-important-save', revertBtnId: 'cfg-important-revert',
    saveHint: '，下次发送「更新公告」指令时即生效；留空则不渲染该区块',
  },
  notice: {
    dataKey: 'update_notice',
    absPath: '', original: '', format: 'text', target: 'update_notice',
    editorId: 'cfg-notice-editor', pathId: 'cfg-notice-path',
    msgId: 'cfg-notice-msg', saveBtnId: 'cfg-notice-save', revertBtnId: 'cfg-notice-revert',
    saveHint: '，下次发送「更新公告」指令时即生效',
  },
  urgent: {
    dataKey: 'urgent_notice',
    absPath: '', original: '', format: 'text', target: 'urgent_notice',
    editorId: 'cfg-urgent-editor', pathId: 'cfg-urgent-path',
    msgId: 'cfg-urgent-msg', saveBtnId: 'cfg-urgent-save', revertBtnId: 'cfg-urgent-revert',
    saveHint: '，下次打开欢迎菜单时即生效（需「启用紧急公告」）',
  },
  trouble: {
    dataKey: 'troubleshooting',
    absPath: '', original: '', format: 'text', target: 'troubleshooting',
    editorId: 'cfg-trouble-editor', pathId: 'cfg-trouble-path',
    msgId: 'cfg-trouble-msg', saveBtnId: 'cfg-trouble-save', revertBtnId: 'cfg-trouble-revert',
    saveHint: '，下次发送「疑难解答」指令时即生效',
  },
  /* SPONSOR_JS_START
   * sponsor_enabled 关闭时整段被服务端切掉,标记之内不要放别的表项。 */
  sponsors: {
    dataKey: 'sponsors',
    absPath: '', original: '', format: 'text', target: 'sponsors',
    editorId: 'cfg-sponsors-editor', pathId: 'cfg-sponsors-path',
    msgId: 'cfg-sponsors-msg', saveBtnId: 'cfg-sponsors-save', revertBtnId: 'cfg-sponsors-revert',
    saveHint: '，下次发送「赞助支持」指令时即生效',
  },
  /* SPONSOR_JS_END */
  engine: {
    dataKey: 'engine_config',
    absPath: '', original: '', format: 'json', target: 'engine_json',
    editorId: 'cfg-engine-editor', pathId: 'cfg-engine-path',
    msgId: 'cfg-engine-msg', saveBtnId: 'cfg-engine-save', revertBtnId: 'cfg-engine-revert',
    saveHint: '，需重启 LGTBot 引擎或整进程才能生效',
  },
};

/* ──── 紧急公告的运行状态(开关 / 已通知群数) ────
 * 与文案编辑器分开:文案存 txt,这两项存 data/urgent_notice.json(重启后仍生效)。
 * 服务端在 urgent_notice 数据块里回带 enabled / notified_count,这里只负责把它画到按钮上。 */
function cfgApplyUrgentState(info) {
  if (!info) return;
  const on = !!info.enabled;
  const toggle = document.getElementById('cfg-urgent-toggle');
  if (toggle) {
    setBtnIcon(toggle, '#i-bell', on ? '关闭紧急公告' : '启用紧急公告');
    toggle.classList.toggle('active', on);
    toggle.disabled = false;          /* 动作跑完后由这里统一恢复可点 */
  }
  const reset = document.getElementById('cfg-urgent-reset');
  if (reset) {
    const n = info.notified_count || 0;
    setBtnIcon(reset, '#i-reset', '重置全部已通知群（' + n + ' 个）');
    /* 一个都没通知过时没什么可重置,禁用避免误点(仍留着让人知道有这功能) */
    reset.disabled = !n;
  }
}

function cfgApplyData(data) {
  cfgApplyUrgentState(data.urgent_notice);
  Object.entries(cfgEditors).forEach(([k, state]) => {
    const info = data[state.dataKey];
    if (!info) return;
    state.absPath = info.abs_path || '';
    state.original = info.content || '';
    const pathEl = document.getElementById(state.pathId);
    if (pathEl) pathEl.textContent = state.absPath || '—';
    const editor = document.getElementById(state.editorId);
    if (editor && !editor.dataset.dirty) {
      editor.value = state.original;
    }
    if (info.read_error) {
      cfgShowMsg(k, '读取失败：' + info.read_error, 'err');
    }
  });
  if (data.config_yaml) pcfgApplyForm(data.config_yaml.form);
}

function cfgLoadInline() {
  try {
    const data = JSON.parse(document.getElementById('config-data').textContent);
    cfgApplyData(data);
  } catch (e) {
    console.warn('[config] load failed:', e);
  }
}

function cfgShowMsg(key, msg, kind) {
  const el = document.getElementById(cfgEditors[key].msgId);
  if (!el) return;
  el.textContent = msg;
  el.className = 'dash-config-msg dash-msg-' + (kind || 'info');
}

async function cfgSave(key) {
  if (key === 'yaml') return pcfgSave();
  const state = cfgEditors[key];
  const editor = document.getElementById(state.editorId);
  if (!editor) return;
  const text = editor.value;
  const btn = document.getElementById(state.saveBtnId);
  if (btn) btn.disabled = true;
  try {
    /* 一律走插件端点:路径由服务端按 target 解析,客户端传的绝对路径不参与保存。
       yaml / json 要过服务端语法 + schema 校验,不过就返回 errors 且文件不动。 */
    if (state.format === 'json') {
      try { JSON.parse(text); }
      catch (e) { cfgShowMsg(key, '❌ JSON 格式错误：' + e.message, 'err'); return; }
    }
    const r = await fetch(CFG_VALIDATED_SAVE_ROUTE + TOKEN_QS, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target: state.target, content: text }),
    });
    const data = await r.json();
    if (data.success) {
      const warn = (data.warnings && data.warnings.length)
        ? '（提醒：' + data.warnings.join('；') + '）' : '';
      const okText = state.format === 'text' ? '✅ 已保存' : '✅ 校验通过并已保存';
      cfgShowMsg(key, okText + state.saveHint + warn, warn ? 'info' : 'ok');
      state.original = text;
      delete editor.dataset.dirty;
    } else {
      const errs = (data.errors && data.errors.length)
        ? data.errors.join('；') : '保存失败';
      cfgShowMsg(key, '❌ 未保存 —— ' + errs, 'err');
    }
  } catch (e) {
    cfgShowMsg(key, '❌ 请求失败：' + e.message, 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function cfgRevert(key) {
  const state = cfgEditors[key];
  const editor = document.getElementById(state.editorId);
  if (!editor) return;
  editor.value = state.original;
  delete editor.dataset.dirty;
  if (key === 'yaml') {
    pcfg.lastEdited = null;
    pcfgRender(pcfg.saved);
    pcfg.dirtyShown = false;
  }
  cfgShowMsg(key, '已恢复至上次加载内容', 'info');
}

/* ──── 插件配置:可视化 / 源文件 ────
 * 字段表 fields 与表单值由服务端从 config.DEFAULT_CONFIG 推导,表单按 config.yaml 的字段顺序。
 * 两种视图是同一份配置:哪边改过(lastEdited)就以哪边为准,切换模式时经保存端点的 dry_run 让服务端互转(yaml 的解析与渲染只在服务端做)。 */
const PCFG_MODE_KEY = 'lgtbot-cfg-yaml-mode';
/* 列表框默认至少几行(最多 6 行,再多出滚动条) */
const PCFG_LIST_MIN_ROWS = {admin_uids: 4, notify_groups: 4};
const pcfg = {mode: 'visual', fields: [], saved: null, extra: [], errors: [], lastEdited: null, dirtyShown: false};

function pcfgSplitList(text) {
  return String(text || '').split('\n').map(s => s.trim()).filter(Boolean);
}

function pcfgOptionLabel(o, known) {
  if (o === '') return '不启用';
  if (o === 'any') return '自动选择';
  return known ? o : o + '（未知）';
}

function pcfgFieldHtml(f, v) {
  const id = 'pcfg-in-' + f.key;
  let head = '<label class="pcfg-label" for="' + id + '"><span class="pcfg-label-text">' + escapeHtml(f.label) +
             '</span><code class="pcfg-key">' + escapeHtml(f.key) + '</code></label>';
  let control = '';
  if (f.kind === 'bool') {
    head += '<input type="checkbox" id="' + id + '" class="pcfg-switch"' + (v ? ' checked' : '') + '>';
  } else if (f.kind === 'list') {
    const items = (Array.isArray(v) ? v : []).map(x => String(x));
    head += '<span class="pcfg-count" data-count-for="' + escapeHtml(f.key) + '"></span>';
    control = '<textarea id="' + id + '" class="pcfg-input" spellcheck="false" placeholder="每行一项" rows="' +
              Math.min(Math.max(items.length, PCFG_LIST_MIN_ROWS[f.key] || 2), 6) + '">' +
              escapeHtml(items.join('\n')) + '</textarea>';
  } else if (f.kind === 'select') {
    const cur = v == null ? '' : String(v);
    const opts = f.options.includes(cur) ? f.options : [...f.options, cur];
    control = '<select id="' + id + '" class="pcfg-input">' + opts.map(o =>
      '<option value="' + escapeHtml(o) + '"' + (o === cur ? ' selected' : '') + '>' +
      escapeHtml(pcfgOptionLabel(o, f.options.includes(o))) + '</option>').join('') + '</select>';
  } else {
    const num = f.kind === 'int' || f.kind === 'float';
    control = '<input id="' + id + '" class="pcfg-input" spellcheck="false" ' +
              (num ? 'type="number" min="0" step="' + (f.kind === 'int' ? '1' : 'any') + '"' : 'type="text"') +
              ' value="' + escapeHtml(v == null ? '' : String(v)) + '">';
  }
  return '<div class="pcfg-field" data-key="' + escapeHtml(f.key) + '"><div class="pcfg-head">' + head + '</div>' +
         control + (f.hint ? '<p class="pcfg-hint">' + escapeHtml(f.hint) + '</p>' : '') + '</div>';
}

function pcfgRender(values) {
  const box = document.getElementById('pcfg-form');
  if (!box) return;
  box.innerHTML = values ? pcfg.fields.map(f => pcfgFieldHtml(f, values[f.key])).join('') : '';
  pcfgRefresh();
}

/* 表单里某个字段当前的值;数字没填 / 填不成数字时为 NaN(按「改过」算,保存时报错) */
function pcfgRead(f) {
  const el = document.getElementById('pcfg-in-' + f.key);
  if (!el) return undefined;
  if (f.kind === 'bool') return el.checked;
  if (f.kind === 'list') return pcfgSplitList(el.value);
  if (f.kind === 'int' || f.kind === 'float') return el.value.trim() === '' ? NaN : Number(el.value);
  return el.value.trim();
}

/* 已保存的值换成与 pcfgRead 同一口径再比 */
function pcfgNorm(f, v) {
  if (f.kind === 'bool') return !!v;
  if (f.kind === 'list') return (Array.isArray(v) ? v : []).map(x => String(x).trim()).filter(Boolean);
  if (f.kind === 'int' || f.kind === 'float') return Number(v);
  return v == null ? '' : String(v).trim();
}

function pcfgDirtyKeys() {
  const dirty = new Set();
  if (!pcfg.saved) return dirty;
  pcfg.fields.forEach(f => {
    const cur = pcfgRead(f);
    if (cur !== undefined && JSON.stringify(cur) !== JSON.stringify(pcfgNorm(f, pcfg.saved[f.key]))) dirty.add(f.key);
  });
  return dirty;
}

function pcfgDirty() {
  const editor = document.getElementById('cfg-yaml-editor');
  return pcfgDirtyKeys().size > 0 || !!(editor && editor.dataset.dirty);
}

/* 收集表单值;数字字段不合法时返回 {error} */
function pcfgCollect() {
  const values = {};
  for (const f of pcfg.fields) {
    const v = pcfgRead(f);
    if (v === undefined) continue;
    if (f.kind === 'int' || f.kind === 'float') {
      if (!Number.isFinite(v)) return {error: f.label + '需要填写数字'};
      if (f.kind === 'int' && !Number.isInteger(v)) return {error: f.label + '需要填写整数'};
      if (v < 0) return {error: f.label + '不能为负数'};
    }
    values[f.key] = v;
  }
  return {values};
}

/* 「有未保存的修改」只由这里写也只由这里擦,不覆盖保存 / 恢复的结果提示 */
function pcfgStatus() {
  if (pcfgDirty()) {
    cfgShowMsg('yaml', '有未保存的修改', 'warn');
    pcfg.dirtyShown = true;
  } else if (pcfg.dirtyShown) {
    cfgShowMsg('yaml', '', 'info');
    pcfg.dirtyShown = false;
  }
}

function pcfgRefresh() {
  const dirty = pcfgDirtyKeys();
  document.querySelectorAll('#pcfg-form .pcfg-field').forEach(el =>
    el.classList.toggle('dirty', dirty.has(el.dataset.key)));
  pcfg.fields.filter(f => f.kind === 'list').forEach(f => {
    const badge = document.querySelector('#pcfg-form [data-count-for="' + f.key + '"]');
    const items = pcfgRead(f);
    if (badge && items) badge.textContent = items.length + ' 项';
  });
  pcfgStatus();
}

function pcfgShowMode() {
  const visual = pcfg.mode === 'visual';
  const box = document.getElementById('pcfg-visual');
  const editor = document.getElementById('cfg-yaml-editor');
  /* 收起的一方只隐身不移出布局,见 config.css 的 .pcfg-stack;拖拽改过的编辑框高度随切换清掉,两边重新对齐 */
  if (box) box.classList.toggle('pcfg-off', !visual);
  if (editor) {
    editor.classList.toggle('pcfg-off', visual);
    editor.style.height = '';
  }
  document.querySelectorAll('#pcfg-mode .pcfg-mode-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.mode === pcfg.mode));
  const note = document.getElementById('pcfg-note');
  if (note) note.textContent = !visual && !pcfg.saved ? '源文件有误，修正并保存后才能可视化编辑' : '';
  /* 这条随表单一起叠在格子里,源文件模式下照样占位 */
  const extra = document.getElementById('pcfg-extra');
  if (extra) {
    extra.textContent = pcfg.extra.length
      ? '源文件中另有字段 ' + pcfg.extra.join('、') + '，可视化编辑不涉及，保存时原样留在文件末尾' : '';
  }
}

/* 服务端回带的表单数据(首屏 / 保存后):已保存的值即表单初值 */
function pcfgApplyForm(form) {
  if (!form) return;
  pcfg.fields = form.fields || [];
  pcfg.saved = form.values || null;
  pcfg.extra = form.extra || [];
  pcfg.errors = form.errors || [];
  pcfg.lastEdited = null;
  if (!pcfg.saved) pcfg.mode = 'source';
  pcfgRender(pcfg.saved);
  pcfgShowMode();
}

async function pcfgPost(body) {
  const r = await fetch(CFG_VALIDATED_SAVE_ROUTE + TOKEN_QS, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(Object.assign({target: cfgEditors.yaml.target}, body)),
  });
  return r.json();
}

function pcfgShowErr(text) {
  cfgShowMsg('yaml', '❌ ' + text, 'err');
  pcfg.dirtyShown = false;
}

async function pcfgSetMode(mode) {
  if (mode === pcfg.mode) return;
  const editor = document.getElementById('cfg-yaml-editor');
  const modeBox = document.getElementById('pcfg-mode');
  if (mode === 'visual' && !pcfg.saved && pcfg.lastEdited !== 'text') {
    pcfgShowErr('源文件有误，修正并保存后才能切到可视化：' + pcfg.errors.join('；'));
    return;
  }
  modeBox.querySelectorAll('button').forEach(b => { b.disabled = true; });
  try {
    if (mode === 'source' && pcfg.lastEdited === 'form') {
      const c = pcfgCollect();
      if (c.error) { pcfgShowErr(c.error); return; }
      const data = await pcfgPost({values: c.values, dry_run: true});
      editor.value = data.content;
      if (editor.value !== cfgEditors.yaml.original) editor.dataset.dirty = '1';
      else delete editor.dataset.dirty;
    } else if (mode === 'visual' && pcfg.lastEdited === 'text') {
      const data = await pcfgPost({content: editor.value, dry_run: true});
      if (!data.form || !data.form.values) {
        pcfgShowErr('源文件有误，修正后才能切到可视化：' + (data.errors || []).join('；'));
        return;
      }
      /* 只换表单内容,已保存的值不动 —— 改过的字段照样标出来 */
      pcfg.extra = data.form.extra || [];
      pcfgRender(data.form.values);
    }
    pcfg.lastEdited = null;
    pcfg.mode = mode;
    try { localStorage.setItem(PCFG_MODE_KEY, mode); } catch (e) { /* 存不了就只在本次生效 */ }
    pcfgShowMode();
    pcfgStatus();
  } catch (e) {
    pcfgShowErr('请求失败：' + e.message);
  } finally {
    modeBox.querySelectorAll('button').forEach(b => { b.disabled = false; });
  }
}

async function pcfgSave() {
  const state = cfgEditors.yaml;
  const editor = document.getElementById(state.editorId);
  let body;
  /* 可视化模式下表单没改、只有源文件里改了注释这类不进表单的内容时,按源文件保存,免得被重新渲染掉 */
  if (pcfg.mode === 'visual' && (pcfgDirtyKeys().size || !editor.dataset.dirty)) {
    const c = pcfgCollect();
    if (c.error) { pcfgShowErr('未保存 —— ' + c.error); return; }
    body = {values: c.values};
  } else {
    body = {content: editor.value};
  }
  const btn = document.getElementById(state.saveBtnId);
  if (btn) btn.disabled = true;
  try {
    const data = await pcfgPost(body);
    if (data.success) {
      state.original = data.content;
      editor.value = data.content;
      delete editor.dataset.dirty;
      pcfgApplyForm(data.form);
      const notes = [];
      if (data.reordered) notes.push('已按代码中的字段顺序整理');
      if (data.warnings && data.warnings.length) notes.push('提醒：' + data.warnings.join('；'));
      cfgShowMsg('yaml', '✅ 校验通过并已保存' + state.saveHint + (notes.length ? '（' + notes.join('；') + '）' : ''),
                 data.warnings && data.warnings.length ? 'info' : 'ok');
      pcfg.dirtyShown = false;
    } else {
      pcfgShowErr('未保存 —— ' + ((data.errors && data.errors.length) ? data.errors.join('；') : '保存失败'));
    }
  } catch (e) {
    pcfgShowErr('请求失败：' + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ──── 紧急公告:启用 / 关闭 + 重置已通知群 ────
 * 两个动作都走隐藏 action 的 fragment 协议(<pre id="result">JSON</pre>),
 * 与热重载同款;响应回带最新 enabled / notified_count,直接喂给 cfgApplyUrgentState。
 * 两个都先弹确认:开关影响所有用户看到的菜单,重置会让全部群再收一次公告。 */
async function cfgUrgentAction(key, btnId) {
  const btn = document.getElementById(btnId);
  const wasDisabled = btn ? btn.disabled : false;
  if (btn) btn.disabled = true;
  try {
    const r = await fetch(apiUrl(CFG_KEYS[key]), { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
    const el = doc.getElementById('result');
    if (!el) throw new Error('响应不含 #result');
    const data = JSON.parse(el.textContent);
    /* 成功与否都按服务端状态重画按钮(写盘失败时状态没变,按钮也就不该变) */
    cfgApplyUrgentState(data);
    cfgShowMsg('urgent', data.message || (data.success ? '✅ 已完成' : '❌ 操作失败'),
               data.success ? 'ok' : 'err');
  } catch (e) {
    cfgShowMsg('urgent', '❌ 请求失败：' + e.message, 'err');
    if (btn) btn.disabled = wasDisabled;
  }
}

async function cfgUrgentToggle() {
  const btn = document.getElementById('cfg-urgent-toggle');
  const isOn = btn && btn.classList.contains('active');
  const ok = await dashConfirm(
    isOn
      ? '确认关闭紧急公告？\n\n欢迎菜单将不再展示公告，新群建房也不再额外通知。\n已通知群记录会保留（重新启用时这些群不会被重复通知）。'
      : '确认启用紧急公告？\n\n欢迎菜单会展示当前公告文案；启用后每个群首次新建游戏房间时，会额外收到一条紧急公告。\n状态写入数据文件，重启后依然生效，直到手动关闭。',
    {level: isOn ? 'info' : 'warn', okText: isOn ? '关闭' : '启用'}
  );
  if (!ok) return;
  await cfgUrgentAction('urgent_toggle', 'cfg-urgent-toggle');
}

async function cfgUrgentReset() {
  const ok = await dashConfirm(
    '确认重置全部已通知群？\n\n将清除所有「已通知」记录，这些群下次新建游戏时会再次收到紧急公告。\n此操作不可撤销，也不会改变公告的启用状态。',
    {level: 'warn', okText: '重置'}
  );
  if (!ok) return;
  await cfgUrgentAction('urgent_reset', 'cfg-urgent-reset');
}

/* ──── 热重载 config.yaml(仅 yaml editor 旁的按钮) ──── */
async function cfgReloadConfig() {
  const ok = await dashConfirm(
    '确认按当前 config.yaml 热重载？\n\n' +
    '会立即把 yaml 里的运行时可调字段重新下发，**不重启插件、不重启引擎**。\n\n' +
    '注意：admin_uids 变更需重启 LGTBot 引擎才能生效。',
    {level: 'warn'}
  );
  if (!ok) return;
  const btn = document.getElementById('cfg-yaml-reload');
  const resEl = document.getElementById('cfg-yaml-reload-result');
  btn.disabled = true;
  setBtnIcon(btn, '#i-hourglass', '重载中……');
  resEl.innerHTML = '';
  try {
    const r = await fetch(apiUrl(CFG_KEYS.reload_config), { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const text = await r.text();
    const doc = new DOMParser().parseFromString(text, 'text/html');
    const el = doc.getElementById('result');
    if (!el) throw new Error('响应不含 #result');
    const data = JSON.parse(el.textContent);
    if (!data.success) {
      resEl.innerHTML = '<div class="dash-msg-err">❌ ' +
                        escapeHtml(data.message || '热重载失败') + '</div>';
      return;
    }
    const parts = [];
    parts.push('<div class="dash-msg-ok">' +
               escapeHtml(data.message || '已重载') + '</div>');
    if (data.changes && data.changes.length) {
      const items = data.changes.map(c =>
        '<li><code class="dash-mono">' + escapeHtml(c.field) + '</code>: ' +
        '<span class="dash-msg-info">' +
        escapeHtml(JSON.stringify(c.before)) + '</span> → ' +
        '<b>' + escapeHtml(JSON.stringify(c.after)) + '</b></li>'
      );
      parts.push('<ul class="dash-pluginconf-changes">' + items.join('') + '</ul>');
    } else {
      parts.push('<div class="dash-msg-info">（运行时参数与 yaml 一致，无变化）</div>');
    }
    if (data.admin_changed) {
      parts.push('<div class="dash-msg-info">📋 当前 admin_uids: ' +
                 (data.admin_count || 0) + ' 人</div>');
    }
    if (data.note) {
      parts.push('<div class="dash-msg-warn">⚠️ ' + escapeHtml(data.note) + '</div>');
    }
    resEl.innerHTML = parts.join('');
  } catch (e) {
    resEl.innerHTML = '<div class="dash-msg-err">❌ ' + escapeHtml(e.message) + '</div>';
  } finally {
    btn.disabled = false;
    setBtnIcon(btn, '#i-restart', '热重载配置');
  }
}

/* ──── 游戏开关 ────
 * 列表走 _threaded 路由(<pre id="result"> 片段),保存走 POST JSON,回带同一份状态:
 *   games    [{module, name, developer, description, error}]
 *   disabled 已保存的停用模块
 *   applied  运行中的引擎启动时实际停用的模块,引擎没在跑为 null
 * 开关只改本地草稿 draft,点「保存」才提交。 */
const CFG_GAMES_LIST_ROUTE = '/api/ext/lgtbot/games/list';
const CFG_GAMES_SAVE_ROUTE = '/api/ext/lgtbot/games/save';
const cfgGames = {games: [], saved: new Set(), applied: null, draft: new Set(), filter: 'all', msg: null};

function cfgGamesLabel(m) {
  const g = cfgGames.games.find(x => x.module === m);
  return g ? (g.name || g.module) : m;
}

function cfgGamesApply(data) {
  cfgGames.games = data.games || [];
  cfgGames.saved = new Set(data.disabled || []);
  cfgGames.applied = Array.isArray(data.applied) ? new Set(data.applied) : null;
  cfgGames.draft = new Set(cfgGames.saved);
}

function cfgGamesChanges() {
  return {
    disabled: [...cfgGames.draft].filter(m => !cfgGames.saved.has(m)),
    enabled: [...cfgGames.saved].filter(m => !cfgGames.draft.has(m)),
  };
}

/* 已保存、但运行中的引擎启动时没加载的游戏 —— 重启 LGTBot 后才从游戏列表里增删 */
function cfgGamesPending() {
  if (!cfgGames.applied) return new Set();
  return new Set(cfgGames.games.map(g => g.module)
    .filter(m => cfgGames.saved.has(m) !== cfgGames.applied.has(m)));
}

function cfgGamesShowMsg(text, kind) {
  const el = document.getElementById('cfg-games-msg');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'dash-config-msg dash-msg-' + (kind || 'info');
}

function cfgGamesStatus(pending) {
  const ch = cfgGamesChanges();
  if (ch.disabled.length || ch.enabled.length) return ['有未保存的修改', 'warn'];
  if (!cfgGames.games.length) return ['', 'info'];
  if (!cfgGames.applied) return ['引擎未运行，保存的名单在下次启动时生效', 'info'];
  if (pending.size) return [pending.size + ' 个游戏要重启 LGTBot 后才在游戏列表里更新（拦截已生效）', 'warn'];
  return ['', 'info'];
}

function cfgGamesRender() {
  const list = document.getElementById('cfg-games-list');
  if (!list) return;
  const q = (document.getElementById('cfg-games-search').value || '').trim().toLowerCase();
  const pending = cfgGamesPending();
  /* 没有待重启的游戏时「待重启」胶囊置灰不可点,停在这个筛选上退回「全部」 */
  if (cfgGames.filter === 'pending' && !pending.size) cfgGames.filter = 'all';
  const pendingChip = document.getElementById('cfg-games-chip-pending');
  if (pendingChip) {
    pendingChip.disabled = !pending.size;
    pendingChip.textContent = pending.size ? '待重启 ' + pending.size : '待重启';
  }
  document.querySelectorAll('#cfg-games-filter .cfg-games-chip')
    .forEach(c => c.classList.toggle('active', c.dataset.filter === cfgGames.filter));
  const shown = cfgGames.games.filter(g => {
    const off = cfgGames.draft.has(g.module);
    if ((cfgGames.filter === 'on' && off) || (cfgGames.filter === 'off' && !off)) return false;
    if (cfgGames.filter === 'pending' && !pending.has(g.module)) return false;
    return !q || g.module.toLowerCase().includes(q) || (g.name || '').toLowerCase().includes(q);
  });
  if (!cfgGames.games.length) {
    list.innerHTML = '<div class="cfg-games-empty">当前构建来源下没有游戏：尚未编译，或尚未安装预编译包</div>';
  } else if (!shown.length) {
    list.innerHTML = '<div class="cfg-games-empty">没有符合条件的游戏</div>';
  } else {
    list.innerHTML = shown.map(g => {
      const off = cfgGames.draft.has(g.module);
      const dirty = off !== cfgGames.saved.has(g.module);
      const tags = [];
      if (dirty) tags.push('<span class="dash-badge">未保存</span>');
      else if (pending.has(g.module)) tags.push('<span class="dash-badge dash-badge-warn">重启后生效</span>');
      if (g.error) tags.push('<span class="dash-badge dash-badge-err" title="' + escapeHtml(g.error) + '">信息读取失败</span>');
      const sub = [g.module, g.developer].filter(Boolean).join(' · ');
      return '<label class="cfg-game' + (off ? ' off' : '') + (dirty ? ' dirty' : '') +
             '" title="' + escapeHtml(g.description || '') + '">' +
             '<input type="checkbox" class="cfg-game-switch" data-module="' + escapeHtml(g.module) + '"' +
             (off ? '' : ' checked') + '>' +
             '<span class="cfg-game-text"><span class="cfg-game-name">' + escapeHtml(g.name || g.module) + '</span>' +
             '<span class="cfg-game-sub">' + escapeHtml(sub) + '</span></span>' +
             (tags.length ? '<span class="cfg-game-tags">' + tags.join('') + '</span>' : '') +
             '</label>';
    }).join('');
  }
  const count = document.getElementById('cfg-games-count');
  if (count) {
    const n = cfgGames.games.length;
    count.textContent = n ? (n - cfgGames.draft.size) + ' / ' + n : '—';
  }
  const ch = cfgGamesChanges();
  const save = document.getElementById('cfg-games-save');
  if (save && save.dataset.busyIcon === undefined) save.disabled = !(ch.disabled.length || ch.enabled.length);
  const all = document.getElementById('cfg-games-enable-all');
  if (all) all.disabled = !cfgGames.draft.size;
  if (cfgGames.msg) cfgGamesShowMsg(cfgGames.msg[0], cfgGames.msg[1]);
  else cfgGamesShowMsg(...cfgGamesStatus(pending));
}

async function cfgGamesLoad() {
  try {
    const r = await fetch(CFG_GAMES_LIST_ROUTE + TOKEN_QS, {cache: 'no-store'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
    const el = doc.getElementById('result');
    if (!el) throw new Error('响应不含 #result');
    const data = JSON.parse(el.textContent);
    if (!data.success) throw new Error(data.message || '读取失败');
    cfgGamesApply(data);
    cfgGamesRender();
  } catch (e) {
    const list = document.getElementById('cfg-games-list');
    if (list) list.innerHTML = '<div class="cfg-games-empty dash-msg-err">❌ 读取游戏列表失败：' +
                               escapeHtml(e.message) + '</div>';
  }
}

async function cfgGamesSave() {
  const ch = cfgGamesChanges();
  const lines = [];
  if (ch.disabled.length) lines.push('将停用：' + ch.disabled.map(cfgGamesLabel).join('、'));
  if (ch.enabled.length) lines.push('将恢复：' + ch.enabled.map(cfgGamesLabel).join('、'));
  const ok = await dashConfirm(
    '确认保存游戏开关？\n\n' + lines.join('\n') + '\n\n' +
    '停用的游戏保存后立即不能开局，进行中的对局不受影响。\n重启 LGTBot 后游戏列表与随机游戏才会更新。',
    {level: 'warn', okText: '保存'}
  );
  if (!ok) return;
  const btn = document.getElementById('cfg-games-save');
  setBtnBusy(btn, '保存中……');
  try {
    const r = await fetch(CFG_GAMES_SAVE_ROUTE + TOKEN_QS, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({disabled: [...cfgGames.draft]}),
    });
    const data = await r.json();
    if (data.success) {
      cfgGamesApply(data);
      cfgGames.msg = ['✅ ' + data.message, 'ok'];
    } else {
      cfgGames.msg = ['❌ 未保存 —— ' + (data.message || '保存失败'), 'err'];
    }
  } catch (e) {
    cfgGames.msg = ['❌ 请求失败：' + e.message, 'err'];
  } finally {
    clearBtnBusy(btn);
    cfgGamesRender();
  }
}

/* 草稿变动,刷新底部提示文字 */
function cfgGamesEdit(fn) {
  fn();
  cfgGames.msg = null;
  cfgGamesRender();
}

window.addEventListener('DOMContentLoaded', () => {
  try { if (localStorage.getItem(PCFG_MODE_KEY) === 'source') pcfg.mode = 'source'; } catch (e) { /* 读不到按默认可视化 */ }
  cfgLoadInline();
  pcfgShowMode();

  /* 给每个编辑器绑 dirty 跟踪 + 保存 / 恢复按钮 */
  Object.keys(cfgEditors).forEach(k => {
    const state = cfgEditors[k];
    const editor = document.getElementById(state.editorId);
    if (editor) {
      editor.addEventListener('input', () => {
        if (editor.value !== state.original) editor.dataset.dirty = '1';
        else delete editor.dataset.dirty;
      });
    }
    const saveBtn = document.getElementById(state.saveBtnId);
    if (saveBtn) saveBtn.addEventListener('click', () => cfgSave(k));
    const revBtn = document.getElementById(state.revertBtnId);
    if (revBtn) revBtn.addEventListener('click', () => cfgRevert(k));
  });

  /* 插件配置:表单 / 源文件各自记下「最后改的是哪边」,模式切换据此决定要不要互转 */
  const pcfgForm = document.getElementById('pcfg-form');
  if (pcfgForm) {
    const onForm = () => { pcfg.lastEdited = 'form'; pcfgRefresh(); };
    pcfgForm.addEventListener('input', onForm);
    pcfgForm.addEventListener('change', onForm);
    document.getElementById('cfg-yaml-editor').addEventListener('input', () => {
      pcfg.lastEdited = 'text';
      pcfgStatus();
    });
    document.getElementById('pcfg-mode').addEventListener('click', e => {
      const b = e.target.closest('.pcfg-mode-btn');
      if (b && !b.disabled) pcfgSetMode(b.dataset.mode);
    });
  }

  /* 热重载按钮(只在 yaml editor 旁) */
  const reloadBtn = document.getElementById('cfg-yaml-reload');
  if (reloadBtn) reloadBtn.addEventListener('click', cfgReloadConfig);

  /* 紧急公告的两个动作按钮(各自带确认弹窗) */
  const urgentToggle = document.getElementById('cfg-urgent-toggle');
  if (urgentToggle) urgentToggle.addEventListener('click', cfgUrgentToggle);
  const urgentReset = document.getElementById('cfg-urgent-reset');
  if (urgentReset) urgentReset.addEventListener('click', cfgUrgentReset);

  /* 游戏开关:开关 / 搜索 / 筛选只动本地草稿与显示,保存才提交 */
  const gamesList = document.getElementById('cfg-games-list');
  if (gamesList) {
    gamesList.addEventListener('change', e => {
      const cb = e.target.closest('.cfg-game-switch');
      if (!cb) return;
      const m = cb.dataset.module;
      cfgGamesEdit(() => cb.checked ? cfgGames.draft.delete(m) : cfgGames.draft.add(m));
    });
    document.getElementById('cfg-games-search').addEventListener('input', cfgGamesRender);
    document.getElementById('cfg-games-filter').addEventListener('click', e => {
      const chip = e.target.closest('.cfg-games-chip');
      if (!chip || chip.disabled) return;
      cfgGames.filter = chip.dataset.filter;
      cfgGamesRender();
    });
    document.getElementById('cfg-games-save').addEventListener('click', cfgGamesSave);
    document.getElementById('cfg-games-revert').addEventListener('click',
      () => cfgGamesEdit(() => { cfgGames.draft = new Set(cfgGames.saved); }));
    document.getElementById('cfg-games-enable-all').addEventListener('click',
      () => cfgGamesEdit(() => cfgGames.draft.clear()));
    cfgGamesLoad();
  }

  /* 「插件配置」的跳转按钮:滚到游戏开关卡片,落定后描边闪一下 */
  const jump = document.getElementById('cfg-games-jump');
  const gamesCard = document.getElementById('cfg-games');
  if (jump && gamesCard) {
    jump.addEventListener('click', () => {
      gamesCard.scrollIntoView({behavior: 'smooth', block: 'start'});
      gamesCard.classList.remove('cfg-games-flash');
      setTimeout(() => gamesCard.classList.add('cfg-games-flash'), 300);
    });
    gamesCard.addEventListener('animationend', () => gamesCard.classList.remove('cfg-games-flash'));
  }
});
