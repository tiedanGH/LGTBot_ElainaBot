#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""更新公告的分段 —— 按日期行切开:默认只给最新一段,更早的(连同同期的重要更新)按日期区间列出与查询。

``update_notice.txt``(更新公告)与 ``important_update.txt``(重要更新)都是最新写在最上面,
每段以 ``----- YYYY年M月D日 -----`` 日期行开头。只在日期行处切,同一日期永远整段出现。
读盘仍在 dispatcher(每次现读 = 热更新),这里只做纯文本处理。
"""

from __future__ import annotations

import re
from datetime import date
from typing import NamedTuple

# 分段只认这种日期行;别的 ----- 行照常算正文
_DATE_LINE = re.compile(r'^-{3,}\s*(\d{4})年(\d{1,2})月(\d{1,2})日\s*-{3,}$')

# 「更新公告」里重要更新的行数上限
IMPORTANT_LINES = 10
# 「历史更新公告」里一个日期区间的行数上限(两份合计);超长的单个日期独占一个区间
RANGE_LINES = 30


class Section(NamedTuple):
    day: date | None     # None = 整份文案没有日期行
    lines: list


def _day(line: str) -> date | None:
    m = _DATE_LINE.match(line.strip())
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def _trim(lines: list) -> list:
    i, j = 0, len(lines)
    while i < j and not lines[i].strip():
        i += 1
    while j > i and not lines[j - 1].strip():
        j -= 1
    return lines[i:j]


def split_sections(text: str) -> list:
    """切成 ``Section`` 列表(文件顺序,最新在前)。段内原样保留,段间空行照文件里的写法,只去掉整份文案首尾的空行。
    第一个日期行之前的文字并进第一段;紧挨着的同一日期合成一段,区间不会把它拆开。"""
    out, day, buf = [], None, []
    for raw in _trim(str(text or '').replace('\r\n', '\n').split('\n')):
        line = raw.rstrip()
        d = _day(line)
        if d is not None and day is not None and d != day:
            out.append(Section(day, buf))
            buf = []
        if d is not None:
            day = d
        buf.append(line)
    if buf:
        out.append(Section(day, buf))
    return out


def recent(sections: list, max_lines: int) -> list:
    """从最新起取整段,累计不超过 ``max_lines`` 行;至少一段。"""
    out, used = [], 0
    for s in sections:
        if out and used + len(s.lines) > max_lines:
            break
        out.append(s)
        used += len(s.lines)
    return out


def older_ranges(details: list, important: list = (), max_lines: int = RANGE_LINES) -> list:
    """「历史更新公告」的日期区间 → ``[(最新日, 最早日), …]``,新的在前。

    公告详情除最新一段外的各段与全部重要更新按日期合成一条时间线,以整日为单位按行数上限拼区间。
    """
    per_day = {}
    for s in [*details[1:], *important]:
        if s.day is not None:
            per_day[s.day] = per_day.get(s.day, 0) + len(s.lines)
    ranges, cur, used = [], [], 0
    for d in sorted(per_day, reverse=True):
        if cur and used + per_day[d] > max_lines:
            ranges.append((cur[0], cur[-1]))
            cur, used = [], 0
        cur.append(d)
        used += per_day[d]
    if cur:
        ranges.append((cur[0], cur[-1]))
    return ranges


def in_range(sections: list, a: date, b: date) -> list:
    """日期落在 ``a``、``b`` 之间(含两端,先后不限)的段。"""
    lo, hi = min(a, b), max(a, b)
    return [s for s in sections if s.day is not None and lo <= s.day <= hi]


def _cn(d: date, year: bool = True) -> str:
    return (f'{d.year}年' if year else '') + f'{d.month}月{d.day}日'


def range_label(newest: date, oldest: date) -> str:
    """日期区间的展示文案:同一天只写一个日期,同一年时后一个省掉年份。"""
    if newest == oldest:
        return _cn(newest)
    return f'{_cn(newest)} ~ {_cn(oldest, newest.year != oldest.year)}'


def range_arg(newest: date, oldest: date) -> str:
    """查询这个区间的指令参数:``YYYYMMDD-YYYYMMDD``,同一天只带一个日期。"""
    a = newest.strftime('%Y%m%d')
    return a if newest == oldest else f'{a}-{oldest.strftime("%Y%m%d")}'


def parse_day(s: str) -> date | None:
    """``YYYYMMDD`` → date;不是合法日期返回 None。"""
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8])) if len(s or '') == 8 and s.isdigit() else None
    except ValueError:
        return None


def render(sections: list) -> str:
    """原样拼回文本,去掉首尾空行。"""
    return '\n'.join(_trim([line for s in sections for line in s.lines]))
