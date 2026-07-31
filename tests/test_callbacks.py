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

import pytest

# conftest.py 已 inject 假 boot,这里安全 import
from plugins.LGTBot_ElainaBot.mod import callbacks, quota, state


@pytest.fixture(autouse=True)
def _clean_active_matches():
    """state.active_matches 逐测清理避免串扰(与 conftest 持久 dict 同源)。"""
    state.active_matches.clear()
    yield
    state.active_matches.clear()


def _active_pairs():
    """把 state.active_matches 摊平成 {(target_id, is_uid)} 便于断言。"""
    return {(r['target_id'], r['is_uid']) for r in state.active_matches.values()}


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


# ─────────────────────────────────────────────────────────────────────────
# 配额耗尽指标口径:仅统计「无主动直推资格」目标(全量群 / 沙箱私信不计)
# ─────────────────────────────────────────────────────────────────────────


def _exhaust_ref(key):
    """建一个 TTL 内引用并把 5 条被动额度用尽 → 之后 try_consume 返回 None、
    has_valid_ref 仍为 True(即「真耗尽」而非「无上下文」)。"""
    quota.refresh_ref(key, 'msg_id', 'M1', 'APP')
    for _ in range(quota.REF_QUOTA):
        quota.try_consume_ref(key)


async def test_active_push_daily_limit_falls_back_to_refresh(monkeypatch):
    """全量群今日主动消息用满 → 失去直推资格,退回「阻塞等刷新」机制;
    额度未满时照常直推。跨天由日分桶自动重置(used 归 0),无需额外逻辑。"""
    sender = _fake_sender()
    monkeypatch.setattr(callbacks.helpers, 'get_sender', lambda appid='': sender)
    monkeypatch.setattr(callbacks.metrics, 'record_quota_exhausted', lambda: None)
    monkeypatch.setattr(callbacks.metrics, 'record_active_push', lambda *a: None)
    monkeypatch.setattr(callbacks, 'ACTIVE_PUSH_DAILY_LIMIT', 1000)
    waited = []
    monkeypatch.setattr(callbacks.metrics, 'record_quota_wait_timeout',
                        lambda: waited.append(1))
    # 普通群配额满会阻塞等刷新 → mock 成立即返回 None(等待超时)
    monkeypatch.setattr(callbacks.quota, 'wait_and_consume',
                        AsyncMock(return_value=None))
    state.full_volume_groups.add('GLIM')
    key = callbacks.helpers.target_key('GLIM', False)

    # ① 额度未满(已用 999 < 1000)→ 直推,不进等待分支
    monkeypatch.setattr(callbacks.metrics, 'active_push_used', lambda t, u: 999)
    _exhaust_ref(key)
    await callbacks._send_text_quota_managed('GLIM', False, 'hi', None)
    sender.send_to_group.assert_awaited_once()
    assert waited == []                       # 没走等待路径

    # ② 额度用满(1000/1000)→ 退回等待刷新;超时后**不强发**(QQ 必拒)
    sender.send_to_group.reset_mock()
    monkeypatch.setattr(callbacks.metrics, 'active_push_used', lambda t, u: 1000)
    _exhaust_ref(key)
    await callbacks._send_text_quota_managed('GLIM', False, 'hi2', None)
    assert waited == [1]                      # 进了等待分支且超时
    sender.send_to_group.assert_not_called()  # 额度已满,不白烧一次调用


