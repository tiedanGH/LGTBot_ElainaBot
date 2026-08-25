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


def mark_push_group(gid: str, ok: bool = True) -> None:
    """把某群标记成(不)可主动推送 —— 直接写 helpers 的 TTL 缓存。

    ``can_push_group`` 改成按群点查 DB + 缓存后,没有集合可写;这里预置一条
    远期不过期的缓存项,等价于「DB 里该群 allow_proactive_msg 是 ok」。
    """
    import time as _t
    from plugins.LGTBot_ElainaBot.mod import helpers as _h
    _h._push_cache()[gid] = (ok, _t.time() + 3600)



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
    """建一个 TTL 内引用并把被动额度用尽(按场景 4 或 5 条)→ 之后 try_consume
    返回 None、has_valid_ref 仍为 True(即「真耗尽」而非「无上下文」)。"""
    quota.refresh_ref(key, 'msg_id', 'M1', 'APP')
    for _ in range(quota.ref_quota(key)):
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
    mark_push_group('GLIM')
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
    mark_push_group('GFULL')          # 标记全量群 → is_active_push True

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
    _exhaust_ref(key)                              # 不加入 proactive_groups

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
        # 可主动推送的群新建同款 → 私信提示打标(教学 consume 时会跳过)
        mark_push_group('grpF')
        callbacks.cb_match_event('grpF', False, 'new_game', game)
        assert 'g:grpF' in callbacks._pending_dm_warn_keys
        # 可推送群新建非私信游戏 → 不标私信提示
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


def test_reply_limit_tip_targets_only_targets_without_push_permission(monkeypatch):
    """★「消息回复限制」教学的投递面 —— 只发给**发不出主动消息**的目标:

      · 群聊看 ``allow_proactive_msg``(DB 权限位)。**只开全量
        消息不算** —— 那只管收得到什么,配额耗尽照样推不出去,仍要教学。
      · 私信没有平台侧权限位可查(框架 users 表没有该字段),沿用沙箱名单,
        ``sandbox_dm_users: ["all"]`` 即全员有权限。
    """
    sent: list = []
    monkeypatch.setattr(callbacks, '_schedule_refresh_tip',
                        lambda tid, is_uid: sent.append((tid, is_uid)))

    def _consume(target_id, is_uid):
        key = callbacks.helpers.target_key(target_id, is_uid)
        callbacks._pending_tip_keys.add(key)
        callbacks._consume_pending_tip(key, target_id, is_uid)

    try:
        state.full_volume_groups.add('gFullOnly')     # 全量,但没主动推送权限
        mark_push_group('gPush')           # 非全量,有主动推送权限
        _consume('gFullOnly', False)
        _consume('gPush', False)
        _consume('gPlain', False)                     # 两种权限都没有
        assert sent == [('gFullOnly', False), ('gPlain', False)]

        # 私信:白名单内跳过、白名单外照发
        sent.clear()
        monkeypatch.setattr(callbacks, 'DM_PUSH_ALL', False)
        monkeypatch.setattr(callbacks, 'SANDBOX_DM_USERS', frozenset({'uOK'}))
        _consume('uOK', True)
        _consume('uNo', True)
        assert sent == [('uNo', True)]

        # all 模式:所有私信用户都有权限 → 一条都不发
        sent.clear()
        monkeypatch.setattr(callbacks, 'DM_PUSH_ALL', True)
        _consume('uAnyone', True)
        assert sent == []
    finally:
        callbacks._pending_tip_keys.clear()
        state.full_volume_groups.discard('gFullOnly')
        mark_push_group('gPush', False)


def test_active_push_eligibility_requires_push_permission(monkeypatch):
    """配额耗尽后能否转主动消息,同样只认主动推送权限 —— 只开全量的群
    仍走「等刷新按钮」路径,不能往没权限的群硬推(QQ 必拒且烧配额)。"""
    assert callbacks.helpers.can_push_group('gFullOnly') is False
    state.full_volume_groups.add('gFullOnly')
    assert callbacks.helpers.can_push_group('gFullOnly') is False
    mark_push_group('gFullOnly')
    try:
        assert callbacks.helpers.can_push_group('gFullOnly') is True
    finally:
        state.full_volume_groups.discard('gFullOnly')
        mark_push_group('gFullOnly', False)


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


