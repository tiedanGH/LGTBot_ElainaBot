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

import os

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
    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', False)


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


# ─────────────────────────────────────────────────────────────────────────
# 数值型可调字段:非法值一律「忽略并保留现值」,绝不写坏运行时
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('bad', ['abc', None, [], {}])
def test_refresh_wait_timeout_invalid_keeps_current(bad):
    config._apply_runtime_tunables(_base_cfg(refresh_wait_timeout=bad))
    assert quota.REFRESH_WAIT_TIMEOUT == 15.0


def test_refresh_wait_timeout_applies_positive_and_rejects_non_positive():
    config._apply_runtime_tunables(_base_cfg(refresh_wait_timeout='8'))   # str 数字可解析
    assert quota.REFRESH_WAIT_TIMEOUT == 8.0
    for bad in (0, -1):
        config._apply_runtime_tunables(_base_cfg(refresh_wait_timeout=bad))
        assert quota.REFRESH_WAIT_TIMEOUT == 8.0     # 保留上一次的有效值


def test_active_push_daily_limit_zero_means_unlimited_and_negative_ignored(monkeypatch):
    monkeypatch.setattr(callbacks, 'ACTIVE_PUSH_DAILY_LIMIT', 1000)
    config._apply_runtime_tunables(_base_cfg(active_push_daily_limit=0))
    assert callbacks.ACTIVE_PUSH_DAILY_LIMIT == 0        # 0 = 不限制,是合法值
    config._apply_runtime_tunables(_base_cfg(active_push_daily_limit=-5))
    assert callbacks.ACTIVE_PUSH_DAILY_LIMIT == 0        # 负数忽略
    config._apply_runtime_tunables(_base_cfg(active_push_daily_limit='250'))
    assert callbacks.ACTIVE_PUSH_DAILY_LIMIT == 250
    config._apply_runtime_tunables(_base_cfg(active_push_daily_limit='x'))
    assert callbacks.ACTIVE_PUSH_DAILY_LIMIT == 250      # 非数值保留现值


def test_image_upload_dedup_ttl_negative_clamped_to_zero():
    """负数归 0(关闭去重)而不是忽略 —— 与 timeout / limit 的语义特意不同。"""
    from plugins.LGTBot_ElainaBot.mod import uploader
    config._apply_runtime_tunables(_base_cfg(image_upload_dedup_ttl=-3))
    assert uploader.URL_CACHE_TTL == 0.0
    config._apply_runtime_tunables(_base_cfg(image_upload_dedup_ttl=30))
    assert uploader.URL_CACHE_TTL == 30.0
    config._apply_runtime_tunables(_base_cfg(image_upload_dedup_ttl='nope'))
    assert uploader.URL_CACHE_TTL == 30.0                # 非数值保留现值


def test_image_hosting_unknown_backend_disabled_when_list_known(monkeypatch):
    """图床名单能取到时,未知值禁用(空串);取不到名单则保留原值让运行时兜底。"""
    from plugins.LGTBot_ElainaBot.mod import uploader

    class _H:
        @staticmethod
        def status():
            return {'cos': {}, 'bilibili': {}}

    monkeypatch.setattr(uploader, '_get_hosting', lambda: _H)
    config._apply_runtime_tunables(_base_cfg(image_hosting=' COS '))   # 大小写 / 空格规范化
    assert uploader.SELECTED_BACKEND == 'cos'
    config._apply_runtime_tunables(_base_cfg(image_hosting='nope'))
    assert uploader.SELECTED_BACKEND == ''
    config._apply_runtime_tunables(_base_cfg(image_hosting='any'))     # any 恒合法
    assert uploader.SELECTED_BACKEND == 'any'
    # 模块未加载 → 无从校验,保留配置值(运行时 _do_upload 自行早退)
    monkeypatch.setattr(uploader, '_get_hosting', lambda: None)
    config._apply_runtime_tunables(_base_cfg(image_hosting='未知床'))
    assert uploader.SELECTED_BACKEND == '未知床'
    config._apply_runtime_tunables(_base_cfg(image_hosting=123))       # 非 str 忽略
    assert uploader.SELECTED_BACKEND == ''


# ─────────────────────────────────────────────────────────────────────────
# 列表型可调字段
# ─────────────────────────────────────────────────────────────────────────

def test_blocked_commands_normalized_dedup_preserving_order():
    config._apply_runtime_tunables(
        _base_cfg(blocked_commands=[' /抽卡 ', '/抽卡', '', '签到', 42]))
    assert dispatcher.BLOCKED_COMMANDS == ('/抽卡', '签到', '42')


