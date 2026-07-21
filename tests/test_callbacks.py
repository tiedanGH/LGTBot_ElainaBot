#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""callbacks 模块测试 —— cb_match_event 的 game_over 分支 + 私信直推/丢弃。

被测行为:
  · 结算广播(kind='game_over')在 pending_buttons 挂「📊 查看战绩 +
    🔄 重开一局」,重开按钮 data 为 ``/新游戏 <当前游戏名>``
  · 游戏名从 state.current_game 回查并**取完即清**(对局已随结算释放)
  · current_game 无记录时退化为仅「查看战绩」单按钮
  · kind='game_over_unrecorded'(结算带「游戏结果不记录」)不挂「查看
    战绩」;此时游戏名也未知则整组不挂(pending_buttons 无该 key)
  · 只动本 target 的状态,不串别的群/用户
  · ``_send_text_quota_managed`` 私信无有效引用时的三种模式:白名单外
    丢弃(老逻辑)/ DM_PUSH_ALL 全员主动直推 / 白名单命中直推
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

# conftest.py 已 inject 假 boot,这里安全 import
from plugins.LGTBot_ElainaBot.mod import callbacks, state


def _flat_buttons(key: str) -> list[dict]:
    rows = state.pending_buttons.get(key) or []
    return [b for row in rows for b in row]


def test_match_event_game_over_attaches_buttons_and_clears_game():
    state.current_game['g:123'] = '差值投标'

    callbacks.cb_match_event('123', False, 'game_over', '')

    by_data = {b['data']: b for b in _flat_buttons('g:123')}
    # 两个按钮:查看战绩(style=1) + 重开一局(style=4),都是 type=2 回填输入框
    assert by_data['/战绩']['style'] == 1
    assert by_data['/战绩']['type'] == 2
    assert by_data['/新游戏 差值投标']['style'] == 4
    assert by_data['/新游戏 差值投标']['type'] == 2
    # 本插件按钮约定:不带 enter 字段(button_enter_to_send 会把 type=2+enter 转 type=1)
    assert all('enter' not in b for b in by_data.values())
    # 游戏名取完即清
    assert 'g:123' not in state.current_game


def test_match_event_game_over_without_known_game_only_record_button():
    assert 'u:9' not in state.current_game

    callbacks.cb_match_event('9', True, 'game_over', '')

    datas = [b['data'] for b in _flat_buttons('u:9')]
    assert datas == ['/战绩']


def test_match_event_game_over_unrecorded_omits_record_button():
    """「游戏结果不记录」的结算(单机 / 非正式局)—— 本局没进战绩,只挂重开。"""
    state.current_game['g:55'] = '差值投标'

    callbacks.cb_match_event('55', False, 'game_over_unrecorded', '')

    datas = [b['data'] for b in _flat_buttons('g:55')]
    assert datas == ['/新游戏 差值投标']
    assert 'g:55' not in state.current_game


def test_match_event_game_over_unrecorded_without_game_no_buttons():
    """不记录 + 游戏名也未知 —— 两个按钮都凑不齐,整组不挂。"""
    assert 'u:7' not in state.current_game

    callbacks.cb_match_event('7', True, 'game_over_unrecorded', '')

    assert 'u:7' not in state.pending_buttons


def test_match_event_game_over_does_not_touch_other_targets():
    state.current_game['g:1'] = '差值投标'
    state.current_game['g:2'] = '田忌赛马'

    callbacks.cb_match_event('1', False, 'game_over', '')

    assert 'g:1' not in state.current_game
    assert state.current_game.get('g:2') == '田忌赛马'
    assert 'g:2' not in state.pending_buttons


# ─────────────────────────────────────────────────────────────────────────
# 私信主动直推 / 丢弃:_send_text_quota_managed 的模式分支
# (conftest 每测清空 quota._active_ref → 私信必然处于「无有效引用」状态,
#  正好落在 直推 vs 丢弃 的决策点上)
# ─────────────────────────────────────────────────────────────────────────


def _fake_sender():
    sender = MagicMock()
    sender.send_to_user = AsyncMock()
    sender.send_to_group = AsyncMock()
    return sender


def _patch_send_env(monkeypatch, sender, *, push_all, whitelist=frozenset()):
    monkeypatch.setattr(callbacks, 'DM_PUSH_ALL', push_all)
    monkeypatch.setattr(callbacks, 'SANDBOX_DM_USERS', whitelist)
    monkeypatch.setattr(callbacks.helpers, 'get_sender', lambda appid='': sender)


async def test_dm_without_ref_dropped_in_legacy_mode(monkeypatch):
    """老逻辑回归:白名单外私信 + 无有效 msg_id → 丢弃,不触发任何发送。"""
    sender = _fake_sender()
    _patch_send_env(monkeypatch, sender, push_all=False)

    await callbacks._send_text_quota_managed('USER_X', True, 'hello', None)

    sender.send_to_user.assert_not_called()


async def test_dm_without_ref_pushed_in_all_mode(monkeypatch):
    """['all'] 全员直推:同样场景改走主动消息(kwargs 无 msg_id/event_id)。"""
    sender = _fake_sender()
    _patch_send_env(monkeypatch, sender, push_all=True)

    await callbacks._send_text_quota_managed('USER_X', True, 'hello', None)

    sender.send_to_user.assert_awaited_once()
    args, kwargs = sender.send_to_user.call_args
    assert args == ('USER_X', 'hello')
    assert 'msg_id' not in kwargs and 'event_id' not in kwargs


async def test_dm_whitelist_member_pushed_others_dropped(monkeypatch):
    """白名单模式(老语义):命中的用户直推,未命中的仍丢弃。"""
    sender = _fake_sender()
    _patch_send_env(monkeypatch, sender, push_all=False,
                    whitelist=frozenset({'SANDBOX_U'}))

    await callbacks._send_text_quota_managed('SANDBOX_U', True, 'hi', None)
    sender.send_to_user.assert_awaited_once()

    sender.send_to_user.reset_mock()
    await callbacks._send_text_quota_managed('OTHER_U', True, 'hi', None)
    sender.send_to_user.assert_not_called()
