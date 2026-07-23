#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""dispatcher 测试 —— 核心是 refresh_ref 三处互斥分支(msg_id 越权 fix 回归保障)。

历史 bug:消息派发 + INTERACTION relay + INTERACTION dispatch 三处的 refresh_ref
逻辑曾用「两个独立 if」(群分支 + 用户分支),导致群里 @bot 产生的 msg_id 也被写
进 ``u:<uid>``,后续给该用户私信尝试用该(实际属于群场景的)凭据被 QQ 拒绝。
修复后改成 ``if event.is_group ... elif event.is_direct ...`` 互斥 + 显式守卫。

本测试覆盖:
  · 群消息事件 → 只刷 g:<gid>,**不污染 u:<uid>**(关键)
  · 私信事件   → 只刷 u:<uid>,不污染 g:<gid>
  · state.started=False → 整个 handler 跳过
  · INTERACTION relay 同上互斥
  · INTERACTION dispatch 同上互斥
  · is_at_self 守卫挡住全量群日常对话
  · _is_blocked_command:内置屏蔽项(斜杠不敏感 + 数字连写参数)与配置追加项(斜杠严格)的匹配语义
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.LGTBot_ElainaBot.mod import dispatcher, quota, state as _state


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


def _mock_event(*, event_type=None, is_group=False, is_direct=False,
                group_id='', user_id='', channel_id='', message_id='',
                event_id='', content='/hello', appid='APPID_X',
                is_at_self=True, username='Tester'):
    """构造一个 MagicMock event,字段够 handler 跑完函数体。"""
    ev = MagicMock()
    ev.event_type = event_type or dispatcher.GROUP_AT_MESSAGE_CREATE
    ev.is_group = is_group
    ev.is_direct = is_direct
    ev.group_id = group_id
    ev.user_id = user_id
    ev.channel_id = channel_id
    ev.message_id = message_id
    ev.event_id = event_id
    ev.content = content
    ev.appid = appid
    ev.is_at_self = is_at_self
    ev.username = username
    ev.is_interaction = False
    # ack_interaction 是 async,MagicMock 默认返 MagicMock 不可 await,要明确 AsyncMock
    ev.ack_interaction = AsyncMock()
    return ev


@pytest.fixture
def patched_downstream():
    """patch lgtbot_dispatch / lgtbot_interaction_* 用到的下游,让函数体能跑完。

    关键点:**不 patch quota.refresh_ref**(那是被测目标),其他副作用(userdb /
    page_logs / _send_welcome_menu / threading.Thread)全 patch 成 noop。
    """
    # threading.Thread 起线程跑 boot.LGTBot_ElainaBot.on_public_message 等
    # —— boot 已是 fake (conftest),on_public_message 是 MagicMock,调用安全;
    # 但起真线程拖慢测试,直接 patch Thread.start 为 noop
    with patch.object(dispatcher, '_send_welcome_menu', new=AsyncMock()) as _swm, \
         patch.object(dispatcher.userdb, 'mark_dirty') as _md, \
         patch.object(dispatcher.page_logs, 'log_incoming') as _li, \
         patch.object(dispatcher.threading.Thread, 'start') as _ts:
        yield {
            '_send_welcome_menu': _swm,
            'mark_dirty': _md,
            'log_incoming': _li,
            'thread_start': _ts,
        }


# ─────────────────────────────────────────────────────────────────────────
# 1-2. 消息派发 lgtbot_dispatch: refresh_ref 互斥(msg_id 越权 fix)
# ─────────────────────────────────────────────────────────────────────────


async def test_dispatch_group_msg_only_refreshes_group_key(patched_downstream):
    """群消息 @bot → 只刷 g:<gid>,**绝不污染 u:<uid>**。

    复现回归:若旧 bug 回来(两个独立 if 而非 elif),u:USER_Y 也会出现 ref,
    导致后续给 USER_Y 发私信用群 msg_id 被 QQ 拒。
    """
    _state.started = True

    event = _mock_event(
        event_type=dispatcher.GROUP_AT_MESSAGE_CREATE,
        is_group=True, is_direct=False,
        group_id='GROUP_X', user_id='USER_Y',
        message_id='MSG_AAA',
        is_at_self=True,
    )

    await dispatcher.lgtbot_dispatch(event, None)

    assert 'g:GROUP_X' in quota._active_ref
    assert quota._active_ref['g:GROUP_X']['ref_value'] == 'MSG_AAA'
    # 关键回归断言:u:USER_Y 必须不存在
    assert 'u:USER_Y' not in quota._active_ref


