#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「头衔系统」面板接口测试 —— 玩家头衔保存 / 删除、查昵称、刷新、等级表保存五条路由与审计。

aiohttp 是 webui 的顶层 import,dev 机可能没装 → importorskip 守卫(同 test_webui_pages)。
"""

from __future__ import annotations

import json

import pytest

from plugins.LGTBot_ElainaBot.mod.users import titles

U1 = '0123456789ABCDEF0123456789ABCDEF'
U2 = 'FEDCBA9876543210FEDCBA9876543210'
U9 = '99999999999999999999999999999999'


@pytest.fixture
def audits():
    return []


@pytest.fixture
def pt(tmp_path, monkeypatch, audits):
    pytest.importorskip('aiohttp')
    from plugins.LGTBot_ElainaBot.mod.webui.pages import titles as page_titles
    monkeypatch.setattr(titles, '_players', titles._JsonFile(str(tmp_path / 'titles.json'), dict))
    monkeypatch.setattr(titles, '_levels', titles._JsonFile(str(tmp_path / 'levels.json'), list))
    monkeypatch.setattr(page_titles.userinfo, 'get_name', lambda uid: {U1: '铁蛋'}.get(uid, ''))
    monkeypatch.setattr(page_titles.userinfo, 'avatar_url', lambda uid: f'https://a/{uid}')
    monkeypatch.setattr(page_titles.audit, 'record', lambda *a, **k: audits.append(a))
    return page_titles


class _Req:
    def __init__(self, body=None, **query):
        self._body = body
        self.query = query

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _body(resp):
    return json.loads(resp.text)


def _edit_rows():
    return [dict(lv, orig=lv['name']) for lv in titles.levels()]


async def test_save_validates_audits_and_returns_the_whole_payload(pt, audits):
    for bad in (ValueError('x'), [U1], {'openid': U1, 'titles': [{'name': 'a$', 'level': '稀有'}]},
                {'openid': 'U1', 'titles': [{'name': 'A', 'level': '稀有'}]}):
        resp = await pt.save_handler(_Req(bad))
        assert resp.status == 400 and _body(resp)['success'] is False
    assert audits == [] and titles.all_records() == {}

    resp = await pt.save_handler(_Req({'openid': U1, 'titles': [{'name': '铁蛋', 'level': '稀有'}], 'equipped': '铁蛋'}))
    body = _body(resp)
    assert body['success'] and body['message'] == '已保存'
    assert body['players'] == [{'openid': U1, 'name': '铁蛋', 'avatar': f'https://a/{U1}',
                                'titles': [{'name': '铁蛋', 'level': '稀有'}], 'equipped': '铁蛋'}]
    assert body['levels'] == list(titles.DEFAULT_LEVELS)
    assert audits[-1] == ('title', '编辑头衔', f'铁蛋 ({U1})：新增 「铁蛋」；装备 无 → 铁蛋')

    await pt.save_handler(_Req({'openid': U1, 'titles': [{'name': '铁蛋', 'level': '神话'}, {'name': 'Ace', 'level': '限定'}],
                                'equipped': 'Ace'}))
    assert audits[-1][2] == f'铁蛋 ({U1})：新增 「Ace」；改等级 「铁蛋」；装备 铁蛋 → Ace'
    n = len(audits)
    await pt.save_handler(_Req({'openid': U1, 'titles': titles.get(U1)['titles'], 'equipped': 'Ace'}))
    assert len(audits) == n                                      # 没变化不记审计

    body = _body(await pt.save_handler(_Req({'openid': U1, 'titles': [], 'equipped': None})))
    assert body['success'] and '已删除' in body['message'] and body['players'] == []


async def test_delete_removes_the_record_and_audits(pt, audits):
    assert (await pt.delete_handler(_Req({'openid': U9}))).status == 404
    assert (await pt.delete_handler(_Req(ValueError('x')))).status == 400
    titles.set_record(U9, [{'name': 'A', 'level': '普通'}, {'name': 'B', 'level': '神话'}], 'A')
    body = _body(await pt.delete_handler(_Req({'openid': U9})))
    assert body['success'] and body['players'] == [] and titles.get(U9) is None
    assert audits[-1] == ('title', '删除头衔记录', f'{U9}：共 2 个头衔')


async def test_lookup_requires_32_chars_and_list_sorts_named_first(pt):
    for bad in ('a b', 'U1', U1[:-1], U1 + 'A'):
        assert (await pt.lookup_handler(_Req(openid=bad))).status == 400, bad
    body = _body(await pt.lookup_handler(_Req(openid=U1)))
    assert body == {'success': True, 'openid': U1, 'name': '铁蛋', 'avatar': f'https://a/{U1}', 'exists': False}
    titles.set_record(U2, [{'name': 'A', 'level': '普通'}], None)
    titles.set_record(U1, [{'name': 'B', 'level': '普通'}], None)
    assert _body(await pt.lookup_handler(_Req(openid=U2)))['exists'] is True
    body = _body(await pt.list_handler(_Req()))
    assert body['success'] and [p['openid'] for p in body['players']] == [U1, U2]
    assert body['name_max'] == titles.NAME_MAX_LEN and body['level_name_max'] == titles.LEVEL_NAME_MAX_LEN


async def test_levels_handler_saves_audits_and_refuses_deleting_used_levels(pt, audits):
    assert (await pt.levels_handler(_Req(ValueError('x')))).status == 400
    assert (await pt.levels_handler(_Req({'levels': []}))).status == 400
    titles.set_record(U1, [{'name': 'A', 'level': '普通'}], None)
    rows = _edit_rows()
    rows[0].update(name='常见', hex='#00AA00')
    body = _body(await pt.levels_handler(_Req({'levels': rows + [{'name': '金', 'hex': 'ffd700'}]})))
    assert body['success'] and body['levels'][0]['name'] == '常见' and body['levels'][-1]['hex'] == '#FFD700'
    assert body['players'][0]['titles'] == [{'name': 'A', 'level': '常见'}]     # 头衔跟着等级改名
    assert audits[-1] == ('title', '编辑头衔等级', '新增 「金 #FFD700」；修改 「普通 #27AE60」 → 「常见 #00AA00」')
    n = len(audits)
    await pt.levels_handler(_Req({'levels': _edit_rows()}))
    assert len(audits) == n                                      # 没变化不记审计
    await pt.levels_handler(_Req({'levels': _edit_rows()[::-1]}))
    assert audits[-1][2] == '调整排序'

    resp = await pt.levels_handler(_Req({'levels': [r for r in _edit_rows() if r['name'] != '常见']}))
    assert resp.status == 400 and '在用' in _body(resp)['message']
    body = _body(await pt.levels_handler(_Req({'levels': [r for r in _edit_rows() if r['name'] != '金']})))
    assert body['success'] and audits[-1][2] == '删除 「金 #FFD700」'


def test_get_data_is_safe_to_embed(pt, monkeypatch):
    titles.set_record(U1, [{'name': 'A', 'level': '普通'}], None)
    monkeypatch.setattr(pt.userinfo, 'get_name', lambda uid: '</script><b>')
    assert '</script>' not in pt.get_data()
