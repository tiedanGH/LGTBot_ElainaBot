#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""紧急公告测试 —— ``data/urgent_notice.txt`` → 欢迎菜单引用块。

这个功能的风险全在**空态**:平时文件是空的(或压根不存在),此时菜单必须与没有这功能时**逐字节一致**
多一个空行,所有用户每次 @bot 都会看到菜单里凭空多出一道缝。所以本文件把"空"当被测主角:

  · 空 / 全空白 / 文件不存在 → ``_read_urgent_notice()`` 返回 ''、**不创建**占位文件
  · 空 → 菜单 markdown 与 baseline 严格相等(不预留空行)
  · 非空 → 引用块垫在菜单最后(「⚡ 免刷新授权」下方),前后各垫一个空行
    (markdown 的 lazy continuation 会把紧贴引用后的普通行吸进引用里)
  · 每次读盘 —— 面板改完下次 @bot 就生效,无需重启
  · 面板「配置管理」里排在「更新公告」与「疑难解答」之间,且进备份清单
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.LGTBot_ElainaBot.mod import backup, boot, buttons, dispatcher


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def urgent_file(tmp_path, monkeypatch):
    """把紧急公告文件指到 tmp,返回「写入内容」的函数;不写 = 文件不存在。"""
    path = tmp_path / 'urgent_notice.txt'
    monkeypatch.setattr(dispatcher, '_URGENT_NOTICE_PATH', str(path))

    def _write(text: str):
        path.write_text(text, encoding='utf-8')
        return path
    _write.path = path
    return _write


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
    from unittest.mock import patch
    with patch.object(dispatcher, '_resolve_menu_logo', AsyncMock(return_value=logo)), \
         patch.object(dispatcher.page_logs, 'log_outgoing', MagicMock()):
        await dispatcher._send_welcome_menu(event)
    assert event.reply.await_args, '菜单没有发出'
    return event.reply.await_args.args[0]


def _mark_pushable(gid: str) -> None:
    """标记该群可主动推送 —— 菜单就不会追加「全量申请」行,便于隔离断言。"""
    import time as _t
    from plugins.LGTBot_ElainaBot.mod import helpers as _h
    _h._push_cache()[gid] = (True, _t.time() + 3600)


# ─────────────────────────────────────────────────────────────────────────
# 1. 读盘语义 —— 空态不留痕
# ─────────────────────────────────────────────────────────────────────────

def test_missing_file_reads_empty_and_creates_nothing(urgent_file):
    """文件不存在 → '' 且**不自动写占位** —— 平时 data/ 里就不该有这个文件。"""
    assert not os.path.exists(urgent_file.path)
    assert dispatcher._read_urgent_notice() == ''
    assert not os.path.exists(urgent_file.path)


def test_whitespace_only_file_reads_empty(urgent_file):
    """用户把内容删干净常会留下换行 / 空格,必须与"空"同义。"""
    urgent_file('   \n\n\t \n')
    assert dispatcher._read_urgent_notice() == ''


def test_content_is_stripped(urgent_file):
    """首尾空行不带进引用块 —— 否则引用里会出现空的 ``>`` 行。"""
    urgent_file('\n\n引擎维护中，预计 20:00 恢复\n\n')
    assert dispatcher._read_urgent_notice() == '引擎维护中，预计 20:00 恢复'


def test_read_error_is_swallowed(urgent_file, monkeypatch):
    """读盘异常不该炸掉整个欢迎菜单 —— 退化成"没有公告"。"""
    urgent_file('x')

    def _boom(*a, **kw):
        raise OSError('disk gone')

    monkeypatch.setattr(dispatcher.os.path, 'isfile', lambda p: True)
    monkeypatch.setattr('builtins.open', _boom)
    assert dispatcher._read_urgent_notice() == ''


