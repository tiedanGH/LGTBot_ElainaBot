/* ──── 配置管理 tab ────
 * 编辑器共享同一套读 / dirty 跟踪 / 保存 / revert 逻辑,通过 cfgEditors 表驱动。
 * 保存分两路:
 *   · config.yaml / lgtbot.json(带 target 字段)→ 本插件校验端点
 *     /api/ext/lgtbot/config/save —— 服务端语法 + schema 校验通过才落盘,
 *     校验失败返回 errors 列表,文件保持原样
 *   · 纯文本编辑器(公告 / 疑难解答等)→ 主框架 /api/config-file/save
 * 热重载按钮调 __lgtbot_dash_reload_config(从原 dashboard 搬迁,key 不变)。
 */

const CFG_KEYS = {
  reload_config: '__lgtbot_dash_reload_config',
};
const CFG_VALIDATED_SAVE_ROUTE = '/api/ext/lgtbot/config/save';

/* 每个编辑器一份状态:
 *   dataKey  —— get_data() 返回的 JSON 里对应哪个 key(缺失 = 后端没渲染该区块)
 *   absPath  —— 保存请求要原样回传给 /api/config-file/save 的绝对路径
 *   original —— 最近一次从后端拉到的内容,用来做 dirty 检测 + 「↩ 恢复」
 *   format   —— 主框架保存端点的格式 hint(yaml / json / text)
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
    absPath: '', original: '', format: 'text',
    editorId: 'cfg-important-editor', pathId: 'cfg-important-path',
    msgId: 'cfg-important-msg', saveBtnId: 'cfg-important-save', revertBtnId: 'cfg-important-revert',
    saveHint: '，下次发送「更新公告」指令时即生效；留空则不渲染该区块',
  },
  notice: {
    dataKey: 'update_notice',
    absPath: '', original: '', format: 'text',
    editorId: 'cfg-notice-editor', pathId: 'cfg-notice-path',
    msgId: 'cfg-notice-msg', saveBtnId: 'cfg-notice-save', revertBtnId: 'cfg-notice-revert',
    saveHint: '，下次发送「更新公告」指令时即生效',
  },
  urgent: {
    dataKey: 'urgent_notice',
    absPath: '', original: '', format: 'text',
    editorId: 'cfg-urgent-editor', pathId: 'cfg-urgent-path',
    msgId: 'cfg-urgent-msg', saveBtnId: 'cfg-urgent-save', revertBtnId: 'cfg-urgent-revert',
    saveHint: '，下次打开欢迎菜单时即生效；留空则整块不显示',
  },
  trouble: {
    dataKey: 'troubleshooting',
    absPath: '', original: '', format: 'text',
    editorId: 'cfg-trouble-editor', pathId: 'cfg-trouble-path',
    msgId: 'cfg-trouble-msg', saveBtnId: 'cfg-trouble-save', revertBtnId: 'cfg-trouble-revert',
    saveHint: '，下次发送「疑难解答」指令时即生效',
  },
  /* SPONSOR_JS_START
   * sponsor_enabled 关闭时整段被服务端切掉,标记之内不要放别的表项。 */
  sponsors: {
    dataKey: 'sponsors',
    absPath: '', original: '', format: 'text',
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

function cfgApplyData(data) {
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
  const btn = document.getElementById(state.saveBtnId);
  if (btn) btn.disabled = true;
  try {
    /* config.yaml / lgtbot.json → 插件校验端点:服务端语法 + schema 校验通过才落盘;失败返回 errors,文件不动。 */
    if (state.target) {
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
        cfgShowMsg(key, '✅ 校验通过并已保存' + state.saveHint + warn, warn ? 'info' : 'ok');
        state.original = text;
        delete editor.dataset.dirty;
      } else {
        const errs = (data.errors && data.errors.length)
          ? data.errors.join('；') : '校验失败';
        cfgShowMsg(key, '❌ 未保存 —— ' + errs, 'err');
      }
      return;
    }

    /* 纯文本编辑器(公告 / 疑难解答等)→ 主框架通用保存端点 */
    if (!state.absPath) {
      cfgShowMsg(key, '文件路径未知，无法保存', 'err');
      return;
    }
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