def test_blocked_commands_invalid_means_builtin_only():
    """非列表 → 空表(仅内置屏蔽项生效),不保留上一次的配置。"""
    config._apply_runtime_tunables(_base_cfg(blocked_commands=['/x']))
    assert dispatcher.BLOCKED_COMMANDS == ('/x',)
    config._apply_runtime_tunables(_base_cfg(blocked_commands='not-a-list'))
    assert dispatcher.BLOCKED_COMMANDS == ()


def test_menu_game_buttons_invalid_falls_back_to_defaults():
    """菜单游戏按钮缺失 / 非法都回退默认 6 个(空菜单会让新用户无从下手)。"""
    config._apply_runtime_tunables(_base_cfg(menu_game_buttons=[' 五子棋 ', '', '大富翁']))
    assert buttons.MENU_GAMES == ['五子棋', '大富翁']
    cfg = _base_cfg()
    cfg.pop('menu_game_buttons')
    config._apply_runtime_tunables(cfg)
    assert buttons.MENU_GAMES == list(buttons.DEFAULT_MENU_GAMES)
    config._apply_runtime_tunables(_base_cfg(menu_game_buttons='五子棋'))
    assert buttons.MENU_GAMES == list(buttons.DEFAULT_MENU_GAMES)


def test_sponsor_enabled_accepts_only_real_bool():
    """yaml 里 'true' / 1 这类近似值一律非法并保留现值 —— 赞助入口默认关,不能被一个手滑的字符串意外打开。"""
    config._apply_runtime_tunables(_base_cfg(sponsor_enabled=True))
    assert buttons.SPONSOR_ENABLED is True
    config._apply_runtime_tunables(_base_cfg(sponsor_enabled='true'))
    assert buttons.SPONSOR_ENABLED is True          # 非法 → 保留现值(仍是 True)
    config._apply_runtime_tunables(_base_cfg(sponsor_enabled=False))
    assert buttons.SPONSOR_ENABLED is False
    cfg = _base_cfg()
    cfg.pop('sponsor_enabled')                       # 缺失 → 显式关闭
    config._apply_runtime_tunables(cfg)
    assert buttons.SPONSOR_ENABLED is False


# ─────────────────────────────────────────────────────────────────────────
# load_plugin_config —— admin 串解析 + ADMIN_UIDS 下发
# ─────────────────────────────────────────────────────────────────────────

class _FakeCtx:
    """PluginContext 替身:ensure_config 返回给定 dict(或抛异常)。"""

    def __init__(self, cfg=None, raises=False):
        self._cfg = cfg
        self._raises = raises
        self.calls: list = []

    def ensure_config(self, defaults, filename='', comments=None):
        self.calls.append(filename)
        if self._raises:
            raise RuntimeError('ensure boom')
        return self._cfg


def test_load_plugin_config_parses_admins_and_publishes_tuple(monkeypatch):
    ctx = _FakeCtx(_base_cfg(admin_uids=[' U1 ', 'U2', '', 42]))
    monkeypatch.setattr(config, '_get_ctx', lambda: ctx)
    assert config.load_plugin_config() == 'U1,U2,42'
    assert config.ADMIN_UIDS == ('U1', 'U2', '42')   # dispatcher 的 %中断 代理要读它
    assert ctx.calls == ['config.yaml']


def test_load_plugin_config_bad_admin_uids_degrade_to_empty(monkeypatch):
    """admin_uids 非列表 → 空(不阻断启动,引擎拿到空 admin 串照常跑)。"""
    monkeypatch.setattr(config, '_get_ctx',
                        lambda: _FakeCtx(_base_cfg(admin_uids='U1')))
    assert config.load_plugin_config() == ''
    assert config.ADMIN_UIDS == ()


def test_load_plugin_config_survives_ctx_failures(monkeypatch):
    """ensure_config 抛异常 / ctx 完全不可用 → 退回 DEFAULT_CONFIG,
    仍要把默认值下发到运行时(不能因为配置读不到就让插件半死不活)。"""
    monkeypatch.setattr(quota, 'REFRESH_WAIT_TIMEOUT', 999.0)
    monkeypatch.setattr(config, '_get_ctx', lambda: _FakeCtx(raises=True))
    assert config.load_plugin_config() == ''
    assert quota.REFRESH_WAIT_TIMEOUT == config.DEFAULT_CONFIG['refresh_wait_timeout']

    monkeypatch.setattr(quota, 'REFRESH_WAIT_TIMEOUT', 999.0)
    monkeypatch.setattr(config, '_get_ctx', lambda: None)
    assert config.load_plugin_config() == ''
    assert quota.REFRESH_WAIT_TIMEOUT == config.DEFAULT_CONFIG['refresh_wait_timeout']


def test_default_config_and_comments_cover_same_fields():
    """每个字段都得有注释 —— ensure_config 生成的模板是用户唯一的字段说明。"""
    assert set(config.DEFAULT_CONFIG) == set(config.CONFIG_COMMENTS)


