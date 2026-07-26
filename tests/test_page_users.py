#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""page_users 两段式懒加载测试 —— 首屏 payload 形状 / page_handler 取剩余全部。

page_users 顶部 import aiohttp,dev 机常无 → importorskip 守卫(CI 有 aiohttp
时才真跑);userinfo 数据源全部 monkeypatch,不建真库。
"""

from __future__ import annotations

import json

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
