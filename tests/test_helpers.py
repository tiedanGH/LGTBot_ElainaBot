#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""helpers 模块测试 —— markdown 转义 / mention 美化 / bot 绑定解析 / 全量群集合载入 / 跨线程协程桥接。

框架侧依赖用替身注入,helpers 全部是**函数内延迟 import**,所以替换生效无需重载 helpers:
  · ``core.bot.manager`` 整个模块塞 ``sys.modules`` 桩(同 conftest 处理 boot)
    —— 真模块会连锁 import ``core.message.silk``,后者的 ``X | None`` 注解要
    Python ≥3.10,dev 机 3.9 直接 TypeError;桩也让 bot 列表完全可控。
  · ``core.base.config.cfg`` 是纯配置对象,直接 monkeypatch 属性。
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from plugins.LGTBot_ElainaBot.mod import helpers, state


# ─────────────────────────────────────────────────────────────────────────
# 框架替身
# ─────────────────────────────────────────────────────────────────────────

class _FakeLogService:
    def __init__(self, rows=None, raises=False):
        self._rows = rows if rows is not None else []
        self._raises = raises
        self.queries: list = []

    def query_data(self, sql):
        self.queries.append(sql)
        if self._raises:
            raise RuntimeError('db boom')
        return self._rows


class _FakeBot:
    def __init__(self, appid, robot_qq='', rows=None, raises=False):
        self.appid = appid
        self.robot_qq = robot_qq
        self.sender = object()
        self.log_service = _FakeLogService(rows, raises)


def _set_bots(monkeypatch, bots):
    """给 ``core.bot.manager`` 塞桩模块;``bots=None`` = manager 未就绪。

    用 setitem(sys.modules) 而非改真模块属性:真 manager 会连锁 import ``core.message.silk``
    (``X | None`` 注解要 Python ≥3.10)。桩随 monkeypatch 自动还原,不污染其它测试。
    """
    stub = types.ModuleType('core.bot.manager')
    stub._bot_manager_ref = (None if bots is None
                             else types.SimpleNamespace(_bots=bots))
    monkeypatch.setitem(sys.modules, 'core.bot.manager', stub)
    return stub._bot_manager_ref


@pytest.fixture(autouse=True)
def _reset_bind():
    """每个用例还原 bind_bot_appid(模块级可变全局,会串扰)。"""
    old = state.bind_bot_appid
    yield
    state.bind_bot_appid = old


# ─────────────────────────────────────────────────────────────────────────
# markdown 转义
# ─────────────────────────────────────────────────────────────────────────

def test_sanitize_md_name_escapes_all_dangerous_chars():
    """转义集内每个字符都要加反斜杠 —— 昵称带 # / * / [ 会把整条 md 消息排版打乱。"""
    for ch in '#*_`~[]<>|\\':
        assert helpers.sanitize_md_name(f'a{ch}b') == f'a\\{ch}b'


def test_sanitize_md_name_single_pass_no_double_escape():
    """str.translate 单趟替换:输入里的 \\ 自身转成 \\\\,新加的反斜杠不被二次处理。"""
    assert helpers.sanitize_md_name('a\\b') == 'a\\\\b'
    assert helpers.sanitize_md_name('**粗**') == '\\*\\*粗\\*\\*'


def test_sanitize_md_name_leaves_common_chars_alone():
    """( ) ! . - + 在昵称里太常见且不构成语法,不转义(注释里的取舍)。"""
    assert helpers.sanitize_md_name('铁蛋(v2.8)-alpha!') == '铁蛋(v2.8)-alpha!'


def test_sanitize_md_name_empty_passthrough():
    assert helpers.sanitize_md_name('') == ''
    assert helpers.sanitize_md_name(None) is None


def test_strip_md_escapes_reverses_sanitize():
    """媒体兜底 / WebUI 日志走非 md 路径,必须能把源头转义原样还原。"""
    for raw in ['a#b', '**粗**', 'a\\b', '<@uid>', 'x|y~z`c`', '铁蛋[1]']:
        assert helpers.strip_md_escapes(helpers.sanitize_md_name(raw)) == raw


