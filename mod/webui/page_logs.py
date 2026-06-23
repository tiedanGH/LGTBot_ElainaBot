#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
「消息日志」标签 —— 日志缓冲(数据层)+ 页面数据生成(渲染层)合并模块。

────────── 数据层(原 message_log.py 合并进来) ──────────
对外暴露 ``log_incoming`` / ``log_outgoing`` / ``get_logs`` / ``clear_logs``,
被 ``callbacks`` 与 ``dispatcher`` 在收发消息路径上直接调用，把每条进出消息
塞进一个环形 deque(默认 500 条上限)。

跨插件热重载:日志 deque 与锁挂在 C++ 扩展常驻的 ``boot._get_persistent()``
字典里 —— 旧 callback 写入的日志在新 dispatcher 注册的页面里也能被读到。

────────── 渲染层 ──────────
HTML / JS 片段在 ``templates/logs/`` 子目录，``TAB_HTML`` / ``TAB_JS`` 在
import 时一次性读入。``get_data()`` 序列化当前 deque 为 JSON,由
``webui/main.py`` 的 ``_render_html()`` 内嵌到 ``<script id="log-data">``。
功能:筛选(全部/收到/发出/群聊/私聊)+ 自动刷新切换 + 主题切换。
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from threading import Lock

from .. import boot


# ──────── 日志缓冲(跨插件热重载共享)─────────────────────────────────────

_MAX_LOGS = 500
_p = boot._get_persistent()
if 'logs_deque' not in _p:
    _p['logs_deque'] = deque(maxlen=_MAX_LOGS)
    _p['logs_lock'] = Lock()
_logs: deque = _p['logs_deque']
_lock: Lock = _p['logs_lock']


def log_incoming(uid: str, gid: str, content: str):
    """记录收到的消息(来自 QQ 玩家，即将转发给 LGTBot 引擎)。"""
    _append({
        'time': time.time(),
        'direction': 'in',
        'kind': 'group' if gid else 'private',
        'uid': uid or '',
        'gid': gid or '',
        'content': content or '',
        'image': False,
    })


def log_outgoing(target_id: str, is_uid: bool, content: str, *, image: bool = False):
    """记录发出的消息(LGTBot 引擎 → QQ)。"""
    _append({
        'time': time.time(),
        'direction': 'out',
        'kind': 'private' if is_uid else 'group',
        'uid': target_id if is_uid else '',
        'gid': '' if is_uid else target_id,
        'content': content or '',
        'image': image,
    })


def _append(entry: dict):
    with _lock:
        _logs.append(entry)


def get_logs() -> list:
    """快照当前所有日志。"""
    with _lock:
        return list(_logs)


def clear_logs():
    with _lock:
        _logs.clear()


# ──────── 页面渲染层 ──────────────────────────────────────────────────────

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')


def _load(name: str) -> str:
    with open(os.path.join(_TEMPLATE_DIR, name), 'r', encoding='utf-8') as f:
        return f.read()


TAB_HTML = _load('logs/logs.html')
TAB_CSS = _load('logs/logs.css')
TAB_JS = _load('logs/logs.js')


def get_data() -> str:
    """返回 logs JSON,可直接嵌入 ``<script id="log-data">``。

    JSON 中可能含 ``</script>``,先转义避免破坏外层 ``<script>`` 标签。"""
    data_json = json.dumps(get_logs(), ensure_ascii=False, default=str)
    return data_json.replace('</script>', '<\\/script>')
