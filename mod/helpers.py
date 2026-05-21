#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通用辅助：sender 查找 / 跨线程协程执行 / target_key / mention 美化"""

from __future__ import annotations
import re
import asyncio

from core.base.logger import get_logger, PLUGIN
from . import state, userdb

log = get_logger(PLUGIN, 'LGTBot')

# QQ 官方机器人头像直链（未在 SDK 文档，但实测可用）
# 尺寸：40 / 100 / 140 / 640；LGTBot 渲染头像约 100x100
QQ_AVATAR_URL = 'https://q.qlogo.cn/qqapp/{appid}/{openid}/100'

# 媒体消息（msg_type=7）的 content 字段是 QQ 协议层不解析 <@openid> 的纯文本，
# 图文同条场景下把 mention 退化为 "@昵称"（损失：无 ping 通知）
_MENTION_RE = re.compile(r'<@([^>\s]+)>')


def target_key(target_id: str, is_uid: bool) -> str:
    """统一 target 标识：群消息 'g:<gid>'，私聊 'u:<uid>'"""
    return ('u:' if is_uid else 'g:') + target_id


def humanize_mentions(text: str) -> str:
    """把 <@openid> 转成 @昵称（用于图文消息 content）

    QQ msg_type=7 的 content 不解析 <@openid> 提及语法，会原样显示为字面字符串。
    本函数从 ``userdb`` (SQLite) 取对应昵称替换，保持图文单条消息的同时让
    文字可读。DB 未命中时退化为截短 uid 占位。
    """
    if not text or '<@' not in text:
        return text

    def _repl(m):
        uid = m.group(1)
        name = userdb.get_name(uid)
        if name:
            return f'@{name}'
        # DB 未命中：截短 openid 占位
        return f'@{uid[:6]}…' if len(uid) > 6 else f'@{uid}'

    return _MENTION_RE.sub(_repl, text)


def get_bot_uin(appid: str = '') -> str:
    """从主框架 BotManager 拿本 bot 的 QQ uin (``BotInstance.robot_qq``)。

    由 ``bot.yaml`` 每个 bot 节下的 ``robot_qq`` 字段配置(框架在
    ``core/bot/instance.py::__init__`` 里读出来挂到 BotInstance)。

    Args:
        appid: bot 的 appid;空字符串时返回任一已加载 bot 的 uin。

    Returns:
        uin 字符串。bot 未加载 / 字段未配置时返回 ``''``,调用方应能优雅降级
        (比如「邀我进群」按钮的链接里 ``robot_uin=`` 留空仍可生成 URL,
        QQ 点击后会提示无效 robot,用户回头查 bot.yaml 即可修复)。
    """
    try:
        from core.bot.manager import _bot_manager_ref
        if _bot_manager_ref is None or not _bot_manager_ref._bots:
            return ''
        bots = _bot_manager_ref._bots
        if appid and appid in bots:
            return getattr(bots[appid], 'robot_qq', '') or ''
        for bot in bots.values():
            uin = getattr(bot, 'robot_qq', '') or ''
            if uin:
                return uin
    except Exception as e:
        log.warning(f'获取 bot uin 失败: {e}')
    return ''


def get_sender(appid: str = ''):
    """从 BotManager 全局引用获取 MessageSender。

    appid 为空时返回任一可用 sender（单 Bot 场景足够）。
    """
    try:
        from core.bot.manager import _bot_manager_ref
        if _bot_manager_ref is None or not _bot_manager_ref._bots:
            return None
        if appid and appid in _bot_manager_ref._bots:
            return _bot_manager_ref._bots[appid].sender
        return next(iter(_bot_manager_ref._bots.values())).sender
    except Exception as e:
        log.warning(f'获取 sender 失败: {e}')
        return None


def run_coro_blocking(coro, timeout: float = 15.0):
    """C++ 工作线程 → asyncio 事件循环 的安全桥接（阻塞等待结果）"""
    loop = state.event_loop
    if loop is None or loop.is_closed():
        log.warning('事件循环不可用，丢弃协程')
        return None
    try:
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=timeout)
    except Exception as e:
        log.warning(f'协程执行异常: {e}')
        return None


def is_full_volume_group(gid: str) -> bool:
    """判断 ``gid`` 是否是「全量推送」群 —— 只信任**运行时观测**到的事实。

    判定唯一依据:``state.full_volume_groups`` 集合,由 dispatcher 在见到
    ``GROUP_MESSAGE_CREATE`` 事件时填入。

    为什么不再退回框架 ``non_at_message.{enabled,group_whitelist}`` 配置:

      · QQ 的全量推送权限是在 **QQ 官方 bot 管理后台**给单个 (bot, 群) 维度开
        的;开了之后 QQ 才会向 bot 投递 ``GROUP_MESSAGE_CREATE`` 事件。
      · 框架 ``non_at_message.*`` 配置只是「框架收到 non-AT 后,要不要派给
        非 ``ignore_at_check`` 插件」的二级开关 —— 它和 QQ 后台权限**不同步**,
        可以一边开一边关。
      · 当用户在 ``bot.yaml`` 里写了 ``group_whitelist``、但 QQ 后台并没真给
        权限时,该群永远不会有 ``GROUP_MESSAGE_CREATE`` 投来。这时 helper 若
        信任配置就会把非全量群误判为全量,引发**非全量群里漏挂刷新按钮、
        被动配额耗尽后乱走主动消息**(用户反馈的现象)。
      · 框架自身在 ``core/bot/event.py::_record_full_access_group`` 也是按
        实际收到 ``GROUP_MESSAGE_CREATE`` 来记录全量群的(内存 cache + SQLite
        表 ``full_access_groups``),并不查 ``non_at_message.*`` —— 这进一步
        说明运行时观测才是 ground truth。

    取舍:进程首次启动后,第一次在某全量群收到 non-AT 消息前,helper 会暂时
    返回 False(空集合);该窗口里第一条引擎回复会按非全量逻辑挂刷新按钮 —— 视觉上多一个按钮,无功能损失。一旦任何 non-AT 消息到达,集合即标记,
    后续行为正确。
    """
    if not gid:
        return False
    try:
        return gid in state.full_volume_groups
    except Exception:
        return False
