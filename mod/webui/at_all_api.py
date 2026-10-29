#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""@全体成员 API —— 面向框架内其他插件的 HTTP 接口(JSON 请求体 + 响应),发送规则见 mod/at_all。

认证与编译 API 共用同一枚 token(``data/build/api_token``,见 build_api;``Authorization: Bearer`` / ``X-API-Token``),
端点在 webui/main.py 以 ``auth=False`` 注册,面板登录态不适用。

``POST /api/ext/lgtbot/at-all``  body:
    {"group_openid": "群 openid",   # 必填
     "content":      "消息内容",     # 必填,跟在 @全体成员 后面发出
     "send_at":      …}             # 可选,预留的定时发送时间,目前不生效(收到即发)

成功 200:``{"success": true, "message", "group_openid", "used_today", "daily_limit"}``。
失败统一 ``{"success": false, "error": 说明, "code": 错误码}``:

    400 bad_request          请求体不是 JSON 对象,或缺少 group_openid / content
    401 unauthorized         token 缺失或错误
    403 not_admin            机器人不是该群管理员
    403 no_push_permission   该群未开启主动消息权限
    409 busy                 该群上一条 @全体成员 还在发送
    429 at_all_limit         该群今日的 @全体成员 次数已用完
    429 push_limit           该群今日的主动消息已达上限(config.yaml 的 active_push_daily_limit)
    502 bot_state_failed     查询机器人在该群的状态失败
    502 send_failed          发送失败 / QQ 拒绝了这条消息
    503 bot_unavailable      没有可用的机器人
"""

from __future__ import annotations

from aiohttp import web

from .. import at_all
from .build_api import _check_auth, _err
from .restart_api import _read_json


async def at_all_handler(request: 'web.Request') -> 'web.Response':
    """``POST /api/ext/lgtbot/at-all`` —— 向一个群发 @全体成员。"""
    if not _check_auth(request):
        return _err(401, 'token 缺失或错误', code='unauthorized')
    body = await _read_json(request)
    if body is None:
        return _err(400, '请求体不是合法 JSON 对象', code='bad_request')
    gid = body.get('group_openid')
    content = body.get('content')
    if not isinstance(gid, str) or not gid.strip():
        return _err(400, '缺少群 openid（group_openid）', code='bad_request')
    if not isinstance(content, str) or not content.strip():
        return _err(400, '缺少消息内容（content）', code='bad_request')
    # TODO(定时任务): 可选的 send_at 是预留的定时发送时间,等定时任务框架落地后实现;目前忽略,收到即发
    gid = gid.strip()
    try:
        res = await at_all.send(gid, content.strip())
    except at_all.AtAllError as e:
        return _err(e.status, e.message, code=e.code)
    return web.json_response({'success': True, 'message': '已发送 @全体成员', 'group_openid': gid, **res})
