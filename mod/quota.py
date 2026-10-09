#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""被动消息引用配额管理（绕过 QQ 单条消息被动回复条数限制）

QQ 协议事实（官方文档「被动消息」表,按场景区分）：
  · 群聊:每个 msg_id 可被回复 **5** 次（msg_seq=1..5），**5 分钟**后过期
  · 单聊:每个 msg_id 可被回复 **4** 次，**60 分钟**后过期
  · 每个 event_id（INTERACTION 等）独立计一轮配额（按所在场景取上限）
  · 在消息上挂 callback 按钮，用户点击 → 新 INTERACTION_CREATE → 新 event_id
    → 又获得一轮新配额，从而绕过单引用条数的硬限制
  · 被动回复不占主动消息频控(单群 20 条/分钟、bot 总量 60 条/分钟)

本模块策略：
  · 每个目标一个**引用池**:TTL 内的 msg_id / event_id 各自计次,发送时先用最快过期的那条。
    新引用不覆盖旧引用剩下的次数;全量群的日常聊天也登记进来(见 dispatcher.lgtbot_dispatch)
  · 倒数第 2 条起自动追加「🔄 刷新」按钮（type=1 callback;按池内剩余次数:剩 1 条 🔄,剩 0 条 ⚠️）
  · 用户点击 → ACK + 立即登记新引用 + 唤醒可能在等待的发送协程
  · 发送时若配额满，最长等待 15s 等待新刷新事件再重试
  · QQ 判某条引用已失效时,发送出口用 ``drop_ref`` 把它移出池子

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
REF_POOL_MAX = 32                # 单个目标同时保留的引用数;32 条 × 5 次
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
# value = [ref, ...] 按登记先后(即 expires_at 升序)排列,
#         ref = {'ref_type': 'msg_id'|'event_id', 'ref_value', 'count', 'expires_at', 'appid'}
#
# 跨重载共享：取自 boot._get_persistent()，挂在 C++ 扩展上常驻进程；
# 旧 callback 与新 dispatcher 操作同一份字典，热重载不会丢配额状态。
_p = boot._get_persistent()
_active_ref: dict[str, list[dict]] = _p['active_ref']
_ref_lock = threading.Lock()


class _Pool(list):
    """引用池。按字符串键读写时代理到**最新**那条引用。"""

    def __getitem__(self, k):
        return self[-1][k] if isinstance(k, str) else super().__getitem__(k)

    def __setitem__(self, k, v):
        if isinstance(k, str):
            self[-1][k] = v
        else:
            super().__setitem__(k, v)

    def get(self, k, default=None):
        return self[-1].get(k, default) if self else default


def _live_pool(key: str, now: float) -> _Pool:
    """取 key 的引用池并剔除过期项,池空时连 key 一起删。调用方须持有 ``_ref_lock``。"""
    pool = _active_ref.get(key)
    if isinstance(pool, dict):          # 热重载前的旧结构:每个目标只存一条引用
        pool = [pool]
    live = _Pool(r for r in pool or () if now <= r['expires_at'])
    if live:
        _active_ref[key] = live
    else:
        _active_ref.pop(key, None)
    return live

# 等待器：每个等待中的协程持有独立 asyncio.Event，避免共享 Event 时 ev.clear()
# 擦掉刚到达的信号导致死等。refresh_ref 时把 list 内所有 Event 都 set。
_ref_waiters: dict[str, list[asyncio.Event]] = _p['ref_waiters']


# ──────── 对外接口 ────────────────────────────────────────────────────────

def refresh_ref(key: str, ref_type: str, ref_value: str, appid: str = ''):
    """把一条新引用登记进某 target 的引用池（用户消息或按钮点击时调用）

    用户消息 → msg_id；INTERACTION → event_id。池满时挤掉最早登记的那条。
    登记会唤醒该 key 下所有正在 wait_and_consume 中阻塞的协程。
    """
    if not ref_value:
        return
    with _ref_lock:
        now = time.time()
        pool = _live_pool(key, now)
        # 同一条消息可能被多个 handler 各登记一次;重置计数会让它多回复几次 → 被拒
        if any(r['ref_value'] == ref_value for r in pool):
            return
        pool.append({
            'ref_type': ref_type,
            'ref_value': ref_value,
            'count': 0,
            'expires_at': now + ref_ttl(key),
            'appid': appid,
        })
        del pool[:-REF_POOL_MAX]
        _active_ref[key] = pool

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
    """尝试取一次配额:池里**最快过期**、还有次数的那条引用计一次(先把快作废的额度用掉)。

    成功返回 ``(ref_type, ref_value, used, appid)``。``used`` 按「只有一条引用」的口径折算:
    ``ref_quota - 池内剩余次数``(下限 0)。池里只剩 1 次时为 4(群)/ 3(私信),用完时为 5 / 4,
    调用方据此在倒数第 2 条起挂刷新按钮。
    失败(无引用 / 全部过期 / 次数全部用完)返回 ``None``。
    """
    with _ref_lock:
        pool = _live_pool(key, time.time())
        q = ref_quota(key)
        for ref in pool:
            if ref['count'] < q:
                ref['count'] += 1
                left = sum(max(0, q - r['count']) for r in pool)
                return (ref['ref_type'], ref['ref_value'], max(0, q - left), ref.get('appid', ''))
        return None


def has_valid_ref(key: str) -> bool:
    """是否存在**未过期**的引用(不管配额是否已用完)。

    用来区分 ``try_consume_ref`` 返回 ``None`` 的两种原因:
      · ``True``  —— 池里有 TTL 内的引用,只是次数全部用完(配额满);
                    此时值得等用户刷新(私信 / 群聊都按原逻辑等待 + 超时强发)
      · ``False`` —— 无引用 / 全部过期(次数没用完也一样:QQ 只认 TTL 内的 msg_id / event_id)。
                    此时等刷新没有可续命的对象,而没有主动推送资格的目标(普通私信 / 无主动推送权限的群)
                    主动消息必被拒 → 调用方应直接丢弃,别白等 15s 也别白烧一次调用

    顺带清掉已过期的 ref(与 ``try_consume_ref`` 的过期处理一致)。
    """
    with _ref_lock:
        return bool(_live_pool(key, time.time()))


def drop_ref(key: str, ref_value: str) -> None:
    """QQ 已不认这条引用(过期 / 越权 / 次数被其他入口用掉)→ 移出池子,下次发送换下一条。"""
    with _ref_lock:
        pool = _live_pool(key, time.time())
        pool[:] = [r for r in pool if r['ref_value'] != ref_value]
        if not pool:
            _active_ref.pop(key, None)


def mark_used(key: str, ref_value: str) -> None:
    """记一次**绕过本模块**的被动回复(``event.reply``)—— QQ 按引用计总数,不区分发送入口。"""
    with _ref_lock:
        for ref in _live_pool(key, time.time()):
            if ref['ref_value'] == ref_value:
                ref['count'] = min(ref['count'] + 1, ref_quota(key))
                return


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
