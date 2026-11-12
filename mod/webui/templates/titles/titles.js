/* ──── 头衔系统 tab ────
 * 读写都走真路由:保存 / 删除 / 刷新都回完整 payload,整块重绘。
 * 玩家在独立弹窗里查看与编辑,等级在顶部编辑框里改;草稿分别在 titlesEditing / titlesLevelEdit 里,重绘不丢。
 * 等级筛选同操作审计:titlesFilter 是 Set<等级键>,空集 = 全部,选中多个 = 拥有其中任一等级头衔的玩家。
 */

const TITLES_LIST_ROUTE = '__TITLES_LIST_ROUTE__';
const TITLES_LOOKUP_ROUTE = '__TITLES_LOOKUP_ROUTE__';
const TITLES_SAVE_ROUTE = '__TITLES_SAVE_ROUTE__';
const TITLES_DELETE_ROUTE = '__TITLES_DELETE_ROUTE__';
const TITLES_LEVELS_ROUTE = '__TITLES_LEVELS_ROUTE__';
const TITLES_OPENID_RE = /^[0-9A-Za-z]{32}$/;
const TITLES_HEX_RE = /^#?([0-9A-Fa-f]{6})$/;
/* 等级已不存在的头衔照常保留,灰色兜底(同后端 _UNKNOWN_LEVEL) */
const TITLES_UNKNOWN_LEVEL = {key: '', name: '未知', hex: '#808080'};

let titlesData = null;
const titlesFilter = new Set();
/* 弹窗里编辑中的玩家:{openid, name, avatar, isNew, rows: [{name, color}], eq: 装备的行下标(-1 不装备), dirty, msg};null = 弹窗关闭 */
let titlesEditing = null;
/* 编辑中的等级表:{rows: [{key, name, hex}], dirty, msg};key 为空的行是新增 */
let titlesLevelEdit = null;

function titlesLevel(key) {
  return ((titlesData && titlesData.levels) || []).find(lv => lv.key === key) || TITLES_UNKNOWN_LEVEL;
}

/* 选中的胶囊用等级色做实底,文字按亮度选深色或白色 */
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

/* 各等级被多少个头衔引用 —— 还有头衔在用的等级不能删 */
function titlesLevelUsage() {
  const usage = {};
  for (const p of (titlesData && titlesData.players) || []) {
    for (const t of p.titles) usage[t.color] = (usage[t.color] || 0) + 1;
  }
  return usage;
}

