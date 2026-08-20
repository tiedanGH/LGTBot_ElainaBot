#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""紧急公告测试 —— 文案(txt)+ 总开关与已通知群(json)+ 两处出口。

被测的三条风险线:

  1. **空态 / 关态不留痕**:平时开关是关的、文案是空的,此时欢迎菜单必须与没有
     这功能时**逐字节一致** —— 多一个空行,所有用户每次 @bot 都看到菜单里凭空
     多出一道缝。所以关态菜单用严格相等断言。
  2. **状态必须活过重启**:``enabled`` 与已通知群写在 ``urgent_notice.json``,
     测试里通过"清缓存重新读盘"模拟 execv 重启。
  3. **两个动作互不越界**:关闭公告**不清**已通知群(重新启用不重复打扰老群);
     重置已通知群**不改**开关。这两条是需求里点名的语义,各有独立断言。

新群通知的触发点(建房命令)覆盖到:``/新游戏`` / ``/随机游戏`` / 单机局 / 按钮
INTERACTION 路径,以及"发失败不落记录"这条兜底。
"""

from __future__ import annotations

import json
import os
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.LGTBot_ElainaBot.mod import backup, boot, buttons, dispatcher, urgent


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def urgent_paths(tmp_path, monkeypatch):
    """把文案 / 状态两个文件都指到 tmp,并清掉模块内的状态缓存。

    缓存必须清:``urgent._state`` 是模块级的,不清会把上一个测试的开关状态带进来
    (monkeypatch 在测试结束时把这两个全局还原回 None,下个测试同样干净)。
    """
    monkeypatch.setattr(urgent, 'NOTICE_PATH', str(tmp_path / 'urgent_notice.txt'))
    monkeypatch.setattr(urgent, 'STATE_PATH', str(tmp_path / 'urgent_notice.json'))
    monkeypatch.setattr(urgent, '_state', None)
    monkeypatch.setattr(urgent, '_state_sig', None)
    return tmp_path


def write_notice(text: str) -> None:
    with open(urgent.NOTICE_PATH, 'w', encoding='utf-8') as f:
        f.write(text)


def simulate_restart() -> None:
    """丢掉进程内缓存 —— 之后的读取只能来自磁盘,等价于重启后的冷启动。"""
    urgent._state = None
    urgent._state_sig = None


def _ev(*, is_group=True, group_id='G1'):
    ev = MagicMock()
    ev.is_group = is_group
    ev.is_direct = not is_group
    ev.is_interaction = False
    ev.group_id = group_id if is_group else ''
    ev.channel_id = ''
    ev.user_id = 'U1'
    ev.appid = 'APPID_X'
    ev.reply = AsyncMock()
    return ev


async def _menu_md(event, *, logo=None) -> str:
    """跑一遍 _send_welcome_menu,取回实际发出的 markdown。"""
    with patch.object(dispatcher, '_resolve_menu_logo', AsyncMock(return_value=logo)), \
         patch.object(dispatcher.page_logs, 'log_outgoing', MagicMock()):
        await dispatcher._send_welcome_menu(event)
    assert event.reply.await_args, '菜单没有发出'
    return event.reply.await_args.args[0]


async def _notify(content: str, *, gid='G1', is_group=True):
    """跑一遍 dispatcher._maybe_notify_urgent,返回 (event, 发出的正文 or None)。"""
    ev = _ev(is_group=is_group, group_id=gid)
    with patch.object(dispatcher.page_logs, 'log_outgoing', MagicMock()):
        await dispatcher._maybe_notify_urgent(ev, content, ev.group_id)
    sent = ev.reply.await_args.args[0] if ev.reply.await_args else None
    return ev, sent


def _mark_pushable(gid: str) -> None:
    """标记该群可主动推送 —— 菜单就不会追加「全量申请」行,便于隔离断言。"""
    import time as _t
    from plugins.LGTBot_ElainaBot.mod import helpers as _h
    _h._push_cache()[gid] = (True, _t.time() + 3600)


def _menu_baseline() -> str:
    """没有紧急公告时的菜单 markdown(私信 / 可推送群场景)。"""
    return (buttons.MENU_TEXT_HEADER + buttons.MENU_TEXT_BODY
            + buttons.MENU_HEADER_EXTRA_MD)


# ─────────────────────────────────────────────────────────────────────────
# 1. 文案读盘语义 —— 空态不留痕
# ─────────────────────────────────────────────────────────────────────────

def test_missing_file_reads_empty_and_creates_nothing():
    """文件不存在 → '' 且**不自动写占位** —— 平时 data/ 里就不该有这个文件。"""
    assert not os.path.exists(urgent.NOTICE_PATH)
    assert urgent.notice_text() == ''
    assert not os.path.exists(urgent.NOTICE_PATH)


def test_whitespace_only_file_reads_empty():
    """用户把内容删干净常会留下换行 / 空格,必须与"空"同义。"""
    write_notice('   \n\n\t \n')
    assert urgent.notice_text() == ''


def test_content_is_stripped():
    """首尾空行不带进引用块 —— 否则引用里会出现空的 ``>`` 行。"""
    write_notice('\n\n引擎维护中，预计 20:00 恢复\n\n')
    assert urgent.notice_text() == '引擎维护中，预计 20:00 恢复'


def test_read_error_is_swallowed(monkeypatch):
    """读盘异常不该炸掉欢迎菜单 / 建房流程 —— 退化成"没有公告"。"""
    write_notice('x')

    def _boom(*a, **kw):
        raise OSError('disk gone')

    monkeypatch.setattr('builtins.open', _boom)
    assert urgent.notice_text() == ''


def test_notice_is_read_live():
    """每次调用都重新读盘 —— 面板保存后下次 @bot 即生效,无需重启。"""
    write_notice('第一版')
    assert urgent.notice_text() == '第一版'
    write_notice('第二版')
    assert urgent.notice_text() == '第二版'


# ─────────────────────────────────────────────────────────────────────────
# 2. 总开关 + 落盘
# ─────────────────────────────────────────────────────────────────────────

def test_disabled_by_default():
    """没有状态文件 = 未启用。第三方部署 / 全新安装都不该凭空展示公告。"""
    assert urgent.is_enabled() is False
    assert urgent.notified_count() == 0


def test_enabled_survives_restart():
    """**核心需求**:启用状态写盘,重启后依然生效(除非手动关闭)。"""
    assert urgent.set_enabled(True) is True
    assert os.path.isfile(urgent.STATE_PATH)
    simulate_restart()
    assert urgent.is_enabled() is True

    assert urgent.set_enabled(False) is True
    simulate_restart()
    assert urgent.is_enabled() is False


def test_state_file_shape():
    """落盘结构固定三个字段,群号**定序**—— 否则每次写盘内容都在抖(备份 / diff 全是噪声)。

    用 9 个群倒序写入:``list(set)`` 的迭代序由字符串哈希决定(默认开启哈希随机化,每个进程都不一样),
    9 个元素刚好凑成 sorted 的概率约 1/9!,足以钉住"必须显式排序"。
    """
    urgent.set_enabled(True)
    want = [f'G{i}' for i in range(1, 10)]
    for gid in reversed(want):
        urgent.mark_notified(gid)
    with open(urgent.STATE_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
    assert data['enabled'] is True
    assert data['notified_groups'] == want                # sorted,不是插入序 / 集合序
    assert data['updated_at']                             # 有时间戳便于排查


@pytest.mark.parametrize('bad', ['{ 坏 json', '[]', 'null', '"text"'])
def test_corrupt_state_falls_back_to_disabled(bad):
    """状态文件坏了最坏也只能是"公告不显示",绝不能把菜单 / 建房带崩。"""
    with open(urgent.STATE_PATH, 'w', encoding='utf-8') as f:
        f.write(bad)
    assert urgent.is_enabled() is False
    assert urgent.notified_count() == 0


def test_external_edit_is_picked_up():
    """手工改 json(签名变化)也能被读到 —— 缓存不是"读一次就锁死"。"""
    assert urgent.is_enabled() is False
    with open(urgent.STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump({'enabled': True, 'notified_groups': ['GX', 'GX', ' ']}, f)
    assert urgent.is_enabled() is True
    assert urgent.notified_count() == 1          # 去重 + 丢掉空白项


def test_write_failure_is_reported(monkeypatch):
    """写盘失败要如实返回 False —— 面板据此提示"开关未生效",不谎报成功。"""
    def _boom(*a, **kw):
        raise OSError('read-only fs')

    monkeypatch.setattr('builtins.open', _boom)
    assert urgent.set_enabled(True) is False


# ─────────────────────────────────────────────────────────────────────────
# 3. 引用块(欢迎菜单出口)—— 只在启用时出现
# ─────────────────────────────────────────────────────────────────────────

def test_menu_block_empty_when_disabled():
    """**需求要点**:未启用时即便文案非空也一律不展示。"""
    write_notice('引擎维护中')
    assert urgent.is_enabled() is False
    assert urgent.menu_block() == ''


def test_menu_block_empty_when_enabled_without_text():
    """启用了但文案空 → 仍是空串(不是 '\\n'),菜单才不会多出空行。"""
    urgent.set_enabled(True)
    assert urgent.menu_block() == ''


def test_menu_block_pads_blank_line_before_and_after():
    """前后各垫一个空行:后面那个防 markdown lazy continuation 吸走后续普通行。"""
    write_notice('停服维护')
    urgent.set_enabled(True)
    assert urgent.menu_block() == '\n> 停服维护\n\n'


def test_menu_block_quotes_every_line_including_blanks():
    """空行也带裸 ``>`` —— 否则 QQ 客户端会在空行处把引用截成两块。"""
    write_notice('第一行\n\n第二行')
    urgent.set_enabled(True)
    assert urgent.menu_block() == '\n> 第一行\n>\n> 第二行\n\n'


def test_menu_block_keeps_inline_markdown():
    """引用(而非代码块)的意义:管理员在 txt 里写的加粗 / emoji 照常生效。"""
    write_notice('**重要** ⚠️')
    urgent.set_enabled(True)
    assert '> **重要** ⚠️' in urgent.menu_block()


# ─────────────────────────────────────────────────────────────────────────
# 4. 欢迎菜单拼装
# ─────────────────────────────────────────────────────────────────────────

async def test_menu_byte_identical_when_disabled_with_content():
    """**核心回归**:关态(即便有文案)的菜单与没这功能时逐字节相同。"""
    write_notice('引擎维护中')
    md = await _menu_md(_ev(is_group=False))
    assert md == _menu_baseline()


async def test_menu_byte_identical_when_enabled_without_content():
    """启用但没写文案 → 同样逐字节等于 baseline,连空行都不多。"""
    urgent.set_enabled(True)
    md = await _menu_md(_ev(is_group=False))
    assert md == _menu_baseline()


async def test_menu_appends_urgent_at_the_very_end():
    """启用 + 有文案:引用块垫在菜单最后(没有「全量申请」行时紧跟公告入口)。"""
    write_notice('引擎维护中')
    urgent.set_enabled(True)
    _mark_pushable('GP')                      # 可推送群 → 不追加「全量申请」
    md = await _menu_md(_ev(group_id='GP'))
    assert md.endswith(buttons.MENU_HEADER_EXTRA_MD + '\n> 引擎维护中\n\n')


async def test_menu_urgent_sits_below_full_volume_line():
    """与「全量申请」共存时:公告入口 → 免刷新授权 → 紧急公告(引用块在最后)。"""
    write_notice('引擎维护中')
    urgent.set_enabled(True)
    md = await _menu_md(_ev(group_id='GNOPUSH'))   # 无推送权限 → 追加全量申请行
    i_notice = md.index('text="更新公告"')
    i_full = md.index('text="全量申请"')
    i_urgent = md.index('> 引擎维护中')
    assert i_notice < i_full < i_urgent
    # 「免刷新授权」那行与引用块之间必须有空行,否则该行会被吸进引用里
    assert md.endswith(buttons.MENU_FULL_VOLUME_CMD_MD + '\n> 引擎维护中\n\n')


async def test_menu_shows_urgent_on_logo_branch_too():
    """图床可用(带 logo)的分支同样要拼上 —— 两条路径都得覆盖。"""
    write_notice('引擎维护中')
    urgent.set_enabled(True)
    _mark_pushable('GP')
    md = await _menu_md(_ev(group_id='GP'),
                        logo={'url': 'https://x/y.png', 'width': 100, 'height': 50})
    assert 'y.png' in md
    assert md.endswith(buttons.MENU_HEADER_EXTRA_MD + '\n> 引擎维护中\n\n')


async def test_menu_follows_toggle_without_restart():
    """开 → 关 一次生效:关掉后菜单立刻回到 baseline。"""
    write_notice('引擎维护中')
    _mark_pushable('GP')
    urgent.set_enabled(True)
    assert '> 引擎维护中' in await _menu_md(_ev(group_id='GP'))
    urgent.set_enabled(False)
    assert await _menu_md(_ev(group_id='GP')) == _menu_baseline()


# ─────────────────────────────────────────────────────────────────────────
# 5. 通知消息文本
# ─────────────────────────────────────────────────────────────────────────

def test_notify_message_needs_enabled_and_text():
    write_notice('引擎维护中')
    assert urgent.notify_message() == ''          # 未启用
    urgent.set_enabled(True)
    assert urgent.notify_message()                # 启用 + 有文案
    write_notice('')
    assert urgent.notify_message() == ''          # 启用但文案空


def test_notify_message_is_title_plus_raw_text():
    """固定标题 + ⚠️,正文用公告原文 —— **不套引用块**(需求明确)。"""
    write_notice('引擎维护中\n\n预计 20:00 恢复')
    urgent.set_enabled(True)
    md = urgent.notify_message()
    assert md.startswith('## ⚠️ ')
    assert '\n\n引擎维护中\n\n预计 20:00 恢复' in md
    # 正文里不该出现引用前缀
    assert '>' not in md


# ─────────────────────────────────────────────────────────────────────────
# 6. 已通知群记录 —— 两个动作互不越界
# ─────────────────────────────────────────────────────────────────────────

def test_mark_notified_counts_once_and_persists():
    urgent.set_enabled(True)
    assert urgent.mark_notified('G1') is True
    assert urgent.mark_notified('G1') is True      # 幂等
    assert urgent.notified_count() == 1
    assert urgent.is_notified('G1') is True
    simulate_restart()
    assert urgent.is_notified('G1') is True        # 活过重启
    assert urgent.notified_count() == 1


def test_pending_notify_only_for_unnotified_group():
    write_notice('引擎维护中')
    urgent.set_enabled(True)
    assert urgent.pending_notify('G1')             # 首次:要通知
    urgent.mark_notified('G1')
    assert urgent.pending_notify('G1') == ''       # 之后:不再打扰
    assert urgent.pending_notify('G2')             # 别的新群照常


def test_disable_keeps_notified_records():
    """**需求明确**:关闭公告不清记录,重新启用时老群不被重复通知。"""
    write_notice('引擎维护中')
    urgent.set_enabled(True)
    urgent.mark_notified('G1')
    urgent.set_enabled(False)
    assert urgent.notified_count() == 1
    urgent.set_enabled(True)
    assert urgent.pending_notify('G1') == ''       # 老群仍然静默
    assert urgent.pending_notify('G2')             # 新群照常


def test_reset_clears_records_but_keeps_switch():
    """重置只清记录、不动开关;清完这些群会重新收到公告。"""
    write_notice('引擎维护中')
    urgent.set_enabled(True)
    urgent.mark_notified('G1')
    urgent.mark_notified('G2')
    assert urgent.reset_notified() == 2            # 返回清掉的条数
    assert urgent.notified_count() == 0
    assert urgent.is_enabled() is True             # 开关不受影响
    assert urgent.pending_notify('G1')             # 老群重新进入通知范围
    simulate_restart()
    assert urgent.notified_count() == 0            # 清空也落盘了


def test_reset_on_empty_is_noop():
    assert urgent.reset_notified() == 0


# ─────────────────────────────────────────────────────────────────────────
# 7. dispatcher:新群首次建房才通知
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def enabled_notice():
    write_notice('引擎维护中')
    urgent.set_enabled(True)


@pytest.mark.parametrize('content', [
    '/新游戏 决胜五子',
    '新游戏',
    '/随机游戏',
    '#新游戏 决胜五子',
    '/新游戏 决胜五子 单机',      # 单机局不特例,命令层面一视同仁
])
async def test_new_game_commands_notify(enabled_notice, content):
    _ev_, sent = await _notify(content)
    assert sent and sent.startswith('## ⚠️ ')
    assert urgent.is_notified('G1') is True


@pytest.mark.parametrize('content', ['/帮助', '/加入', '', '/游戏列表'])
async def test_non_new_game_commands_do_not_notify(enabled_notice, content):
    _ev_, sent = await _notify(content)
    assert sent is None
    assert urgent.notified_count() == 0


async def test_group_is_notified_only_once(enabled_notice):
    _ev_, first = await _notify('/新游戏 决胜五子')
    assert first
    _ev2, second = await _notify('/新游戏 决胜五子')
    assert second is None
    assert urgent.notified_count() == 1


async def test_other_group_still_gets_notified(enabled_notice):
    await _notify('/新游戏 决胜五子', gid='G1')
    _ev_, sent = await _notify('/新游戏 决胜五子', gid='G2')
    assert sent
    assert urgent.notified_count() == 2


async def test_direct_message_never_notifies(enabled_notice):
    """私信不通知:已通知记录按群号存,私信没有"新群"可言。

    这里刻意给私信事件挂上 ``channel_id`` —— 频道私信
    (``DIRECT_MESSAGE_CREATE``)确实带 channel_id,而 dispatcher 传进来的 gid 是
    ``group_id or channel_id``,**可能非空**。所以闸必须看 ``is_group``,
    只判断"gid 有没有值"会把频道私信也通知一遍(还会污染已通知群记录)。
    """
    ev = _ev(is_group=False)
    ev.channel_id = 'C1'
    with patch.object(dispatcher.page_logs, 'log_outgoing', MagicMock()):
        await dispatcher._maybe_notify_urgent(ev, '/新游戏 决胜五子', 'C1')
    assert ev.reply.await_args is None
    assert urgent.notified_count() == 0


async def test_no_notify_when_disabled():
    write_notice('引擎维护中')                      # 有文案但开关是关的
    _ev_, sent = await _notify('/新游戏 决胜五子')
    assert sent is None
    assert urgent.notified_count() == 0             # 也不该记录


async def test_no_notify_when_text_empty():
    urgent.set_enabled(True)                        # 启用但没写文案
    _ev_, sent = await _notify('/新游戏 决胜五子')
    assert sent is None
    assert urgent.notified_count() == 0


async def test_send_failure_does_not_mark_group(enabled_notice):
    """发送失败(网络 / 配额)不落记录 —— 下次建房还能补上,不静默丢公告。"""
    ev = _ev()
    ev.reply = AsyncMock(side_effect=RuntimeError('QQ rejected'))
    with patch.object(dispatcher.page_logs, 'log_outgoing', MagicMock()):
        await dispatcher._maybe_notify_urgent(ev, '/新游戏 决胜五子', 'G1')
    assert urgent.is_notified('G1') is False


async def test_notify_burns_one_passive_ref(enabled_notice):
    """直接 reply 会真实吃掉一条被动引用额度,必须同步烧掉计数(否则引擎超发被吞)。"""
    from plugins.LGTBot_ElainaBot.mod import quota
    with patch.object(quota, 'try_consume_ref', MagicMock()) as consume:
        await _notify('/新游戏 决胜五子')
    consume.assert_called_once_with('g:G1')


def test_both_dispatch_paths_notify():
    """消息事件与按钮 INTERACTION 两条路径都要挂 —— 菜单快捷开局按钮走后者。

    源码级断言:漏挂一条路径时,单测很难在集成层面察觉(两个 handler 都很长),
    但"少一个调用点"是确定性的。
    """
    import inspect
    src = inspect.getsource(dispatcher)
    assert src.count('await _maybe_notify_urgent(') == 2


# ─────────────────────────────────────────────────────────────────────────
# 8. Web 面板「配置管理」
# ─────────────────────────────────────────────────────────────────────────

def _page_config():
    pytest.importorskip('aiohttp')
    from plugins.LGTBot_ElainaBot.mod.webui import page_config
    return page_config


def _fragment_payload(html: str) -> dict:
    """按前端的方式解析 ``<pre id="result">…</pre>`` fragment。"""
    m = re.fullmatch(r'<pre id="result">(.*)</pre>', html, re.S)
    assert m, f'不是合法 fragment: {html[:120]!r}'
    import html as _html
    return json.loads(_html.unescape(m.group(1)))


def test_panel_payload_carries_notice_and_state():
    pc = _page_config()
    urgent.set_enabled(True)
    urgent.mark_notified('G1')
    data = json.loads(pc.get_data())['urgent_notice']
    assert data['abs_path'].endswith('urgent_notice.txt')
    assert data['state_path'].endswith('urgent_notice.json')
    assert data['enabled'] is True
    assert data['notified_count'] == 1


def test_panel_toggle_action_flips_and_reports():
    pc = _page_config()
    on = _fragment_payload(pc.render_urgent_toggle())
    assert on['success'] is True and on['enabled'] is True
    assert urgent.is_enabled() is True
    off = _fragment_payload(pc.render_urgent_toggle())
    assert off['enabled'] is False
    assert urgent.is_enabled() is False


def test_panel_toggle_keeps_notified_records():
    """面板关开关同样不清记录(与 urgent.set_enabled 的语义一致)。"""
    pc = _page_config()
    urgent.set_enabled(True)
    urgent.mark_notified('G1')
    payload = _fragment_payload(pc.render_urgent_toggle())     # 关掉
    assert payload['enabled'] is False
    assert payload['notified_count'] == 1
    assert urgent.notified_count() == 1


def test_panel_toggle_reports_write_failure(monkeypatch):
    """写盘失败时不能谎报成功 —— 前端会把 success=False 显示成红字。"""
    pc = _page_config()
    monkeypatch.setattr(urgent, '_write_state', lambda st: False)
    payload = _fragment_payload(pc.render_urgent_toggle())
    assert payload['success'] is False


def test_panel_reset_action_clears():
    pc = _page_config()
    urgent.set_enabled(True)
    urgent.mark_notified('G1')
    urgent.mark_notified('G2')
    payload = _fragment_payload(pc.render_urgent_reset())
    assert payload['success'] is True and payload['cleared'] == 2
    assert payload['notified_count'] == 0
    assert urgent.is_enabled() is True         # 重置不动开关
    assert urgent.notified_count() == 0


def test_panel_actions_are_registered_and_hidden():
    """两个 action 必须登记进 _HIDDEN_KEYS,否则会作为独立页面漏进侧边栏。"""
    pytest.importorskip('aiohttp')
    from plugins.LGTBot_ElainaBot.mod.webui import main as webui_main
    for key in (webui_main._URGENT_TOGGLE_KEY, webui_main._URGENT_RESET_KEY):
        assert key in webui_main._HIDDEN_KEYS
    src = __import__('inspect').getsource(webui_main.register)
    assert '_URGENT_TOGGLE_KEY' in src and '_URGENT_RESET_KEY' in src


def test_panel_buttons_present_in_urgent_section():
    """两个按钮要落在紧急公告那一段里(而不是别的编辑器旁边)。"""
    pc = _page_config()
    html = pc.render_tab_html()
    i_urgent = html.index('cfg-urgent-editor')
    i_trouble = html.index('cfg-trouble-editor')
    for btn in ('cfg-urgent-toggle', 'cfg-urgent-reset'):
        assert i_urgent < html.index(btn) < i_trouble, btn


def test_panel_js_wires_both_actions_with_confirm():
    """两个动作都必须先弹确认(误点代价:全群刷公告 / 记录全清且不可撤销)。"""
    pc = _page_config()
    js = pc.render_tab_js()
    assert "urgent_toggle: '__lgtbot_urgent_toggle'" in js
    assert "urgent_reset: '__lgtbot_urgent_reset'" in js
    for fn in ('cfgUrgentToggle', 'cfgUrgentReset'):
        body = js[js.index('async function ' + fn):]
        body = body[:body.index('\n}')]
        assert 'dashConfirm' in body, f'{fn} 少了确认弹窗'
    # 按钮绑定在 DOMContentLoaded 里
    tail = js[js.index("addEventListener('DOMContentLoaded'"):]
    assert 'cfg-urgent-toggle' in tail and 'cfg-urgent-reset' in tail


def test_panel_toggle_button_has_state_style():
    """启用 / 关闭两态要有视觉区分 —— .active 样式 + JS 按服务端状态切换。"""
    pc = _page_config()
    assert '.cfg-urgent-toggle.active' in pc.TAB_CSS
    js = pc.render_tab_js()
    assert "classList.toggle('active', on)" in js


# ─────────────────────────────────────────────────────────────────────────
# 9. 备份清单
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('name', ['urgent_notice.txt', 'urgent_notice.json'])
def test_urgent_files_are_backed_up(name):
    """文案是手工写的、记录丢了会让所有群再收一次 —— 两份都得进备份。"""
    path = os.path.join(boot.DATA_DIR, name)
    os.makedirs(boot.DATA_DIR, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('{}' if name.endswith('.json') else '维护公告')
    try:
        arcs = [arc for arc, _p, _k in backup._collect_sources()]
        assert f'data/{name}' in arcs
    finally:
        os.remove(path)