def test_strip_md_escapes_untouched_when_no_backslash():
    """无反斜杠直接短路返回原对象,不做无谓 regex。"""
    s = '普通昵称'
    assert helpers.strip_md_escapes(s) is s
    assert helpers.strip_md_escapes('') == ''


def test_strip_md_escapes_leaves_foreign_backslash_sequences():
    """只还原转义集内的 \\X;\\n / \\d 这类不是我们加的,保持原样。"""
    assert helpers.strip_md_escapes('a\\nb') == 'a\\nb'
    assert helpers.strip_md_escapes('\\d+') == '\\d+'


# ─────────────────────────────────────────────────────────────────────────
# target_key / humanize_mentions
# ─────────────────────────────────────────────────────────────────────────

def test_target_key_prefixes():
    assert helpers.target_key('G1', False) == 'g:G1'
    assert helpers.target_key('U1', True) == 'u:U1'


def test_humanize_mentions_uses_nickname(monkeypatch):
    monkeypatch.setattr(helpers.userinfo, 'get_name',
                        lambda uid: '铁蛋' if uid == 'U1' else '')
    assert helpers.humanize_mentions('<@U1> 出牌了') == '@铁蛋 出牌了'


def test_humanize_mentions_falls_back_to_truncated_openid(monkeypatch):
    """DB 未命中:长 openid 截 6 位 + 省略号,短的原样(避免 @ 后空荡荡)。"""
    monkeypatch.setattr(helpers.userinfo, 'get_name', lambda uid: '')
    assert helpers.humanize_mentions('<@ABCDEFGHIJ>') == '@ABCDEF…'
    assert helpers.humanize_mentions('<@ABC>') == '@ABC'


def test_humanize_mentions_multiple_and_passthrough(monkeypatch):
    monkeypatch.setattr(helpers.userinfo, 'get_name',
                        lambda uid: {'U1': '甲', 'U2': '乙'}.get(uid, ''))
    assert helpers.humanize_mentions('<@U1> 对 <@U2>') == '@甲 对 @乙'
    # 无 mention / 空串:原样返回,不进 regex
    assert helpers.humanize_mentions('没有提及') == '没有提及'
    assert helpers.humanize_mentions('') == ''


def test_humanize_mentions_does_not_md_escape(monkeypatch):
    """输出只用于纯文本语境(媒体 caption / 日志),昵称里的 md 字符不转义。"""
    monkeypatch.setattr(helpers.userinfo, 'get_name', lambda uid: '**铁蛋**')
    assert helpers.humanize_mentions('<@U1>') == '@**铁蛋**'


# ─────────────────────────────────────────────────────────────────────────
# bot 绑定解析
# ─────────────────────────────────────────────────────────────────────────

def test_get_bound_appid_prefers_configured_when_loaded(monkeypatch):
    _set_bots(monkeypatch, {'A': _FakeBot('A'), 'B': _FakeBot('B')})
    state.bind_bot_appid = 'B'
    assert helpers.get_bound_appid() == 'B'


def test_get_bound_appid_falls_back_to_first_when_config_offline(monkeypatch):
    """配置的 bot 没加载 → 回退框架第一个(不是返回空,否则整插件失声)。"""
    _set_bots(monkeypatch, {'A': _FakeBot('A'), 'B': _FakeBot('B')})
    state.bind_bot_appid = 'NOT_LOADED'
    assert helpers.get_bound_appid() == 'A'
    state.bind_bot_appid = ''          # 未配置同样回退第一个
    assert helpers.get_bound_appid() == 'A'


def test_get_bound_appid_empty_without_bots(monkeypatch):
    _set_bots(monkeypatch, {})
    assert helpers.get_bound_appid() == ''
    _set_bots(monkeypatch, None)       # manager 未就绪(插件早于框架加载)
    assert helpers.get_bound_appid() == ''


def test_get_bound_bot_and_sender(monkeypatch):
    a, b = _FakeBot('A'), _FakeBot('B')
    _set_bots(monkeypatch, {'A': a, 'B': b})
    state.bind_bot_appid = 'B'
    assert helpers.get_bound_bot() is b
    assert helpers.get_sender() is b.sender
    # 显式 appid 且已加载 → 用它;未加载 → 回退绑定 bot
    assert helpers.get_sender('A') is a.sender
    assert helpers.get_sender('ZZZ') is b.sender


