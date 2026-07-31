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
import re
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

    关键点:**不 patch quota.refresh_ref**(那是被测目标),其他副作用(userinfo
    写回 / page_logs / _send_welcome_menu / threading.Thread)全 patch 成 noop。
    """
    # threading.Thread 起线程跑 boot.LGTBot_ElainaBot.on_public_message 等
    # —— boot 已是 fake (conftest),on_public_message 是 MagicMock,调用安全;
    # 但起真线程拖慢测试,直接 patch Thread.start 为 noop
    with patch.object(dispatcher, '_send_welcome_menu', new=AsyncMock()) as _swm, \
         patch.object(dispatcher.userinfo, 'note_username') as _nu, \
         patch.object(dispatcher.page_logs, 'log_incoming') as _li, \
         patch.object(dispatcher.threading.Thread, 'start') as _ts:
        yield {
            '_send_welcome_menu': _swm,
            'note_username': _nu,
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


async def test_admin_interrupt_proxies_for_group_admin(patched_downstream, monkeypatch):
    """群管理发 %中断 → 用**已配置的引擎管理员 uid** 派发(引擎据此放行),
    并记一条对局干预审计;普通群员 / 私信 → 原样用本人 uid(交引擎裁决)。"""
    from plugins.LGTBot_ElainaBot.mod import audit, boot, config as _config
    _state.started = True
    monkeypatch.setattr(_config, 'ADMIN_UIDS', ('OWNER_UID',))
    sent = []
    monkeypatch.setattr(boot.LGTBot_ElainaBot, 'on_public_message',
                        lambda *a: sent.append(a))
    monkeypatch.setattr(boot.LGTBot_ElainaBot, 'on_private_message',
                        lambda *a: sent.append(a))
    audits = []
    monkeypatch.setattr(audit, 'record', lambda *a, **k: audits.append(a))
    # patched_downstream 把 Thread.start 变 noop,这里要真跑 target
    monkeypatch.setattr(dispatcher.threading, 'Thread',
                        lambda target, args, daemon=True: type(
                            'T', (), {'start': lambda s: target(*args)})())

    # ① 群管理 → 换成引擎管理员 uid
    ev = _mock_event(is_group=True, group_id='G1', user_id='ADMIN_USER',
                     content='%中断', message_id='M1')
    ev.member_role = 'admin'
    await dispatcher.lgtbot_admin_interrupt(ev, None)
    assert sent == [('%中断', 'OWNER_UID', 'G1')]
    assert audits and audits[0][0] == 'match'

    # ② 普通群员 → 用本人 uid,不审计
    sent.clear(); audits.clear()
    ev2 = _mock_event(is_group=True, group_id='G1', user_id='PLAIN_USER',
                      content='%中断', message_id='M2')
    ev2.member_role = 'member'
    await dispatcher.lgtbot_admin_interrupt(ev2, None)
    assert sent == [('%中断', 'PLAIN_USER', 'G1')]
    assert audits == []

    # ③ 私信(无 member_role)→ 用本人 uid,带 mid 参数原样透传
    sent.clear()
    ev3 = _mock_event(is_direct=True, user_id='DM_USER',
                      content='%中断 42', message_id='M3')
    ev3.member_role = ''
    await dispatcher.lgtbot_admin_interrupt(ev3, None)
    assert sent == [('%中断 42', 'DM_USER')]

    # ④ 群管理但未配置引擎管理员 → 明确提示,不派发
    sent.clear()
    monkeypatch.setattr(_config, 'ADMIN_UIDS', ())
    ev4 = _mock_event(is_group=True, group_id='G1', user_id='ADMIN_USER',
                      content='%中断', message_id='M4')
    ev4.member_role = 'owner'
    ev4.reply = AsyncMock()
    await dispatcher.lgtbot_admin_interrupt(ev4, None)
    assert sent == []                                        # 不派发
    assert '未配置' in ev4.reply.await_args.args[0]           # 明确告知配置缺失


async def test_admin_interrupt_super_admin_not_proxied_no_audit(patched_downstream, monkeypatch):
    """超级管理员自己发 %中断 —— 即便他同时是群主 / 群管理,也**不算代为中断**:
    用本人 uid 派发、不写审计(审计只为"权限下放给群管"留追责线索)。"""
    from plugins.LGTBot_ElainaBot.mod import audit, boot, config as _config
    _state.started = True
    monkeypatch.setattr(_config, 'ADMIN_UIDS', ('SUPER_UID', 'OTHER_ADMIN'))
    sent, audits = [], []
    monkeypatch.setattr(boot.LGTBot_ElainaBot, 'on_public_message',
                        lambda *a: sent.append(a))
    monkeypatch.setattr(audit, 'record', lambda *a, **k: audits.append(a))
    monkeypatch.setattr(dispatcher.threading, 'Thread',
                        lambda target, args, daemon=True: type(
                            'T', (), {'start': lambda s: target(*args)})())

    for role in ('owner', 'admin', 'member'):
        sent.clear(); audits.clear()
        ev = _mock_event(is_group=True, group_id='G1', user_id='SUPER_UID',
                         content='%中断', message_id='M1')
        ev.member_role = role
        await dispatcher.lgtbot_admin_interrupt(ev, None)
        # 用本人 uid(不借用 ADMIN_UIDS[0]),且无审计
        assert sent == [('%中断', 'SUPER_UID', 'G1')], f'role={role}'
        assert audits == [], f'role={role} 不应写审计'


async def test_admin_interrupt_audit_distinguishes_no_game(patched_downstream, monkeypatch):
    """代理中断的审计详情区分三态:有名字 / 未知游戏(有对局无名) / 无游戏。"""
    from plugins.LGTBot_ElainaBot.mod import audit, boot, config as _config
    _state.started = True
    monkeypatch.setattr(_config, 'ADMIN_UIDS', ('SUPER_UID',))
    audits = []
    monkeypatch.setattr(boot.LGTBot_ElainaBot, 'on_public_message', lambda *a: None)
    monkeypatch.setattr(audit, 'record',
                        lambda *a, **k: audits.append(a[2] if len(a) > 2 else ''))
    monkeypatch.setattr(dispatcher.threading, 'Thread',
                        lambda target, args, daemon=True: type(
                            'T', (), {'start': lambda s: target(*args)})())

    async def _interrupt(gid):
        ev = _mock_event(is_group=True, group_id=gid, user_id='ADMIN_USER',
                         content='%中断', message_id='M1')
        ev.member_role = 'admin'
        await dispatcher.lgtbot_admin_interrupt(ev, None)
        return audits[-1]

    # ① 群里没有任何对局 → 无游戏(回归:以前一律写「未知游戏」,会误导)
    assert '无游戏' in await _interrupt('G_EMPTY')
    # ② 有对局但游戏名未知 → 未知游戏
    _state.active_matches['g:G_UNK'] = {'target_id': 'G_UNK', 'is_uid': False,
                                        'game': '', 'since': 0}
    assert '未知游戏' in await _interrupt('G_UNK')
    # ③ 有名字(等待房间 / 已开局)→ 游戏名
    _state.current_game['g:G_NAMED'] = '五子棋'
    assert '五子棋' in await _interrupt('G_NAMED')


def test_deny_super_admin_cmd_matrix(monkeypatch):
    """_deny_super_admin_cmd:仅拦「群管理 + 非超级管理员 + 非 %中断」。"""
    from plugins.LGTBot_ElainaBot.mod import config as _config
    monkeypatch.setattr(_config, 'ADMIN_UIDS', ('SUPER_UID',))

    def _ev(role, is_group=True):
        e = _mock_event(is_group=is_group, is_direct=not is_group,
                        group_id='G1' if is_group else '', user_id='U1')
        e.member_role = role
        return e

    deny = dispatcher._deny_super_admin_cmd
    # 群管 + 其他管理指令 → 拦
    assert deny(_ev('admin'), '%清除战绩 123 理由', 'U1')
    assert deny(_ev('owner'), '%荣誉', 'U1')
    # 群管 + %中断(已授权)→ 不拦
    assert not deny(_ev('admin'), '%中断', 'U1')
    assert not deny(_ev('admin'), '%中断 42', 'U1')
    # 普通成员 → 不拦(交引擎回原文案)
    assert not deny(_ev('member'), '%清除战绩 123 理由', 'U1')
    assert not deny(_ev(''), '%荣誉', 'U1')
    # 群管但本人就是超级管理员 → 不拦(引擎真执行)
    assert not deny(_ev('admin'), '%荣誉', 'SUPER_UID')
    # 私信(无群管概念)→ 不拦
    assert not deny(_ev('', is_group=False), '%荣誉', 'U1')
    # 非 % 指令 → 与本闸无关
    assert not deny(_ev('admin'), '/新游戏 五子棋', 'U1')


async def test_dispatch_denies_group_admin_super_cmd(patched_downstream, monkeypatch):
    """catch-all 里群管发 %清除战绩 → 回插件自定义文案,**不派发**给引擎。"""
    from plugins.LGTBot_ElainaBot.mod import config as _config
    _state.started = True
    monkeypatch.setattr(_config, 'ADMIN_UIDS', ('SUPER_UID',))

    ev = _mock_event(is_group=True, group_id='G1', user_id='ADMIN_USER',
                     content='%清除战绩 123 恶意刷分', message_id='M1')
    ev.member_role = 'admin'
    ev.reply = AsyncMock()
    await dispatcher.lgtbot_dispatch(ev, None)

    reply = ev.reply.await_args.args[0]
    # 排版对齐引擎群聊回执:<@uid> + 换行 + [错误] 开头(bot_core.cc PublicReplyMsgSender)
    assert reply.startswith('<@ADMIN_USER>\n[错误] ')
    assert '超级管理员' in reply
    assert '%中断' in reply          # 明确告知群管唯一可用的管理指令
    patched_downstream['thread_start'].assert_not_called()   # 未派发给引擎


async def test_dispatch_plain_user_super_cmd_goes_to_engine(patched_downstream, monkeypatch):
    """普通成员发 % 指令 → 照常派发给引擎(由引擎回它自己的错误文案)。"""
    from plugins.LGTBot_ElainaBot.mod import config as _config
    _state.started = True
    monkeypatch.setattr(_config, 'ADMIN_UIDS', ('SUPER_UID',))

    ev = _mock_event(is_group=True, group_id='G1', user_id='PLAIN_USER',
                     content='%清除战绩 123 恶意刷分', message_id='M2')
    ev.member_role = 'member'
    ev.reply = AsyncMock()
    await dispatcher.lgtbot_dispatch(ev, None)

    ev.reply.assert_not_awaited()                            # 插件不自造回复
    patched_downstream['thread_start'].assert_called()       # 交给引擎


def test_admin_interrupt_pattern_scope():
    """%中断 被登记为专属指令(catch-all 不再重复派发);玩家投票的 /中断 与
    其他管理指令(%清除战绩)**不**被抢占。"""
    assert dispatcher._is_exclusive_command('%中断')
    assert dispatcher._is_exclusive_command('%中断 42')
    assert not dispatcher._is_exclusive_command('/中断')
    assert not dispatcher._is_exclusive_command('中断')
    assert not dispatcher._is_exclusive_command('%清除战绩 123 理由')


async def test_planned_restart_notice_carries_support_buttons(patched_downstream, monkeypatch):
    """计划重启维护提示底部挂「官方群聊 / 问题反馈」link 按钮(execv 前安全可点)。"""
    from plugins.LGTBot_ElainaBot.mod import buttons
    _state.started = True
    monkeypatch.setattr(dispatcher.state, 'is_planned_restart', lambda: True)

    event = _mock_event(is_group=True, group_id='G1', user_id='U1',
                        content='/新游戏 五子棋', message_id='M1')
    event.reply = AsyncMock()
    await dispatcher.lgtbot_dispatch(event, None)

    event.reply.assert_awaited_once()
    assert event.reply.await_args.args[0] == dispatcher._planned_restart_notice()
    assert event.reply.await_args.kwargs['buttons'] == buttons.build_support_buttons()


def test_planned_restart_notice_shows_reason_and_remaining():
    """维护提示带「剩余进行中对局数」与管理员填写的「维护原因」;
    原因经 markdown 转义;无对局时改说「随时可能重启」;关闭维护模式清掉原因。"""
    from plugins.LGTBot_ElainaBot.mod import state as st
    st.active_matches.clear()
    st.set_planned_restart(False)
    try:
        # 无对局 + 无原因
        st.set_planned_restart(True)
        txt = dispatcher._planned_restart_notice()
        assert '当前已无进行中的对局' in txt and '维护原因' not in txt
        # 有对局 + 有原因(带 markdown 特殊字符 → 转义后不破坏排版)
        st.active_matches['g:1'] = {'target_id': '1', 'is_uid': False, 'game': 'X', 'since': 0}
        st.active_matches['g:2'] = {'target_id': '2', 'is_uid': False, 'game': 'Y', 'since': 0}
        st.set_planned_restart(True, '数据库迁移 *紧急*')
        txt = dispatcher._planned_restart_notice()
        assert '**2** 局' in txt
        assert '📌 维护原因：' in txt and r'\*紧急\*' in txt
        # 关闭 → 原因清空(下次开启不复用旧原因)
        st.set_planned_restart(False)
        assert st.planned_restart_reason() == ''
    finally:
        st.active_matches.clear()
        st.set_planned_restart(False)


async def test_planned_restart_command_accepts_reason(monkeypatch):
    """「计划重启 <原因>」记录原因并在回执里回显;不带原因时同旧行为。"""
    from plugins.LGTBot_ElainaBot.mod import state as st
    st.active_matches.clear()
    st.set_planned_restart(False)
    monkeypatch.setattr(dispatcher.helpers, 'is_foreign_event', lambda e: False)
    try:
        ev = _mock_event(is_group=True, group_id='G1', user_id='U1',
                         content='计划重启 例行维护')
        ev.reply = AsyncMock()
        m = re.match(dispatcher._P_PLANNED, '计划重启 例行维护')
        await dispatcher.lgtbot_planned_restart(ev, m)
        assert st.is_planned_restart() and st.planned_restart_reason() == '例行维护'
        assert '例行维护' in ev.reply.await_args.args[0]
        # 再次触发(关闭)→ 原因清空
        ev2 = _mock_event(is_group=True, group_id='G1', user_id='U1', content='计划重启')
        ev2.reply = AsyncMock()
        await dispatcher.lgtbot_planned_restart(ev2, re.match(dispatcher._P_PLANNED, '计划重启'))
        assert not st.is_planned_restart() and st.planned_restart_reason() == ''
    finally:
        st.set_planned_restart(False)


def test_push_quota_view_group_and_dm():
    """额度视图:群里看本群、私信看本人;上限 0 = 未设上限;达到上限标 exhausted。"""
    from plugins.LGTBot_ElainaBot.mod import callbacks, metrics
    real_used = metrics.active_push_used
    orig_limit = callbacks.ACTIVE_PUSH_DAILY_LIMIT
    metrics.active_push_used = lambda t, u: {('G1', False): 999,
                                             ('U1', True): 1000}.get((t, u), 0)
    try:
        callbacks.ACTIVE_PUSH_DAILY_LIMIT = 1000
        g = dispatcher._push_quota_view('G1', False)
        assert g['shown'] and g['is_group'] and g['used'] == 999
        assert g['limit'] == 1000 and g['remaining'] == 1 and not g['exhausted']
        u = dispatcher._push_quota_view('U1', True)
        assert u['shown'] and not u['is_group'] and u['used'] == 1000
        assert u['remaining'] == 0 and u['exhausted']
        # 无目标 → 不展示
        assert dispatcher._push_quota_view('', False)['shown'] is False
        # 上限 0 → 只报用量,不算用满
        callbacks.ACTIVE_PUSH_DAILY_LIMIT = 0
        z = dispatcher._push_quota_view('U1', True)
        assert z['limit'] == 0 and not z['exhausted']
    finally:
        metrics.active_push_used = real_used
        callbacks.ACTIVE_PUSH_DAILY_LIMIT = orig_limit


async def test_stats_command_text_shows_push_quota(monkeypatch):
    """「数据统计」文本输出带本会话额度行:群里显示「本群」、私信显示「你的私信」。"""
    from plugins.LGTBot_ElainaBot.mod import callbacks, metrics, uploader
    monkeypatch.setattr(dispatcher.helpers, 'is_foreign_event', lambda e: False)
    monkeypatch.setattr(uploader, 'SELECTED_BACKEND', '')      # 走文本通道
    monkeypatch.setattr(dispatcher.metrics, 'query_game_stats',
                        lambda: {'available': True, 'today_matches': 1,
                                 'today_players': 1, 'today_groups': 1,
                                 'top_games_today': [], 'top_players_today': [],
                                 'trend_10d': []})
    monkeypatch.setattr(callbacks, 'ACTIVE_PUSH_DAILY_LIMIT', 1000)
    monkeypatch.setattr(metrics, 'active_push_used', lambda t, u: 1000 if u else 12)

    ev = _mock_event(is_group=True, group_id='G1', user_id='U1', content='数据统计')
    ev.reply = AsyncMock()
    await dispatcher.lgtbot_data_stats(ev, None)
    txt = ev.reply.await_args.args[0]
    assert '本群今日主动消息: 12/1000 条' in txt and '已用满' not in txt

    ev2 = _mock_event(is_direct=True, user_id='U1', content='数据统计')
    ev2.reply = AsyncMock()
    await dispatcher.lgtbot_data_stats(ev2, None)
    txt2 = ev2.reply.await_args.args[0]
    assert '你的私信今日主动消息: 1000/1000 条' in txt2
    assert '已用满' in txt2                        # 用满时给出说明


def test_match_list_is_exclusive_command():
    """「赛事列表」已登记进专属指令表 → catch-all 不再派发给引擎(否则普通玩家
    仍能通过 catch-all 拿到全部公开 + 私密赛事)。带 / 与不带 / 两种输入都要挡。"""
    assert dispatcher._is_exclusive_command('赛事列表')
    assert dispatcher._is_exclusive_command('/赛事列表')
    # 前缀相同但不是本指令的文本不受影响
    assert not dispatcher._is_exclusive_command('赛事列表2')
    assert not dispatcher._is_exclusive_command('/赛事')


async def test_match_list_relays_to_engine_with_quota_ref(patched_downstream):
    """主人触发(owner_only 由框架前置放行)→ 补齐引用配额并把 /赛事列表 透传引擎。

    block=True 抢在 catch-all 前,catch-all 的 refresh_ref 不会跑,本 handler
    必须自己登记 msg_id,否则引擎生成的列表没有可用引用会被丢弃。
    """
    _state.started = True
    event = _mock_event(is_group=True, group_id='GML', user_id='UML',
                        content='赛事列表', message_id='M_ML')

    await dispatcher.lgtbot_match_list(event, None)

    # 配额引用已登记到群 key(不污染 u:<uid>)
    assert quota._active_ref['g:GML']['ref_value'] == 'M_ML'
    assert 'u:UML' not in quota._active_ref
    # 已起线程把指令派进引擎(patched_downstream 把 Thread.start 换成 noop mock)
    assert patched_downstream['thread_start'].called


async def test_match_list_replies_when_engine_not_ready(patched_downstream):
    """引擎未就绪 → 回提示,不派发。"""
    _state.started = False
    event = _mock_event(is_group=True, group_id='G1', user_id='U1',
                        content='赛事列表', message_id='M1')
    event.reply = AsyncMock()
    await dispatcher.lgtbot_match_list(event, None)
    event.reply.assert_awaited_once()
    assert '引擎尚未就绪' in event.reply.await_args.args[0]


async def test_welcome_menu_full_volume_cmd_line(monkeypatch):
    """欢迎菜单:非全量群追加「全量申请」内联指令行;全量群 / 私信不追加。"""
    from plugins.LGTBot_ElainaBot.mod import buttons, state
    monkeypatch.setattr(dispatcher, '_resolve_menu_logo', AsyncMock(return_value=None))

    async def _menu_md(ev):
        ev.reply = AsyncMock()
        await dispatcher._send_welcome_menu(ev)
        return ev.reply.await_args.args[0]

    # 非全量群 → 追加
    md = await _menu_md(_mock_event(is_group=True, group_id='GNORM', user_id='U1'))
    assert buttons.MENU_FULL_VOLUME_CMD_MD.strip() in md
    assert 'qqbot-cmd-input text="全量申请"' in md
    # 全量群 → 不追加
    state.full_volume_groups.add('GFULL')
    md = await _menu_md(_mock_event(is_group=True, group_id='GFULL', user_id='U1'))
    assert '全量申请' not in md
    # 私信 → 不追加
    md = await _menu_md(_mock_event(is_direct=True, user_id='U1'))
    assert '全量申请' not in md


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
