#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""config 测试 —— ``_apply_runtime_tunables`` 的 sandbox_dm_users 双模式下发。

函数尾部的 sandbox_dm_users / menu_game_buttons 应用块整段丢了也不会报错,
只是 ``SANDBOX_DM_USERS`` 恒为空集合。本文件补上这道闸:

  · 白名单模式:列表 → frozenset,DM_PUSH_ALL 关闭
  · ``['all']``(仅此一项)→ 全员直推模式:DM_PUSH_ALL=True + 空白名单
  · ``['all', openid]`` 混入其他项 → 回退普通白名单
  · 非法 / 缺失 → 空集合 + 模式关闭
  · ['all'] → 白名单可干净回退(官方收回权限的还原路径)
  · 回归保障:函数确实写到 callbacks / buttons 模块属性(块被删则必红)
"""

from __future__ import annotations

import os
import re

import pytest
import yaml

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
    monkeypatch.setattr(dispatcher, 'BUTTON_COOLDOWN', 3.0)
    monkeypatch.setattr(callbacks, 'NOTIFY_GROUPS', ())
    monkeypatch.setattr(callbacks, 'SANDBOX_DM_USERS', frozenset())
    monkeypatch.setattr(callbacks, 'DM_PUSH_ALL', False)
    monkeypatch.setattr(dispatcher, 'BLOCKED_COMMANDS', ())
    monkeypatch.setattr(dispatcher, 'TEXT_AT_AS_MENTION', True)
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
# appid 是纯数字,不带引号写进 yaml 会被解析成 int;读取端不收下的话,绑定在重启后静默回退第一个 bot。


def test_bind_bot_appid_unquoted_number_coerced_to_str():
    config._apply_runtime_tunables(_base_cfg(bind_bot_appid=102003762))
    assert state.bind_bot_appid == '102003762'


def test_bind_bot_appid_bool_and_garbage_still_ignored():
    """bool 是 int 子类但显然不是 appid;list 等其他类型同样保持忽略语义。"""
    config._apply_runtime_tunables(_base_cfg(bind_bot_appid=True))
    assert state.bind_bot_appid == ''
    config._apply_runtime_tunables(_base_cfg(bind_bot_appid=['x']))
    assert state.bind_bot_appid == ''


def test_notify_groups_normalized_to_tuple_of_str():
    """多个群:strip + 去空 + 去重保序;裸数字群号(yaml 不带引号)按字符串收下。"""
    config._apply_runtime_tunables(
        _base_cfg(notify_groups=['G1', ' G2 ', '', 'G1', 987654321]))
    assert callbacks.NOTIFY_GROUPS == ('G1', 'G2', '987654321')


def test_notify_groups_non_list_ignored():
    """填成字符串 / 数字等非列表 → 视为未配置(不推送),不崩。"""
    for bad in ('G1', 123, {'a': 1}, True):
        config._apply_runtime_tunables(_base_cfg(notify_groups=bad))
        assert callbacks.NOTIFY_GROUPS == ()


def test_legacy_crash_notify_group_still_honored():
    """★ 旧字段名先迁移再下发:notify_groups 留空而旧的 crash_notify_group 有值时用旧值。

    老部署的 yaml 里只有旧字段,无视它的话崩溃 / 重启通知会**静默消失**。新字段一旦填了就完全接管(不合并旧值)。
    """
    cfg = _base_cfg(crash_notify_group='GOLD')
    cfg['notify_groups'] = []
    config._apply_runtime_tunables(cfg)
    assert callbacks.NOTIFY_GROUPS == ('GOLD',)

    # 新字段有值 → 旧字段被忽略
    cfg2 = _base_cfg(crash_notify_group='GOLD', notify_groups=['GNEW'])
    config._apply_runtime_tunables(cfg2)
    assert callbacks.NOTIFY_GROUPS == ('GNEW',)


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


def test_button_cooldown_zero_turns_off_and_negative_clamped():
    config._apply_runtime_tunables(_base_cfg(button_cooldown='1.5'))
    assert dispatcher.BUTTON_COOLDOWN == 1.5
    config._apply_runtime_tunables(_base_cfg(button_cooldown='x'))
    assert dispatcher.BUTTON_COOLDOWN == 1.5             # 非数值保留现值
    config._apply_runtime_tunables(_base_cfg(button_cooldown=-2))
    assert dispatcher.BUTTON_COOLDOWN == 0.0             # 负数同 0:关闭冷却
    config._apply_runtime_tunables(_base_cfg(button_cooldown=3))
    assert dispatcher.BUTTON_COOLDOWN == 3.0


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
    """每个字段都得有注释与面板字段名 —— 模板里的注释是字段说明,可视化编辑也拿它当说明文字。"""
    assert set(config.DEFAULT_CONFIG) == set(config.CONFIG_COMMENTS) == set(config.CONFIG_LABELS)


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
    """老配置文件没有这个 key → 带注释补进来(而不是静默丢弃),并按代码顺序排到 admin_uids 前面。"""
    with open(_cfg_file, 'w', encoding='utf-8') as f:
        f.write('admin_uids: []')                      # 末尾无换行
    ok, _msg = config.persist_bind_bot_appid('A1')
    assert ok is True
    text = open(_cfg_file, encoding='utf-8').read()
    assert text == f"# {config.CONFIG_COMMENTS['bind_bot_appid']}\nbind_bot_appid: 'A1'\nadmin_uids: []\n"


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


def test_validator_flags_renamed_field_as_migration_not_unknown():
    """★ 改过名的字段会自动迁移到新字段 —— 校验器不能说它"不会读取",要提示改名去向。

    提示成未知字段会让人误删掉唯一生效的通知群配置。
    """
    pytest.importorskip('aiohttp')
    from plugins.LGTBot_ElainaBot.mod.webui import page_config as pc
    errs, warns = pc._validate_config_yaml('crash_notify_group: GOLD\n')
    assert errs == []
    hit = [w for w in warns if 'crash_notify_group' in w]
    assert hit and 'notify_groups' in hit[0], warns
    assert '不会读取' not in hit[0]
    # 真正的未知字段仍照旧提示
    _e, w2 = pc._validate_config_yaml('nonsense_field: 1\n')
    assert any('未知字段' in w for w in w2)


# ─────────────────────────────────────────────────────────────────────────
# text_at_as_mention
# ─────────────────────────────────────────────────────────────────────────


def test_text_at_as_mention_defaults_on():
    """★ 该开关默认打开 —— 默认值散在三处,任何一处改成 False 都会让老配置文件的用户悄悄失去这个行为。"""
    assert config.DEFAULT_CONFIG['text_at_as_mention'] is True
    config._apply_runtime_tunables(_base_cfg())
    assert dispatcher.TEXT_AT_AS_MENTION is True
    # 老配置文件里根本没有这个 key
    cfg = _base_cfg()
    del cfg['text_at_as_mention']
    config._apply_runtime_tunables(cfg)
    assert dispatcher.TEXT_AT_AS_MENTION is True


def test_text_at_as_mention_switches_off_and_rejects_non_bool():
    config._apply_runtime_tunables(_base_cfg(text_at_as_mention=False))
    assert dispatcher.TEXT_AT_AS_MENTION is False
    # 'true' / 1 这类近似值按非法忽略,保留现值(不静默翻转)
    config._apply_runtime_tunables(_base_cfg(text_at_as_mention='true'))
    assert dispatcher.TEXT_AT_AS_MENTION is False


# ─────────────────────────────────────────────────────────────────────────
# 配置保存端点:七个编辑器统一走 /api/ext/lgtbot/config/save
# ─────────────────────────────────────────────────────────────────────────


def _pc():
    pytest.importorskip('aiohttp')
    from plugins.LGTBot_ElainaBot.mod.webui import page_config
    return page_config


class _FakeSaveReq:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _resp_body(resp) -> dict:
    import json as _json
    return _json.loads(resp.text)


def test_every_editor_saves_through_the_plugin_endpoint():
    """★ 每个编辑器都必须带 target,且 target 在 _SAVE_TARGETS 里有登记。"""
    pc = _pc()
    targets = re.findall(r"target: '([\w]+)'", pc.TAB_JS)
    # 表项数 == 带 target 的项数 —— 漏一个就会走到不存在的分支
    assert len(targets) == pc.TAB_JS.count('dataKey:')
    for t in targets:
        assert t in pc._SAVE_TARGETS, t


async def test_plain_text_target_writes_the_file(tmp_path, monkeypatch):
    """纯文案 target 无可校验,但要真的落盘并记一条审计。"""
    pc = _pc()
    target_file = tmp_path / 'update_notice.txt'
    monkeypatch.setitem(pc._SAVE_TARGETS, 'update_notice', (str(target_file), pc._validate_plain_text))
    recorded = []
    monkeypatch.setattr(pc.audit, 'record', lambda *a, **k: recorded.append((a, k)))
    resp = await pc.save_config_handler(
        _FakeSaveReq({'target': 'update_notice', 'content': '# 公告正文\n第二行'}))

    assert _resp_body(resp)['success'] is True
    assert target_file.read_text(encoding='utf-8') == '# 公告正文\n第二行'
    assert recorded and recorded[0][0][1] == '保存 update_notice.txt'


async def test_unknown_target_is_rejected():
    """路径只认服务端登记的 target,客户端不能塞任意文件名。"""
    pc = _pc()
    resp = await pc.save_config_handler(
        _FakeSaveReq({'target': '../../etc/passwd', 'content': 'x'}))
    assert resp.status == 400
    assert _resp_body(resp)['success'] is False


async def test_save_creates_missing_parent_dir(tmp_path, monkeypatch):
    """sponsors.txt 这类文件可能从未写过,父目录缺失也要能建出来。"""
    pc = _pc()
    target_file = tmp_path / 'nested' / 'sponsors.txt'
    monkeypatch.setitem(pc._SAVE_TARGETS, 'sponsors', (str(target_file), pc._validate_plain_text))
    monkeypatch.setattr(pc.audit, 'record', lambda *a, **k: None)
    resp = await pc.save_config_handler(
        _FakeSaveReq({'target': 'sponsors', 'content': '感谢名单'}))

    assert _resp_body(resp)['success'] is True
    assert target_file.read_text(encoding='utf-8') == '感谢名单'


# ─────────────────────────────────────────────────────────────────────────
# config.yaml 的渲染与字段顺序:字段永远按 DEFAULT_CONFIG 的顺序排
# ─────────────────────────────────────────────────────────────────────────

def test_render_follows_code_order_and_round_trips():
    """★ 渲染按 DEFAULT_CONFIG 顺序、带注释,解析回来与原值一致;本插件不读取的字段排在最后。"""
    cfg = dict(reversed(list(config.DEFAULT_CONFIG.items())))
    cfg.update(bind_bot_appid='1905305499', blocked_commands=['/帮助', "it's", 'a: b', '多\n行'],
               legacy_field={'x': [1, 2]})
    text = config.render_config_yaml(cfg)
    data = yaml.safe_load(text)
    assert list(data) == [*config.DEFAULT_CONFIG, 'legacy_field']
    assert data == cfg
    assert f"bind_bot_appid: '1905305499'  # {config.CONFIG_COMMENTS['bind_bot_appid']}\n" in text
    assert f"# {config.CONFIG_COMMENTS['blocked_commands']}\nblocked_commands:\n  - /帮助\n" in text
    assert f"# {config.CONFIG_COMMENTS['notify_groups']}\nnotify_groups: []\n" in text


def test_order_moves_whole_blocks_and_keeps_comments():
    """★ 乱序文本按代码顺序整块挪动:手写注释跟着字段走,行内注释与原写法不变,不读取的字段排最后;已有序的原样返回。"""
    src = ("bind_bot_appid: '1'  # 绑定\n"
           "sponsor_enabled: false  # 赞助\n"
           "legacy: 1\n"
           "# 我的管理员\n"
           "admin_uids:\n  - U1\n"
           "button_cooldown: 3.0  # 冷却")
    out = config.order_config_yaml(src)
    assert out == ("bind_bot_appid: '1'  # 绑定\n"
                   "button_cooldown: 3.0  # 冷却\n"
                   "# 我的管理员\n"
                   "admin_uids:\n  - U1\n"
                   "sponsor_enabled: false  # 赞助\n"
                   "legacy: 1\n")
    assert config.order_config_yaml(out) == out


def test_order_falls_back_to_rendering_when_blocks_cannot_be_cut():
    """带引号的字段名切不准块,整块挪动后顺序仍不对 → 按代码顺序重新渲染,值不变。"""
    src = '"button_cooldown": 1\nbind_bot_appid: a\nsponsor_enabled: true\n'
    out = config.order_config_yaml(src)
    assert list(yaml.safe_load(out)) == ['bind_bot_appid', 'button_cooldown', 'sponsor_enabled']
    assert yaml.safe_load(out) == yaml.safe_load(src)


@pytest.mark.parametrize('text', ['a: [1,\n', '- just\n- a list\n', '', 'plain'])
def test_order_leaves_unparsable_or_non_mapping_text_alone(text):
    assert config.order_config_yaml(text) == text


def test_load_puts_appended_fields_back_in_code_order(_cfg_file, monkeypatch):
    """★ ensure_config 把新增字段补在文件末尾 → 加载时按代码顺序整理落盘,原有注释不丢;已有序就不动文件。"""
    src = ("bind_bot_appid: ''  # 绑定\n"
           "# 手写说明\n"
           "sponsor_enabled: false\n"
           "button_cooldown: 3.0  # 新补的字段\n")
    with open(_cfg_file, 'w', encoding='utf-8') as f:
        f.write(src)
    monkeypatch.setattr(config, '_get_ctx', lambda: _FakeCtx(_base_cfg()))
    config.load_plugin_config()
    text = open(_cfg_file, encoding='utf-8').read()
    assert text == ("bind_bot_appid: ''  # 绑定\n"
                    "button_cooldown: 3.0  # 新补的字段\n"
                    "# 手写说明\n"
                    "sponsor_enabled: false\n")
    writes = []
    monkeypatch.setattr(config, '_write_atomic', lambda *a: writes.append(a))
    config.load_plugin_config()
    assert writes == []


# ─────────────────────────────────────────────────────────────────────────
# 插件配置的可视化编辑:字段表、表单值与 config.yaml 保存的三种形态
# ─────────────────────────────────────────────────────────────────────────

def test_form_fields_follow_code_order_with_kinds(monkeypatch):
    """字段表就是 DEFAULT_CONFIG 的顺序,带面板字段名与说明;拿得到图床名单时图床给下拉选项。"""
    pc = _pc()
    monkeypatch.setattr(pc, '_valid_backends', lambda: {'nature', 'cos'})
    fields = pc._config_form('')['fields']
    assert [f['key'] for f in fields] == list(config.DEFAULT_CONFIG)
    kinds = {f['key']: f['kind'] for f in fields}
    assert kinds['text_at_as_mention'] == 'bool' and kinds['menu_game_buttons'] == 'list'
    assert kinds['active_push_daily_limit'] == 'int' and kinds['button_cooldown'] == 'float'
    assert kinds['bind_bot_appid'] == 'str'
    hosting = next(f for f in fields if f['key'] == 'image_hosting')
    assert hosting['kind'] == 'select' and hosting['options'] == ['any', 'cos', 'nature', '']
    assert all(f['label'] == config.CONFIG_LABELS[f['key']] and f['hint'] for f in fields)
    monkeypatch.setattr(pc, '_valid_backends', lambda: set())
    assert next(f for f in pc._config_form('')['fields'] if f['key'] == 'image_hosting')['kind'] == 'str'


def test_form_values_fill_defaults_and_list_extra_fields():
    pc = _pc()
    form = pc._config_form('button_cooldown: 5\nimage_hosting:\nold_field: 1\n')
    assert form['errors'] == [] and form['extra'] == ['old_field']
    assert form['values']['button_cooldown'] == 5
    assert form['values']['image_hosting'] == config.DEFAULT_CONFIG['image_hosting']   # 裸 key = 未配置
    assert form['values']['menu_game_buttons'] == config.DEFAULT_CONFIG['menu_game_buttons']


def test_form_unavailable_when_source_is_broken_or_unreadable():
    """源文件校验不过 / 读不到时不给表单值,前端停在源文件模式 —— 免得按默认值把整份配置覆盖掉。"""
    pc = _pc()
    for text, err in (('a: [1,\n', ''), ('sponsor_enabled: 1\n', ''), ('', 'Permission denied')):
        form = pc._config_form(text, err)
        assert form['values'] is None and form['errors'] and form['fields']


def test_validator_checks_integer_fields_too():
    pc = _pc()
    assert any('active_push_daily_limit' in e for e in pc._validate_config_yaml('active_push_daily_limit: x\n')[0])
    assert pc._validate_config_yaml('active_push_daily_limit: 0\n')[0] == []


@pytest.fixture
def yaml_target(tmp_path, monkeypatch):
    """config.yaml 的保存路径指到 tmp,审计记下来;返回 (page_config, 文件路径, 审计记录)。"""
    pc = _pc()
    path = tmp_path / 'config.yaml'
    monkeypatch.setattr(pc, '_CONFIG_YAML_PATH', str(path))
    recorded = []
    monkeypatch.setattr(pc.audit, 'record', lambda *a, **k: recorded.append((a, k)))
    return pc, path, recorded


_OUT_OF_ORDER = ("# 手写注释\n"
                 "sponsor_enabled: false\n"
                 "old_field: G0\n"
                 "bind_bot_appid: 'B1'\n")


async def test_visual_save_renders_code_order_and_keeps_extra_fields(yaml_target):
    """★ 可视化保存:按代码顺序渲染整份文件,不读取的字段留在末尾;数值按字段类型写回(20 → 20.0)。"""
    pc, path, recorded = yaml_target
    path.write_text(_OUT_OF_ORDER, encoding='utf-8')
    values = dict(pc._config_form(_OUT_OF_ORDER)['values'],
                  refresh_wait_timeout=20, active_push_daily_limit=50.0, sponsor_enabled=True)
    body = _resp_body(await pc.save_config_handler(_FakeSaveReq({'target': 'config_yaml', 'values': values})))
    text = path.read_text(encoding='utf-8')
    data = yaml.safe_load(text)
    assert body['success'] is True and body['content'] == text and body['reordered'] is False
    assert list(data) == [*config.DEFAULT_CONFIG, 'old_field']
    assert data['old_field'] == 'G0' and data['sponsor_enabled'] is True and data['bind_bot_appid'] == 'B1'
    assert 'refresh_wait_timeout: 20.0  #' in text and 'active_push_daily_limit: 50  #' in text
    assert body['form']['values']['sponsor_enabled'] is True
    assert recorded[-1][0][1] == '保存 config.yaml' and recorded[-1][0][2].startswith('可视化')


@pytest.mark.parametrize('change, error', [
    ({'text_at_as_mention': 'yes'}, 'text_at_as_mention'),
    ({'menu_game_buttons': 'LIE'}, 'menu_game_buttons'),
    ({'button_cooldown': True}, 'button_cooldown'),
    ({'refresh_wait_timeout': 0}, 'refresh_wait_timeout'),
])
async def test_visual_save_rejects_bad_values_without_writing(yaml_target, change, error):
    pc, path, _ = yaml_target
    path.write_text(_OUT_OF_ORDER, encoding='utf-8')
    values = dict(pc._config_form(_OUT_OF_ORDER)['values'], **change)
    body = _resp_body(await pc.save_config_handler(_FakeSaveReq({'target': 'config_yaml', 'values': values})))
    assert body['success'] is False and any(error in e for e in body['errors'])
    assert path.read_text(encoding='utf-8') == _OUT_OF_ORDER


async def test_source_save_puts_fields_in_code_order(yaml_target):
    """★ 源文件保存:校验通过后按代码顺序整块挪动再落盘,注释跟着字段走,回带整理后的文本与 reordered 标记。"""
    pc, path, recorded = yaml_target
    body = _resp_body(await pc.save_config_handler(_FakeSaveReq({'target': 'config_yaml', 'content': _OUT_OF_ORDER})))
    text = path.read_text(encoding='utf-8')
    assert body['success'] is True and body['reordered'] is True and body['content'] == text
    assert text == "bind_bot_appid: 'B1'\n# 手写注释\nsponsor_enabled: false\nold_field: G0\n"
    assert '已整理字段顺序' in recorded[-1][0][2]


async def test_dry_run_converts_between_modes_without_writing(yaml_target):
    """★ dry_run 只换算不落盘:表单值 → 文本(切到源文件),文本 → 表单值(切回可视化),坏文本不给表单值。"""
    pc, path, recorded = yaml_target
    path.write_text(_OUT_OF_ORDER, encoding='utf-8')
    values = dict(pc._config_form(_OUT_OF_ORDER)['values'], button_cooldown=7)
    to_text = _resp_body(await pc.save_config_handler(
        _FakeSaveReq({'target': 'config_yaml', 'values': values, 'dry_run': True})))
    assert yaml.safe_load(to_text['content'])['button_cooldown'] == 7.0
    to_form = _resp_body(await pc.save_config_handler(
        _FakeSaveReq({'target': 'config_yaml', 'content': 'button_cooldown: 9\n', 'dry_run': True})))
    assert to_form['success'] is True and to_form['form']['values']['button_cooldown'] == 9
    broken = _resp_body(await pc.save_config_handler(
        _FakeSaveReq({'target': 'config_yaml', 'content': 'a: [1,\n', 'dry_run': True})))
    assert broken['success'] is False and broken['form'] is None
    assert path.read_text(encoding='utf-8') == _OUT_OF_ORDER and recorded == []


# ─────────────────────────────────────────────────────────────────────────
# 旧字段 crash_notify_group 自动迁移到 notify_groups
# ─────────────────────────────────────────────────────────────────────────

def test_migration_moves_legacy_group_into_empty_notify_groups():
    """★ notify_groups 没配群时把旧值迁进去并删掉旧字段;其余字段的写法与注释不动,顺序按代码整理。"""
    src = ("sponsor_enabled: false\n"
           "crash_notify_group: G0\n"
           "# 我的通知群\n"
           "notify_groups: []\n"
           "bind_bot_appid: 'B1'  # 我的绑定\n")
    out, notes = config.normalize_config_yaml(src)
    assert notes == ['crash_notify_group → notify_groups']
    assert out == ("bind_bot_appid: 'B1'  # 我的绑定\n"
                   f"# {config.CONFIG_COMMENTS['notify_groups']}\n"
                   "notify_groups:\n  - G0\n"
                   "sponsor_enabled: false\n")
    # 缺 notify_groups 时同样补出来
    out2, _ = config.normalize_config_yaml('crash_notify_group: 123\n')
    assert yaml.safe_load(out2) == {'notify_groups': ['123']}


@pytest.mark.parametrize('src, kept', [
    ("notify_groups:\n  - GNEW\ncrash_notify_group: GOLD\n", ['GNEW']),    # 新字段已配:旧值本来就没生效
    ("notify_groups: []\ncrash_notify_group: ''\n", []),                    # 旧字段是空值
])
def test_migration_only_drops_legacy_when_it_has_nothing_to_add(src, kept):
    out, notes = config.normalize_config_yaml(src)
    assert yaml.safe_load(out) == {'notify_groups': kept}
    assert len(notes) == 1 and notes[0].startswith('crash_notify_group 已删除')
    assert config.normalize_config_yaml('notify_groups: []\n') == ('notify_groups: []\n', [])


def test_load_migrates_legacy_field_on_disk_and_at_runtime(_cfg_file, monkeypatch):
    """★ 加载时把文件里的旧字段迁到 notify_groups 并按代码顺序落盘,运行时立即按迁移后的值推送通知。"""
    with open(_cfg_file, 'w', encoding='utf-8') as f:
        f.write("crash_notify_group: GOLD  # 旧版写法\nbind_bot_appid: ''\n")
    cfg = dict(_base_cfg(notify_groups=[]), crash_notify_group='GOLD')
    monkeypatch.setattr(config, '_get_ctx', lambda: _FakeCtx(cfg))
    config.load_plugin_config()
    assert yaml.safe_load(open(_cfg_file, encoding='utf-8')) == {'bind_bot_appid': '', 'notify_groups': ['GOLD']}
    assert callbacks.NOTIFY_GROUPS == ('GOLD',)


async def test_source_save_migrates_legacy_field(yaml_target):
    """源文件里写了旧字段:保存时迁移并回带迁移说明,不再提示改名。"""
    pc, path, recorded = yaml_target
    body = _resp_body(await pc.save_config_handler(
        _FakeSaveReq({'target': 'config_yaml', 'content': "crash_notify_group: G0\nbind_bot_appid: 'B1'\n"})))
    assert body['success'] is True and body['migrated'] == ['crash_notify_group → notify_groups']
    assert yaml.safe_load(path.read_text(encoding='utf-8')) == {'bind_bot_appid': 'B1', 'notify_groups': ['G0']}
    assert not any('crash_notify_group' in w for w in body['warnings'])
    assert '已迁移旧字段' in recorded[-1][0][2]


async def test_visual_form_and_save_use_migrated_values(yaml_target):
    """可视化表单按迁移后的值显示,旧字段不算「另有字段」;保存后文件里只剩新字段。"""
    pc, path, _ = yaml_target
    src = "crash_notify_group: G0\nnotify_groups: []\n"
    path.write_text(src, encoding='utf-8')
    form = pc._config_form(src)
    assert form['values']['notify_groups'] == ['G0'] and form['extra'] == []
    body = _resp_body(await pc.save_config_handler(_FakeSaveReq({'target': 'config_yaml', 'values': form['values']})))
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    assert body['success'] is True and data['notify_groups'] == ['G0'] and 'crash_notify_group' not in data
    assert body['migrated'] == []      # 表单里已是迁移后的值,不再报「旧值未生效」这类说明
