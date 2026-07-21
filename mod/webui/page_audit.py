#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「🛡️ 操作审计」标签 —— 只读展示 ``mod/audit.py`` 的持久化审计记录。

★ 安全准则 ★
  · **纯只读**:唯一 action 端点是「刷新列表」(``render_list``);
    **不提供清空 / 删除端点**(防自毁审计)。容量控制完全由
    ``audit.MAX_ENTRIES`` 滚动淘汰负责,无任何可干预入口。
  · 本模块不产生审计记录,只消费;写入方是各状态变更端点的入口层
    (page_build / page_dashboard / page_config / page_backup /
    dispatcher / webui.main / backup 自动任务)。

Python 侧职责:
  · ``TAB_HTML`` / ``TAB_CSS`` / ``TAB_JS`` 从 ``templates/audit/`` 加载
  · ``get_data()`` 返回首屏 payload(全量记录 + 状态 + 类别映射)
  · ``render_list`` —— 无参刷新 endpoint,沿用 ``<pre id="result">JSON</pre>``
    fragment 协议,由 ``webui/main.py`` 用 ``_register_hidden_action`` 注册
"""

from __future__ import annotations

import html as _html
import json
import os
import time

from .. import audit

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')


def _load(name: str) -> str:
    with open(os.path.join(_TEMPLATE_DIR, name), 'r', encoding='utf-8') as f:
        return f.read()


TAB_HTML = _load('audit/audit.html')
TAB_CSS = _load('audit/audit.css')
TAB_JS = _load('audit/audit.js')


def _fragment(payload: dict) -> str:
    """把 ``payload`` 包成 ``<pre id="result">…</pre>`` —— 同 page_backup。"""
    body = json.dumps(payload, ensure_ascii=False, default=str)
    return f'<pre id="result">{_html.escape(body)}</pre>'


def _payload() -> dict:
    """首屏与刷新共用的完整 payload。

    entries 新 → 旧全量下发(≤ MAX_ENTRIES 条),类别筛选在客户端做;
    categories 把 ``audit.CATEGORIES``(单一真相源)转成 JS 好用的形状。
    """
    st = audit.file_status()
    return {
        'entries': audit.get_entries(),
        'count': st['count'],
        'oldest_ts': st['oldest_ts'],
        'size_bytes': st['size_bytes'],
        'max_entries': audit.MAX_ENTRIES,
        'audit_path': audit.AUDIT_PATH,
        'query_time': int(time.time()),
        'categories': {cat: {'emoji': emoji, 'label': label}
                       for cat, (emoji, label) in audit.CATEGORIES.items()},
    }


def get_data() -> str:
    """返回可嵌入 ``<script id="audit-data">`` 的 JSON。"""
    data_json = json.dumps(_payload(), ensure_ascii=False, default=str)
    return data_json.replace('</script>', '<\\/script>')


def render_list() -> str:
    """返回最新审计记录 + 状态(「🔄 刷新」按钮用)。"""
    return _fragment({'success': True, **_payload()})