def test_get_bound_bot_and_sender_none_without_bots(monkeypatch):
    _set_bots(monkeypatch, {})
    assert helpers.get_bound_bot() is None
    assert helpers.get_sender() is None


def test_get_bot_uin_resolution(monkeypatch):
    a = _FakeBot('A', robot_qq='10001')
    b = _FakeBot('B', robot_qq='20002')
    _set_bots(monkeypatch, {'A': a, 'B': b})
    state.bind_bot_appid = 'B'
    assert helpers.get_bot_uin() == '20002'        # 回退绑定 bot
    assert helpers.get_bot_uin('A') == '10001'     # 显式 appid
    assert helpers.get_bot_uin('ZZZ') == '20002'   # 未加载 → 绑定 bot
    _set_bots(monkeypatch, {})
    assert helpers.get_bot_uin() == ''             # 无 bot → 空串(调用方降级)


def test_get_bot_uin_empty_when_field_unset(monkeypatch):
    """robot_qq 未配置时返回 ''(而非 None)—— 「邀我进群」链接可留空拼接。"""
    _set_bots(monkeypatch, {'A': _FakeBot('A', robot_qq='')})
    assert helpers.get_bot_uin() == ''


def test_is_foreign_event_filters_other_bots(monkeypatch):
    _set_bots(monkeypatch, {'A': _FakeBot('A'), 'B': _FakeBot('B')})
    state.bind_bot_appid = 'B'
    assert helpers.is_foreign_event(types.SimpleNamespace(appid='A')) is True
    assert helpers.is_foreign_event(types.SimpleNamespace(appid='B')) is False


def test_is_foreign_event_never_blocks_when_unresolvable(monkeypatch):
    """解析不出绑定 bot 时一律放行 —— 宁可多处理也不要整插件静默失声。"""
    _set_bots(monkeypatch, {})
    assert helpers.is_foreign_event(types.SimpleNamespace(appid='A')) is False
    # appid 为 None(框架某些事件不带)且有绑定 → 视为外来
    _set_bots(monkeypatch, {'B': _FakeBot('B')})
    assert helpers.is_foreign_event(types.SimpleNamespace(appid=None)) is True


# ─────────────────────────────────────────────────────────────────────────
# 框架 bot 枚举(面板下拉)
# ─────────────────────────────────────────────────────────────────────────

def _set_bot_configs(monkeypatch, configs, raises=False):
    from core.base import config as _core_cfg

    def _get():
        if raises:
            raise RuntimeError('cfg boom')
        return configs
    monkeypatch.setattr(_core_cfg.cfg, 'get_bot_configs', _get, raising=False)


def test_list_framework_bots_shape_and_full_volume(monkeypatch):
    """每项 {appid, qq, full_volume};full_volume 来自该 bot 自己的
    full_access_groups 表 —— 未加载的 bot 也在列表里,只是数量为 None。"""
    _set_bot_configs(monkeypatch, [
        {'appid': 'A', 'robot_qq': '10001'},
        {'appid': 'B', 'robot_qq': '20002'},
        {'appid': '  ', 'robot_qq': 'x'},        # 空 appid → 丢弃
    ])
    _set_bots(monkeypatch, {'A': _FakeBot('A', rows=[{'n': 7}])})
    out = helpers.list_framework_bots()
    assert [b['appid'] for b in out] == ['A', 'B']
    assert out[0] == {'appid': 'A', 'qq': '10001', 'full_volume': 7}
    assert out[1]['full_volume'] is None          # B 未加载


def test_list_framework_bots_survives_query_failure(monkeypatch):
    """单个 bot 查询抛错 → 该项 None,不影响其它项,更不能整个列表塌成 []。"""
    _set_bot_configs(monkeypatch, [{'appid': 'A', 'robot_qq': ''}])
    _set_bots(monkeypatch, {'A': _FakeBot('A', raises=True)})
    assert helpers.list_framework_bots() == [{'appid': 'A', 'qq': '',
                                              'full_volume': None}]
    # 空结果集 → 0(而非 None):表存在但没有全量群
    _set_bots(monkeypatch, {'A': _FakeBot('A', rows=[])})
    assert helpers.list_framework_bots()[0]['full_volume'] == 0