/* OpenID 是 32 位定长串,每 16 位一行 */
function titlesOpenidHtml(openid) {
  return (String(openid).match(/.{1,16}/g) || ['']).map(escapeHtml).join('<br>');
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

/* ──── 等级胶囊(筛选)与等级编辑 ──── */

function titlesRenderLevelChips() {
  const wrap = document.getElementById('titles-level-chips');
  const chips = ['<button type="button" class="titles-level-chip all' + (titlesFilter.size ? '' : ' active') + '" data-level="">全部</button>'];
  for (const lv of titlesData.levels) {
    chips.push('<button type="button" class="titles-level-chip' + (titlesFilter.has(lv.key) ? ' active' : '') + '" data-level="' + escapeHtml(lv.key) + '"' +
      ' style="--c:' + escapeHtml(lv.hex) + ';--on-c:' + titlesOnColor(lv.hex) + '" title="筛选拥有该等级头衔的玩家">' + escapeHtml(lv.name) + '</button>');
  }
  wrap.innerHTML = chips.join('');
}

function titlesRenderLevelEditor() {
  const box = document.getElementById('titles-level-editor');
  const bar = document.getElementById('titles-level-bar');
  const e = titlesLevelEdit;
  box.hidden = !e;
  bar.hidden = !!e;
  if (!e) {
    box.innerHTML = '';
    return;
  }
  const usage = titlesLevelUsage();
  const rows = e.rows.map((r, i) => {
    const hex = titlesNormHex(r.hex);
    const used = r.key ? (usage[r.key] || 0) : 0;
    const lock = used ? ' disabled title="被 ' + used + ' 个头衔使用，先给这些头衔换个等级"'
                      : (e.rows.length <= 1 ? ' disabled title="至少保留一个等级"' : '');
    return '<div class="titles-level-row" data-row="' + i + '">' +
      '<input type="color" class="titles-level-pick" value="' + (hex || '#808080').toLowerCase() + '" title="选择颜色">' +
      '<input type="text" class="titles-level-name" maxlength="' + titlesData.level_name_max + '" placeholder="等级名称" autocomplete="off" spellcheck="false"' +
        ' value="' + escapeHtml(r.name) + '"' + (hex ? ' style="color:' + hex + '"' : '') + '>' +
      '<input type="text" class="titles-level-hexinput" maxlength="7" placeholder="#RRGGBB" autocomplete="off" spellcheck="false" value="' + escapeHtml(r.hex) + '">' +
      '<span class="titles-level-usage">' + (r.key ? (used ? used + ' 个头衔' : '未使用') : '新增') + '</span>' +
      '<button class="dash-btn dash-btn-small" type="button" data-act="level-del"' + lock + '><svg class="ui-icon btn-icon"><use href="#i-x"/></svg><span class="btn-label">删除</span></button>' +
    '</div>';
  }).join('');
  const full = e.rows.length >= titlesData.max_levels;
  const msg = e.msg || ['', 'info'];
  box.innerHTML = '<div class="titles-level-rows">' + rows + '</div>' +
    '<div class="titles-edit-actions">' +
      '<button class="dash-btn dash-btn-small" type="button" data-act="level-add"' + (full ? ' disabled title="最多 ' + titlesData.max_levels + ' 个等级"' : '') + '>' +
        '<svg class="ui-icon btn-icon"><use href="#i-plus"/></svg><span class="btn-label">新增等级</span></button>' +
      '<span class="titles-edit-msg dash-msg-' + msg[1] + '">' + escapeHtml(msg[0]) + '</span>' +
      '<button class="dash-btn dash-btn-small" type="button" data-act="level-cancel"><svg class="ui-icon btn-icon"><use href="#i-x"/></svg><span class="btn-label">取消</span></button>' +
      '<button class="dash-btn dash-btn-small dash-btn-primary" type="button" data-act="level-save"><svg class="ui-icon btn-icon"><use href="#i-save"/></svg><span class="btn-label">保存</span></button>' +
    '</div>';
}

function titlesOpenLevelEditor(addRow) {
  if (!titlesLevelEdit) {
    titlesLevelEdit = {rows: titlesData.levels.map(lv => ({key: lv.key, name: lv.name, hex: lv.hex})), dirty: false, msg: null};
  }
  if (addRow && titlesLevelEdit.rows.length < titlesData.max_levels) {
    titlesLevelEdit.rows.push({key: '', name: '', hex: ''});
    titlesLevelEdit.dirty = true;
  }
  titlesRenderLevelEditor();
  if (addRow) {
    const names = document.querySelectorAll('#titles-level-editor .titles-level-name');
    if (names.length) names[names.length - 1].focus();
  }
}

async function titlesSaveLevels(btn) {
  const e = titlesLevelEdit;
  const rows = e.rows.map(r => ({key: r.key, name: r.name.trim(), hex: titlesNormHex(r.hex)}));
  if (rows.some(r => !r.name)) {
    titlesEditMsg('#titles-level-editor', e, '❌ 还有等级缺少名称', 'err');
    return;
  }
  if (rows.some(r => !r.hex)) {
    titlesEditMsg('#titles-level-editor', e, '❌ 颜色需为 #RRGGBB 格式的 HEX', 'err');
    return;
  }
  setBtnBusy(btn, '保存中……');
  try {
    const data = await titlesPost(TITLES_LEVELS_ROUTE, {levels: rows});
    if (data.success) {
      titlesLevelEdit = null;
      titlesApplyData(data);
      titlesShowMsg('✅ ' + data.message, 'ok');
    } else {
      titlesEditMsg('#titles-level-editor', e, '❌ ' + (data.message || '保存失败'), 'err');
    }
  } catch (err) {
    titlesEditMsg('#titles-level-editor', e, '❌ 请求失败：' + err.message, 'err');
  } finally {
    clearBtnBusy(btn);
  }
}

async function titlesOnLevelClick(ev) {
  const btn = ev.target.closest('[data-act]');
  const e = titlesLevelEdit;
  if (!btn || btn.disabled || !e) return;
  switch (btn.dataset.act) {
    case 'level-add':
      titlesOpenLevelEditor(true);
      break;
    case 'level-del':
      e.rows.splice(+btn.closest('.titles-level-row').dataset.row, 1);
      e.dirty = true;
      titlesRenderLevelEditor();
      break;
    case 'level-cancel':
      titlesLevelEdit = null;
      titlesRenderLevelEditor();
      break;
    case 'level-save':
      await titlesSaveLevels(btn);
      break;
  }
}

/* 名称 / HEX / 取色器三处输入互相同步,不重绘 */
function titlesOnLevelInput(ev) {
  const e = titlesLevelEdit;
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

function titlesOnChipClick(ev) {
  const chip = ev.target.closest('.titles-level-chip');
  if (!chip) return;
  const key = chip.dataset.level;
  if (!key) titlesFilter.clear();
  else if (titlesFilter.has(key)) titlesFilter.delete(key);
  else titlesFilter.add(key);
  titlesRenderLevelChips();
  titlesRender();
}

/* ──── 玩家列表 ──── */

function titlesChip(t, equipped, clickable) {
  const lv = titlesLevel(t.color);
  const tag = clickable ? 'button' : 'span';
  const attrs = clickable
    ? ' type="button" data-act="equip" data-name="' + escapeHtml(t.name) + '" title="' + (equipped ? '当前装备' : '设为当前装备') + '"'
    : '';
  return '<' + tag + ' class="titles-chip' + (equipped ? ' equipped' : '') + '" style="--c:' + escapeHtml(lv.hex) + '"' + attrs + '>' +
    (equipped ? '<svg class="ui-icon titles-chip-icon"><use href="#i-check"/></svg>' : '') +
    escapeHtml(t.name) + '<span class="titles-rarity">' + escapeHtml(lv.name) + '</span></' + tag + '>';
}

function titlesHead(p, ops, note) {
  const avatar = p.avatar
    ? '<img class="titles-avatar" src="' + escapeHtml(p.avatar) + '" alt="" loading="lazy" referrerpolicy="no-referrer">'
    : '<div class="titles-avatar"></div>';
  const name = p.name ? escapeHtml(p.name) : '<span class="titles-noname">昵称未知</span>';
  return '<div class="titles-card-head">' + avatar +
    '<div class="titles-who"><div class="titles-name">' + name + '</div>' +
    '<div class="titles-openid dash-mono">' + titlesOpenidHtml(p.openid) + '</div>' +
    (note ? '<div class="titles-head-note">' + note + '</div>' : '') + '</div>' + (ops || '') + '</div>';
}

function titlesCard(p) {
  const ops = '<div class="titles-ops">' +
    '<button class="dash-btn dash-btn-small" type="button" data-act="edit"><svg class="ui-icon btn-icon"><use href="#i-edit"/></svg><span class="btn-label">编辑</span></button>' +
    '<button class="dash-btn dash-btn-small dash-btn-danger" type="button" data-act="delete"><svg class="ui-icon btn-icon"><use href="#i-trash"/></svg><span class="btn-label">删除</span></button>' +
    '</div>';
  const chips = p.titles.map(t => titlesChip(t, t.name === p.equipped, true)).join('');
  return '<div class="titles-card" data-openid="' + escapeHtml(p.openid) + '">' + titlesHead(p, ops) +
    '<div class="titles-chips">' + chips + '</div>' +
    (p.equipped ? '' : '<div class="titles-hint">未装备头衔，新建 / 加入房间时不播报</div>') + '</div>';
}

function titlesRender() {
  const list = document.getElementById('titles-list');
  if (!list || !titlesData) return;
  const q = (document.getElementById('titles-search').value || '').trim().toLowerCase();
  const players = titlesData.players || [];
  const shown = players.filter(p => (!q || (p.name || '').toLowerCase().includes(q) || p.openid.toLowerCase().includes(q) ||
                                     p.titles.some(t => t.name.toLowerCase().includes(q))) &&
                                    (!titlesFilter.size || p.titles.some(t => titlesFilter.has(t.color))));
  list.innerHTML = shown.length ? shown.map(titlesCard).join('') : '<div class="titles-empty">' +
    (players.length ? '没有匹配的玩家' : '还没有玩家拥有头衔 —— 在上方填入玩家 OpenID 后点「添加玩家」') + '</div>';
}

function titlesApplyData(d) {
  titlesData = d;
  /* 被删掉的等级不再参与筛选 */
  for (const key of [...titlesFilter]) {
    if (!d.levels.some(lv => lv.key === key)) titlesFilter.delete(key);
  }
  document.getElementById('titles-path').textContent = d.path || '—';
  document.getElementById('titles-count').textContent = (d.players || []).length;
  document.getElementById('titles-name-max').textContent = d.name_max;
  titlesRenderLevelChips();
  titlesRenderLevelEditor();
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
  const levels = titlesData.levels || [];
  const options = sel => (levels.some(lv => lv.key === sel) ? '' : '<option value="" selected disabled>等级已不存在，请重新选择</option>') +
    levels.map(lv => '<option value="' + escapeHtml(lv.key) + '"' + (lv.key === sel ? ' selected' : '') + '>' +
      escapeHtml(lv.name + ' · ' + lv.hex) + '</option>').join('');
  const rows = e.rows.map((r, i) =>
    '<div class="titles-edit-row" data-row="' + i + '">' +
      '<span class="titles-edit-idx">' + (i + 1) + '</span>' +
      '<label class="titles-eq" title="装备这个头衔"><input type="radio" name="titles-eq" value="' + i + '"' + (e.eq === i ? ' checked' : '') + '><span>装备</span></label>' +
      '<input class="titles-edit-name" type="text" maxlength="' + titlesData.name_max + '" placeholder="头衔名" autocomplete="off" spellcheck="false"' +
        ' value="' + escapeHtml(r.name) + '" style="color:' + escapeHtml(titlesLevel(r.color).hex) + '">' +
      '<select class="titles-edit-color">' + options(r.color) + '</select>' +
      '<button class="dash-btn dash-btn-small" type="button" data-act="row-del" title="移除这个头衔"><svg class="ui-icon btn-icon"><use href="#i-x"/></svg><span class="btn-label">移除</span></button>' +
    '</div>').join('');
  /* 信息栏按已保存的数据显示,编辑中的草稿不算 */
  const saved = e.isNew ? null : titlesFindPlayer(e.openid);
  const eqTitle = saved && saved.titles.find(t => t.name === saved.equipped);
  const info = saved
    ? '<span class="titles-info-label">当前装备</span>' + (eqTitle ? titlesChip(eqTitle, true, false) : '<span class="titles-hint">未装备</span>') +
      '<span class="titles-info-label">头衔库</span><span>' + saved.titles.length + ' 个</span>'
    : '<span class="titles-hint">新玩家：添加头衔并保存后出现在玩家列表里</span>';
  const close = '<button class="titles-modal-close" type="button" data-act="cancel" title="关闭"><svg class="ui-icon"><use href="#i-x"/></svg></button>';
  const note = e.isNew && !e.name ? '用户数据库里不存在此 OpenID，请确认是否正确' : '';
  const full = e.rows.length >= titlesData.max_titles;
  const msg = e.msg || ['', 'info'];
  modal.innerHTML =
    '<div class="titles-modal-head">' + titlesHead(e, close, note) + '</div>' +
    '<div class="titles-modal-body">' +
      '<div class="titles-modal-info">' + info + '</div>' +
      '<div class="titles-edit-rows">' + (rows || '<div class="titles-hint">头衔库是空的，点下方「添加头衔」</div>') + '</div>' +
      (e.rows.length ? '<label class="titles-eq titles-eq-none"><input type="radio" name="titles-eq" value="-1"' + (e.eq < 0 ? ' checked' : '') + '><span>不装备头衔</span></label>' : '') +
    '</div>' +
    '<div class="titles-modal-foot titles-edit-actions">' +
      '<button class="dash-btn dash-btn-small" type="button" data-act="row-add"' + (full ? ' disabled title="每人最多 ' + titlesData.max_titles + ' 个头衔"' : '') + '>' +
        '<svg class="ui-icon btn-icon"><use href="#i-plus"/></svg><span class="btn-label">添加头衔</span></button>' +
      '<span class="titles-edit-msg dash-msg-' + msg[1] + '">' + escapeHtml(msg[0]) + '</span>' +
      '<button class="dash-btn dash-btn-small" type="button" data-act="cancel"><svg class="ui-icon btn-icon"><use href="#i-x"/></svg><span class="btn-label">取消</span></button>' +
      '<button class="dash-btn dash-btn-small dash-btn-primary" type="button" data-act="save"><svg class="ui-icon btn-icon"><use href="#i-save"/></svg><span class="btn-label">保存</span></button>' +
    '</div>';
}

function titlesFocusLastName() {
  const inputs = document.querySelectorAll('#titles-modal .titles-edit-name');
  if (inputs.length) inputs[inputs.length - 1].focus();
}

function titlesStartEdit(p, isNew) {
  titlesEditing = {
    openid: p.openid, name: p.name || '', avatar: p.avatar || '', isNew: !!isNew,
    rows: (p.titles || []).map(t => ({name: t.name, color: t.color})),
    eq: (p.titles || []).findIndex(t => t.name === p.equipped),
    dirty: false, msg: null,
  };
  titlesRenderModal();
  const first = document.querySelector('#titles-modal .titles-edit-name') || document.querySelector('#titles-modal [data-act=row-add]');
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
  const rows = e.rows.map(r => ({name: r.name.trim(), color: r.color}));
  if (rows.some(r => !r.name)) {
    titlesEditMsg('#titles-modal', e, '❌ 还有头衔没填名字', 'err');
    return;
  }
  if (!rows.length) {
    if (e.isNew) {
      titlesEditMsg('#titles-modal', e, '❌ 至少添加一个头衔', 'err');
      return;
    }
    if (!await dashConfirm('头衔库清空后会删除 ' + titlesWho(e) + ' 的头衔记录，确定保存？', {level: 'warn', okText: '保存'})) return;
  }
  setBtnBusy(btn, '保存中……');
  try {
    const data = await titlesPost(TITLES_SAVE_ROUTE, {openid: e.openid, titles: rows, equipped: e.eq >= 0 ? rows[e.eq].name : null});
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

async function titlesOnModalClick(ev) {
  const btn = ev.target.closest('[data-act]');
  const e = titlesEditing;
  if (!btn || btn.disabled || !e) return;
  switch (btn.dataset.act) {
    case 'row-add':
      if (e.rows.length >= titlesData.max_titles) return;
      e.rows.push({name: '', color: titlesData.levels[0].key});
      /* 头衔库从空到有时默认装备第一个 */
      if (e.eq < 0 && e.rows.length === 1) e.eq = 0;
      e.dirty = true;
      titlesRenderModal();
      titlesFocusLastName();
      break;
    case 'row-del': {
      const i = +btn.closest('.titles-edit-row').dataset.row;
      e.rows.splice(i, 1);
      if (e.eq === i) e.eq = -1;
      else if (e.eq > i) e.eq -= 1;
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
  if (t.classList.contains('titles-edit-color')) {
    const row = t.closest('.titles-edit-row');
    const r = e.rows[+row.dataset.row];
    r.color = t.value;
    row.querySelector('.titles-edit-name').style.color = titlesLevel(r.color).hex;
    e.dirty = true;
  } else if (t.name === 'titles-eq') {
    e.eq = +t.value;
    e.dirty = true;
  }
}

/* ──── 列表上的操作:打开编辑 / 快速装备 / 删除 / 添加玩家 / 刷新 ──── */

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

async function titlesAdd() {
  const input = document.getElementById('titles-add-openid');
  const openid = (input.value || '').trim();
  if (!TITLES_OPENID_RE.test(openid)) {
    titlesShowMsg('❌ OpenID 应为 32 位字母或数字', 'err');
    return;
  }
  const btn = document.getElementById('titles-add');
  setBtnBusy(btn, '查询中……');
  try {
    const r = await fetch(TITLES_LOOKUP_ROUTE + TOKEN_QS + (TOKEN_QS ? '&' : '?') + 'openid=' + encodeURIComponent(openid));
    const d = await r.json();
    if (!d.success) {
      titlesShowMsg('❌ ' + (d.message || '查询失败'), 'err');
      return;
    }
    input.value = '';
    titlesShowMsg('');
    /* 已有记录(可能是面板打开后才加的)先拉最新数据,再打开它的编辑弹窗,免得新建覆盖掉 */
    if (d.exists && !titlesFindPlayer(openid)) await titlesRefresh();
    const exist = titlesFindPlayer(openid);
    titlesStartEdit(exist || {openid, name: d.name, avatar: d.avatar, titles: [], equipped: null}, !exist);
  } catch (err) {
    titlesShowMsg('❌ 请求失败：' + err.message, 'err');
  } finally {
    clearBtnBusy(btn);
  }
}

async function titlesQuickEquip(openid, name, btn) {
  const p = titlesFindPlayer(openid);
  if (!p || p.equipped === name) return;
  btn.disabled = true;
  try {
    const data = await titlesPost(TITLES_SAVE_ROUTE, {openid, titles: p.titles, equipped: name});
    if (data.success) {
      titlesApplyData(data);
      titlesShowMsg('✅ ' + titlesWho(p) + '：已装备「' + name + '」', 'ok');
    } else {
      titlesShowMsg('❌ ' + (data.message || '切换失败'), 'err');
      btn.disabled = false;
    }
  } catch (err) {
    titlesShowMsg('❌ 请求失败：' + err.message, 'err');
    btn.disabled = false;
  }
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
    case 'equip':
      await titlesQuickEquip(openid, btn.dataset.name, btn);
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
    if (ev.key !== 'Escape' || !titlesEditing) return;
    if (!document.getElementById('dash-modal-backdrop').classList.contains('hidden')) return;
    titlesCloseEditor();
  }, true);
  const levelBox = document.getElementById('titles-level-editor');
  levelBox.addEventListener('click', titlesOnLevelClick);
  levelBox.addEventListener('input', titlesOnLevelInput);
  document.getElementById('titles-level-chips').addEventListener('click', titlesOnChipClick);
  document.getElementById('titles-level-add').addEventListener('click', () => titlesOpenLevelEditor(true));
  document.getElementById('titles-level-edit').addEventListener('click', () => titlesOpenLevelEditor(false));
  document.getElementById('titles-search').addEventListener('input', titlesRender);
  document.getElementById('titles-refresh').addEventListener('click', titlesRefresh);
  document.getElementById('titles-add').addEventListener('click', titlesAdd);
  document.getElementById('titles-add-openid').addEventListener('keydown', ev => {
    if (ev.key === 'Enter') titlesAdd();
  });
});