def test_image_hosting_defaults_to_any():
    """新装 / 老配置缺字段时默认 ``any``:自动依次尝试全部可用图床,开箱即走
    markdown 内嵌通道。'any' 不在任何图床名单里,必须被校验器与运行时都当作
    合法值放行(而不是当未知图床禁用)。"""
    from plugins.LGTBot_ElainaBot.mod import uploader
    assert config.DEFAULT_CONFIG['image_hosting'] == 'any'

    class _H:
        @staticmethod
        def status():
            return {'cos': {}}

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(uploader, '_get_hosting', lambda: _H)
        config._apply_runtime_tunables(_base_cfg())
        assert uploader.SELECTED_BACKEND == 'any'
    finally:
        monkeypatch.undo()

    pytest.importorskip('aiohttp')
    from plugins.LGTBot_ElainaBot.mod.webui import page_config
    errors, _w = page_config._validate_config_yaml("image_hosting: 'any'")
    assert not any('image_hosting' in e for e in errors)


# ─────────────────────────────────────────────────────────────────────────
# persist_bind_bot_appid —— 行级替换保注释
# ─────────────────────────────────────────────────────────────────────────

def _cfg_path():
    from plugins.LGTBot_ElainaBot.mod import boot
    return os.path.join(boot.DATA_DIR, 'config.yaml')


@pytest.fixture
def _cfg_file():
    """每个用例前后清掉 tmp data/config.yaml。"""
    p = _cfg_path()

    def _wipe():
        try:
            os.remove(p)
        except OSError:
            pass
    _wipe()
    yield p
    _wipe()


def test_persist_bind_replaces_line_and_keeps_comments(_cfg_file):
    """行级替换而非 yaml 全量重写 —— 用户手写注释必须原样活下来。"""
    original = ("# 我手写的注释\n"
                "bind_bot_appid: 'OLD'\n"
                "admin_uids:\n  - U1\n")
    with open(_cfg_file, 'w', encoding='utf-8') as f:
        f.write(original)
    ok, msg = config.persist_bind_bot_appid('  NEW  ')
    assert ok is True and isinstance(msg, str)
    text = open(_cfg_file, encoding='utf-8').read()
    assert "bind_bot_appid: 'NEW'" in text            # 已替换且去空格
    assert 'OLD' not in text
    assert '# 我手写的注释' in text and 'admin_uids:' in text
    assert state.bind_bot_appid == 'NEW'              # 运行时同步生效


def test_persist_bind_appends_with_comment_when_key_absent(_cfg_file):
    """老配置文件没有这个 key → 带注释追加到末尾(而不是静默丢弃)。"""
    with open(_cfg_file, 'w', encoding='utf-8') as f:
        f.write('admin_uids: []')                      # 末尾无换行
    ok, _msg = config.persist_bind_bot_appid('A1')
    assert ok is True
    text = open(_cfg_file, encoding='utf-8').read()
    assert text.startswith('admin_uids: []\n')         # 补了换行
    assert config.CONFIG_COMMENTS['bind_bot_appid'] in text
    assert "bind_bot_appid: 'A1'" in text


def test_rebind_invalidates_push_permission_cache(_cfg_file, monkeypatch):
    """★ 主动推送权限是 **per-bot** 的:换绑 / 重载配置后旧 bot 的结论必须作废,
    否则新 bot 会沿用上一个 bot 的推送资格判定(该挂刷新按钮的群不挂,或反之)。"""
    from plugins.LGTBot_ElainaBot.mod import helpers
    helpers._push_cache()['G1'] = (True, 9e9)
    config.persist_bind_bot_appid('NEWBOT')
    assert helpers._push_cache() == {}

    helpers._push_cache()['G1'] = (True, 9e9)
    config._apply_runtime_tunables(_base_cfg(bind_bot_appid='OTHER'))
    assert helpers._push_cache() == {}


def test_persist_bind_creates_file_and_clears_binding(_cfg_file):
    """文件不存在照样落地;空串 = 解除绑定(回到自动第一个)。"""
    ok, _msg = config.persist_bind_bot_appid('')
    assert ok is True
    assert "bind_bot_appid: ''" in open(_cfg_file, encoding='utf-8').read()
    assert state.bind_bot_appid == ''


def test_persist_bind_reports_failure_without_touching_state(monkeypatch, _cfg_file):
    """写盘失败 → (False, 原因),且**不能**把 state 改成没落盘的值
    (否则面板显示已绑定、重启后又变回去)。"""
    monkeypatch.setattr(state, 'bind_bot_appid', 'KEEP')

    def _boom(*a, **k):
        raise OSError('disk full')
    monkeypatch.setattr('builtins.open', _boom)
    ok, msg = config.persist_bind_bot_appid('NEW')
    assert ok is False and 'config.yaml' in msg
    assert state.bind_bot_appid == 'KEEP'
