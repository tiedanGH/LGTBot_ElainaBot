/* ──── 配置管理 tab ────
 * 四块编辑器(yaml / notice / trouble / engine)共享同一套读 / dirty 跟踪 /
 * 保存 / revert 逻辑,通过 cfgEditors 表驱动避免重复代码。保存全部走主框架
 * /api/config-file/save(yaml/json/text format)。
 * 热重载按钮调 __lgtbot_dash_reload_config(从原 dashboard 搬迁,key 不变)。
 */

const CFG_KEYS = {
  reload_config: '__lgtbot_dash_reload_config',
};

/* 每个编辑器一份状态:
 *   absPath  —— 保存请求要原样回传给 /api/config-file/save 的绝对路径
 *   original —— 最近一次从后端拉到的内容,用来做 dirty 检测 + 「↩ 恢复」
 *   format   —— 主框架保存端点的格式 hint(yaml / json / text)
 *   editorId / pathId / msgId / saveBtnId —— 对应 DOM 元素 id */
const cfgEditors = {
  yaml: {
    absPath: '', original: '', format: 'yaml',
    editorId: 'cfg-yaml-editor', pathId: 'cfg-yaml-path',
    msgId: 'cfg-yaml-msg', saveBtnId: 'cfg-yaml-save', revertBtnId: 'cfg-yaml-revert',
    saveHint: '，请点「🔁 热重载配置」即时下发到运行时',
  },
  notice: {
    absPath: '', original: '', format: 'text',
    editorId: 'cfg-notice-editor', pathId: 'cfg-notice-path',
    msgId: 'cfg-notice-msg', saveBtnId: 'cfg-notice-save', revertBtnId: 'cfg-notice-revert',
    saveHint: '，下次发送「更新公告」指令时即生效',
  },
  trouble: {
    absPath: '', original: '', format: 'text',
    editorId: 'cfg-trouble-editor', pathId: 'cfg-trouble-path',
    msgId: 'cfg-trouble-msg', saveBtnId: 'cfg-trouble-save', revertBtnId: 'cfg-trouble-revert',
    saveHint: '，下次发送「疑难解答」指令时即生效',
  },
  engine: {
    absPath: '', original: '', format: 'json',
    editorId: 'cfg-engine-editor', pathId: 'cfg-engine-path',
    msgId: 'cfg-engine-msg', saveBtnId: 'cfg-engine-save', revertBtnId: 'cfg-engine-revert',
    saveHint: '，需重启 LGTBot 引擎或整进程才能生效',
  },
};

function cfgApplyData(data) {
  const map = {
    yaml: data.config_yaml,
    notice: data.update_notice,
    trouble: data.troubleshooting,
    engine: data.engine_config,
  };
  Object.entries(map).forEach(([k, info]) => {
    if (!info) return;
    const state = cfgEditors[k];
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
  const state = cfgEditors[key];
  const editor = document.getElementById(state.editorId);
  if (!editor) return;
  const text = editor.value;
  /* json / yaml 前端浅校验:json 用 JSON.parse,yaml 没有内置 parser 让主框架兜底。
     text(更新公告 / 疑难解答)无格式约束,直接保存。 */
  if (state.format === 'json') {
    try { JSON.parse(text); }
    catch (e) { cfgShowMsg(key, 'JSON 格式错误：' + e.message, 'err'); return; }
  }
  if (!state.absPath) {
    cfgShowMsg(key, '文件路径未知，无法保存', 'err');
    return;
  }
  const btn = document.getElementById(state.saveBtnId);
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/config-file/save' + TOKEN_QS, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        path: state.absPath,
        content: text,
        format: state.format,
      }),
    });
    const data = await r.json();
    if (data.success) {
      cfgShowMsg(key, '✅ 已保存' + state.saveHint, 'ok');
      state.original = text;
      delete editor.dataset.dirty;
    } else {
      cfgShowMsg(key, '❌ ' + (data.message || '保存失败'), 'err');
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
  cfgShowMsg(key, '已恢复至上次加载内容', 'info');
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
  btn.textContent = '⏳ 重载中……';
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
    parts.push('<div class="dash-msg-info">📋 当前 admin_uids: ' +
               (data.admin_count || 0) + ' 人</div>');
    if (data.note) {
      parts.push('<div class="dash-msg-warn">⚠️ ' + escapeHtml(data.note) + '</div>');
    }
    resEl.innerHTML = parts.join('');
  } catch (e) {
    resEl.innerHTML = '<div class="dash-msg-err">❌ ' + escapeHtml(e.message) + '</div>';
  } finally {
    btn.disabled = false;
    btn.textContent = '🔁 热重载配置';
  }
}

window.addEventListener('DOMContentLoaded', () => {
  cfgLoadInline();

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

  /* 热重载按钮(只在 yaml editor 旁) */
  const reloadBtn = document.getElementById('cfg-yaml-reload');
  if (reloadBtn) reloadBtn.addEventListener('click', cfgReloadConfig);
});
