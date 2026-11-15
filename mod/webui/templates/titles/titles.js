/* ──── 头衔系统 tab ────
 * 读写都走真路由:保存 / 删除 / 刷新都回完整 payload,整块重绘。
 * 颜色、分类、头衔库在顶部编辑框里改,玩家在独立弹窗里查看与编辑;草稿在 titlesPresetEdit / titlesLibEdit / titlesEditing 里,重绘不丢。
 * 筛选同操作审计:titlesFilter.color / .category 各是一个 Set<名字>,空集 = 不限;两者同时生效,作用于头衔库与玩家(拥有任一符合的头衔)。
 * 与头衔库同名的玩家头衔是库内头衔,其余是限定头衔(不进头衔库,只在玩家本人的头衔列表里出现)。排序一律先分类再颜色。
 */

const TITLES_LIST_ROUTE = '__TITLES_LIST_ROUTE__';
const TITLES_LOOKUP_ROUTE = '__TITLES_LOOKUP_ROUTE__';
const TITLES_SEARCH_ROUTE = '__TITLES_SEARCH_ROUTE__';
const TITLES_SAVE_ROUTE = '__TITLES_SAVE_ROUTE__';
const TITLES_DELETE_ROUTE = '__TITLES_DELETE_ROUTE__';
const TITLES_COLORS_ROUTE = '__TITLES_COLORS_ROUTE__';
const TITLES_CATEGORIES_ROUTE = '__TITLES_CATEGORIES_ROUTE__';
const TITLES_LIBRARY_ROUTE = '__TITLES_LIBRARY_ROUTE__';
const TITLES_OPENID_RE = /^[0-9A-Za-z]{32}$/;
const TITLES_HEX_RE = /^#?([0-9A-Fa-f]{6})$/;
/* 颜色 / 分类已不存在的头衔照常保留,灰色、「未分类」兜底(同后端) */
const TITLES_UNKNOWN_COLOR = {name: '未知', hex: '#808080'};
const TITLES_UNKNOWN_CATEGORY = '未分类';
/* 两套预设:field 是头衔里引用它的键,key 是 payload 里的列表 */
const TITLES_PRESETS = {
  color: {label: '颜色', field: 'color', key: 'colors', route: TITLES_COLORS_ROUTE, hex: true},
  category: {label: '分类', field: 'category', key: 'categories', route: TITLES_CATEGORIES_ROUTE, hex: false},
};

let titlesData = null;
const titlesFilter = {color: new Set(), category: new Set()};
let titlesLibOpen = false;
let titlesSearchResults = [];
/* 弹窗里编辑中的玩家:{openid, name, avatar, isNew, rows: [{name, color, category, lib}], eq: 装备的那一行(null 不装备), dirty, msg};null = 弹窗关闭 */
let titlesEditing = null;
/* 编辑中的颜色 / 分类表:{rows: [{orig, name, hex?}], dirty, msg};orig 是原来的名称,为空的行是新增;行序即排序 */
const titlesPresetEdit = {color: null, category: null};
/* 编辑中的头衔库:{rows: [{orig, name, color, category}], dirty, msg};orig 为空的行是新增 */
let titlesLibEdit = null;

function titlesColor(name) {
  return ((titlesData && titlesData.colors) || []).find(c => c.name === name) || TITLES_UNKNOWN_COLOR;
}

function titlesCategory(name) {
  return ((titlesData && titlesData.categories) || []).some(c => c.name === name) ? name : TITLES_UNKNOWN_CATEGORY;
}

/* 选中的胶囊用颜色做实底,文字按亮度选深色或白色 */
function titlesOnColor(hex) {
  const n = parseInt(String(hex).slice(1), 16);
  if (isNaN(n)) return '#fff';
  return (0.299 * (n >> 16 & 255) + 0.587 * (n >> 8 & 255) + 0.114 * (n & 255)) > 160 ? '#1f2433' : '#fff';
}

function titlesNormHex(value) {
  const m = TITLES_HEX_RE.exec(String(value || '').trim());
  return m ? '#' + m[1].toUpperCase() : '';
}

function titlesFindPlayer(openid) {
  return ((titlesData && titlesData.players) || []).find(p => p.openid === openid) || null;
}

function titlesWho(p) {
  return p.name || p.openid;
}

/* 排序键:先分类再颜色,都按预设顺序,已不存在的排最后(同后端) */
function titlesSortKey() {
  const ci = new Map(((titlesData && titlesData.colors) || []).map((c, i) => [c.name, i]));
  const ki = new Map(((titlesData && titlesData.categories) || []).map((c, i) => [c.name, i]));
  return t => [ki.has(t.category) ? ki.get(t.category) : ki.size, ci.has(t.color) ? ci.get(t.color) : ci.size];
}

function titlesBySortKey(list) {
  const key = titlesSortKey();
  return list.map((t, i) => [key(t), i, t]).sort((a, b) => a[0][0] - b[0][0] || a[0][1] - b[0][1] || a[1] - b[1]).map(x => x[2]);
}

/* 头衔库的名字 → 位置(后端已排序) */
function titlesLibIndex() {
  const idx = new Map();
  ((titlesData && titlesData.library) || []).forEach((t, i) => idx.set(t.name, i));
  return idx;
}

/* 展示顺序(同「头衔 我的」与切换序号):限定在前(先分类再颜色),库内按头衔库的顺序 */
function titlesDisplayOrder(list) {
  const idx = titlesLibIndex();
  return titlesBySortKey(list.filter(t => !idx.has(t.name))).concat(list.filter(t => idx.has(t.name)).sort((a, b) => idx.get(a.name) - idx.get(b.name)));
}

/* 编辑弹窗加行后重排:限定行保持在前,库内行按头衔库的顺序 */
function titlesSortRows(rows) {
  const idx = titlesLibIndex();
  const lib = rows.filter(r => r.lib).sort((a, b) => (idx.has(a.name) ? idx.get(a.name) : 1e9) - (idx.has(b.name) ? idx.get(b.name) : 1e9));
  rows.splice(0, rows.length, ...rows.filter(r => !r.lib), ...lib);
}

/* 颜色、分类两个筛选同时生效;空集 = 不限 */
function titlesMatches(t) {
  return (!titlesFilter.color.size || titlesFilter.color.has(t.color)) && (!titlesFilter.category.size || titlesFilter.category.has(t.category));
}

function titlesFiltering() {
  return titlesFilter.color.size > 0 || titlesFilter.category.size > 0;
}

/* 各颜色 / 分类被多少个头衔引用:头衔库每项一个 + 玩家的限定头衔(同后端),在用的不能删 */
function titlesUsage(field) {
  const usage = {};
  const idx = titlesLibIndex();
  for (const t of (titlesData && titlesData.library) || []) usage[t[field]] = (usage[t[field]] || 0) + 1;
  for (const p of (titlesData && titlesData.players) || []) {
    for (const t of p.titles) if (!idx.has(t.name)) usage[t[field]] = (usage[t[field]] || 0) + 1;
  }
  return usage;
}

