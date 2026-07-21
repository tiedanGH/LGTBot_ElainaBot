#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""config 测试 —— ``_apply_runtime_tunables`` 的 sandbox_dm_users 双模式下发。

历史回归:一次 webui 校验重构曾把函数尾部的 sandbox_dm_users /
menu_game_buttons 应用块整段误删,``SANDBOX_DM_USERS`` 从此恒为空集合而
没有任何测试报警。本文件补上这道闸:

  · 白名单模式:列表 → frozenset,DM_PUSH_ALL 关闭
  · ``['all']``(仅此一项)→ 全员直推模式:DM_PUSH_ALL=True + 空白名单
  · ``['all', openid]`` 混入其他项 → 回退普通白名单
  · 非法 / 缺失 → 空集合 + 模式关闭
  · ['all'] → 白名单可干净回退(官方收回权限的还原路径)
  · 回归保障:函数确实写到 callbacks / buttons 模块属性(块被删则必红)
"""

from __future__ import annotations

import pytest

# conftest.py 已 inject 假 boot,这里安全 import
from plugins.LGTBot_ElainaBot.mod import (
    buttons, callbacks, config, dispatcher, quota, state,
)


def _base_cfg(**overrides) -> dict:
    """``DEFAULT_CONFIG`` 的浅拷贝(list 单独复制)+ 定向覆盖。"""
    cfg = {k: (list(v) if isinstance(v, list) else v)
           for k, v in config.DEFAULT_CONFIG.items()}
    cfg.update(overrides)
    return cfg


@pytest.fixture(autouse=True)
def _reset_tunables(monkeypatch):
    """把 ``_apply_runtime_tunables`` 会覆写的运行时全局预置成默认值 ——
    测试内随意下发,teardown 由 monkeypatch 自动还原,不串到其他测试文件。
    (uploader.SELECTED_BACKEND / URL_CACHE_TTL 由 conftest 的 autouse 复位。)"""
    monkeypatch.setattr(state, 'bind_bot_appid', '')
    monkeypatch.setattr(quota, 'REFRESH_WAIT_TIMEOUT', 15.0)
    monkeypatch.setattr(callbacks, 'CRASH_NOTIFY_GROUP', '')
    monkeypatch.setattr(callbacks, 'SANDBOX_DM_USERS', frozenset())
    monkeypatch.setattr(callbacks, 'DM_PUSH_ALL', False)
    monkeypatch.setattr(dispatcher, 'BLOCKED_COMMANDS', ())
    monkeypatch.setattr(buttons, 'MENU_GAMES', list(buttons.DEFAULT_MENU_GAMES))


def test_sandbox_whitelist_mode():
    config._apply_runtime_tunables(_base_cfg(sandbox_dm_users=['U1', ' U2 ', '']))
    assert callbacks.SANDBOX_DM_USERS == frozenset({'U1', 'U2'})
    assert callbacks.DM_PUSH_ALL is False


def test_sandbox_all_mode_enables_push_all():
    config._apply_runtime_tunables(_base_cfg(sandbox_dm_users=['all']))
    assert callbacks.DM_PUSH_ALL is True
    assert callbacks.SANDBOX_DM_USERS == frozenset()


def test_sandbox_all_mixed_with_others_falls_back_to_whitelist():
    """混入其他项时 'all' 只是普通(无效)白名单项,不触发全员模式。"""
    config._apply_runtime_tunables(_base_cfg(sandbox_dm_users=['all', 'U1']))
    assert callbacks.DM_PUSH_ALL is False
    assert callbacks.SANDBOX_DM_USERS == frozenset({'all', 'U1'})


def test_sandbox_missing_or_invalid_means_empty_and_off():
    cfg = _base_cfg()
    cfg.pop('sandbox_dm_users')
    config._apply_runtime_tunables(cfg)
    assert callbacks.SANDBOX_DM_USERS == frozenset()
    assert callbacks.DM_PUSH_ALL is False

    config._apply_runtime_tunables(_base_cfg(sandbox_dm_users='oops'))
    assert callbacks.SANDBOX_DM_USERS == frozenset()
    assert callbacks.DM_PUSH_ALL is False


def test_all_mode_reverts_cleanly_to_whitelist():
    """「官方收回权限」回退路径:['all'] → 白名单,一次热重载整体还原。"""
    config._apply_runtime_tunables(_base_cfg(sandbox_dm_users=['all']))
    assert callbacks.DM_PUSH_ALL is True

    config._apply_runtime_tunables(_base_cfg(sandbox_dm_users=['U9']))
    assert callbacks.DM_PUSH_ALL is False
    assert callbacks.SANDBOX_DM_USERS == frozenset({'U9'})


def test_apply_reaches_function_tail_regression_guard():
    """回归保障:sandbox / menu 两个块真的在函数体内(整段被删时此测必红)。"""
    config._apply_runtime_tunables(_base_cfg(
        sandbox_dm_users=['GUARD_U'],
        menu_game_buttons=['游戏A', '游戏B'],
    ))
    assert callbacks.SANDBOX_DM_USERS == frozenset({'GUARD_U'})
    assert buttons.MENU_GAMES == ['游戏A', '游戏B']