# ─────────────────────────────────────────────────────────────────────────
# 图文混排:排版还原(桥接层占位符 \x01IMG<i>\x01)
# ─────────────────────────────────────────────────────────────────────────

_PNG_1x1 = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'
            + (1).to_bytes(4, 'big') + (1).to_bytes(4, 'big'))


def _img(i: int) -> str:
    return f'\x01IMG{i}\x01'


def test_split_layout_keeps_engine_order():
    """占位符在文字前 → 图片段排在文字段前面(引擎的「图片换行文字」排版)。"""
    segs = callbacks._split_layout(f'{_img(0)}\n对局结束', 1)
    assert segs == [('image', 0), ('text', '\n对局结束')]


def test_split_layout_supports_text_image_text():
    segs = callbacks._split_layout(f'开局\n{_img(0)}\n轮到你了', 1)
    assert segs == [('text', '开局\n'), ('image', 0), ('text', '\n轮到你了')]


def test_split_layout_without_placeholder_falls_back_to_text_first():
    """旧桥接层(无占位符)→ 文字在前、图片依次在后,与 2.7 行为一致。"""
    assert callbacks._split_layout('说明文字', 2) == [
        ('text', '说明文字'), ('image', 0), ('image', 1)]


def test_split_layout_appends_images_missing_from_layout():
    """占位符没提到的图片补在末尾 —— 任何情况下不丢图。"""
    segs = callbacks._split_layout(f'{_img(1)}尾巴', 3)
    assert segs == [('image', 1), ('text', '尾巴'), ('image', 0), ('image', 2)]


def test_build_layout_markdown_interleaves():
    segs = [('text', '\n第一段\n'), ('image', 0), ('text', '\n第二段')]
    md = callbacks._build_layout_markdown(segs, {0: 'http://x/a.png'}, {0: (100, 50)})
    assert md == '第一段\n\n![image #100px #50px](http://x/a.png)\n\n第二段'


async def test_mixed_send_single_markdown_in_engine_order(monkeypatch):
    """全部上传成功 → 一条 markdown,图片位置照引擎排版(图在文字之前)。"""
    sender = _fake_sender()
    _patch_send_env(monkeypatch, sender, push_all=False)
    monkeypatch.setattr(callbacks.uploader, 'upload_image',
                        AsyncMock(return_value='http://bed/a.png'))
    mark_push_group('GMIX')
    try:
        segs = [('image', 0), ('text', '\n游戏结束')]
        await callbacks._send_mixed_message('GMIX', False, segs,
                                            {0: (_PNG_1x1, 'a.png')}, '\n游戏结束', None)
    finally:
        mark_push_group('GMIX', False)

    sender.send_to_group.assert_awaited_once()
    md = sender.send_to_group.call_args[0][1]
    assert md.index('![image') < md.index('游戏结束')


async def test_mixed_send_merges_multiple_images_into_one_message(monkeypatch):
    """多图不再拆条:一条 markdown 内联全部图片(顺带省配额)。"""
    sender = _fake_sender()
    _patch_send_env(monkeypatch, sender, push_all=False)
    urls = iter(['http://bed/1.png', 'http://bed/2.png'])
    monkeypatch.setattr(callbacks.uploader, 'upload_image',
                        AsyncMock(side_effect=lambda *a, **k: next(urls)))
    mark_push_group('GMULTI')
    try:
        segs = [('image', 0), ('text', '中间'), ('image', 1)]
        await callbacks._send_mixed_message(
            'GMULTI', False, segs,
            {0: (_PNG_1x1, '1.png'), 1: (_PNG_1x1, '2.png')}, '中间', None)
    finally:
        mark_push_group('GMULTI', False)

    sender.send_to_group.assert_awaited_once()
    md = sender.send_to_group.call_args[0][1]
    assert md.index('1.png') < md.index('中间') < md.index('2.png')