/* 每个头衔有几位玩家持有 */
function titlesHolders() {
  const n = {};
  for (const p of (titlesData && titlesData.players) || []) for (const t of p.titles) n[t.name] = (n[t.name] || 0) + 1;
  return n;
}

/* 颜色下拉只写颜色名,选项文字色与左侧圆点给出颜色(--c 随选中项更新);分类下拉是普通下拉 */
function titlesSelect(kind, cls, sel) {
  const P = TITLES_PRESETS[kind];
  const list = (titlesData && titlesData[P.key]) || [];
  const opts = (list.some(p => p.name === sel) ? '' : '<option value="" selected disabled>' + P.label + '已不存在，请重新选择</option>') +
    list.map(p => '<option value="' + escapeHtml(p.name) + '"' + (P.hex ? ' style="color:' + escapeHtml(p.hex) + '"' : '') + (p.name === sel ? ' selected' : '') + '>' + escapeHtml(p.name) + '</option>').join('');
  return P.hex
    ? '<select class="' + cls + ' titles-level-select" style="--c:' + escapeHtml(titlesColor(sel).hex) + '">' + opts + '</select>'
    : '<select class="' + cls + ' titles-plain-select">' + opts + '</select>';
}

function titlesShowMsg(text, kind) {
  const el = document.getElementById('titles-msg');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'titles-msg dash-msg-' + (kind || 'info');
}

/* 编辑框里的提示就地改,不重绘(重绘会让输入框丢焦点) */
function titlesEditMsg(sel, state, text, kind) {
  if (state) state.msg = [text, kind];
  const el = document.querySelector(sel + ' .titles-edit-msg');
  if (!el) return;
  el.textContent = text;
  el.className = 'titles-edit-msg dash-msg-' + kind;
}