def test_active_push_allowed_gate():
    """_active_push_allowed:上限 0 = 不限制;未满 True;达到 / 超过上限 False。"""
    orig = callbacks.ACTIVE_PUSH_DAILY_LIMIT
    used_val = {'n': 0}
    real_used = callbacks.metrics.active_push_used
    callbacks.metrics.active_push_used = lambda t, u: used_val['n']
    try:
        callbacks.ACTIVE_PUSH_DAILY_LIMIT = 0          # 不限制
        used_val['n'] = 10 ** 9
        assert callbacks._active_push_allowed('G', False) is True
        callbacks.ACTIVE_PUSH_DAILY_LIMIT = 1000
        used_val['n'] = 999
        assert callbacks._active_push_allowed('G', False) is True
        used_val['n'] = 1000                           # 达到上限即禁止
        assert callbacks._active_push_allowed('G', False) is False
        used_val['n'] = 1001
        assert callbacks._active_push_allowed('G', False) is False
    finally:
        callbacks.ACTIVE_PUSH_DAILY_LIMIT = orig
        callbacks.metrics.active_push_used = real_used


async def test_quota_exhausted_not_counted_for_full_volume_group(monkeypatch):
    """全量群配额耗尽后可无缝转主动消息、无影响 → 不计入配额压力。"""
    sender = _fake_sender()
    monkeypatch.setattr(callbacks.helpers, 'get_sender', lambda appid='': sender)
    calls = []
    monkeypatch.setattr(callbacks.metrics, 'record_quota_exhausted', lambda: calls.append(1))
    key = callbacks.helpers.target_key('GFULL', False)
    _exhaust_ref(key)
    state.full_volume_groups.add('GFULL')          # 标记全量群 → is_active_push True

    await callbacks._send_text_quota_managed('GFULL', False, 'hi', None)

    assert calls == []                             # 未计入
    sender.send_to_group.assert_awaited_once()     # 仍走主动消息送达


async def test_quota_exhausted_counted_for_normal_group(monkeypatch):
    """普通群配额耗尽无主动兜底(阻塞等刷新)→ 计入配额压力。"""
    sender = _fake_sender()
    monkeypatch.setattr(callbacks.helpers, 'get_sender', lambda appid='': sender)
    calls = []
    monkeypatch.setattr(callbacks.metrics, 'record_quota_exhausted', lambda: calls.append(1))
    monkeypatch.setattr(callbacks.metrics, 'record_quota_wait_timeout', lambda: None)
    # 普通群耗尽会阻塞等刷新 → mock 立即返回 None,避免测试挂满超时
    monkeypatch.setattr(callbacks.quota, 'wait_and_consume', AsyncMock(return_value=None))
    key = callbacks.helpers.target_key('GNORM', False)
    _exhaust_ref(key)                              # 不加入 full_volume_groups

    await callbacks._send_text_quota_managed('GNORM', False, 'hi', None)

    assert calls == [1]                            # 计入一次


# ─────────────────────────────────────────────────────────────────────────
# 崩溃中断通知:进行中对局缓存(事件流维护)+ fan-out 去重 + 发送分支
# ─────────────────────────────────────────────────────────────────────────


def test_match_event_game_started_adds_active_targets():
    """真正开局(game_started)→ 记入 active_matches;群局与私聊局都进。"""
    callbacks.cb_match_event('777', False, 'game_started', '')
    callbacks.cb_match_event('U_HOST', True, 'game_started', '')
    assert _active_pairs() == {('777', False), ('U_HOST', True)}


def test_match_event_game_name_snapshotted_from_current_game():
    """game_started 广播本身无 brief → 游戏名从此前 new_game 写入的 current_game 快照。"""
    callbacks.cb_match_event('777', False, 'new_game', '五子棋')  # 建房,写 current_game
    callbacks.cb_match_event('777', False, 'game_started', '')     # 开局,game_name 为空
    rec = state.active_matches['g:777']
    assert rec['game'] == '五子棋'
    assert rec['target_id'] == '777' and rec['is_uid'] is False
    assert isinstance(rec['since'], float)


def test_match_event_waiting_room_not_active():
    """等待房间(new_game)不发 game_started → 不算进行中,不进 active_matches。"""
    callbacks.cb_match_event('888', False, 'new_game', '五子棋')
    assert ('888', False) not in _active_pairs()