async def test_dispatch_direct_msg_only_refreshes_user_key(patched_downstream):
    """私信事件 → 只刷 u:<uid>,不污染任何 g:..."""
    _state.started = True

    event = _mock_event(
        event_type=dispatcher.C2C_MESSAGE_CREATE,
        is_group=False, is_direct=True,
        group_id='', user_id='USER_DM',
        message_id='DM_MSG_BBB',
    )

    await dispatcher.lgtbot_dispatch(event, None)

    assert 'u:USER_DM' in quota._active_ref
    assert quota._active_ref['u:USER_DM']['ref_value'] == 'DM_MSG_BBB'
    # 不应出现任何 g: 前缀
    assert not any(k.startswith('g:') for k in quota._active_ref)


# ─────────────────────────────────────────────────────────────────────────
# 3. state.started=False 时整个 handler 跳过
# ─────────────────────────────────────────────────────────────────────────


async def test_dispatch_skips_when_not_started(patched_downstream):
    """state.started=False(引擎崩溃 30s 窗口)时,handler 直接 return,
    不应调 refresh_ref,也不应起线程。"""
    _state.started = False

    event = _mock_event(
        is_group=True, is_direct=False,
        group_id='GROUP_Z', message_id='MSG_X',
    )

    await dispatcher.lgtbot_dispatch(event, None)

    # 整个 handler 早返,refresh_ref 不会被调到 → _active_ref 仍空
    assert quota._active_ref == {}
    patched_downstream['thread_start'].assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# 4-5. INTERACTION relay: event_id 互斥
# ─────────────────────────────────────────────────────────────────────────


async def test_interaction_relay_group_only_event_id_to_group(patched_downstream):
    """群内点「🔄 刷新会话」按钮 → 只刷 g:<gid> 的 event_id,不污染 u:<uid>"""
    event = _mock_event(
        is_group=True, is_direct=False,
        group_id='GROUP_R', user_id='USER_R',
        event_id='EVENT_RELAY_AAA',
    )

    await dispatcher.lgtbot_interaction_relay(event, None)

    assert 'g:GROUP_R' in quota._active_ref
    assert quota._active_ref['g:GROUP_R']['ref_type'] == 'event_id'
    assert quota._active_ref['g:GROUP_R']['ref_value'] == 'EVENT_RELAY_AAA'
    assert 'u:USER_R' not in quota._active_ref


async def test_interaction_relay_direct_only_event_id_to_user(patched_downstream):
    """私信里点刷新按钮 → 只刷 u:<uid> 的 event_id"""
    event = _mock_event(
        is_group=False, is_direct=True,
        group_id='', user_id='USER_DM_R',
        event_id='EVENT_DM_RELAY',
    )

    await dispatcher.lgtbot_interaction_relay(event, None)

    assert 'u:USER_DM_R' in quota._active_ref
    assert quota._active_ref['u:USER_DM_R']['ref_value'] == 'EVENT_DM_RELAY'
    assert not any(k.startswith('g:') for k in quota._active_ref)


# ─────────────────────────────────────────────────────────────────────────
# 6. INTERACTION dispatch(非刷新按钮):同样互斥
# ─────────────────────────────────────────────────────────────────────────


async def test_interaction_dispatch_mutex_branches(patched_downstream):
    """非刷新 callback 按钮的 data 派发,event_id 也必须按 group/direct 互斥写入"""
    _state.started = True

    # 群内场景
    event_grp = _mock_event(
        is_group=True, is_direct=False,
        group_id='GROUP_D', user_id='USER_GD',
        event_id='EV_D_AAA',
        content='/帮助',
    )
    await dispatcher.lgtbot_interaction_dispatch(event_grp, None)

    assert 'g:GROUP_D' in quota._active_ref
    assert 'u:USER_GD' not in quota._active_ref


# ─────────────────────────────────────────────────────────────────────────
# 7. is_at_self 守卫:全量群里非 @bot 消息被挡
# ─────────────────────────────────────────────────────────────────────────