async function titlesPost(route, body) {
  const r = await fetch(route + TOKEN_QS, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  return r.json();
}

/* 胶囊只展示(按颜色上色,小字写分类);切换装备走编辑弹窗或玩家指令,避免误触 */
function titlesChip(t, opts) {
  const o = opts || {};
  return '<span class="titles-chip' + (o.equipped ? ' equipped' : '') + (o.custom ? ' custom' : '') + '" style="--c:' + escapeHtml(titlesColor(t.color).hex) + '"' +
    (o.custom ? ' title="限定头衔，不进头衔库，只在玩家本人的头衔列表里出现"' : '') + '>' +
    (o.equipped ? '<svg class="ui-icon titles-chip-icon"><use href="#i-check"/></svg>' : '') +
    escapeHtml(t.name) + '<span class="titles-rarity">' + escapeHtml(titlesCategory(t.category) + (o.extra ? ' · ' + o.extra : '')) + '</span></span>';
}

/* ──── 颜色 / 分类:筛选胶囊与编辑框 ──── */

function titlesRenderPresetChips(kind) {
  const P = TITLES_PRESETS[kind];
  const lib = titlesData.library || [];
  const count = {};
  for (const t of lib) count[t[P.field]] = (count[t[P.field]] || 0) + 1;
  const n = c => '<span class="titles-chip-n">' + c + '</span>';
  const f = titlesFilter[kind];
  const chips = ['<button type="button" class="titles-level-chip all' + (f.size ? '' : ' active') + '" data-value="" title="头衔库里的头衔数">全部' + n(lib.length) + '</button>'];
  for (const p of titlesData[P.key]) {
    const style = P.hex ? ' style="--c:' + escapeHtml(p.hex) + ';--on-c:' + titlesOnColor(p.hex) + '"' : '';
    chips.push('<button type="button" class="titles-level-chip' + (P.hex ? '' : ' plain') + (f.has(p.name) ? ' active' : '') + '" data-value="' + escapeHtml(p.name) + '"' + style +
      ' title="筛选该' + P.label + '的头衔与拥有它的玩家；数字是头衔库里的头衔数">' + escapeHtml(p.name) + n(count[p.name] || 0) + '</button>');
  }
  document.getElementById('titles-' + kind + '-chips').innerHTML = chips.join('');
}

function titlesRenderPresetEditor(kind) {
  const P = TITLES_PRESETS[kind];
  const box = document.getElementById('titles-' + kind + '-editor');
  const e = titlesPresetEdit[kind];
  box.hidden = !e;
  document.getElementById('titles-' + kind + '-bar').hidden = !!e;
  if (!e) {
    box.innerHTML = '';
    return;
  }
  const usage = titlesUsage(P.field);
  const last = e.rows.length - 1;
  const rows = e.rows.map((r, i) => {
    const hex = P.hex ? titlesNormHex(r.hex) : '';
    const used = r.orig ? (usage[r.orig] || 0) : 0;
    const lock = used ? ' disabled title="被 ' + used + ' 个头衔使用，先给这些头衔换一个' + P.label + '"'
                      : (e.rows.length <= 1 ? ' disabled title="至少保留一个' + P.label + '"' : '');
    return '<div class="titles-level-row" data-row="' + i + '">' +
      (P.hex ? '<input type="color" class="titles-level-pick" value="' + (hex || '#808080').toLowerCase() + '" title="选择颜色">' : '') +
      '<input type="text" class="titles-level-name" maxlength="' + titlesData.preset_name_max + '" placeholder="' + P.label + '名称" autocomplete="off" spellcheck="false"' +
        ' value="' + escapeHtml(r.name) + '"' + (hex ? ' style="color:' + hex + '"' : '') + '>' +
      (P.hex ? '<input type="text" class="titles-level-hexinput" maxlength="7" placeholder="#RRGGBB" autocomplete="off" spellcheck="false" value="' + escapeHtml(r.hex) + '">' : '') +
      '<span class="titles-level-usage">' + (r.orig ? (used ? used + ' 个头衔' : '未使用') : '新增') + '</span>' +
      '<button class="dash-btn dash-btn-small titles-level-move" type="button" data-act="preset-up" title="上移"' + (i ? '' : ' disabled') + '><svg class="ui-icon btn-icon"><use href="#i-arrow-up"/></svg></button>' +
      '<button class="dash-btn dash-btn-small titles-level-move" type="button" data-act="preset-down" title="下移"' + (i < last ? '' : ' disabled') + '><svg class="ui-icon btn-icon"><use href="#i-arrow-down"/></svg></button>' +
      '<button class="dash-btn dash-btn-small" type="button" data-act="preset-del"' + lock + '><svg class="ui-icon btn-icon"><use href="#i-x"/></svg><span class="btn-label">删除</span></button>' +
    '</div>';
  }).join('');
  const msg = e.msg || ['', 'info'];
  box.innerHTML = '<div class="titles-level-rows">' + rows + '</div>' +
    '<div class="titles-edit-actions">' +
      '<button class="dash-btn dash-btn-small" type="button" data-act="preset-add"><svg class="ui-icon btn-icon"><use href="#i-plus"/></svg><span class="btn-label">新增' + P.label + '</span></button>' +
      '<span class="titles-edit-msg dash-msg-' + msg[1] + '">' + escapeHtml(msg[0]) + '</span>' +
      '<button class="dash-btn dash-btn-small" type="button" data-act="preset-cancel"><svg class="ui-icon btn-icon"><use href="#i-x"/></svg><span class="btn-label">取消</span></button>' +
      '<button class="dash-btn dash-btn-small dash-btn-primary" type="button" data-act="preset-save"><svg class="ui-icon btn-icon"><use href="#i-save"/></svg><span class="btn-label">保存</span></button>' +
    '</div>';
}

function titlesOpenPresetEditor(kind, addRow) {
  const P = TITLES_PRESETS[kind];
  if (!titlesPresetEdit[kind]) {
    titlesPresetEdit[kind] = {rows: titlesData[P.key].map(p => P.hex ? {orig: p.name, name: p.name, hex: p.hex} : {orig: p.name, name: p.name}), dirty: false, msg: null};
  }
  if (addRow) {
    titlesPresetEdit[kind].rows.push(P.hex ? {orig: '', name: '', hex: ''} : {orig: '', name: ''});
    titlesPresetEdit[kind].dirty = true;
  }
  titlesRenderPresetEditor(kind);
  if (addRow) {
    const names = document.querySelectorAll('#titles-' + kind + '-editor .titles-level-name');
    if (names.length) names[names.length - 1].focus();
  }
}

async function titlesSavePreset(kind, btn) {
  const P = TITLES_PRESETS[kind];
  const sel = '#titles-' + kind + '-editor';
  const e = titlesPresetEdit[kind];
  const rows = e.rows.map(r => P.hex ? {orig: r.orig, name: r.name.trim(), hex: titlesNormHex(r.hex)} : {orig: r.orig, name: r.name.trim()});
  if (rows.some(r => !r.name)) {
    titlesEditMsg(sel, e, '❌ 还有' + P.label + '缺少名称', 'err');
    return;
  }
  if (P.hex && rows.some(r => !r.hex)) {
    titlesEditMsg(sel, e, '❌ 颜色需为 #RRGGBB 格式的 HEX', 'err');
    return;
  }
  setBtnBusy(btn, '保存中……');
  try {
    const data = await titlesPost(P.route, {[P.key]: rows});
    if (data.success) {
      titlesPresetEdit[kind] = null;
      titlesApplyData(data);
      titlesShowMsg('✅ ' + data.message, 'ok');
    } else {
      titlesEditMsg(sel, e, '❌ ' + (data.message || '保存失败'), 'err');
    }
  } catch (err) {
    titlesEditMsg(sel, e, '❌ 请求失败：' + err.message, 'err');
  } finally {
    clearBtnBusy(btn);
  }
}

async function titlesOnPresetClick(kind, ev) {
  const btn = ev.target.closest('[data-act]');
  const e = titlesPresetEdit[kind];
  if (!btn || btn.disabled || !e) return;
  switch (btn.dataset.act) {
    case 'preset-add':
      titlesOpenPresetEditor(kind, true);
      break;
    case 'preset-del':
      e.rows.splice(+btn.closest('.titles-level-row').dataset.row, 1);
      e.dirty = true;
      titlesRenderPresetEditor(kind);
      break;
    case 'preset-up':
    case 'preset-down': {
      const i = +btn.closest('.titles-level-row').dataset.row;
      const j = btn.dataset.act === 'preset-up' ? i - 1 : i + 1;
      [e.rows[i], e.rows[j]] = [e.rows[j], e.rows[i]];
      e.dirty = true;
      titlesRenderPresetEditor(kind);
      break;
    }
    case 'preset-cancel':
      titlesPresetEdit[kind] = null;
      titlesRenderPresetEditor(kind);
      break;
    case 'preset-save':
      await titlesSavePreset(kind, btn);
      break;
  }
}

/* 名称 / HEX / 取色器三处输入互相同步,不重绘 */
function titlesOnPresetInput(kind, ev) {
  const e = titlesPresetEdit[kind];
  const t = ev.target;
  const row = t.closest('.titles-level-row');
  if (!e || !row) return;
  const r = e.rows[+row.dataset.row];
  if (t.classList.contains('titles-level-name')) {
    r.name = t.value;
  } else if (t.classList.contains('titles-level-hexinput')) {
    r.hex = t.value;
    const hex = titlesNormHex(r.hex);
    if (hex) row.querySelector('.titles-level-pick').value = hex.toLowerCase();
    row.querySelector('.titles-level-name').style.color = hex;
  } else if (t.classList.contains('titles-level-pick')) {
    r.hex = t.value.toUpperCase();
    row.querySelector('.titles-level-hexinput').value = r.hex;
    row.querySelector('.titles-level-name').style.color = r.hex;
  } else {
    return;
  }
  e.dirty = true;
}

function titlesOnChipClick(kind, ev) {
  const chip = ev.target.closest('.titles-level-chip');
  if (!chip) return;
  const f = titlesFilter[kind];
  const v = chip.dataset.value;
  if (!v) f.clear();
  else if (f.has(v)) f.delete(v);
  else f.add(v);
  titlesRenderPresetChips(kind);
  titlesRenderLibrary();
  titlesRender();
}

/* ──── 头衔库:默认折叠的列表 + 编辑框 ──── */

function titlesRenderLibrary() {
  const lib = titlesData.library || [];
  document.getElementById('titles-lib-count').textContent = lib.length;
  const toggle = document.getElementById('titles-lib-toggle');
  toggle.setAttribute('aria-expanded', titlesLibOpen ? 'true' : 'false');
  toggle.classList.toggle('open', titlesLibOpen);
  const body = document.getElementById('titles-lib-body');
  body.hidden = !titlesLibOpen || !!titlesLibEdit;
  if (body.hidden) {
    body.innerHTML = '';
    return;
  }
  const holders = titlesHolders();
  const shown = lib.filter(titlesMatches);
  body.innerHTML = shown.length
    ? shown.map(t => titlesChip(t, {extra: (holders[t.name] || 0) + ' 人'})).join('')
    : '<div class="titles-hint">' + (lib.length ? '所选颜色 / 分类下还没有头衔，「限定」头衔仅属于玩家' : '头衔库是空的，点右侧「新增头衔」添加') + '</div>';
}

/* 草稿对比已保存的头衔库:没有原名的是新增,名字或颜色分类变了的是修改,原名不在草稿里的是删除(held 是其中有玩家持有的) */
function titlesLibChanges(e) {
  const lib = new Map(((titlesData && titlesData.library) || []).map(t => [t.name, t]));
  const holders = titlesHolders();
  const kept = new Set(e.rows.map(r => r.orig).filter(Boolean));
  const gone = [...lib.keys()].filter(n => !kept.has(n));
  const changed = r => { const t = lib.get(r.orig); return !t || r.orig !== r.name.trim() || t.color !== r.color || t.category !== r.category; };
  return {added: e.rows.filter(r => !r.orig).length, modified: e.rows.filter(r => r.orig && changed(r)).length, deleted: gone.length, held: gone.filter(n => holders[n]).length};
}

/* 有改动的几项 [样式, 文字],编辑状态与保存确认共用 */
function titlesLibChangeParts(c) {
  return [['add', '新增', c.added], ['mod', '修改', c.modified], ['del', '删除', c.deleted]].filter(x => x[2]).map(x => [x[0], x[1] + ' ' + x[2] + ' 项']);
}

/* 编辑状态就地改,不重绘(重绘会让输入框丢焦点);新增、修改、删除各自着色 */
function titlesUpdateLibStat() {
  const el = document.querySelector('#titles-lib-editor .titles-edit-stat');
  if (!el || !titlesLibEdit) return;
  const node = (cls, text) => {
    const s = document.createElement('span');
    s.className = cls;
    s.textContent = text;
    return s;
  };
  const parts = titlesLibChangeParts(titlesLibChanges(titlesLibEdit)).map(([cls, text]) => node(cls, text));
  el.replaceChildren(...(parts.length ? parts.flatMap((s, i) => i ? [node('sep', '·'), s] : [s]) : ['暂无修改']));
}

function titlesRenderLibEditor() {
  const box = document.getElementById('titles-lib-editor');
  const e = titlesLibEdit;
  box.hidden = !e;
  document.querySelector('.titles-lib-head .titles-level-actions').hidden = !!e;
  if (!e) {
    box.innerHTML = '';
    return;
  }
  const holders = titlesHolders();
  const rows = e.rows.map((r, i) => {
    const held = r.orig ? (holders[r.orig] || 0) : 0;
    return '<div class="titles-level-row titles-lib-row" data-row="' + i + '">' +
      '<input type="text" class="titles-lib-name" maxlength="' + titlesData.name_max + '" placeholder="头衔名" autocomplete="off" spellcheck="false"' +
        ' value="' + escapeHtml(r.name) + '" style="color:' + escapeHtml(titlesColor(r.color).hex) + '">' +
      titlesSelect('color', 'titles-lib-color', r.color) + titlesSelect('category', 'titles-lib-category', r.category) +
      '<span class="titles-level-usage">' + (r.orig ? (held ? held + ' 人持有' : '无人持有') : '新增') + '</span>' +
      '<button class="dash-btn dash-btn-small" type="button" data-act="lib-del"' + (held ? ' title="删除后持有者照常保留，变成他们的限定头衔"' : '') + '><svg class="ui-icon btn-icon"><use href="#i-x"/></svg><span class="btn-label">删除</span></button>' +
    '</div>';
  }).join('');
  const msg = e.msg || ['', 'info'];
  box.innerHTML = '<div class="titles-level-rows">' + (rows || '<div class="titles-hint">头衔库是空的，点下方「新增头衔」</div>') + '</div>' +
    '<div class="titles-edit-actions">' +
      '<button class="dash-btn dash-btn-small" type="button" data-act="lib-add"><svg class="ui-icon btn-icon"><use href="#i-plus"/></svg><span class="btn-label">新增头衔</span></button>' +
      '<span class="titles-edit-stat"></span>' +
      '<span class="titles-edit-msg dash-msg-' + msg[1] + '">' + escapeHtml(msg[0]) + '</span>' +
      '<button class="dash-btn dash-btn-small" type="button" data-act="lib-cancel"><svg class="ui-icon btn-icon"><use href="#i-x"/></svg><span class="btn-label">取消</span></button>' +
      '<button class="dash-btn dash-btn-small dash-btn-primary" type="button" data-act="lib-save"><svg class="ui-icon btn-icon"><use href="#i-save"/></svg><span class="btn-label">保存</span></button>' +
    '</div>';
  titlesUpdateLibStat();
}

function titlesOpenLibEditor(addRow) {
  if (!titlesLibEdit) {
    titlesLibEdit = {rows: (titlesData.library || []).map(t => ({orig: t.name, name: t.name, color: t.color, category: t.category})), dirty: false, msg: null};
  }
  if (addRow) {
    titlesLibEdit.rows.push({orig: '', name: '', color: (titlesData.colors[0] || TITLES_UNKNOWN_COLOR).name, category: (titlesData.categories[0] || {}).name || ''});
    titlesLibEdit.dirty = true;
  }
  titlesLibOpen = true;
  titlesRenderLibrary();
  titlesRenderLibEditor();
  if (addRow) {
    const names = document.querySelectorAll('#titles-lib-editor .titles-lib-name');
    if (names.length) names[names.length - 1].focus();
  }
}

async function titlesSaveLibrary(btn) {
  const e = titlesLibEdit;
  const rows = e.rows.map(r => ({orig: r.orig, name: r.name.trim(), color: r.color, category: r.category}));
  if (rows.some(r => !r.name)) {
    titlesEditMsg('#titles-lib-editor', e, '❌ 还有头衔没填名字', 'err');
    return;
  }
  if (rows.some(r => !r.color || !r.category)) {
    titlesEditMsg('#titles-lib-editor', e, '❌ 还有头衔没选颜色或分类', 'err');
    return;
  }
  const c = titlesLibChanges(e);
  const stat = titlesLibChangeParts(c).map(x => x[1]).join('，');
  const note = c.held ? '\n其中删除头衔有 ' + c.held + ' 个已有玩家持有，删除后仍会保留玩家头衔，变成「限定」头衔。' : '';
  if (stat && !await dashConfirm('确定保存对头衔库的修改？\n\n' + '变更：' + stat + '。' + note, {level: c.held ? 'warn' : 'info', okText: '保存'})) return;
  setBtnBusy(btn, '保存中……');
  try {
    const data = await titlesPost(TITLES_LIBRARY_ROUTE, {library: rows});
    if (data.success) {
      titlesLibEdit = null;
      titlesApplyData(data);
      titlesShowMsg('✅ ' + data.message, 'ok');
    } else {
      titlesEditMsg('#titles-lib-editor', e, '❌ ' + (data.message || '保存失败'), 'err');
    }
  } catch (err) {
    titlesEditMsg('#titles-lib-editor', e, '❌ 请求失败：' + err.message, 'err');
  } finally {
    clearBtnBusy(btn);
  }
}

async function titlesOnLibClick(ev) {
  const btn = ev.target.closest('[data-act]');
  const e = titlesLibEdit;
  if (!btn || btn.disabled || !e) return;
  switch (btn.dataset.act) {
    case 'lib-add':
      titlesOpenLibEditor(true);
      break;
    case 'lib-del':
      e.rows.splice(+btn.closest('.titles-level-row').dataset.row, 1);
      e.dirty = true;
      titlesRenderLibEditor();
      break;
    case 'lib-cancel':
      titlesLibEdit = null;
      titlesRenderLibEditor();
      titlesRenderLibrary();
      break;
    case 'lib-save':
      await titlesSaveLibrary(btn);
      break;
  }
}

function titlesOnLibInput(ev) {
  const e = titlesLibEdit;
  const row = ev.target.closest('.titles-level-row');
  if (!e || !row || !ev.target.classList.contains('titles-lib-name')) return;
  e.rows[+row.dataset.row].name = ev.target.value;
  e.dirty = true;
  titlesUpdateLibStat();
}

function titlesOnLibChange(ev) {
  const e = titlesLibEdit;
  const t = ev.target;
  const row = t.closest('.titles-level-row');
  if (!e || !row) return;
  const r = e.rows[+row.dataset.row];
  if (t.classList.contains('titles-lib-color')) {
    r.color = t.value;
    const hex = titlesColor(r.color).hex;
    row.querySelector('.titles-lib-name').style.color = hex;
    t.style.setProperty('--c', hex);
  } else if (t.classList.contains('titles-lib-category')) {
    r.category = t.value;
  } else {
    return;
  }
  e.dirty = true;
  titlesUpdateLibStat();
}

/* ──── 玩家列表 ──── */

/* 卡片只写昵称,点昵称浮出 OpenID;弹窗表头 OpenID 写在昵称下方 */
function titlesHead(p, ops, note, showId) {
  const avatar = p.avatar
    ? '<img class="titles-avatar" src="' + escapeHtml(p.avatar) + '" alt="" loading="lazy" referrerpolicy="no-referrer">'
    : '<div class="titles-avatar"></div>';
  const name = p.name ? escapeHtml(p.name) : '<span class="titles-noname">昵称未知</span>';
  return '<div class="titles-card-head">' + avatar +
    '<div class="titles-who">' +
    (showId ? '<div class="titles-name">' + name + '</div><div class="titles-openid dash-mono one-line">' + escapeHtml(p.openid) + '</div>'
            : '<div class="titles-name"><span class="titles-name-pop" title="点击查看 OpenID">' + name + '</span></div>') +
    (note ? '<div class="titles-head-note">' + note + '</div>' : '') + '</div>' + (ops || '') + '</div>';
}

/* OpenID 气泡贴着点击位置浮在上方,不锁整页 */
function titlesPopOpenid(ev, openid) {
  const el = document.getElementById('titles-pop');
  if (!el) return;
  el.textContent = openid || '';
  el.hidden = false;
  const left = Math.min(Math.max(8, ev.clientX - el.offsetWidth / 2), window.innerWidth - el.offsetWidth - 8);
  const above = ev.clientY - el.offsetHeight - 10;
  el.style.left = left + 'px';
  el.style.top = (above >= 8 ? above : ev.clientY + 16) + 'px';
}

function titlesCard(p) {
  /* 没装备头衔时编辑按钮橙色高亮:新建 / 加入房间时不会播报 */
  const edit = p.equipped ? '<button class="dash-btn dash-btn-small"'
                          : '<button class="dash-btn dash-btn-small dash-btn-warn" title="未装备头衔，新建 / 加入房间时不播报"';
  const ops = '<div class="titles-ops">' +
    edit + ' type="button" data-act="edit"><svg class="ui-icon btn-icon"><use href="#i-edit"/></svg><span class="btn-label">编辑</span></button>' +
    '<button class="dash-btn dash-btn-small dash-btn-danger" type="button" data-act="delete"><svg class="ui-icon btn-icon"><use href="#i-trash"/></svg><span class="btn-label">删除</span></button>' +
    '</div>';
  const idx = titlesLibIndex();
  const chips = titlesDisplayOrder(p.titles).map(t => titlesChip(t, {equipped: t.name === p.equipped, custom: !idx.has(t.name)})).join('');
  return '<div class="titles-card" data-openid="' + escapeHtml(p.openid) + '">' + titlesHead(p, ops) +
    '<div class="titles-chips">' + chips + '</div></div>';
}

function titlesRender() {
  const list = document.getElementById('titles-list');
  if (!list || !titlesData) return;
  const q = (document.getElementById('titles-search').value || '').trim().toLowerCase();
  const players = titlesData.players || [];
  const shown = players.filter(p => (!q || (p.name || '').toLowerCase().includes(q) || p.openid.toLowerCase().includes(q) ||
                                     p.titles.some(t => t.name.toLowerCase().includes(q))) &&
                                    (!titlesFiltering() || p.titles.some(titlesMatches)));
  list.innerHTML = shown.length ? shown.map(titlesCard).join('') : '<div class="titles-empty">' +
    (players.length ? '没有匹配的玩家' : '还没有玩家拥有头衔 —— 在上方输入玩家昵称或 OpenID 后点「添加玩家」') + '</div>';
}

function titlesApplyData(d) {
  titlesData = d;
  /* 被删掉或改了名的颜色 / 分类不再参与筛选 */
  for (const kind of Object.keys(TITLES_PRESETS)) {
    const names = new Set(d[TITLES_PRESETS[kind].key].map(p => p.name));
    for (const v of [...titlesFilter[kind]]) if (!names.has(v)) titlesFilter[kind].delete(v);
  }
  /* 头衔库改过后,弹窗里的行重新认一遍是不是库内头衔 */
  if (titlesEditing) {
    const lib = new Map((d.library || []).map(t => [t.name, t]));
    for (const r of titlesEditing.rows) {
      const t = lib.get(r.name.trim());
      r.lib = !!t;
      if (t) Object.assign(r, {color: t.color, category: t.category});
    }
    titlesSortRows(titlesEditing.rows);
  }
  document.getElementById('titles-path').textContent = d.path || '—';
  document.getElementById('titles-count').textContent = (d.players || []).length;
  document.getElementById('titles-name-max').textContent = d.name_max;
  for (const kind of Object.keys(TITLES_PRESETS)) {
    titlesRenderPresetChips(kind);
    titlesRenderPresetEditor(kind);
  }
  titlesRenderLibrary();
  titlesRenderLibEditor();
  titlesRender();
  titlesRenderModal();
}

/* ──── 玩家编辑弹窗 ──── */

function titlesRenderModal() {
  const backdrop = document.getElementById('titles-modal-backdrop');
  const modal = document.getElementById('titles-modal');
  const e = titlesEditing;
  backdrop.hidden = !e;
  if (!e) {
    modal.innerHTML = '';
    return;
  }
  /* 库内头衔的名字、颜色、分类跟随头衔库,只能移除或装备;限定头衔都可改 */
  const rows = e.rows.map((r, i) => {
    const c = titlesColor(r.color);
    const hex = escapeHtml(c.hex);
    const body = r.lib
      ? '<span class="titles-edit-static" style="color:' + hex + '">' + escapeHtml(r.name) + '</span>' +
        '<span class="titles-edit-lvl" style="--c:' + hex + '">' + escapeHtml(c.name + ' · ' + titlesCategory(r.category)) + '</span>'
      : '<input class="titles-edit-name" type="text" maxlength="' + titlesData.name_max + '" placeholder="限定头衔名" autocomplete="off" spellcheck="false"' +
          ' value="' + escapeHtml(r.name) + '" style="color:' + hex + '">' +
        titlesSelect('color', 'titles-edit-color', r.color) + titlesSelect('category', 'titles-edit-category', r.category) +
        '<span class="titles-tag-custom" title="不进头衔库，只在玩家本人的头衔列表里出现">限定</span>';
    return '<div class="titles-edit-row" data-row="' + i + '">' +
      '<span class="titles-edit-idx">' + (i + 1) + '</span>' +
      '<label class="titles-eq" title="装备这个头衔"><input type="radio" name="titles-eq" value="' + i + '"' + (e.eq === r ? ' checked' : '') + '><span>装备</span></label>' +
      body +
      '<button class="dash-btn dash-btn-small" type="button" data-act="row-del" title="移除这个头衔"><svg class="ui-icon btn-icon"><use href="#i-x"/></svg><span class="btn-label">移除</span></button>' +
    '</div>';
  }).join('');
  const owned = new Set(e.rows.map(r => r.name.trim()));
  const avail = (titlesData.library || []).filter(t => !owned.has(t.name));
  const picks = avail.length
    ? '<option value="">从头衔库选择…</option>' + avail.map(t => '<option value="' + escapeHtml(t.name) + '" style="color:' + escapeHtml(titlesColor(t.color).hex) + '">' +
        escapeHtml(t.name + ' · ' + titlesCategory(t.category)) + '</option>').join('')
    : '<option value="">' + ((titlesData.library || []).length ? '头衔库里的头衔都已拥有' : '头衔库是空的') + '</option>';
  const quick = '<div class="titles-quick-add">' +
    '<select class="titles-quick-pick titles-level-select"' + (avail.length ? '' : ' disabled') + '>' + picks + '</select>' +
    '<button class="dash-btn dash-btn-small" type="button" data-act="lib-pick"' + (avail.length ? '' : ' disabled') + '><svg class="ui-icon btn-icon"><use href="#i-plus"/></svg><span class="btn-label">添加</span></button>' +
    '<button class="dash-btn dash-btn-small" type="button" data-act="row-add"><svg class="ui-icon btn-icon"><use href="#i-edit"/></svg><span class="btn-label">新增限定头衔</span></button>' +
  '</div>';
  /* 信息栏按已保存的数据显示,编辑中的草稿不算 */
  const saved = e.isNew ? null : titlesFindPlayer(e.openid);
  const eqTitle = saved && saved.titles.find(t => t.name === saved.equipped);
  const info = saved
    ? '<span class="titles-info-label">当前装备</span>' + (eqTitle ? titlesChip(eqTitle, {equipped: true, custom: !titlesLibIndex().has(eqTitle.name)}) : '<span class="titles-hint">未装备</span>') +
      '<span class="titles-info-label">持有</span><span>' + saved.titles.length + ' 个</span>'
    : '<span class="titles-hint">新玩家：添加头衔并保存后出现在玩家列表里</span>';
  const close = '<button class="titles-modal-close" type="button" data-act="cancel" title="关闭"><svg class="ui-icon"><use href="#i-x"/></svg></button>';
  const note = e.isNew && !e.name ? '用户数据库里不存在此 OpenID，请确认是否正确' : '';
  const msg = e.msg || ['', 'info'];
  modal.innerHTML =
    '<div class="titles-modal-head">' + titlesHead(e, close, note, true) + '</div>' +
    '<div class="titles-modal-body">' +
      '<div class="titles-modal-info">' + info + '</div>' +
      '<div class="titles-edit-rows">' + (rows || '<div class="titles-hint">还没有头衔，可从头衔库添加，或添加限定头衔</div>') + '</div>' +
      (e.rows.length ? '<label class="titles-eq titles-eq-none"><input type="radio" name="titles-eq" value="-1"' + (e.eq ? '' : ' checked') + '><span>不装备头衔</span></label>' : '') +
      quick +
    '</div>' +
    '<div class="titles-modal-foot titles-edit-actions">' +
      '<span class="titles-edit-msg dash-msg-' + msg[1] + '">' + escapeHtml(msg[0]) + '</span>' +
      '<button class="dash-btn dash-btn-small" type="button" data-act="cancel"><svg class="ui-icon btn-icon"><use href="#i-x"/></svg><span class="btn-label">取消</span></button>' +
      '<button class="dash-btn dash-btn-small dash-btn-primary" type="button" data-act="save"><svg class="ui-icon btn-icon"><use href="#i-save"/></svg><span class="btn-label">保存</span></button>' +
    '</div>';
}

function titlesStartEdit(p, isNew) {
  const idx = titlesLibIndex();
  const rows = titlesDisplayOrder(p.titles || []).map(t => ({name: t.name, color: t.color, category: t.category, lib: idx.has(t.name)}));
  titlesEditing = {
    openid: p.openid, name: p.name || '', avatar: p.avatar || '', isNew: !!isNew,
    rows, eq: rows.find(r => r.name === p.equipped) || null,
    dirty: false, msg: null,
  };
  titlesRenderModal();
  const first = document.querySelector('#titles-modal .titles-quick-pick:not([disabled])') || document.querySelector('#titles-modal [data-act=row-add]');
  if (first) first.focus();
}

async function titlesCloseEditor() {
  if (titlesEditing && titlesEditing.dirty &&
      !await dashConfirm('正在编辑的头衔还没有保存，确定放弃修改？', {level: 'warn', okText: '放弃修改'})) return;
  titlesEditing = null;
  titlesRenderModal();
}

async function titlesSave(btn) {
  const e = titlesEditing;
  const rows = e.rows.map(r => ({name: r.name.trim(), color: r.color, category: r.category}));
  if (rows.some(r => !r.name)) {
    titlesEditMsg('#titles-modal', e, '❌ 还有头衔没填名字', 'err');
    return;
  }
  if (!rows.length) {
    if (e.isNew) {
      titlesEditMsg('#titles-modal', e, '❌ 至少添加一个头衔', 'err');
      return;
    }
    if (!await dashConfirm('头衔清空后会删除 ' + titlesWho(e) + ' 的头衔记录，确定保存？', {level: 'warn', okText: '保存'})) return;
  }
  setBtnBusy(btn, '保存中……');
  try {
    const data = await titlesPost(TITLES_SAVE_ROUTE, {openid: e.openid, titles: rows, equipped: e.eq ? e.eq.name.trim() : null});
    if (data.success) {
      titlesEditing = null;
      titlesApplyData(data);
      titlesShowMsg('✅ ' + titlesWho(e) + '：' + data.message, 'ok');
    } else {
      titlesEditMsg('#titles-modal', e, '❌ ' + (data.message || '保存失败'), 'err');
    }
  } catch (err) {
    titlesEditMsg('#titles-modal', e, '❌ 请求失败：' + err.message, 'err');
  } finally {
    clearBtnBusy(btn);
  }
}

/* 加一行后重排并重绘;头衔从无到有时默认装备这一个 */
function titlesAddRow(row) {
  const e = titlesEditing;
  e.rows.push(row);
  titlesSortRows(e.rows);
  if (!e.eq && e.rows.length === 1) e.eq = row;
  e.dirty = true;
  titlesRenderModal();
}

async function titlesOnModalClick(ev) {
  const btn = ev.target.closest('[data-act]');
  const e = titlesEditing;
  if (!btn || btn.disabled || !e) return;
  switch (btn.dataset.act) {
    case 'lib-pick': {
      const name = document.querySelector('#titles-modal .titles-quick-pick').value;
      const t = (titlesData.library || []).find(x => x.name === name);
      if (!t) {
        titlesEditMsg('#titles-modal', e, '❌ 请先从头衔库选择一个头衔', 'err');
        return;
      }
      titlesAddRow({name: t.name, color: t.color, category: t.category, lib: true});
      break;
    }
    case 'row-add': {
      const row = {name: '', color: (titlesData.colors[0] || TITLES_UNKNOWN_COLOR).name, category: (titlesData.categories[0] || {}).name || '', lib: false};
      titlesAddRow(row);
      const input = document.querySelector('#titles-modal [data-row="' + e.rows.indexOf(row) + '"] .titles-edit-name');
      if (input) input.focus();
      break;
    }
    case 'row-del': {
      const r = e.rows.splice(+btn.closest('.titles-edit-row').dataset.row, 1)[0];
      if (e.eq === r) e.eq = null;
      e.dirty = true;
      titlesRenderModal();
      break;
    }
    case 'cancel':
      await titlesCloseEditor();
      break;
    case 'save':
      await titlesSave(btn);
      break;
  }
}

function titlesOnModalInput(ev) {
  const e = titlesEditing;
  const row = ev.target.closest('.titles-edit-row');
  if (!e || !row || !ev.target.classList.contains('titles-edit-name')) return;
  e.rows[+row.dataset.row].name = ev.target.value;
  e.dirty = true;
}

function titlesOnModalChange(ev) {
  const e = titlesEditing;
  const t = ev.target;
  if (!e) return;
  const row = t.closest('.titles-edit-row');
  if (t.classList.contains('titles-edit-color')) {
    const r = e.rows[+row.dataset.row];
    r.color = t.value;
    const hex = titlesColor(r.color).hex;
    row.querySelector('.titles-edit-name').style.color = hex;
    t.style.setProperty('--c', hex);
    e.dirty = true;
  } else if (t.classList.contains('titles-edit-category')) {
    e.rows[+row.dataset.row].category = t.value;
    e.dirty = true;
  } else if (t.classList.contains('titles-quick-pick')) {
    const pick = (titlesData.library || []).find(x => x.name === t.value);
    t.style.setProperty('--c', pick ? titlesColor(pick.color).hex : '');
  } else if (t.name === 'titles-eq') {
    e.eq = +t.value >= 0 ? e.rows[+t.value] : null;
    e.dirty = true;
  }
}

/* ──── 列表上的操作:打开编辑 / 删除 / 添加玩家(OpenID 或昵称) / 刷新 ──── */

async function titlesRefresh() {
  const btn = document.getElementById('titles-refresh');
  titlesShowMsg('');
  setBtnBusy(btn, '刷新中……');
  try {
    const r = await fetch(TITLES_LIST_ROUTE + TOKEN_QS, {cache: 'no-store'});
    const data = await r.json();
    if (data.success) titlesApplyData(data);
    else titlesShowMsg('❌ 刷新失败', 'err');
  } catch (err) {
    titlesShowMsg('❌ 请求失败：' + err.message, 'err');
  } finally {
    clearBtnBusy(btn);
  }
}

function titlesHideResults() {
  const box = document.getElementById('titles-add-results');
  box.hidden = true;
  box.innerHTML = '';
  titlesSearchResults = [];
}

/* 已有记录(可能是面板打开后才加的)先拉最新数据,再打开它的编辑弹窗,免得新建覆盖掉 */
async function titlesOpenPlayer(openid, name, avatar, exists) {
  document.getElementById('titles-add-openid').value = '';
  titlesShowMsg('');
  if (exists && !titlesFindPlayer(openid)) await titlesRefresh();
  const exist = titlesFindPlayer(openid);
  titlesStartEdit(exist || {openid, name, avatar, titles: [], equipped: null}, !exist);
}

function titlesRenderResults() {
  const box = document.getElementById('titles-add-results');
  box.innerHTML = titlesSearchResults.map((u, i) =>
    '<button type="button" class="titles-add-result" data-idx="' + i + '">' +
      (u.avatar ? '<img class="titles-avatar" src="' + escapeHtml(u.avatar) + '" alt="" loading="lazy" referrerpolicy="no-referrer">' : '<div class="titles-avatar"></div>') +
      '<span class="titles-who"><span class="titles-name">' + escapeHtml(u.name || '昵称未知') + '</span><span class="titles-openid dash-mono">' + escapeHtml(u.openid) + '</span></span>' +
      (u.exists ? '<span class="titles-tag-exists">已有头衔</span>' : '') +
    '</button>').join('');
  box.hidden = false;
}

async function titlesAdd() {
  const value = (document.getElementById('titles-add-openid').value || '').trim();
  titlesHideResults();
  if (!value) {
    titlesShowMsg('❌ 请输入玩家昵称或 OpenID', 'err');
    return;
  }
  const btn = document.getElementById('titles-add');
  setBtnBusy(btn, '查询中……');
  try {
    if (TITLES_OPENID_RE.test(value)) {
      const r = await fetch(TITLES_LOOKUP_ROUTE + TOKEN_QS + (TOKEN_QS ? '&' : '?') + 'openid=' + encodeURIComponent(value));
      const d = await r.json();
      if (!d.success) titlesShowMsg('❌ ' + (d.message || '查询失败'), 'err');
      else await titlesOpenPlayer(d.openid, d.name, d.avatar, d.exists);
      return;
    }
    const r = await fetch(TITLES_SEARCH_ROUTE + TOKEN_QS + (TOKEN_QS ? '&' : '?') + 'q=' + encodeURIComponent(value));
    const d = await r.json();
    if (!d.success) {
      titlesShowMsg('❌ ' + (d.message || '搜索失败'), 'err');
    } else if (!d.results.length) {
      titlesShowMsg('❌ 没有找到昵称匹配的玩家', 'err');
    } else {
      titlesShowMsg('');
      titlesSearchResults = d.results;
      titlesRenderResults();
    }
  } catch (err) {
    titlesShowMsg('❌ 请求失败：' + err.message, 'err');
  } finally {
    clearBtnBusy(btn);
  }
}

async function titlesOnResultClick(ev) {
  const item = ev.target.closest('.titles-add-result');
  if (!item) return;
  const u = titlesSearchResults[+item.dataset.idx];
  titlesHideResults();
  if (u) await titlesOpenPlayer(u.openid, u.name, u.avatar, u.exists);
}

async function titlesDelete(openid, btn) {
  const p = titlesFindPlayer(openid);
  if (!p) return;
  const ok = await dashConfirm('确定删除 ' + titlesWho(p) + ' 的头衔记录？\n\n共 ' + p.titles.length + ' 个头衔，删除后玩家将失去全部头衔。',
                               {level: 'danger', okText: '删除'});
  if (!ok) return;
  setBtnBusy(btn, '删除中……');
  try {
    const data = await titlesPost(TITLES_DELETE_ROUTE, {openid});
    if (data.success) {
      titlesApplyData(data);
      titlesShowMsg('✅ 已删除 ' + titlesWho(p) + ' 的头衔记录', 'ok');
    } else {
      titlesShowMsg('❌ ' + (data.message || '删除失败'), 'err');
    }
  } catch (err) {
    titlesShowMsg('❌ 请求失败：' + err.message, 'err');
  } finally {
    clearBtnBusy(btn);
  }
}

async function titlesOnListClick(ev) {
  const who = ev.target.closest('.titles-name-pop');
  if (who) {
    titlesPopOpenid(ev, who.closest('.titles-card').dataset.openid);
    return;
  }
  const btn = ev.target.closest('[data-act]');
  if (!btn || btn.disabled) return;
  const openid = btn.closest('.titles-card').dataset.openid;
  switch (btn.dataset.act) {
    case 'edit':
      titlesStartEdit(titlesFindPlayer(openid));
      break;
    case 'delete':
      await titlesDelete(openid, btn);
      break;
  }
}

function titlesLoadInline() {
  try {
    titlesApplyData(JSON.parse(document.getElementById('titles-data').textContent));
  } catch (e) {
    console.warn('[titles] load failed:', e);
  }
}

window.addEventListener('DOMContentLoaded', () => {
  titlesLoadInline();
  const list = document.getElementById('titles-list');
  if (!list) return;
  list.addEventListener('click', titlesOnListClick);
  const modal = document.getElementById('titles-modal');
  modal.addEventListener('click', titlesOnModalClick);
  modal.addEventListener('input', titlesOnModalInput);
  modal.addEventListener('change', titlesOnModalChange);
  const backdrop = document.getElementById('titles-modal-backdrop');
  /* 点弹窗外的遮罩 = 关闭(有未保存的修改先确认) */
  backdrop.addEventListener('mousedown', ev => {
    if (ev.target === backdrop) titlesCloseEditor();
  });
  /* Esc 关闭;公共确认框开着时归它处理。捕获阶段注册:main.js 的冒泡监听会先把确认框关掉,晚了就判断不出来 */
  document.addEventListener('keydown', ev => {
    if (ev.key !== 'Escape') return;
    document.getElementById('titles-pop').hidden = true;
    if (!document.getElementById('titles-add-results').hidden) titlesHideResults();
    if (!titlesEditing) return;
    if (!document.getElementById('dash-modal-backdrop').classList.contains('hidden')) return;
    titlesCloseEditor();
  }, true);
  for (const kind of Object.keys(TITLES_PRESETS)) {
    const box = document.getElementById('titles-' + kind + '-editor');
    box.addEventListener('click', ev => titlesOnPresetClick(kind, ev));
    box.addEventListener('input', ev => titlesOnPresetInput(kind, ev));
    document.getElementById('titles-' + kind + '-chips').addEventListener('click', ev => titlesOnChipClick(kind, ev));
    document.getElementById('titles-' + kind + '-add').addEventListener('click', () => titlesOpenPresetEditor(kind, true));
    document.getElementById('titles-' + kind + '-edit').addEventListener('click', () => titlesOpenPresetEditor(kind, false));
  }
  const libBox = document.getElementById('titles-lib-editor');
  libBox.addEventListener('click', titlesOnLibClick);
  libBox.addEventListener('input', titlesOnLibInput);
  libBox.addEventListener('change', titlesOnLibChange);
  document.getElementById('titles-lib-toggle').addEventListener('click', () => {
    titlesLibOpen = !titlesLibOpen;
    titlesRenderLibrary();
  });
  document.getElementById('titles-lib-add').addEventListener('click', () => titlesOpenLibEditor(true));
  document.getElementById('titles-lib-edit').addEventListener('click', () => titlesOpenLibEditor(false));
  document.getElementById('titles-search').addEventListener('input', titlesRender);
  document.getElementById('titles-refresh').addEventListener('click', titlesRefresh);
  document.getElementById('titles-add').addEventListener('click', titlesAdd);
  const addInput = document.getElementById('titles-add-openid');
  addInput.addEventListener('keydown', ev => {
    if (ev.key === 'Enter') titlesAdd();
  });
  addInput.addEventListener('input', titlesHideResults);
  document.getElementById('titles-add-results').addEventListener('click', titlesOnResultClick);
  /* 点搜索结果以外的地方收起结果 */
  document.addEventListener('mousedown', ev => {
    if (!ev.target.closest('.titles-add-wrap')) titlesHideResults();
  });
  /* 捕获阶段注册,别处 stopPropagation 也挡不住收起;点昵称或气泡本身不收,方便选中复制 */
  const pop = document.getElementById('titles-pop');
  document.addEventListener('click', ev => {
    if (!ev.target.closest('.titles-name-pop, #titles-pop')) pop.hidden = true;
  }, true);
  window.addEventListener('scroll', () => { pop.hidden = true; }, true);
});
