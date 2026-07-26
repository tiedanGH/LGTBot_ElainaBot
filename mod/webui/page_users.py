#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
「用户数据」标签 —— 仅保留 Python 逻辑(主框架库查询 + 模板加载)。

HTML / JS 片段在 ``templates/users/``。本侧只:
  · 加载并暴露 ``TAB_HTML`` / ``TAB_CSS`` / ``TAB_JS``(供 ``webui/main.py`` 拼装)
  · ``get_data()`` 经 ``userinfo`` 门面读**主框架**数据库(data.db users /
    wakeup.db / groups_users / statistics.db)序列化为可嵌入的 JSON
  · ``page_handler`` —— ``GET /api/ext/lgtbot/users/page?offset=N`` 取剩余全部

两段式懒加载:用户数据 payload 内嵌在面板整页 HTML 里(打开任何标签都要下载),
所以首屏只带前 ``_HEAD``(1000)条控制面板重量,同时携带真实 ``total`` —— 前端
按 total 展示完整页数;翻/跳到 1000 名以后或发起搜索时,**一次**拉取剩余全部
(排序键跨三源无法下推 SQL,服务端每次请求都是全量合并排序 —— 按块反复取只会
放大合并次数,一次取完使每会话最多多合并一次,搜索也随之全量覆盖)。

只展示属于单个用户的独立数据(昵称 / 头像 / 累计消息 / 最后活跃日期);
最后活跃为日粒度(框架无终身精确时间;精确值仅日志留存期内可查,由 ``查询id`` 指令按需提供)。

前端布局简述(详见模板):查询时间 / 搜索框(同时匹配 name 和 openid)/
分页控件 / 刷新按钮;表格列「序号 / 用户(头像+名称合并)/ OpenID / 消息数 /
最后活跃」;屏幕宽 ≥ 1200px 时切 2 列(左列填满 50 行再去右列),每页 50 行(2 列 100)。
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from aiohttp import web

from .. import userinfo

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')

# 首屏内嵌条数:控制面板整页体积(≈0.3MB @1000 行);其余按需一次取完
_HEAD = 1000


def _load(name: str) -> str:
    with open(os.path.join(_TEMPLATE_DIR, name), 'r', encoding='utf-8') as f:
        return f.read()


TAB_HTML = _load('users/users.html')
TAB_CSS = _load('users/users.css')
TAB_JS = _load('users/users.js')


def get_data() -> str:
    """返回 ``{query_time, total, head, users}`` JSON,嵌入 ``<script id="user-data">``。

    ``total`` 来自框架 users 表全量 COUNT(前端据此算完整页数);``users`` 仅前
    ``_HEAD`` 条(按最后活跃日期倒序),剩余经 ``page_handler`` 一次取完;
    ``total_messages`` 无统计行时为 None(前端显 —)。
    """
    payload = {
        'query_time': int(time.time()),
        'total': userinfo.count_users(),
        'head': _HEAD,
        'users': userinfo.list_users(limit=_HEAD),
    }
    data_json = json.dumps(payload, ensure_ascii=False, default=str)
    return data_json.replace('</script>', '<\\/script>')


async def page_handler(request: 'web.Request') -> 'web.Response':
    """``GET /api/ext/lgtbot/users/page?offset=N`` —— 取 ``offset`` 起的**剩余全部**。

    前端在首块(``_HEAD``)之外的翻页 / 搜索时调用一次(offset=_HEAD),之后
    数据齐备不再请求。合并排序在线程池跑,不阻塞事件循环;响应带 ``total``
    供前端校准页数。
    """
    try:
        offset = max(0, int(request.query.get('offset', '0')))
    except (TypeError, ValueError):
        return web.json_response({'success': False, 'message': 'offset 非法'}, status=400)
    loop = asyncio.get_running_loop()
    users = await loop.run_in_executor(
        None, lambda: userinfo.list_users(offset=offset))
    total = await loop.run_in_executor(None, userinfo.count_users)
    return web.json_response({
        'success': True,
        'offset': offset,
        'total': total,
        'users': users,
        'query_time': int(time.time()),
    })
