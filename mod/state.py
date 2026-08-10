#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""共享运行时状态 —— 多个子模块共享的可变全局变量。

设计：Python 模块本身就是单例，把所有跨模块共享的状态集中在这里，
其他子模块通过 `from . import state; state.xxx = ...` 读写，避免到处传参。

跨插件热重载：`pending_buttons` 等可变容器从 `boot._get_persistent()` 取得，
挂在 C++ 扩展模块对象上常驻进程，新旧模块实例引用同一份字典 —— 这样
热重载时即便旧 callback 还在用旧 state 对象，读到的还是同一份数据。

用户昵称 / 头像等用户数据改读主框架数据库（见 ``userinfo.py``），不在此模块。
"""

from __future__ import annotations
import asyncio
from typing import Optional

from . import boot

# 由 main.py 在 module top-level 捕获（PluginManager 仅在加载窗口期 set 此值）
plugin_ctx = None

# 由 @on_load 设置，C++ 工作线程通过 run_coroutine_threadsafe 调度到此循环
# （asyncio loop 本身跨重载不变，每次 @on_load 重新捕获是 OK 的）
event_loop: Optional[asyncio.AbstractEventLoop] = None

# LGTBot 引擎是否已成功 start（per-load，与 boot.is_engine_running() 配合使用）
started: bool = False

# config.yaml 的 bind_bot_appid 原值（'' = 自动取框架第一个 bot）。由
# config._apply_runtime_tunables 每次加载 / 热重载写入;真正的解析(配置值
# 是否在线、回退第一个)由 helpers.get_bound_appid() **每次调用时**惰性完成,
# 避免"插件加载早于 bot 就绪"的时序问题。
bind_bot_appid: str = ''

# ── 跨重载共享的可变容器(取自 boot 持久化字典) ──
# 所有默认 key 由 ``boot._get_persistent()`` 集中保证,这里直接取下标即可。
_p = boot._get_persistent()
pending_buttons: dict[str, list] = _p['pending_buttons']  # 'g:gid'/'u:uid' → [[btn]]
# /新游戏 X 时记录;/加入 时回查给「📜 规则」按钮用 —— 跨热重载持久,
# 进程重启即丢(失忆群按 /加入 时该按钮会缺规则,无大碍)。
current_game: dict[str, str] = _p['current_game']  # target_key → 游戏名
# 进行中(已开局)的对局 —— target_key → {'target_id','is_uid','game','since'}。
# 由 callbacks.cb_match_event 以引擎「开局 / 结束」事件维护(game_started 加、结算 / 解散移除)。
# 供仪表盘「进行中的对局」展示 + 引擎崩溃时给受牵连对局 fan-out 中断通知。
# 跨热重载持久(挂持久字典,与 current_game 同源,重载时活跃对局不丢);进程 execv 重启后引擎所有 match 已失联,空 dict 正是正确态。
active_matches: dict[str, dict] = _p['active_matches']
# 「/新游戏 X …」命令里抓下的游戏名(target_key → 游戏名),由 dispatcher 在派发给引擎前写入,callbacks 的 game_started 消费。
# 单机局引擎跳过 new_game 广播、game_started 又无 brief,current_game 拿不到名字 → 回退用这里抓的命令名;
# 多人局仍以引擎 new_game 的 brief 为准,这份只在 current_game 为空时兜底。跨热重载持久。
pending_new_game_name: dict[str, str] = _p['pending_new_game_name']
# 运行时观测到 ``GROUP_MESSAGE_CREATE`` 的群 openid 集合 —— 由 dispatcher 填入。
#
# 这是「真·全量群」的唯一判定信号。理由:
#   · QQ 的全量推送权限在 QQ 官方 bot 管理后台 per-(bot, 群) 开关;开了 QQ 才
#     会向 bot 投递 GROUP_MESSAGE_CREATE。事件投递即权限授予的事实证据。
#   · 框架 ``non_at_message.{enabled,group_whitelist}`` 配置只是「框架收到
#     non-AT 后要不要派给非 ignore_at_check 插件」的二级开关,跟 QQ 后台权限
#     不同步。用 bot.yaml 配置当真值会把没开 QQ 权限的群误判为全量,后果是
#     非全量群也走主动消息(QQ 必拒)+ 刷新按钮漏挂 —— 用户反馈过的现象。
#   · 框架自身的 ``core/bot/event.py::_record_full_access_group`` 也是按实际
#     收到 GROUP_MESSAGE_CREATE 来记的(内存 cache + SQLite
#     ``groups_users.is_full_access``),并不查 ``non_at_message.*``。
#
# 持久化:跨热重载存活(挂在 C++ 扩展模块上,见 ``boot._get_persistent()``);
# 进程重启即丢,首次在某全量群收到 non-AT 消息前会暂时按非全量行为兜底。
full_volume_groups: set[str] = _p['full_volume_groups']


def is_planned_restart() -> bool:
    """「计划重启」维护模式是否开启。

    开启后仅禁止创建新游戏(dispatcher 拦下「新游戏」类指令回维护提示),
    进行中的对局与已创建的房间不受影响。跨热重载持久(挂持久字典);真正
    os.execv 重启后进程重建,自动回到关闭状态,无需手动清理。
    """
    return bool(_p.get('planned_restart'))


def set_planned_restart(on: bool, reason: str = '', auto: bool = False) -> None:
    """开 / 关维护模式。``reason`` 为管理员填写的原因(仅开启时有意义,
    会展示在玩家收到的维护提示里);``auto`` 为「自动重启」子开关 —— 开启后
    由 dispatcher 的 watcher 轮询,全部对局结束即自动执行重启(默认手动)。
    关闭时两者一并清掉,避免下次开启复用旧值。"""
    _p['planned_restart'] = bool(on)
    _p['planned_restart_reason'] = (reason or '').strip() if on else ''
    _p['planned_restart_auto'] = bool(auto) if on else False


def is_planned_restart_auto() -> bool:
    """「计划重启」的自动重启子开关(仅在维护模式开启期间有意义)。"""
    return bool(_p.get('planned_restart_auto'))


def planned_restart_reason() -> str:
    """当前维护原因('' = 未填写);跨热重载持久,同 planned_restart 标志。"""
    return str(_p.get('planned_restart_reason') or '')