def test_match_event_game_over_discards():
    """结束事件 → 从 active_matches 移除本 target(群局)。"""
    callbacks.cb_match_event('888', False, 'game_started', '')
    callbacks.cb_match_event('999', False, 'game_started', '')
    callbacks.cb_match_event('888', False, 'game_over', '')
    assert _active_pairs() == {('999', False)}   # 888 被剔除


def test_match_event_dm_dissolve_discards():
    """私聊局解散(all_left)→ 从 active_matches 移除;pop 对未记录者也幂等安全。"""
    callbacks.cb_match_event('U9', True, 'game_started', '')
    callbacks.cb_match_event('U9', True, 'all_left', '')
    assert ('U9', True) not in _active_pairs()
    callbacks.cb_match_event('never', False, 'terminate', '')   # 不在记录里也不报错


def test_match_event_single_player_name_from_pending():
    """单机局:无 new_game / current_game,game_started 从 pending 命令名取游戏名,
    并写回 current_game 供结算重开按钮。"""
    state.pending_new_game_name['g:sp1'] = '决胜五子'
    callbacks.cb_match_event('sp1', False, 'game_started', '')
    assert state.active_matches['g:sp1']['game'] == '决胜五子'
    assert state.current_game['g:sp1'] == '决胜五子'          # 写回
    assert 'g:sp1' not in state.pending_new_game_name          # 已消费


def test_match_event_multiplayer_prefers_current_game_over_pending():
    """多人局:current_game 已由 new_game 写入,game_started 用它而非 pending;pending 仍清掉。"""
    state.current_game['g:mp1'] = '炼金术士'           # 模拟 new_game 已写入
    state.pending_new_game_name['g:mp1'] = '数字蜂巢'  # 陈旧 pending(如此前失败的命令)
    callbacks.cb_match_event('mp1', False, 'game_started', '')
    assert state.active_matches['g:mp1']['game'] == '炼金术士'
    assert 'g:mp1' not in state.pending_new_game_name


def test_mention_rewrite_for_proxied_admin_interrupt():
    """%中断 群管代理:引擎回执里的 @引擎管理员 被改写回 @真实操作者。

    一次性(命中即注销,不影响后续消息)+ 限 target + 5s 过期,避免误伤真的
    要 @ 该管理员的消息。"""
    key = 'g:GX'
    callbacks._mention_rewrites.clear()
    try:
        callbacks.register_mention_rewrite(key, 'ENGINE_ADMIN', 'GROUP_ADMIN')
        # 命中:mention 被替换,其余内容不动
        out = callbacks._apply_mention_rewrite(key, '<@ENGINE_ADMIN>\n中断成功')
        assert out == '<@GROUP_ADMIN>\n中断成功'
        # 已注销:同样的后续消息不再被改写(引擎管理员本人若在该群玩游戏不受影响)
        assert callbacks._apply_mention_rewrite(key, '<@ENGINE_ADMIN> 轮到你了') == \
            '<@ENGINE_ADMIN> 轮到你了'
        # 不含目标 mention 的消息不消耗登记(等真正的回执或自然过期)
        callbacks.register_mention_rewrite(key, 'ENGINE_ADMIN', 'GROUP_ADMIN')
        assert callbacks._apply_mention_rewrite(key, '别人的消息') == '别人的消息'
        assert key in callbacks._mention_rewrites
        # 过期后不再改写
        f, t, _exp = callbacks._mention_rewrites[key]
        callbacks._mention_rewrites[key] = (f, t, 0.0)
        assert callbacks._apply_mention_rewrite(key, '<@ENGINE_ADMIN> x') == \
            '<@ENGINE_ADMIN> x'
        # 其他 target 不受影响
        callbacks.register_mention_rewrite('g:OTHER', 'ENGINE_ADMIN', 'GROUP_ADMIN')
        assert callbacks._apply_mention_rewrite(key, '<@ENGINE_ADMIN> y') == \
            '<@ENGINE_ADMIN> y'
        # 自我改写 / 空参数不登记
        callbacks._mention_rewrites.clear()
        callbacks.register_mention_rewrite(key, 'SAME', 'SAME')
        callbacks.register_mention_rewrite(key, '', 'X')
        assert not callbacks._mention_rewrites
    finally:
        callbacks._mention_rewrites.clear()


