#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""更新公告的分段与三种回执 —— 默认只发最新、历史更新公告列表、按日期区间查询。

分段只认 ``----- YYYY年M月D日 -----`` 日期行,任何截断都落在日期行上:同一日期永远整段出现。
"""

from __future__ import annotations

import re
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.LGTBot_ElainaBot.mod import buttons, dispatcher, notice


def _doc(*days_and_bodies) -> str:
    """按 (日期, 正文行数) 拼一份最新在前的公告,正文行写成「MMDD-序号」方便断言。"""
    out = []
    for d, n in days_and_bodies:
        out.append(f'----- {d.year}年{d.month}月{d.day}日 -----')
        out += [f'{d.month:02d}{d.day:02d}-{i}' for i in range(n)]
        out.append('')
    return '\n'.join(out)


D1, D2, D3, D4 = date(2026, 10, 12), date(2026, 10, 6), date(2026, 8, 27), date(2025, 12, 30)


# ─────────────────────────────────────────────────────────────────────────
# 分段
# ─────────────────────────────────────────────────────────────────────────

def test_split_only_at_date_lines():
    """★ 只在日期行处切;别的 ----- 行、写错的日期都算正文;文件首尾空行去掉,段间空行照原样。"""
    text = ('\n\n----- 2026年10月12日 -----\n第一行\n----- 主框架 -----\n----- 2026年2月30日 -----\n\n'
            '----- 2026年10月6日 -----\r\n第二段\n\n')
    s = notice.split_sections(text)
    assert [x.day for x in s] == [D1, D2]
    assert s[0].lines == ['----- 2026年10月12日 -----', '第一行', '----- 主框架 -----',
                          '----- 2026年2月30日 -----', '']
    assert s[1].lines == ['----- 2026年10月6日 -----', '第二段']


def test_text_before_the_first_date_line_belongs_to_the_newest_section():
    s = notice.split_sections('开头的总述\n----- 2026年10月12日 -----\n正文\n----- 2026年10月6日 -----\n旧的')
    assert [x.day for x in s] == [D1, D2]
    assert s[0].lines[:2] == ['开头的总述', '----- 2026年10月12日 -----']


def test_adjacent_same_date_is_one_section():
    """★ 同一日期写了两个日期行(紧挨着)也合成一段,区间不会从中间把它拆开。"""
    s = notice.split_sections('----- 2026年8月11日 -----\na\n----- 2026年8月11日 -----\nb\n'
                              '----- 2026年8月7日 -----\nc')
    assert [x.day for x in s] == [date(2026, 8, 11), date(2026, 8, 7)]
    assert s[0].lines[-1] == 'b'


def test_text_without_date_lines_is_one_undated_section():
    assert notice.split_sections('暂无更新公告') == [notice.Section(None, ['暂无更新公告'])]
    assert notice.split_sections('\n \n') == []


# ─────────────────────────────────────────────────────────────────────────
# 取最新 / 拼区间 / 按区间查
# ─────────────────────────────────────────────────────────────────────────

def test_recent_takes_whole_sections_within_the_budget_and_at_least_one():
    s = notice.split_sections(_doc((D1, 2), (D2, 2), (D3, 9)))      # 每段 = 日期行 + 正文 + 段尾空行
    assert [x.day for x in notice.recent(s, 8)] == [D1, D2]
    assert [x.day for x in notice.recent(s, 3)] == [D1]               # 第一段超了也照给
    assert [x.day for x in notice.recent(s, 100)] == [D1, D2, D3]
    # 放不下就停,不能跳过中间那个日期去拿后面更短的
    s = notice.split_sections(_doc((D1, 2), (D2, 9), (D3, 1)))
    assert [x.day for x in notice.recent(s, 8)] == [D1]


def test_older_ranges_skip_the_newest_and_never_split_a_date():
    """★ 区间从第二段开始按行数拼;超长的单个日期独占一个区间;每个区间按日期查回来正好是拼进去的那几段。"""
    days = [date(2026, 10, d) for d in (12, 9, 6, 3, 1)]
    s = notice.split_sections(_doc((days[0], 3), (days[1], 3), (days[2], 3), (days[3], 40), (days[4], 1)))
    ranges = notice.older_ranges(s, max_lines=10)
    assert ranges == [(days[1], days[2]), (days[3], days[3]), (days[4], days[4])]
    grouped = [[x.day for x in notice.in_range(s, a, b)] for a, b in ranges]
    assert grouped == [[days[1], days[2]], [days[3]], [days[4]]]
    assert all(days[0] not in g for g in grouped)


def test_older_ranges_put_important_updates_on_the_same_timeline():
    """★ 重要更新与公告详情按日期合成一条时间线:落在公告详情两段之间的重要更新也有区间收留,同一天两份都有时算一整天、不拆开。"""
    mid = date(2026, 9, 14)
    details = notice.split_sections(_doc((D1, 1), (D2, 1), (D3, 1)))
    important = notice.split_sections(_doc((mid, 1), (D3, 1)))
    assert notice.older_ranges(details, important) == [(D2, D3)]
    ranges = notice.older_ranges(details, important, max_lines=6)
    assert ranges == [(D2, mid), (D3, D3)]                 # 8/27 两份合计一天
    covered = [[x.day for x in notice.in_range(details[1:] + important, a, b)] for a, b in ranges]
    assert covered == [[D2, mid], [D3, D3]]
    # 比公告详情最新一段还新的重要更新照样进列表
    later = date(2026, 10, 20)
    only_one = notice.split_sections(_doc((D1, 1)))
    assert notice.older_ranges(only_one, notice.split_sections(_doc((later, 1)))) == [(later, later)]


def test_in_range_is_inclusive_and_order_free():
    s = notice.split_sections(_doc((D1, 1), (D2, 1), (D3, 1)))
    assert [x.day for x in notice.in_range(s, D3, D2)] == [D2, D3]
    assert [x.day for x in notice.in_range(s, D2, D3)] == [D2, D3]       # 列表里的指令是新的在前
    assert [x.day for x in notice.in_range(s, D2, D2)] == [D2]


def test_labels_and_query_args():
    assert notice.range_label(D1, D2) == '2026年10月12日 ~ 10月6日'
    assert notice.range_label(D1, D4) == '2026年10月12日 ~ 2025年12月30日'      # 跨年后一个也带年份
    assert notice.range_label(D3, D3) == '2026年8月27日'
    assert notice.range_arg(D1, D2) == '20261012-20261006'
    assert notice.range_arg(D3, D3) == '20260827'
    assert notice.parse_day('20261012') == D1
    assert notice.parse_day('20260230') is None and notice.parse_day('2026101') is None


def test_render_keeps_the_file_spacing():
    s = notice.split_sections('----- 2026年10月12日 -----\na\n----- 2026年10月6日 -----\nb\n\n'
                              '----- 2026年8月27日 -----\nc\n')
    assert notice.render(s[:2]) == '----- 2026年10月12日 -----\na\n----- 2026年10月6日 -----\nb'
    assert notice.render(s[1:]) == '----- 2026年10月6日 -----\nb\n\n----- 2026年8月27日 -----\nc'


# ─────────────────────────────────────────────────────────────────────────
# 三种回执
# ─────────────────────────────────────────────────────────────────────────

def _ev(content='更新公告', is_interaction=False):
    ev = MagicMock()
    ev.is_group, ev.is_interaction, ev.group_id, ev.user_id = True, is_interaction, 'G1', 'U1'
    ev.content = content
    ev.reply = AsyncMock()
    return ev


@pytest.fixture
def notices(monkeypatch):
    """两份公告换成内存文本;返回 (更新公告, 重要更新) 的写入函数。"""
    monkeypatch.setattr(dispatcher.helpers, 'is_foreign_event', lambda e: False)
    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', False)
    box = {'notice': _doc((D1, 2), (D2, 2), (D3, 2)), 'important': ''}
    monkeypatch.setattr(dispatcher, '_read_update_notice', lambda: box['notice'])
    monkeypatch.setattr(dispatcher, '_read_important_update', lambda: box['important'])
    return box


async def _say(content, *, interaction=False, match=None):
    ev = _ev(content, interaction)
    await dispatcher.lgtbot_update_notice(ev, match)
    return ev.reply.await_args.args[0], ev.reply.await_args.kwargs.get('buttons')


async def test_default_reply_shows_only_the_newest_section(notices):
    """★ 「更新公告」只发公告详情最新一个日期段,重要更新只取最近几段;底部一个「历史更新公告」回调按钮。"""
    notices['important'] = _doc((D1, 1), (D2, 1), (D3, 30))
    md, kb = await _say('更新公告')
    assert md.split('\n')[0] == '## 📢 最新更新公告' and '---' not in md.split('\n')
    assert '1012-1' in md and '1006-0' not in md.split('```公告详情')[1]
    important = md.split('```重要更新')[1].split('```')[0]
    assert '----- 2026年10月6日 -----' in important and '0827-0' not in important
    assert kb == [[buttons.BTN_NOTICE_OLDER]]
    assert (buttons.BTN_NOTICE_OLDER['type'], buttons.BTN_NOTICE_OLDER['style']) == (1, 4)
    assert 'enter' not in buttons.BTN_NOTICE_OLDER


async def test_default_reply_without_older_sections_has_no_older_button(notices, monkeypatch):
    notices['notice'] = '暂无更新公告'
    md, kb = await _say('更新公告')
    assert kb is None
    notices['important'] = _doc((D3, 1))                   # 只有重要更新有日期段,也挂「更早」
    _md, kb = await _say('更新公告')
    assert kb == [[buttons.BTN_NOTICE_OLDER]]
    notices['important'] = ''
    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', True)
    notices['notice'] = _doc((D1, 1), (D2, 1))
    _md, kb = await _say('更新公告')
    assert kb == [[buttons.BTN_NOTICE_OLDER], [buttons.BTN_SPONSOR]]


async def test_older_list_is_cmd_inputs_labelled_with_their_dates(notices):
    """★ 点「历史更新公告」新发一条列表:每行一个回填指令;最新那段公告详情不在列表里,重要更新并进同一条时间线。"""
    notices['important'] = _doc((date(2026, 7, 1), 1))
    md, kb = await _say('历史更新公告', interaction=True)
    assert kb is None and '---' not in md.split('\n')
    hint = next(line for line in md.split('\n') if '蓝字' in line)
    assert hint.startswith('> ')
    cmds = re.findall(r'<qqbot-cmd-input text="([^"]+)" show="([^"]+)"', md)
    assert cmds == [('更新公告 20261006-20260701', '2026年10月6日 ~ 7月1日')]
    assert '2026年10月12日' not in md


async def test_range_query_returns_exactly_that_range(notices):
    """★ 历史页:标题只写「历史更新公告」,第二行用引用块写时间段;同期的重要更新在上、公告详情在下,没有的那块不出。"""
    notices['important'] = _doc((date(2026, 9, 14), 1), (date(2026, 7, 1), 1))
    md, kb = await _say('更新公告 20260827-20261006')
    lines = md.split('\n')
    assert lines[:2] == ['## 📜 历史更新公告', '> 📅 2026年10月6日 ~ 8月27日'] and '---' not in lines
    important = md.split('```重要更新')[1].split('```')[0]
    assert '0914-0' in important and '0701-0' not in md
    details = md.split('```公告详情')[1]
    assert '1006-0' in details and '0827-1' in details and '1012-0' not in md
    assert md.index('```重要更新') < md.index('```公告详情')
    assert kb == [[buttons.BTN_NOTICE_OLDER]]
    md, _kb = await _say('更新公告 20260827')
    assert '```重要更新' not in md and md.split('\n')[1] == '> 📅 2026年8月27日'


@pytest.mark.usefixtures('notices')
async def test_range_query_uses_the_framework_match_too():
    m = re.match(dispatcher._P_NOTICE, '更新公告20260827')
    md, _kb = await _say('', match=m)
    assert '0827-0' in md and '1006-0' not in md


@pytest.mark.usefixtures('notices')
@pytest.mark.parametrize('content', ['更新公告 20991231', '更新公告 20260230-20260231'])
async def test_empty_or_invalid_range_does_not_echo_the_input(content):
    md, kb = await _say(content)
    assert md.startswith('## 📜 历史更新公告\n') and '没有更新公告' in md and '---' not in md.split('\n')
    assert not re.search(r'\d{8}', md) and '2099' not in md
    assert kb == [[buttons.BTN_NOTICE_OLDER]]


@pytest.mark.parametrize('text, hit', [
    ('更新公告', True), ('/更新公告', True), ('更新公告 20261006-20261004', True),
    ('更新公告20260813', True), ('更新公告 20261006 ~ 20261004', True), ('历史更新公告', True),
    ('/历史更新公告', True), ('更新公告 abc', False), ('更新公告 2026', False),
])
def test_notice_commands_never_reach_the_engine(text, hit):
    """新的几种写法同样是专属指令,不会再漏给引擎当未知指令。"""
    assert dispatcher._is_exclusive_command(text) is hit
