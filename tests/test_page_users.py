#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""page_users 两段式懒加载测试 —— 首屏 payload 形状 / page_handler 取剩余全部。

page_users 顶部 import aiohttp,dev 机常无 → importorskip 守卫(CI 有 aiohttp
时才真跑);userinfo 数据源全部 monkeypatch,不建真库。
"""

from __future__ import annotations

import json
import re

import pytest


def _page_users():
    pytest.importorskip('aiohttp')
    from plugins.LGTBot_ElainaBot.mod.webui import page_users
    return page_users


class _FakeReq:
    """模拟 aiohttp Request:query.get('offset') 返回给定值。"""
    def __init__(self, offset):
        self.query = self
        self._offset = offset
    def get(self, key, default=None):
        return self._offset if key == 'offset' else default


def test_get_data_head_only_with_real_total(monkeypatch):
    """首屏:users 只取首段(limit=_HEAD),total 为全量,payload 带 head。"""
    pu = _page_users()
    from plugins.LGTBot_ElainaBot.mod import userinfo
    calls = {}

    def fake_list(limit=None, offset=0):
        calls['limit'], calls['offset'] = limit, offset
        return [{'openid': 'U1', 'name': 'x</script>y', 'avatar': '',
                 'total_messages': None, 'private_messages': None,
                 'last_active_date': ''}]
    monkeypatch.setattr(userinfo, 'list_users', fake_list)
    monkeypatch.setattr(userinfo, 'count_users', lambda: 2345)

    raw = pu.get_data()
    assert '</script>' not in raw                       # 注入防护仍在
    data = json.loads(raw.replace('<\\/script>', '</script>'))
    assert calls == {'limit': pu._HEAD, 'offset': 0}    # 只查首段
    assert data['total'] == 2345                        # 页数依据 = 全量总数
    assert data['head'] == pu._HEAD
    assert len(data['users']) == 1


async def test_page_handler_returns_rest_from_offset(monkeypatch):
    """?offset=1000 → 返回 offset 起的剩余全部(limit=None),payload 带 total。"""
    pu = _page_users()
    from plugins.LGTBot_ElainaBot.mod import userinfo
    seen = {}

    def fake_list(limit=None, offset=0):
        seen['limit'], seen['offset'] = limit, offset
        return [{'openid': f'U{offset + i}'} for i in range(3)]
    monkeypatch.setattr(userinfo, 'list_users', fake_list)
    monkeypatch.setattr(userinfo, 'count_users', lambda: 1003)

    resp = await pu.page_handler(_FakeReq('1000'))
    data = json.loads(resp.text)
    assert data['success'] is True
    assert seen == {'limit': None, 'offset': 1000}      # 不限量:一次取完剩余
    assert data['offset'] == 1000 and data['total'] == 1003
    assert [u['openid'] for u in data['users']] == ['U1000', 'U1001', 'U1002']

    resp2 = await pu.page_handler(_FakeReq('-5'))       # 负数 → 0
    assert json.loads(resp2.text)['offset'] == 0


async def test_page_handler_rejects_bad_offset():
    pu = _page_users()
    resp = await pu.page_handler(_FakeReq('abc'))
    assert getattr(resp, 'status', None) == 400


def _mobile_css(css: str) -> str:
    """把文件里所有窄屏 @media 块的正文拼起来(块尾的 ``}`` 顶格,规则缩进)。"""
    import re
    blocks = re.findall(r'@media \(max-width: 600px\) \{(.*?)\n\}', css, re.S)
    assert blocks, '没找到窄屏 @media 块'
    return '\n'.join(blocks)


def test_user_list_uses_fixed_columns_and_scrolls_on_mobile():
    """★ 窄屏下列表改成定宽列 + 横向滚动。"""
    css = _page_users().TAB_CSS
    m = _mobile_css(css)
    assert '.users-section { overflow-x: auto; }' in m
    tracks = re.findall(r'grid-template-columns:\s*([\d\w ]+px[\d\w ]*);', m)
    assert tracks and len(set(tracks)) == 1, f'表头与数据行轨道不一致: {tracks}'
    assert len(tracks[0].split()) == 5                 # 序号 / 用户 / OpenID / 消息数 / 最后活跃
    # 表头必须一行放完,否则两行表头与数据行错位
    assert '.users-header .header-row > div { white-space: nowrap;' in m
    # 消息数与最后活跃日期不允许断行
    assert '.user-row .col-msgs, .user-row .col-seen { white-space: nowrap; }' in m


def test_openid_splits_into_two_lines_of_sixteen_on_mobile():
    """★ OpenID 是 32 位定长串:内层块宽 16 个字符,正好上下两行。"""
    pu = _page_users()
    assert '<span class="user-openid">' in pu.TAB_JS
    m = _mobile_css(pu.TAB_CSS)
    assert 'display: block; width: 16ch;' in m
    assert 'word-break: break-all;' in m
    # 宽屏区不给这个 span 任何规则(否则会连带改宽屏)
    assert '.user-openid' not in pu.TAB_CSS.replace(m, '')


def test_toolbar_search_is_full_width_with_paging_and_refresh_split():
    """★ 窄屏工具栏:搜索框独占一行、左右都顶到边;下一行左页码、右刷新。"""
    m = _mobile_css(_page_users().TAB_CSS)
    assert '.users-toolbar .spacer { display: none; }' in m
    assert 'flex: 1 0 100%;' in m and 'max-width: none;' in m    # 搜索框整行铺满
    assert '.users-toolbar .pagination { margin-right: auto; }' in m
