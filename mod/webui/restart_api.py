#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""重启 / 计划重启 API —— 面向框架内其他插件的 HTTP 接口(JSON 请求体 + 响应)。

认证与编译 API 共用同一枚 token(``data/build/api_token``,见 build_api;
``Authorization: Bearer`` / ``X-API-Token``),端点在 webui/main.py 以
``auth=False`` 注册,面板登录态不适用。

端点(POST 与面板既有的 GET /planned-restart 同路径不同方法,互不影响):
  · ``POST /api/ext/lgtbot/restart``          body 可空 ``{}``
    立即重启(与面板「🔁 重启 LGTBot」/ /重启 指令同一原子预检):有进行中
    对局 → 409 拒绝;成功 → 200,响应送达后 ~0.5s 整进程 os.execv。
  · ``POST /api/ext/lgtbot/planned-restart``  body:
        {"enable": bool,        # 必填:开 / 关维护模式
         "auto":   bool=false,  # 自动重启:全部对局结束后自动执行(默认手动)
         "reason": "文本"}      # 维护原因,展示在玩家的维护提示里(≤200 字)
    手动模式(auto=false)禁止创建新游戏,进行中对局不受影响;auto=true
    **不限制新游戏创建**,watcher 每 20s 轮询,对局清空并静默 30s 后自动
    重启(计审计 SRC_AUTO;静默期内新局出现则顺延)。

状态码:200 成功 / 400 参数缺失或类型错 / 401 token 错 / 409 有对局拒绝重启。
审计:两个端点的操作都以 ``SRC_API`` 落审计;自动重启触发时由 watcher 以 ``SRC_AUTO`` 另记一条。
"""

from __future__ import annotations

from aiohttp import web

from core.base.logger import get_logger, PLUGIN

from .. import audit, metrics
from .. import state as _plugin_state
from .build_api import _check_auth, _err

log = get_logger(PLUGIN, 'LGTBot')

_ROUTE_RESTART = '/api/ext/lgtbot/restart'
_ROUTE_PLANNED = '/api/ext/lgtbot/planned-restart'

_REASON_MAX = 200      # 与面板 ?reason= 的截断一致


async def _read_json(request) -> dict | None:
    """请求体 JSON(空体按 {});解析失败返回 None(调用方回 400)。"""
    if not request.can_read_body:
        return {}
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


async def restart_handler(request: 'web.Request') -> 'web.Response':
    """``POST /api/ext/lgtbot/restart`` —— 立即重启(有对局则 409 拒绝)。

    与面板按钮 / /重启 指令共用 ``check_and_prepare_restart`` 原子预检:
    预检通过时引擎已被干净释放,**必须**随即调度 execv;响应先行返回,~0.5s 后进程替换(与面板重启的时序完全一致)。
    """
    if not _check_auth(request):
        return _err(401, 'token 缺失或错误')
    from .. import dispatcher   # 延迟 import,断开 webui ← dispatcher 循环
    # 可选 body {"reason": "更新内容"} —— 随重启通知发给还有等待中房间的群。
    # body 不是合法 JSON 时按无 reason 处理:重启本身不该被一个可选字段挡住。
    body = await _read_json(request) or {}
    reason = str(body.get('reason') or '').strip()[:_REASON_MAX]
    ok, msg = dispatcher.check_and_prepare_restart()
    audit.record('restart', '重启 LGTBot',
                 (f'更新内容：{reason}' if reason else '') if ok else msg,
                 ok=ok, src=audit.SRC_API)
    if not ok:
        return _err(409, msg, active_matches=len(_plugin_state.active_matches))
    metrics.record_restart()
    # API 重启同属手动重启:不推通知群,但等待中房间照常通知
    await dispatcher._notify_restart_rooms(reason)
    dispatcher.schedule_exec_after(0.5)
    return web.json_response({'success': True, 'message': msg,
                              'restarting_in_sec': 0.5})


async def planned_restart_handler(request: 'web.Request') -> 'web.Response':
    """``POST /api/ext/lgtbot/planned-restart`` —— 开 / 关计划重启维护模式。

    body ``enable`` 必填(显式语义,不做翻转 —— API 调用方要的是确定状态);``auto`` / ``reason`` 仅开启时生效。
    响应带当前剩余对局数,auto 开启且对局已清空时 watcher 会在数秒内自动触发重启。
    """
    if not _check_auth(request):
        return _err(401, 'token 缺失或错误')
    body = await _read_json(request)
    if body is None:
        return _err(400, '请求体不是合法 JSON 对象')
    enable = body.get('enable')
    if not isinstance(enable, bool):
        return _err(400, '缺少布尔参数 enable(true=开启维护模式 / false=关闭)')
    auto = bool(body.get('auto'))
    reason = str(body.get('reason') or '').strip()[:_REASON_MAX]

    from .. import dispatcher
    on, msg = dispatcher.set_planned_mode(enable, reason, auto)
    if on:
        detail = ('已开启维护模式'
                  + (f'（原因：{reason}）' if reason else '')
                  + (' + 自动重启' if auto else ''))
    else:
        detail = '已取消维护模式'
    audit.record('restart', '计划重启模式', detail, src=audit.SRC_API)
    return web.json_response({
        'success': True,
        'enabled': on,
        'auto': bool(on and auto),
        'reason': reason if on else '',
        'active_matches': len(_plugin_state.active_matches),
        'message': msg,
    })