def test_read_is_live(urgent_file):
    """每次调用都重新读盘 —— 面板保存后下次 @bot 即生效,无需重启。"""
    urgent_file('第一版')
    assert dispatcher._read_urgent_notice() == '第一版'
    urgent_file('第二版')
    assert dispatcher._read_urgent_notice() == '第二版'


# ─────────────────────────────────────────────────────────────────────────
# 2. 引用块渲染
# ─────────────────────────────────────────────────────────────────────────

def test_block_is_empty_string_when_no_content(urgent_file):
    """空 → **空串**,不是 '\\n'。菜单靠这个才做到"不预留空行"。"""
    assert dispatcher._menu_urgent_block() == ''
    urgent_file('')
    assert dispatcher._menu_urgent_block() == ''


def test_block_pads_blank_line_before_and_after(urgent_file):
    """前后各垫一个空行:后面那个是刚需 —— 少了它,紧跟的「全量申请」内联指令
    会被 markdown 的 lazy continuation 吸进引用块。"""
    urgent_file('停服维护')
    assert dispatcher._menu_urgent_block() == '\n> 停服维护\n\n'


def test_block_quotes_every_line_including_blanks(urgent_file):
    """空行也带裸 ``>`` —— 否则 QQ 客户端会在空行处把引用截成两块。"""
    urgent_file('第一行\n\n第二行')
    assert dispatcher._menu_urgent_block() == '\n> 第一行\n>\n> 第二行\n\n'


def test_block_keeps_inline_markdown(urgent_file):
    """引用(而非代码块)的意义:管理员在 txt 里写的加粗 / emoji 照常生效。"""
    urgent_file('**重要** ⚠️')
    assert '> **重要** ⚠️' in dispatcher._menu_urgent_block()


# ─────────────────────────────────────────────────────────────────────────
# 3. 欢迎菜单拼装
# ─────────────────────────────────────────────────────────────────────────

async def test_menu_byte_identical_to_baseline_when_empty(urgent_file):
    """**核心回归**:空态菜单与没有这功能时逐字节相同,连空行都不多。"""
    md = await _menu_md(_ev(is_group=False))
    assert md == (buttons.MENU_TEXT_HEADER
                  + buttons.MENU_TEXT_BODY
                  + buttons.MENU_HEADER_EXTRA_MD)


async def test_menu_appends_urgent_at_the_very_end(urgent_file):
    """引用块垫在菜单最后;没有「全量申请」行时紧跟公告入口(隔一个空行)。"""
    urgent_file('引擎维护中')
    _mark_pushable('GP')                      # 可推送群 → 不追加「全量申请」
    md = await _menu_md(_ev(group_id='GP'))
    assert md.endswith(buttons.MENU_HEADER_EXTRA_MD + '\n> 引擎维护中\n\n')


async def test_menu_urgent_sits_below_full_volume_line(urgent_file):
    """与「全量申请」共存时:公告入口 → 免刷新授权 → 紧急公告(引用块在最后)。"""
    urgent_file('引擎维护中')
    md = await _menu_md(_ev(group_id='GNOPUSH'))   # 无推送权限 → 追加全量申请行
    i_notice = md.index('text="更新公告"')
    i_full = md.index('text="全量申请"')
    i_urgent = md.index('> 引擎维护中')
    assert i_notice < i_full < i_urgent
    # 「免刷新授权」那行与引用块之间必须有空行,否则该行会被吸进引用里
    assert md.endswith(buttons.MENU_FULL_VOLUME_CMD_MD + '\n> 引擎维护中\n\n')


async def test_menu_shows_urgent_on_logo_branch_too(urgent_file):
    """图床可用(带 logo)的分支同样要拼上 —— 两条路径都得覆盖。"""
    urgent_file('引擎维护中')
    _mark_pushable('GP')
    md = await _menu_md(_ev(group_id='GP'),
                        logo={'url': 'https://x/y.png', 'width': 100, 'height': 50})
    assert 'y.png' in md
    assert md.endswith(buttons.MENU_HEADER_EXTRA_MD + '\n> 引擎维护中\n\n')


