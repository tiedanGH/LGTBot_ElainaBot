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


# ─────────────────────────────────────────────────────────────────────────
# 纯数字 ID 字段的 int → str 容错
# ─────────────────────────────────────────────────────────────────────────
# 历史回归:appid 是纯数字,手工 / 框架通用编辑器写成不带引号的
# ``bind_bot_appid: 102003762`` 时 yaml 解析成 int,旧读取端直接「忽略」——
# 绑定在重启后静默回退第一个 bot,看起来像绑定丢失。


def test_bind_bot_appid_unquoted_number_coerced_to_str():
    config._apply_runtime_tunables(_base_cfg(bind_bot_appid=102003762))
    assert state.bind_bot_appid == '102003762'


def test_bind_bot_appid_bool_and_garbage_still_ignored():
    """bool 是 int 子类但显然不是 appid;list 等其他类型同样保持忽略语义。"""
    config._apply_runtime_tunables(_base_cfg(bind_bot_appid=True))
    assert state.bind_bot_appid == ''
    config._apply_runtime_tunables(_base_cfg(bind_bot_appid=['x']))
    assert state.bind_bot_appid == ''


def test_crash_notify_group_unquoted_number_coerced_to_str():
    config._apply_runtime_tunables(_base_cfg(crash_notify_group=987654321))
    assert callbacks.CRASH_NOTIFY_GROUP == '987654321'


def test_validator_warns_not_errors_on_numeric_id_fields():
    """webui 校验器与运行时同语义:ID 字段的裸数字放行(警告),
    其余 str 字段(image_hosting)填数字仍是错误。"""
    pytest.importorskip('aiohttp')   # page_config 顶层 import;CI 装框架依赖必有
    from plugins.LGTBot_ElainaBot.mod.webui import page_config

    errors, warnings = page_config._validate_config_yaml('bind_bot_appid: 102003762')
    assert errors == []
    assert any('bind_bot_appid' in w and '引号' in w for w in warnings)

    errors2, _w2 = page_config._validate_config_yaml('image_hosting: 123')
    assert any('image_hosting' in e for e in errors2)