async def test_mixed_send_falls_back_to_media_and_returns_buttons(monkeypatch):
    """图床失败 → 媒体兜底(排版压平),按钮还给 pending_buttons 等下条文本。"""
    monkeypatch.setattr(callbacks.uploader, 'upload_image', AsyncMock(return_value=None))
    calls = []

    async def _fake_media(target_id, is_uid, data, raw_content, filename, *, pre_url=None):
        calls.append((raw_content, pre_url))

    monkeypatch.setattr(callbacks, '_send_image_quota_managed', _fake_media)
    btns = [[{'label': 'x', 'data': '/x'}]]
    state.pending_buttons.pop('g:GFALL', None)
    try:
        segs = [('image', 0), ('text', '文案')]
        await callbacks._send_mixed_message('GFALL', False, segs,
                                            {0: (_PNG_1x1, 'a.png')}, '文案', btns)
        # 首图带全部文字;pre_url='' 表示已知上传失败,不再重传
        assert calls == [('文案', '')]
        assert state.pending_buttons.get('g:GFALL') == btns
    finally:
        state.pending_buttons.pop('g:GFALL', None)


async def test_mixed_send_text_only_when_no_image_readable(monkeypatch):
    """图片一张都没读出来 → 退化成纯文本,文案不跟着丢。"""
    sender = _fake_sender()
    _patch_send_env(monkeypatch, sender, push_all=False)
    mark_push_group('GTXT')
    try:
        await callbacks._send_mixed_message('GTXT', False, [('text', '只剩文字')],
                                            {}, '只剩文字', None)
    finally:
        mark_push_group('GTXT', False)
    sender.send_to_group.assert_awaited_once()
    assert sender.send_to_group.call_args[0][1] == '只剩文字'


def test_mention_rewrite_registry_lives_in_persistent_dict():
    """@ 改写登记表必须挂持久 dict —— 登记方(新 dispatcher)与消费方(可能是
    引擎复用时的旧 callbacks)只有经 boot._get_persistent() 才能看到同一份;
    模块级 dict 会让改写永远不生效(线上回归:%中断 回执仍 @引擎管理员)。"""
    from plugins.LGTBot_ElainaBot.mod import boot
    shared = boot._get_persistent()['mention_rewrites']
    assert callbacks._mention_rewrites is shared      # 同一对象,非拷贝

    callbacks.register_mention_rewrite('g:GX', 'ADMIN_UID', 'OP_UID')
    assert 'g:GX' in shared                           # 写入即对"另一模块"可见
    out = callbacks._apply_mention_rewrite('g:GX', '<@ADMIN_UID> 中断成功')
    assert out == '<@OP_UID> 中断成功'
    assert 'g:GX' not in shared                       # 一次性:命中即注销


# ─────────────────────────────────────────────────────────────────────────
# 通知群广播(崩溃报告 / 熔断告警 / 自动重启说明共用)
# ─────────────────────────────────────────────────────────────────────────

async def test_broadcast_notify_reaches_every_group(monkeypatch):
    """★ 配了多个通知群 → 每个群都收到同一条消息,返回成功条数。"""
    sender = _fake_sender()
    monkeypatch.setattr(callbacks.helpers, 'get_sender', lambda appid='': sender)
    monkeypatch.setattr(callbacks, 'NOTIFY_GROUPS', ('G1', 'G2', 'G3'))
    ok = await callbacks.broadcast_notify('# 告警', '测试通知')
    assert ok == 3
    sent = {c.args[0]: c.args[1] for c in sender.send_to_group.await_args_list}
    assert sent == {'G1': '# 告警', 'G2': '# 告警', 'G3': '# 告警'}


async def test_broadcast_notify_one_failure_does_not_block_others(monkeypatch):
    """★ 一个群失败(最常见:没开全量推送权限被 QQ 拒)不能连累其他群 ——
    这几条恰恰是最不能漏的消息。异常吞掉,只反映在返回的成功条数上。"""
    sender = _fake_sender()

    async def _send(gid, md, **kw):
        if gid == 'G2':
            raise RuntimeError('QQ rejected: no push permission')
        return (True, {}, {})

    sender.send_to_group = AsyncMock(side_effect=_send)
    monkeypatch.setattr(callbacks.helpers, 'get_sender', lambda appid='': sender)
    monkeypatch.setattr(callbacks, 'NOTIFY_GROUPS', ('G1', 'G2', 'G3'))
    ok = await callbacks.broadcast_notify('# 告警', '测试通知')
    assert ok == 2
    assert {c.args[0] for c in sender.send_to_group.await_args_list} == {'G1', 'G2', 'G3'}


