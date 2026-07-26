#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
「用户数据」标签 —— 仅保留 Python 逻辑(主框架库查询 + 模板加载)。

HTML / JS 片段在 ``templates/users/``。本侧只:
  · 加载并暴露 ``TAB_HTML`` / ``TAB_CSS`` / ``TAB_JS``(供 ``webui/main.py`` 拼装)
  · ``get_data()`` 经 ``userinfo`` 门面读**主框架**数据库(data.db users /
    wakeup.db / groups_users / statistics.db)序列化为可嵌入的 JSON

只展示属于单个用户的独立数据(昵称 / 头像 / 累计消息 / 最后活跃日期);
最后活跃为日粒度(框架无终身精确时间;精确值仅日志留存期内可查,由 ``查询id`` 指令按需提供)。

前端布局简述(详见模板):查询时间 / 搜索框(同时匹配 name 和 openid)/
分页控件 / 刷新按钮;表格列「序号 / 用户(头像+名称合并)/ OpenID / 消息数 /
最后活跃」;屏幕宽 ≥ 1200px 时切 2 列(左列填满 50 行再去右列),每页 50 行(2 列 100)。
"""

from __future__ import annotations

import json
import os
import time

from .. import userinfo

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')


def _load(name: str) -> str:
    with open(os.path.join(_TEMPLATE_DIR, name), 'r', encoding='utf-8') as f:
        return f.read()


TAB_HTML = _load('users/users.html')
TAB_CSS = _load('users/users.css')
TAB_JS = _load('users/users.js')


def get_data() -> str:
    """返回 ``{query_time, total, users}`` JSON,可嵌入 ``<script id="user-data">``。

    ``total`` 来自框架 users 表全量 COUNT;``users`` 最多 1000 条(按最后活跃
    日期倒序),``total_messages`` 无统计行时为 None(前端显 —)。
    """
    payload = {
        'query_time': int(time.time()),
        'total': userinfo.count_users(),
        'users': userinfo.list_users(),
    }
    data_json = json.dumps(payload, ensure_ascii=False, default=str)
    return data_json.replace('</script>', '<\\/script>')