async def test_menu_reflects_edits_without_restart(urgent_file):
    """面板改一次、菜单跟一次(含改回空 → 菜单回到 baseline)。"""
    _mark_pushable('GP')
    urgent_file('第一版')
    assert '> 第一版' in await _menu_md(_ev(group_id='GP'))
    urgent_file('第二版')
    md = await _menu_md(_ev(group_id='GP'))
    assert '> 第二版' in md and '第一版' not in md
    urgent_file('')                            # 清空 → 整块消失
    assert await _menu_md(_ev(group_id='GP')) == (
        buttons.MENU_TEXT_HEADER + buttons.MENU_TEXT_BODY
        + buttons.MENU_HEADER_EXTRA_MD)


# ─────────────────────────────────────────────────────────────────────────
# 4. Web 面板「配置管理」
# ─────────────────────────────────────────────────────────────────────────

def _page_config():
    pytest.importorskip('aiohttp')
    from plugins.LGTBot_ElainaBot.mod.webui import page_config
    return page_config


def test_panel_payload_carries_urgent_notice():
    """前端靠 dataKey ``urgent_notice`` 取数据,漏了 textarea 就永远是空的。"""
    import json
    pc = _page_config()
    data = json.loads(pc.get_data())
    assert 'urgent_notice' in data
    assert data['urgent_notice']['abs_path'].endswith('urgent_notice.txt')


def test_panel_section_between_notice_and_trouble():
    """需求明确要求的位置:更新公告 → 紧急公告 → 疑难解答。"""
    pc = _page_config()
    html = pc.render_tab_html()
    i_notice = html.index('cfg-notice-editor')
    i_urgent = html.index('cfg-urgent-editor')
    i_trouble = html.index('cfg-trouble-editor')
    assert i_notice < i_urgent < i_trouble


def test_panel_js_entry_between_notice_and_trouble():
    """JS 表项顺序无关渲染,但保持与 HTML 同序,读代码时不用来回跳。"""
    pc = _page_config()
    js = pc.render_tab_js()
    assert "dataKey: 'urgent_notice'" in js
    assert (js.index("dataKey: 'update_notice'")
            < js.index("dataKey: 'urgent_notice'")
            < js.index("dataKey: 'troubleshooting'"))


def test_panel_editor_ids_match_between_html_and_js():
    """JS 表项声明的 5 个 id 在 HTML 里都要有 ``id="…"`` **精确**对应。

    任何一处拼错都是静默失效:textarea 永远填不上内容、按钮点了没反应,页面上
    看不出任何异常。所以这里比"子串出现过"严格 —— 按 ``id="…"`` 全等匹配。
    """
    import re
    pc = _page_config()
    html, js = pc.render_tab_html(), pc.render_tab_js()
    i = js.index('urgent: {')
    entry = js[i:js.index('},', i)]
    for field in ('editorId', 'pathId', 'msgId', 'saveBtnId', 'revertBtnId'):
        m = re.search(field + r": '([^']+)'", entry)
        assert m, f'JS 表项缺 {field}'
        assert f'id="{m.group(1)}"' in html, f'{field} = {m.group(1)} 在 HTML 里没有对应元素'


# ─────────────────────────────────────────────────────────────────────────
# 5. 备份清单
# ─────────────────────────────────────────────────────────────────────────

def test_urgent_notice_txt_is_backed_up():
    """手工维护的文本,丢了没法从别处重建 —— 必须进备份清单。"""
    path = os.path.join(boot.DATA_DIR, 'urgent_notice.txt')
    os.makedirs(boot.DATA_DIR, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('维护公告')
    try:
        arcs = [arc for arc, _p, _k in backup._collect_sources()]
        assert 'data/urgent_notice.txt' in arcs
    finally:
        os.remove(path)