async def test_broadcast_notify_skips_when_unconfigured(monkeypatch):
    """未配置通知群 → 连 sender 都不取(0 条),静默跳过。"""
    called = []
    monkeypatch.setattr(callbacks.helpers, 'get_sender',
                        lambda appid='': called.append(1))
    monkeypatch.setattr(callbacks, 'NOTIFY_GROUPS', ())
    assert await callbacks.broadcast_notify('x', '测试通知') == 0
    assert called == []


async def test_crash_notification_fans_out_to_every_group(monkeypatch):
    """崩溃报告走同一条广播 —— 多个群都收到,且正文不含用户原文(风控约束)。"""
    sender = _fake_sender()
    monkeypatch.setattr(callbacks.helpers, 'get_sender', lambda appid='': sender)
    monkeypatch.setattr(callbacks, 'NOTIFY_GROUPS', ('GA', 'GB'))
    await callbacks._try_send_crash_notification(
        'SIGSEGV', 'U1', 'G9', False, 42)
    sent = {c.args[0]: c.args[1] for c in sender.send_to_group.await_args_list}
    assert set(sent) == {'GA', 'GB'}
    md = sent['GA']
    assert 'SIGSEGV' in md and '群聊 G9' in md and '42 字符' in md
    assert sent['GA'] == sent['GB']


async def test_crash_loop_alert_fans_out_to_every_group(monkeypatch):
    sender = _fake_sender()
    monkeypatch.setattr(callbacks.helpers, 'get_sender', lambda appid='': sender)
    monkeypatch.setattr(callbacks, 'NOTIFY_GROUPS', ('GA', 'GB'))
    await callbacks._alert_crash_loop_tripped(5)
    sent = {c.args[0]: c.args[1] for c in sender.send_to_group.await_args_list}
    assert set(sent) == {'GA', 'GB'}
    assert '熔断' in sent['GA'] and '5 次' in sent['GA']


# ─────────────────────────────────────────────────────────────────────────
# 群管中断投票 → 「强制中断游戏」按钮
# ─────────────────────────────────────────────────────────────────────────
# 引擎对 /中断 会连发两条(上游 match.cc::UserInterrupt):
#   ① reply()     「确定中断成功」
#   ② Boardcast() 「有玩家确定中断比赛，目前 N 人尚未确定中断，所有玩家可通过…」
# 按钮只挂第 ②,且只在**群管**发起时挂。

_VOTE_MSG = ('有玩家确定中断比赛，目前 6 人尚未确定中断，'
             '所有玩家可通过「/中断」命令确定中断比赛，或「/中断 取消」命令取消中断比赛')


def _hint(key: str, ttl: float = 60.0) -> None:
    """模拟 dispatcher 端记下「群管刚发过 /中断」。"""
    import time as _t
    callbacks.state.force_interrupt_hints[key] = _t.time() + ttl


def test_force_interrupt_button_only_on_the_vote_broadcast():
    """★ 只认第 ② 条广播:第 ① 条「确定中断成功」不挂,标记留给下一条。"""
    _hint('g:G1')
    assert callbacks._force_interrupt_buttons_for('g:G1', '确定中断成功') is None
    assert 'g:G1' in callbacks.state.force_interrupt_hints      # 标记还在
    btns = callbacks._force_interrupt_buttons_for('g:G1', _VOTE_MSG)
    assert btns == [[callbacks.buttons.BTN_FORCE_INTERRUPT]]


def test_force_interrupt_button_absent_without_admin_hint():
    """★ 普通玩家发 /中断 → dispatcher 不打标记 → 广播上不挂任何按钮。"""
    assert callbacks._force_interrupt_buttons_for('g:G1', _VOTE_MSG) is None