def test_game_started_attaches_game_help_button():
    """开局广播(game_started)挂「🎮 游戏帮助」—— data='帮助'(不带斜杠,引擎在
    match 上下文解释为当前游戏帮助)、type=1 callback、style=4,与未知游戏指令
    引导里那颗同款。"""
    state.pending_buttons.clear()
    callbacks.cb_match_event('gs1', False, 'game_started', '')
    btns = _flat_buttons('g:gs1')
    assert len(btns) == 1
    b = btns[0]
    assert b['data'] == '帮助' and b['type'] == 1 and b['style'] == 4
    assert 'enter' not in b                                  # 本插件按钮约定
    # 与「未知游戏指令」里的游戏帮助按钮完全一致(同款视觉 / 行为)
    from plugins.LGTBot_ElainaBot.mod import buttons
    same = buttons.build_unknown_game_buttons()[0][0]
    assert (b['text'], b['data'], b['type'], b['style']) == \
           (same['text'], same['data'], same['type'], same['style'])


def test_mid_quit_clears_dm_match_but_not_group():
    """mid_quit(玩家中途强退广播):私信对局全员 LEFT 后的解散广播私发不达,
    这条就是终止信号 → 清 active_matches + current_game;群聊对局仍在进行 →
    两者都不动。回归:私信单机/私信局中途退出后「进行中的对局」永久卡住。"""
    # 私信局:开局 → mid_quit → 清理
    state.current_game['u:dm1'] = '单机游戏'
    callbacks.cb_match_event('dm1', True, 'game_started', '')
    assert 'u:dm1' in state.active_matches
    # 开局广播会挂「游戏帮助」按钮并被该条广播的 cb_send_text_message pop 掉;
    # 这里手动清掉模拟那次发送,好让下面的断言只检验 mid_quit 自身不挂按钮。
    state.pending_buttons.pop('u:dm1', None)
    callbacks.cb_match_event('dm1', True, 'mid_quit', '')
    assert 'u:dm1' not in state.active_matches
    assert 'u:dm1' not in state.current_game
    assert 'u:dm1' not in state.pending_buttons          # mid_quit 自身不挂按钮
    # 群聊局:一人中途退出,对局继续 → 状态保留
    state.current_game['g:grp9'] = '狼人杀'
    callbacks.cb_match_event('grp9', False, 'game_started', '')
    callbacks.cb_match_event('grp9', False, 'mid_quit', '')
    assert 'g:grp9' in state.active_matches
    assert state.current_game.get('g:grp9') == '狼人杀'


