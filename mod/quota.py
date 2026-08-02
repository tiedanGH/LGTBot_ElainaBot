#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""被动消息引用配额管理（绕过 QQ 单条消息被动回复条数限制）

QQ 协议事实（官方文档「被动消息」表,按场景区分）：
  · 群聊:每个 msg_id 可被回复 **5** 次（msg_seq=1..5），**5 分钟**后过期
  · 单聊:每个 msg_id 可被回复 **4** 次，**60 分钟**后过期
  · 每个 event_id（INTERACTION 等）独立计一轮配额（按所在场景取上限）
  · 在消息上挂 callback 按钮，用户点击 → 新 INTERACTION_CREATE → 新 event_id
    → 又获得一轮新配额，从而绕过单引用条数的硬限制

本模块策略：
  · 倒数第 2 条起自动追加「🔄 刷新」按钮（type=1 callback;群 4/5 条,单聊 3/4 条）
  · 用户点击 → ACK + 立即刷新引用 + 唤醒可能在等待的发送协程
  · 发送时若配额满，最长等待 15s 等待新刷新事件再重试

场景由 key 前缀判定（``helpers.target_key``:群 'g:' / 单聊 'u:'），配额与
TTL 都经 ``ref_quota(key)`` / ``ref_ttl(key)`` 按场景取值。
"""

from __future__ import annotations
import asyncio
import time
import threading

from core.base.logger import get_logger, PLUGIN
from . import state, boot

log = get_logger(PLUGIN, 'LGTBot')

# ──────── 常量配置 ────────────────────────────────────────────────────────
# 被动配额按场景区分(见模块 docstring)。TTL 各留余量:群 5min-10s;
# 单聊 60min-60s(窗口长,预留也放大 —— QQ 从消息发出计时,我们从收到计时)。
REF_QUOTA_GROUP = 5
REF_QUOTA_DM = 4
REF_TTL_GROUP = 290.0
REF_TTL_DM = 3540.0
REFRESH_WAIT_TIMEOUT = 15.0      # 配额耗尽时等待刷新的最长秒数（可在 config.yaml 覆盖）
RELAY_BUTTON_DATA = '__lgt_relay__'


def is_dm_key(key: str) -> bool:
    """key 是否单聊场景('u:<uid>';其余按群聊规则)。"""
    return str(key).startswith('u:')


def ref_quota(key: str) -> int:
    """该 key 场景下每条消息可被动回复的条数(群 5 / 单聊 4)。"""
    return REF_QUOTA_DM if is_dm_key(key) else REF_QUOTA_GROUP


def ref_ttl(key: str) -> float:
    """该 key 场景下引用的有效期秒数(群 ~5min / 单聊 ~60min)。"""
    return REF_TTL_DM if is_dm_key(key) else REF_TTL_GROUP


def refresh_threshold(key: str) -> int:
    """第 N 条起追加刷新按钮 = 倒数第 2 条(群 4/5 条,单聊 3/4 条)。"""
    return ref_quota(key) - 1

# ──────── 内部状态 ────────────────────────────────────────────────────────
# key = 'g:<gid>' / 'u:<uid>'
# value = {'ref_type': 'msg_id'|'event_id', 'ref_value', 'count', 'expires_at', 'appid'}
#
# 跨重载共享：取自 boot._get_persistent()，挂在 C++ 扩展上常驻进程；
# 旧 callback 与新 dispatcher 操作同一份字典，热重载不会丢配额状态。
_p = boot._get_persistent()
_active_ref: dict[str, dict] = _p['active_ref']
_ref_lock = threading.Lock()

# 等待器：每个等待中的协程持有独立 asyncio.Event，避免共享 Event 时 ev.clear()
# 擦掉刚到达的信号导致死等。refresh_ref 时把 list 内所有 Event 都 set。
_ref_waiters: dict[str, list[asyncio.Event]] = _p['ref_waiters']


# ──────── 对外接口 ────────────────────────────────────────────────────────

def refresh_ref(key: str, ref_type: str, ref_value: str, appid: str = ''):
    """重置某 target 的引用配额（用户消息或按钮点击时调用）

    用户消息 → 用 msg_id 刷新；INTERACTION → 用 event_id 刷新。
    刷新会唤醒该 key 下所有正在 wait_and_consume 中阻塞的协程。
    """
    if not ref_value:
        return
    with _ref_lock:
        _active_ref[key] = {
            'ref_type': ref_type,
            'ref_value': ref_value,
            'count': 0,
            'expires_at': time.time() + ref_ttl(key),
            'appid': appid,
        }

    # 唤醒所有等待器（asyncio.Event 跨线程 set 必须走 call_soon_threadsafe）
    waiters = list(_ref_waiters.get(key, ()))
    if not waiters:
        return
    loop = state.event_loop
    if loop is None or loop.is_closed():
        return
    for ev in waiters:
        try:
            loop.call_soon_threadsafe(ev.set)
        except RuntimeError:
            pass


def try_consume_ref(key: str):
    """尝试取一次配额。

    成功返回 ``(ref_type, ref_value, count_after, appid)``;
    失败(无引用 / 已过期 / 配额已满)返回 ``None``。
    """
    with _ref_lock:
        ref = _active_ref.get(key)
        if not ref:
            return None
        if time.time() > ref['expires_at']:
            _active_ref.pop(key, None)
            return None
        if ref['count'] >= ref_quota(key):
            return None
        ref['count'] += 1
        return (ref['ref_type'], ref['ref_value'], ref['count'], ref.get('appid', ''))


def has_valid_ref(key: str) -> bool:
    """是否存在**未过期**的引用(不管配额是否已用完)。

    用来区分 ``try_consume_ref`` 返回 ``None`` 的两种原因:
      · ``True``  —— 引用存在且在 TTL 内,只是 ``count`` 已达 5(配额满);
                    此时值得等用户刷新(私信 / 群聊都按原逻辑等待 + 超时强发)
      · ``False`` —— 无引用 / 已过期;私信场景下没有有效 msg_id,主动消息正式
                    环境必拒,等待也是白等 → 调用方应直接丢弃

    顺带清掉已过期的 ref(与 ``try_consume_ref`` 的过期处理一致)。
    """
    with _ref_lock:
        ref = _active_ref.get(key)
        if not ref:
            return False
        if time.time() > ref['expires_at']:
            _active_ref.pop(key, None)
            return False
        return True


async def wait_and_consume(key: str, timeout: float = REFRESH_WAIT_TIMEOUT):
    """配额满时调用：阻塞等待 ≤ timeout 秒新引用到达，再取一次配额。

    采用「双重检查 + 私有 Event」模式避免信号丢失：
      1. 注册私有 Event 到 _ref_waiters 列表（每个等待者独立 Event）
      2. 注册后再 try_consume_ref 一次（覆盖"注册前一刻刚刚刷新"的窗口）
      3. 没拿到再真正 await Event；refresh_ref 会同时 set 所有等待者
    """
    # 注册一个属于自己的等待 Event
    ev = asyncio.Event()
    _ref_waiters.setdefault(key, []).append(ev)

    try:
        # 第二次尝试：注册后立即再试，覆盖竞态窗口
        consumed = try_consume_ref(key)
        if consumed is not None:
            return consumed

        # 真正进入等待
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        # 被唤醒，再尝试取配额
        return try_consume_ref(key)
    finally:
        # 移除自己的等待器
        lst = _ref_waiters.get(key)
        if lst is not None:
            try:
                lst.remove(ev)
            except ValueError:
                pass
            if not lst:
                _ref_waiters.pop(key, None)


def build_refresh_button(is_last: bool = False) -> list:
    """返回单按钮一行的'刷新'回调按钮（type=1，纯 callback，不回填、不发消息）

    Args:
        is_last: 是否是配额内最后一条（count == ref_quota(key)）。True 时按钮文字改为「最终刷新」配 ⚠️ 高亮，提示玩家"再不点就没机会发了"
    """
    text = '⚠️ 最终刷新' if is_last else '🔄 刷新会话'
    style = 1 if is_last else 0   # 最终按钮用主色提高视觉权重
    return [{
        'text': text,
        'data': RELAY_BUTTON_DATA,
        'type': 1,
        'style': style,
    }]
