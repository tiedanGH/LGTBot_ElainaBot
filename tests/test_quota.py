#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""quota 模块测试 —— 配额状态机 / 异步等待 / 多 waiter 唤醒 race。

被测重点(对应 plan 的 11 个 case):
  · refresh_ref + try_consume_ref 基本路径
  · try_consume_ref 三种 None 原因:无 ref / 过期 / 配额满
  · has_valid_ref **区分配额满 vs 无 ref**(msg_id 越权前置 — 这是关键
    历史 bug fix:必须能区分"配额满但 5min 内 ref 仍有效"和"完全没 ref",
    前者值得等刷新,后者私信场景应直接丢弃)
  · wait_and_consume 唤醒 + 超时
  · 多 waiter 并发唤醒(per-waiter Event 避免共享 Event 信号丢失)
"""

from __future__ import annotations

import asyncio
import time

import pytest

# 注意:conftest.py 已 inject 假 boot,这里 import 不会触发真 C++ 加载
from plugins.LGTBot_ElainaBot.mod import quota
from plugins.LGTBot_ElainaBot.mod import state as _state


# ─────────────────────────────────────────────────────────────────────────
# 同步测试:refresh / try_consume / has_valid_ref 配额状态机
# ─────────────────────────────────────────────────────────────────────────


def test_refresh_ref_sets_active_ref():
    """refresh_ref 写入后,try_consume_ref 能拿到完整 4-tuple"""
    quota.refresh_ref('g:group1', 'msg_id', 'MSG_AAA', appid='app1')

    consumed = quota.try_consume_ref('g:group1')
    assert consumed is not None
    ref_type, ref_value, count, appid = consumed
    assert ref_type == 'msg_id'
    assert ref_value == 'MSG_AAA'
    assert count == 1
    assert appid == 'app1'


def test_try_consume_ref_missing_returns_none():
    """从未 refresh 过的 key,try_consume_ref 返回 None"""
    assert quota.try_consume_ref('u:never_seen') is None


def test_try_consume_ref_expired_returns_none_and_reaps():
    """过期 ref 返回 None,且**自动从 _active_ref 字典里清掉**"""
    quota.refresh_ref('g:gx', 'msg_id', 'msg_x')
    # 手动把 expires_at 设到过去
    quota._active_ref['g:gx']['expires_at'] = time.time() - 1

    assert quota.try_consume_ref('g:gx') is None
    # 过期 ref 应该被 try_consume_ref 顺手清掉
    assert 'g:gx' not in quota._active_ref


def test_try_consume_ref_quota_exhausted_returns_none():
    """配额满 (count >= REF_QUOTA) 时返回 None,但 ref 仍在字典里"""
    quota.refresh_ref('g:gy', 'msg_id', 'msg_y')
    # 消费 REF_QUOTA 次,此时 count == REF_QUOTA
    for _ in range(quota.REF_QUOTA):
        assert quota.try_consume_ref('g:gy') is not None

    # 第 REF_QUOTA+1 次:配额耗尽 → None,但 ref 还在字典(关键,跟过期分支区分)
    assert quota.try_consume_ref('g:gy') is None
    assert 'g:gy' in quota._active_ref
    assert quota._active_ref['g:gy']['count'] == quota.REF_QUOTA


def test_has_valid_ref_distinguishes_full_vs_missing():
    """**关键 msg_id 越权前置区分** —— has_valid_ref 必须能区分:
       · 配额满但 ref 在 5min 内 → True(值得等刷新)
       · 无 ref / 已过期       → False(无效 msg_id,私信发主动消息必拒,应丢弃)
    """
    # 场景 A:完全没 ref
    assert quota.has_valid_ref('u:nobody') is False

    # 场景 B:有 ref 且配额未满 → True
    quota.refresh_ref('u:alice', 'msg_id', 'msg_a')
    assert quota.has_valid_ref('u:alice') is True

    # 场景 C:配额耗尽但 ref 在 TTL 内 → 仍然 True
    for _ in range(quota.REF_QUOTA):
        quota.try_consume_ref('u:alice')
    assert quota.try_consume_ref('u:alice') is None  # 确认配额已满
    assert quota.has_valid_ref('u:alice') is True    # ← 关键:仍 True


def test_has_valid_ref_reaps_expired():
    """has_valid_ref 调用时也清过期 ref,与 try_consume_ref 行为一致"""
    quota.refresh_ref('u:zombie', 'msg_id', 'm')
    quota._active_ref['u:zombie']['expires_at'] = time.time() - 1

    assert quota.has_valid_ref('u:zombie') is False
    assert 'u:zombie' not in quota._active_ref


def test_refresh_ref_with_empty_value_noop():
    """ref_value='' 是非法输入,refresh_ref 直接返回不写入"""
    quota.refresh_ref('g:emptyval', 'msg_id', '')
    assert 'g:emptyval' not in quota._active_ref


def test_build_refresh_button_last_flag():
    """is_last=True 时文案变「⚠️ 最终刷新」+ style=1(主色高亮)"""
    normal = quota.build_refresh_button(is_last=False)
    assert isinstance(normal, list) and len(normal) == 1
    assert '🔄' in normal[0]['text']
    assert normal[0]['style'] == 0
    assert normal[0]['type'] == 1
    assert normal[0]['data'] == quota.RELAY_BUTTON_DATA

    last = quota.build_refresh_button(is_last=True)
    assert '⚠️' in last[0]['text'] and '最终' in last[0]['text']
    assert last[0]['style'] == 1


# ─────────────────────────────────────────────────────────────────────────
# 异步测试:wait_and_consume 唤醒 / 超时 / 多 waiter
# ─────────────────────────────────────────────────────────────────────────


async def test_wait_and_consume_wakes_on_refresh():
    """wait_and_consume 阻塞期间,refresh_ref 应能唤醒并让它拿到新配额。

    模拟「配额满 → 等刷新 → 刷新到」流程:
      1. 先把配额耗光
      2. 拉起 wait_and_consume 协程
      3. 100ms 后另一个协程 refresh_ref
      4. wait_and_consume 应返回新 ref 的 (type, value, count=1, appid)
    """
    # state.event_loop 是 refresh_ref 用来跨线程唤醒 waiter 的依据,本测试
    # 在协程里跑,直接拿 running loop 注入
    _state.event_loop = asyncio.get_running_loop()
    quota.refresh_ref('g:gw', 'msg_id', 'old', appid='a1')
    for _ in range(quota.REF_QUOTA):
        quota.try_consume_ref('g:gw')

    async def _refresher():
        await asyncio.sleep(0.05)
        quota.refresh_ref('g:gw', 'msg_id', 'new', appid='a2')

    # 并行:wait_and_consume + 0.05s 后 refresh
    waiter_task = asyncio.create_task(quota.wait_and_consume('g:gw', timeout=2.0))
    refresher_task = asyncio.create_task(_refresher())

    consumed = await waiter_task
    await refresher_task

    assert consumed is not None
    ref_type, ref_value, count, appid = consumed
    assert ref_value == 'new'      # 新刷新的 value
    assert count == 1              # 全新 ref,从 1 开始
    assert appid == 'a2'


async def test_wait_and_consume_timeout_returns_none():
    """无 refresh 在 timeout 内到达 → 返回 None"""
    _state.event_loop = asyncio.get_running_loop()
    # 没有任何 ref,直接等很短超时
    result = await quota.wait_and_consume('u:lonely', timeout=0.1)
    assert result is None
    # 测完 waiter 字典应该已清干净
    assert 'u:lonely' not in quota._ref_waiters


async def test_multiple_waiters_all_woken():
    """3 个并发 wait_and_consume 同一 key,refresh 后应**全部苏醒**。

    这是 per-waiter Event 设计(quota.py 历史 bug fix:旧实现用共享 Event,
    一个 waiter ev.clear() 后其他 waiter 死等)。
    """
    _state.event_loop = asyncio.get_running_loop()

    async def _refresher():
        await asyncio.sleep(0.05)
        quota.refresh_ref('g:multi', 'msg_id', 'new')

    # 3 个 waiter 并发等同一 key
    tasks = [
        asyncio.create_task(quota.wait_and_consume('g:multi', timeout=1.0))
        for _ in range(3)
    ]
    asyncio.create_task(_refresher())

    results = await asyncio.gather(*tasks)
    # 全部应该苏醒(返回非 None)。注意:同一 ref 配额有 REF_QUOTA=5,3 个 waiter
    # 抢同一份配额,前 5 个 ok,第 6+ 个会因 try_consume_ref 配额满返 None。
    # 这里只 3 个,都能抢到。
    assert all(r is not None for r in results), \
        f'expected all 3 waiters woken, got {results}'
    # 三个抢到的 count 应该是 1,2,3 的某种排列(并发顺序不定)
    counts = sorted(r[2] for r in results)
    assert counts == [1, 2, 3]