def test_new_game_marks_reply_limit_tip_and_suppresses_dm_warn():
    """新建房间(new_game)即标记「消息回复限制」教学(开局消息发得晚,配额可能已耗尽把提示吞掉);带开局私信的游戏在**非全量群**只发教学、私信提示被抑制,
    **全量群**(教学不发)才标私信提示;私信新建不标私信提示;game_started 不再标教学。"""
    game = next(iter(callbacks._DM_LIMITED_GAMES))     # 任取一个带开局私信的游戏
    callbacks._pending_dm_warn_keys.clear()
    callbacks._pending_tip_keys.clear()
    state.current_game.clear()
    state.pending_buttons.clear()
    try:
        # 非全量群新建(带开局私信游戏)→ 标教学,私信提示被抑制
        callbacks.cb_match_event('grp1', False, 'new_game', game)
        assert 'g:grp1' in callbacks._pending_tip_keys
        assert 'g:grp1' not in callbacks._pending_dm_warn_keys
        # 全量群新建同款 → 私信提示打标(教学 consume 时会因全量群跳过)
        state.full_volume_groups.add('grpF')
        callbacks.cb_match_event('grpF', False, 'new_game', game)
        assert 'g:grpF' in callbacks._pending_dm_warn_keys
        # 全量群新建非私信游戏 → 不标私信提示
        callbacks._pending_dm_warn_keys.clear()
        callbacks.cb_match_event('grpF', False, 'new_game', '五子棋')
        assert 'g:grpF' not in callbacks._pending_dm_warn_keys
        # 私信新建 → 标教学(consume 时按直推私信过滤),不标私信提示
        callbacks.cb_match_event('usr1', True, 'new_game', game)
        assert 'u:usr1' in callbacks._pending_tip_keys
        assert 'u:usr1' not in callbacks._pending_dm_warn_keys
        # game_started 不再标教学(教学已前移到建房)
        callbacks._pending_tip_keys.clear()
        callbacks.cb_match_event('grp1', False, 'game_started', '')
        assert 'g:grp1' not in callbacks._pending_tip_keys
    finally:
        callbacks._pending_dm_warn_keys.clear()
        callbacks._pending_tip_keys.clear()
        state.current_game.clear()
        state.pending_buttons.clear()


def test_single_player_game_over_restart_button_has_name():
    """单机局结算(game_over_unrecorded):重开按钮 data 为「/新游戏 <名>」(名字来自 pending)。"""
    state.pending_new_game_name['u:spuser'] = '天赋云巢'
    callbacks.cb_match_event('spuser', True, 'game_started', '')
    callbacks.cb_match_event('spuser', True, 'game_over_unrecorded', '')
    flat = [b for row in (state.pending_buttons.get('u:spuser') or []) for b in row]
    assert any(b.get('data') == '/新游戏 天赋云巢' for b in flat)


def test_collateral_targets_excludes_crash_source():
    active = {('g1', False), ('g2', False), ('u1', True)}
    assert callbacks._collateral_targets(active, 'g1', False) == {('g2', False), ('u1', True)}
    assert callbacks._collateral_targets(active, 'u1', True) == {('g1', False), ('g2', False)}
    # 崩溃源不在进行中列表(如崩溃在等待房间)→ 原样全发
    assert callbacks._collateral_targets(active, 'gX', False) == active
    # 快照独立:改原集合不影响已返回结果
    snap = callbacks._collateral_targets(active, 'g1', False)
    active.clear()
    assert snap == {('g2', False), ('u1', True)}


async def test_collateral_notice_active_when_no_ref(monkeypatch):
    """无有效引用 → 立即主动消息(空 kwargs,不等 15s)。"""
    sender = _fake_sender()
    monkeypatch.setattr(callbacks.helpers, 'get_sender', lambda appid='': sender)
    await callbacks._send_collateral_notice('GROUP_X', False)
    sender.send_to_group.assert_awaited_once()
    args, kwargs = sender.send_to_group.call_args
    assert args[0] == 'GROUP_X'
    assert 'msg_id' not in kwargs and 'event_id' not in kwargs


async def test_collateral_notice_passive_when_ref(monkeypatch):
    """有未超额引用 → 被动(带 msg_id)。"""
    sender = _fake_sender()
    monkeypatch.setattr(callbacks.helpers, 'get_sender', lambda appid='': sender)
    key = callbacks.helpers.target_key('GROUP_P', False)
    quota.refresh_ref(key, 'msg_id', 'MID123', 'appid1')
    await callbacks._send_collateral_notice('GROUP_P', False)
    _args, kwargs = sender.send_to_group.call_args
    assert kwargs.get('msg_id') == 'MID123'


async def test_collateral_notice_dm_uses_send_to_user(monkeypatch):
    sender = _fake_sender()
    monkeypatch.setattr(callbacks.helpers, 'get_sender', lambda appid='': sender)
    await callbacks._send_collateral_notice('USER_X', True)
    sender.send_to_user.assert_awaited_once()
    sender.send_to_group.assert_not_called()