async def test_dispatch_at_self_guard_blocks_group_chitchat(patched_downstream):
    """GROUP_MESSAGE_CREATE 事件 + is_at_self=False(用户没 @bot)→ 整个 handler
    早返,refresh_ref 不应被调,引擎也不该派发。"""
    _state.started = True

    event = _mock_event(
        event_type=dispatcher.GROUP_MESSAGE_CREATE,
        is_group=True, is_direct=False,
        group_id='FULL_GROUP', user_id='CHITCHAT_USER',
        message_id='MSG_NONAT',
        content='今天天气真好',
        is_at_self=False,    # ← 关键:用户没 @ bot
    )

    await dispatcher.lgtbot_dispatch(event, None)

    # is_at_self 守卫应让 handler 在 refresh_ref 之前就 return
    assert quota._active_ref == {}
    patched_downstream['thread_start'].assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# 8. _is_blocked_command:内置屏蔽项 + 配置追加项
# ─────────────────────────────────────────────────────────────────────────


def test_builtin_blocked_commands_cover_all_plugin_forms():
    """内置指令的全部真实触发形态都要命中:裸 / 带斜杠 / 空白参数 /
    无空格数字连写参数(全量申请、dau 的参数正则是 ``\\s*`` 空格可选,
    主框架派发又做斜杠互换匹配 —— 这些形态都会被对应插件应答)。"""
    hits = (
        'dau', '/dau', 'dau 0503', 'dau0503',
        '全量申请', '/全量申请', '全量申请 123456789', '全量申请123456789',
        '全量列表', '/全量列表',
        '关闭欢迎', '/关闭欢迎', '开启欢迎', '/开启欢迎',
    )
    for text in hits:
        assert dispatcher._is_blocked_command(text), f'应命中却漏过: {text!r}'

    misses = ('', 'daux', 'dau测试', '全量', '全量列表们', '开启', '关闭欢迎吧', '新游戏 五子棋')
    for text in misses:
        assert not dispatcher._is_blocked_command(text), f'不应命中却挡了: {text!r}'


def test_config_blocked_commands_keep_strict_slash(monkeypatch):
    """配置追加项维持原严格语义:斜杠按配置原样匹配,不做互换;
    「指令 + 空白 + 参数」命中,数字连写**不**命中(与内置项的宽松规则区分)。"""
    monkeypatch.setattr(dispatcher, 'BLOCKED_COMMANDS', ('帮助', '/规则'))

    assert dispatcher._is_blocked_command('帮助')
    assert dispatcher._is_blocked_command('帮助 xxx')
    assert not dispatcher._is_blocked_command('/帮助')     # 配置无斜杠,不通配
    assert not dispatcher._is_blocked_command('帮助123')   # 数字连写仅内置项放行

    assert dispatcher._is_blocked_command('/规则')
    assert not dispatcher._is_blocked_command('规则')      # 配置带斜杠,只挡带斜杠

    # 内置项不受配置影响,依旧生效
    assert dispatcher._is_blocked_command('dau')


def test_data_stats_command_is_exclusive():
    """/数据统计 必须在独占表内 —— 否则 catch-all 会把它二次派发进引擎。"""
    assert dispatcher._is_exclusive_command('数据统计')
    assert dispatcher._is_exclusive_command('/数据统计')
    assert not dispatcher._is_exclusive_command('数据统计2')   # 不误伤带参形态


# ─────────────────────────────────────────────────────────────────────────
# 9. _capture_pending_game_name:单机局游戏名兜底(供 dashboard / 重开按钮)
# ─────────────────────────────────────────────────────────────────────────


def test_capture_pending_game_name_group_and_dm():
    """「/新游戏 X …」记 pending 游戏名(第一个 token,群 / 私聊各自 key);
    裸「/新游戏」与「/随机游戏」不记。"""
    _state.pending_new_game_name.clear()
    ev_g = _mock_event(is_group=True, group_id='G1')
    dispatcher._capture_pending_game_name('/新游戏 决胜五子 单机', ev_g, 'G1', 'U1')
    assert _state.pending_new_game_name.get('g:G1') == '决胜五子'   # 只取第一个 token

    ev_d = _mock_event(is_direct=True, user_id='U1')
    dispatcher._capture_pending_game_name('/新游戏 炼金术士', ev_d, '', 'U1')
    assert _state.pending_new_game_name.get('u:U1') == '炼金术士'

    _state.pending_new_game_name.clear()
    dispatcher._capture_pending_game_name('/新游戏', ev_g, 'G1', 'U1')      # 裸命令无名
    dispatcher._capture_pending_game_name('/随机游戏', ev_g, 'G1', 'U1')    # 随机游戏无名
    assert _state.pending_new_game_name == {}
