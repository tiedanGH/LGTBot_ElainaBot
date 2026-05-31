#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
「用户数据」标签 —— 仅保留 Python 逻辑(数据库查询 + 模板加载)。

HTML / JS 片段在 ``templates/users.html`` 和 ``templates/users.js``。
本侧只:
  · 加载并暴露 ``TAB_HTML`` / ``TAB_JS``(供 ``webui/main.py`` 拼装主模板)
  · ``get_data()`` 查询 ``data/user_cache.db`` 序列化为可嵌入的 JSON

前端布局简述(详见模板):查询时间 / 搜索框(同时匹配 name 和 openid)/
分页控件 / 刷新按钮;表格列「序号 / 用户(头像+名称合并)/ OpenID / 上次活跃」;
屏幕宽 ≥ 1200px 时切 2 列(左列填满 50 行再去右列),每页 50 行(2 列 100)。
"""

from __future__ import annotations

import json
import os
import time

from .. import userdb

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')


def _load(name: str) -> str:
    with open(os.path.join(_TEMPLATE_DIR, name), 'r', encoding='utf-8') as f:
        return f.read()


TAB_HTML = _load('users/users.html')
TAB_CSS = _load('users/users.css')
TAB_JS = _load('users/users.js')


def get_data() -> str:
    """返回 ``{query_time, total, users}`` JSON,可嵌入 ``<script id="user-data">``。

    ``total`` 来自 ``userdb.count_users()``,反映 DB + _pending 的去重总数,
    与 ``users`` 列表长度可能不同(后者最多 1000 条)。前端用 total 显示
    「总用户: N」,用 users 渲染当前页。
    """
    payload = {
        'query_time': int(time.time()),
        'total': userdb.count_users(),
        'users': userdb.list_users(),
    }
    data_json = json.dumps(payload, ensure_ascii=False, default=str)
    return data_json.replace('</script>', '<\\/script>')