def test_force_interrupt_hint_is_one_shot():
    """命中即注销 —— 同群里后续别人的中断投票不会蹭到按钮。"""
    _hint('g:G1')
    assert callbacks._force_interrupt_buttons_for('g:G1', _VOTE_MSG)
    assert callbacks._force_interrupt_buttons_for('g:G1', _VOTE_MSG) is None
    assert 'g:G1' not in callbacks.state.force_interrupt_hints


def test_force_interrupt_hint_expires():
    """全员已确定(引擎直接中断、没有那条广播)时标记会过期,不悬挂。"""
    _hint('g:G1', ttl=-1)                     # 已过期
    assert callbacks._force_interrupt_buttons_for('g:G1', _VOTE_MSG) is None
    assert 'g:G1' not in callbacks.state.force_interrupt_hints


def test_force_interrupt_button_shape():
    """按钮契约:data=%中断 / style=3 / 仅管理员可点。

    文案只要求点明「强制中断」—— 前缀 emoji 之类属于文案微调,不写死。
    """
    b = callbacks.buttons.BTN_FORCE_INTERRUPT
    assert b['data'] == '%中断'
    assert b['style'] == 3
    assert b.get('admin') is True             # 框架 keyboard.py → permission type=1
    assert '强制中断' in b['text']


async def test_force_interrupt_does_not_steal_engine_buttons(monkeypatch):
    """★ 已有 cb_match_event 排好的按钮组时不抢位 —— 那组按钮是对局动作,更重要。"""
    sent = await _capture_send(monkeypatch, hint=True, msg=_VOTE_MSG,
                               pending=[[{'text': '既有按钮'}]])
    assert sent['btns'] == [[{'text': '既有按钮'}]]
    assert 'g:G1' in callbacks.state.force_interrupt_hints    # 标记没被消费


async def test_force_interrupt_button_reaches_the_send_task(monkeypatch):
    """端到端:cb_send_text_message 真的把按钮交给发送任务。"""
    sent = await _capture_send(monkeypatch, hint=True, msg=_VOTE_MSG)
    assert sent['btns'] == [[callbacks.buttons.BTN_FORCE_INTERRUPT]]


async def test_force_interrupt_absent_end_to_end_for_plain_user(monkeypatch):
    """端到端反面:没有群管标记时,同一条广播不带任何按钮。"""
    sent = await _capture_send(monkeypatch, hint=False, msg=_VOTE_MSG)
    assert sent['btns'] is None


async def _capture_send(monkeypatch, *, hint: bool, msg: str, pending=None) -> dict:
    """跑一次 cb_send_text_message,截获交给发送任务的按钮组。

    cb_send_text_message 是 fire-and-forget(投递到 state.event_loop),所以要把
    当前测试的 loop 挂上去,再让出一次调度让投递的任务真正跑到。
    """
    import asyncio as _a
    sender = _fake_sender()
    monkeypatch.setattr(callbacks.helpers, 'get_sender', lambda appid='': sender)
    monkeypatch.setattr(callbacks.state, 'event_loop', _a.get_running_loop())
    sent: dict = {}

    async def _fake_send(key, tid, is_uid, m, btns):
        sent['btns'] = btns

    monkeypatch.setattr(callbacks, '_serialized_text_send', _fake_send)
    if hint:
        _hint('g:G1')
    if pending is not None:
        callbacks.state.pending_buttons['g:G1'] = pending
    callbacks.cb_send_text_message('G1', False, msg)
    for _ in range(5):                    # 等 run_coroutine_threadsafe 排到
        await _a.sleep(0)
        if 'btns' in sent:
            break
    assert 'btns' in sent, '发送任务没有被调度'
    return sent


# ─────────────────────────────────────────────────────────────────────────
# 引用**按时间过期**:非全量群直接丢弃,不等刷新、不强发
# ─────────────────────────────────────────────────────────────────────────
# 官方只认 TTL 内的 msg_id / event_id —— 群 5 分钟。次数没用完但时间过了,那条引用已经没有任何意义:
# 等刷新没有可续命的对象(还会占着 per-target Lock 15s 把后续消息一起拖住),主动消息又会被 QQ 拒。
# 典型场景:冷群里超时触发的「游戏解散」广播。

