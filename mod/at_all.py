#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""@全体成员 —— 发送前校验(当日次数 / 主动消息额度 / 管理员身份 / 主动推送权限)+ 发送 + 记账,供 webui.at_all_api 调用。

官方限制(群聊):机器人须是群管理员,每个群每个自然日只能 @全体 1 次,文本与 markdown 消息都支持。
由其他插件经 API 触发,不挂在任何对话上,只能以主动消息发出:群要开通主动推送权限,并计入每群每日主动消息上限。
"""

from __future__ import annotations

from core.base.logger import get_logger, PLUGIN

from . import helpers, log_attribution, metrics
from .webui import page_logs

log = get_logger(PLUGIN, 'LGTBot')

# 每个群每个自然日上限 1 次
DAILY_LIMIT = 1

# 正在发 @全体 的群:同一个群并发两次请求时,第二次在第一次记账之前读到的当日次数还是旧的
_inflight: set = set()


class AtAllError(Exception):
    """校验没过 / 发送失败。``status`` 是给 API 的 HTTP 状态码,``code`` 是机器可读的错误码(见 webui.at_all_api)。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


async def send(gid: str, content: str) -> dict:
    """向群 ``gid`` 发一条 @全体成员,正文是 ``content``;成功返回 ``{'used_today', 'daily_limit'}``,否则抛 AtAllError。

    ``content`` 里现成的 ``<qqbot-…>`` 标签一律拆掉:每次请求只 @ 一次全体,当日次数才对得上。
    次数与主动消息额度只看本地记账,先于 bot_state 查询,用完了就不必再打一次 QQ 接口。
    """
    from . import callbacks       # 延迟取:ACTIVE_PUSH_DAILY_LIMIT 是 config 下发后的最新值
    if gid in _inflight:
        raise AtAllError(409, 'busy', '该群上一条 @全体成员 还在发送')
    _inflight.add(gid)
    try:
        if metrics.at_all_used(gid) >= DAILY_LIMIT:
            raise AtAllError(429, 'at_all_limit', f'该群今日的 @全体成员 次数已用完（每天 {DAILY_LIMIT} 次）')
        limit = callbacks.ACTIVE_PUSH_DAILY_LIMIT
        if limit and metrics.active_push_used(gid, False) >= limit:
            raise AtAllError(429, 'push_limit', f'该群今日的主动消息已达上限（{limit} 条）')

        sender = helpers.get_sender('')
        if sender is None or not hasattr(sender, 'get_group_bot_state'):
            raise AtAllError(503, 'bot_unavailable', '没有可用的机器人')
        try:
            data, _err = await sender.get_group_bot_state(gid, return_error=True)
        except Exception as e:
            log.warning(f'[@全体] 查询群 {gid} 的 bot_state 失败: {e}')
            data = None
        if not isinstance(data, dict):
            raise AtAllError(502, 'bot_state_failed', '查询机器人在该群的状态失败')
        if str(data.get('member_role') or '') not in ('admin', 'owner'):
            raise AtAllError(403, 'not_admin', '机器人不是该群管理员，无法 @全体成员')
        if not data.get('allow_proactive_msg'):
            raise AtAllError(403, 'no_push_permission', '该群未开启主动消息权限')

        md = f'{helpers.AT_EVERYONE}\n{helpers.neutralize_qq_tags(content)}'
        page_logs.log_outgoing(gid, False, md)
        try:
            with log_attribution.mark_outbound():
                ret = await sender.send_to_group(gid, md)
        except Exception as e:
            log.warning(f'[@全体] 发送到群 {gid} 失败: {e}')
            raise AtAllError(502, 'send_failed', '发送失败') from e
        ok, code = callbacks._send_result(ret)
        if not ok:
            log.warning(f'[@全体] 群 {gid} 的 @全体成员 被 QQ 拒绝(错误码 {code})')
            raise AtAllError(502, 'send_failed', f'QQ 拒绝了这条消息（错误码 {code}）')
        metrics.record_at_all(gid)
        metrics.record_active_push(gid, False)
        used = metrics.at_all_used(gid)
    finally:
        _inflight.discard(gid)
    log.info(f'📣 [@全体] 已向群 {gid} 发送 @全体成员(今日 {used}/{DAILY_LIMIT})')
    return {'used_today': used, 'daily_limit': DAILY_LIMIT}