def test_list_framework_bots_empty_on_config_failure(monkeypatch):
    _set_bot_configs(monkeypatch, None, raises=True)
    assert helpers.list_framework_bots() == []
    _set_bot_configs(monkeypatch, None)          # get_bot_configs 返回 None
    assert helpers.list_framework_bots() == []


# ─────────────────────────────────────────────────────────────────────────
# 全量群集合
# ─────────────────────────────────────────────────────────────────────────

def test_seed_full_volume_groups_replaces_in_place(monkeypatch):
    """★ 关键回归:``state.full_volume_groups`` 是挂在 C++ 扩展上的跨热重载持久 set,
    必须 clear+update **原地**改;一旦写成重新赋值,旧模块持有的引用就此分家,全量群判定跨热重载后集体失效。"""
    _set_bots(monkeypatch, {'A': _FakeBot('A', rows=[{'group_id': 'G1'},
                                                     {'group_id': 'G2'},
                                                     {'group_id': ''}])})
    before = state.full_volume_groups          # 持有引用,模拟旧模块
    state.full_volume_groups.add('STALE')
    n = helpers.seed_full_volume_groups_from_db()
    assert n == 2
    assert state.full_volume_groups is before  # 同一对象,没有被重新赋值
    assert before == {'G1', 'G2'}              # 旧引用能看到新内容,且旧值被清掉


def test_seed_full_volume_groups_keeps_state_on_failure(monkeypatch):
    """bot 未就绪 / 查询失败返回 -1 并**保留现状** —— 不能把已知的全量群清空
    (清空会让这些群里的回复退化成挂刷新按钮)。"""
    state.full_volume_groups.add('G_KEEP')
    _set_bots(monkeypatch, {})                       # 无 bot
    assert helpers.seed_full_volume_groups_from_db() == -1
    assert 'G_KEEP' in state.full_volume_groups
    _set_bots(monkeypatch, {'A': _FakeBot('A', raises=True)})   # 查询抛错
    assert helpers.seed_full_volume_groups_from_db() == -1
    assert 'G_KEEP' in state.full_volume_groups


def test_is_full_volume_group_trusts_runtime_set_only():
    """判定唯一依据是运行时观测集合 —— 不查框架 non_at_message 配置
    (配置与 QQ 后台权限不同步,信配置会把非全量群误判为全量)。"""
    state.full_volume_groups.add('G1')
    assert helpers.is_full_volume_group('G1') is True
    assert helpers.is_full_volume_group('G2') is False
    assert helpers.is_full_volume_group('') is False
    assert helpers.is_full_volume_group(None) is False


# ─────────────────────────────────────────────────────────────────────────
# 跨线程协程桥接
# ─────────────────────────────────────────────────────────────────────────

def test_run_coro_blocking_returns_result():
    """C++ 工作线程调用形态:从**非 loop 线程**提交协程并阻塞取结果。"""
    import threading

    async def _work():
        return 42

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    old = state.event_loop
    state.event_loop = loop
    try:
        assert helpers.run_coro_blocking(_work()) == 42
    finally:
        state.event_loop = old
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5)
        loop.close()


def test_run_coro_blocking_none_when_loop_unavailable():
    """loop 未就绪 / 已关闭 → 丢弃协程返回 None,绝不抛给 C++ 调用方。"""
    async def _work():
        return 1

    old = state.event_loop
    try:
        state.event_loop = None
        c1 = _work()
        assert helpers.run_coro_blocking(c1) is None
        c1.close()
        closed = asyncio.new_event_loop()
        closed.close()
        state.event_loop = closed
        c2 = _work()
        assert helpers.run_coro_blocking(c2) is None
        c2.close()
    finally:
        state.event_loop = old


def test_run_coro_blocking_swallows_coroutine_exception():
    """协程内部抛异常 → None(引擎线程不该被 Python 异常打断)。"""
    import threading

    async def _boom():
        raise RuntimeError('boom')

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    old = state.event_loop
    state.event_loop = loop
    try:
        assert helpers.run_coro_blocking(_boom()) is None
    finally:
        state.event_loop = old
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5)
        loop.close()