def _expire_ref(key: str) -> None:
    """建一个引用后把它的过期时间推到过去 —— 次数一条没用,纯粹是时间到了。"""
    import time as _t
    quota.refresh_ref(key, 'msg_id', 'M_OLD', 'APP')
    quota._active_ref[key]['expires_at'] = _t.time() - 1


async def test_group_drops_when_ref_expired_by_time(monkeypatch):
    """★ 非全量群 + 引用已超时 → 直接丢弃:不发送、不进等待分支。"""
    sender = _fake_sender()
    monkeypatch.setattr(callbacks.helpers, 'get_sender', lambda appid='': sender)
    waited = []
    monkeypatch.setattr(callbacks.quota, 'wait_and_consume',
                        AsyncMock(side_effect=lambda *a: waited.append(1)))
    key = callbacks.helpers.target_key('GCOLD', False)
    _expire_ref(key)

    await callbacks._send_text_quota_managed('GCOLD', False, '游戏已解散', None)

    sender.send_to_group.assert_not_called()
    assert waited == [], '不该再阻塞等刷新'


async def test_group_still_waits_when_quota_exhausted_in_ttl(monkeypatch):
    """★ 对照:TTL 内、只是 5 条用完 → 仍按原逻辑等刷新(这条路没被误伤)。"""
    sender = _fake_sender()
    monkeypatch.setattr(callbacks.helpers, 'get_sender', lambda appid='': sender)
    monkeypatch.setattr(callbacks.metrics, 'record_quota_exhausted', lambda: None)
    monkeypatch.setattr(callbacks.metrics, 'record_quota_wait_timeout', lambda: None)
    monkeypatch.setattr(callbacks.metrics, 'record_active_push', lambda *a: None)
    waited = []
    monkeypatch.setattr(callbacks.quota, 'wait_and_consume',
                        AsyncMock(side_effect=lambda *a: waited.append(1)))
    key = callbacks.helpers.target_key('GBUSY', False)
    _exhaust_ref(key)

    await callbacks._send_text_quota_managed('GBUSY', False, 'hi', None)

    assert waited == [1]


async def test_push_group_still_active_pushes_when_ref_expired(monkeypatch):
    """★ 有主动推送权限的群不受影响:引用过期照常走主动消息(不带 msg_id)。"""
    sender = _fake_sender()
    monkeypatch.setattr(callbacks.helpers, 'get_sender', lambda appid='': sender)
    monkeypatch.setattr(callbacks.metrics, 'record_active_push', lambda *a: None)
    monkeypatch.setattr(callbacks, 'ACTIVE_PUSH_DAILY_LIMIT', 1000)
    monkeypatch.setattr(callbacks.metrics, 'active_push_used', lambda t, u: 0)
    mark_push_group('GFULL')
    _expire_ref(callbacks.helpers.target_key('GFULL', False))

    await callbacks._send_text_quota_managed('GFULL', False, '游戏已解散', None)

    sender.send_to_group.assert_awaited_once()
    _args, kwargs = sender.send_to_group.call_args
    assert 'msg_id' not in kwargs and 'event_id' not in kwargs


async def test_image_path_drops_on_expired_ref_too(monkeypatch):
    """图片走的是另一个函数,同一条规则要一起生效(否则赛况图仍会卡 15s)。"""
    sender = _fake_sender()
    monkeypatch.setattr(callbacks.helpers, 'get_sender', lambda appid='': sender)
    waited = []
    monkeypatch.setattr(callbacks.quota, 'wait_and_consume',
                        AsyncMock(side_effect=lambda *a: waited.append(1)))
    monkeypatch.setattr(callbacks.uploader, 'SELECTED_BACKEND', '')
    _expire_ref(callbacks.helpers.target_key('GCOLD2', False))

    await callbacks._send_image_quota_managed(
        'GCOLD2', False, b'\x89PNG', '赛况', 'x.png')

    sender.send_to_group.assert_not_called()
    assert waited == []
