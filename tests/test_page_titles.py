#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「头衔系统」面板接口测试 —— 玩家头衔保存 / 删除、按 OpenID 查昵称、按昵称找玩家、刷新、颜色 / 分类 / 头衔库保存八条路由与审计。

aiohttp 是 webui 的顶层 import,dev 机可能没装 → importorskip 守卫(同 test_webui_pages)。
"""

from __future__ import annotations

import json
import os

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
    for attr, name, kind in (('_players', 'titles.json', dict), ('_colors', 'colors.json', list),
                             ('_categories', 'categories.json', list), ('_library', 'library.json', list)):
        monkeypatch.setattr(titles, attr, titles._JsonFile(str(tmp_path / name), kind))
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


def _t(name, color='蓝', category='常驻'):
    return {'name': name, 'color': color, 'category': category}


def _rows(items):
    return [dict(p, orig=p['name']) for p in items]


async def test_save_validates_audits_and_returns_the_whole_payload(pt, audits, tmp_path):
    for bad in (ValueError('x'), [U1], {'openid': U1, 'titles': [_t('a$')]}, {'openid': 'U1', 'titles': [_t('A')]}):
        resp = await pt.save_handler(_Req(bad))
        assert resp.status == 400 and _body(resp)['success'] is False
    assert audits == [] and titles.all_records() == {}

    resp = await pt.save_handler(_Req({'openid': U1, 'titles': [_t('铁蛋')], 'equipped': '铁蛋'}))
    body = _body(resp)
    assert body['success'] and body['message'] == '已保存'
    assert body['players'] == [{'openid': U1, 'name': '铁蛋', 'avatar': f'https://a/{U1}', 'titles': [_t('铁蛋')], 'equipped': '铁蛋'}]
    assert body['colors'] == list(titles.DEFAULT_COLORS) and body['categories'] == list(titles.DEFAULT_CATEGORIES)
    assert body['path'] == str(tmp_path)                                 # 数据有好几份文件,只给目录
    assert audits[-1] == ('title', '编辑头衔', f'铁蛋 ({U1})：新增 「铁蛋」；装备 无 → 铁蛋')

    await pt.save_handler(_Req({'openid': U1, 'titles': [_t('铁蛋', '红'), _t('Ace', '粉')], 'equipped': 'Ace'}))
    assert audits[-1][2] == f'铁蛋 ({U1})：新增 「Ace」；改样式 「铁蛋」；装备 铁蛋 → Ace'
    n = len(audits)
    await pt.save_handler(_Req({'openid': U1, 'titles': titles.get(U1)['titles'], 'equipped': 'Ace'}))
    assert len(audits) == n                                      # 没变化不记审计

    body = _body(await pt.save_handler(_Req({'openid': U1, 'titles': [], 'equipped': None})))
    assert body['success'] and '已删除' in body['message'] and body['players'] == []


async def test_delete_removes_the_record_and_audits(pt, audits):
    assert (await pt.delete_handler(_Req({'openid': U9}))).status == 404
    assert (await pt.delete_handler(_Req(ValueError('x')))).status == 400
    titles.set_record(U9, [_t('A', '绿'), _t('B', '红')], 'A')
    body = _body(await pt.delete_handler(_Req({'openid': U9})))
    assert body['success'] and body['players'] == [] and titles.get(U9) is None
    assert audits[-1] == ('title', '删除头衔记录', f'{U9}：共 2 个头衔')


async def test_lookup_requires_32_chars_and_list_sorts_named_first(pt):
    for bad in ('a b', 'U1', U1[:-1], U1 + 'A'):
        assert (await pt.lookup_handler(_Req(openid=bad))).status == 400, bad
    body = _body(await pt.lookup_handler(_Req(openid=U1)))
    assert body == {'success': True, 'openid': U1, 'name': '铁蛋', 'avatar': f'https://a/{U1}', 'exists': False}
    titles.set_record(U2, [_t('A')], None)
    titles.set_record(U1, [_t('B')], None)
    assert _body(await pt.lookup_handler(_Req(openid=U2)))['exists'] is True
    body = _body(await pt.list_handler(_Req()))
    assert body['success'] and [p['openid'] for p in body['players']] == [U1, U2]
    assert body['name_max'] == titles.NAME_MAX_LEN and body['preset_name_max'] == titles.PRESET_NAME_MAX_LEN
    assert body['library'] == []


async def test_search_finds_players_by_nickname_and_never_echoes_the_keyword(pt, monkeypatch):
    seen = []

    def fake_search(q, limit):
        seen.append((q, limit))
        return [{'openid': U1, 'name': '铁蛋', 'avatar': 'a1'}, {'openid': U2, 'name': '铁蛋2', 'avatar': 'a2'}]

    monkeypatch.setattr(pt.userinfo, 'search_users', fake_search)
    titles.set_record(U2, [_t('A')], None)
    body = _body(await pt.search_handler(_Req(q=' 铁蛋 ')))
    assert seen == [('铁蛋', 20)]
    assert body == {'success': True, 'results': [{'openid': U1, 'name': '铁蛋', 'avatar': 'a1', 'exists': False},
                                                 {'openid': U2, 'name': '铁蛋2', 'avatar': 'a2', 'exists': True}]}
    for bad in ('', '  ', '长' * 33):
        resp = await pt.search_handler(_Req(q=bad))
        assert resp.status == 400 and (not bad.strip() or bad not in _body(resp)['message'])
    assert len(seen) == 1


async def test_color_and_category_handlers_save_audit_and_refuse_deleting_used_ones(pt, audits):
    assert (await pt.colors_handler(_Req(ValueError('x')))).status == 400
    assert (await pt.colors_handler(_Req({'colors': []}))).status == 400
    titles.set_record(U1, [_t('A')], None)
    rows = _rows(titles.colors())
    i = next(i for i, r in enumerate(rows) if r['name'] == '蓝')
    rows[i].update(name='天蓝', hex='#00AAFF')
    body = _body(await pt.colors_handler(_Req({'colors': rows + [{'name': '金', 'hex': 'ffd700'}]})))
    assert body['success'] and body['colors'][i] == {'name': '天蓝', 'hex': '#00AAFF'} and body['colors'][-1]['hex'] == '#FFD700'
    assert body['players'][0]['titles'] == [_t('A', '天蓝')]                # 头衔跟着颜色改名
    assert audits[-1] == ('title', '编辑头衔颜色', '新增 「金 #FFD700」；修改 「蓝 #2980B9」 → 「天蓝 #00AAFF」')
    n = len(audits)
    await pt.colors_handler(_Req({'colors': _rows(titles.colors())}))
    assert len(audits) == n                                      # 没变化不记审计
    await pt.colors_handler(_Req({'colors': _rows(titles.colors())[::-1]}))
    assert audits[-1][2] == '调整排序'
    resp = await pt.colors_handler(_Req({'colors': [r for r in _rows(titles.colors()) if r['name'] != '天蓝']}))
    assert resp.status == 400 and '在用' in _body(resp)['message']

    body = _body(await pt.categories_handler(_Req({'categories': _rows(titles.categories()) + [{'name': '活动'}]})))
    assert body['success'] and body['categories'] == [{'name': '常驻'}, {'name': '活动'}]
    assert audits[-1] == ('title', '编辑头衔分类', '新增 「活动」')
    resp = await pt.categories_handler(_Req({'categories': [{'name': '活动', 'orig': '活动'}]}))
    assert resp.status == 400 and '在用' in _body(resp)['message']


async def test_library_handler_saves_syncs_holders_and_audits(pt, audits):
    assert (await pt.library_handler(_Req(ValueError('x')))).status == 400
    assert (await pt.library_handler(_Req({'library': [_t('A', '金')]}))).status == 400
    body = _body(await pt.library_handler(_Req({'library': [_t('萌新', '蓝'), _t('开发者', '橙')]})))
    assert body['success'] and body['library'] == [_t('开发者', '橙'), _t('萌新', '蓝')]
    assert audits[-1] == ('title', '编辑头衔库', '新增 「萌新 · 蓝 · 常驻」、「开发者 · 橙 · 常驻」')
    titles.set_record(U1, [_t('开发者', '绿')], '开发者')
    rows = _rows(titles.library())
    n = len(audits)
    await pt.library_handler(_Req({'library': rows}))
    assert len(audits) == n                                      # 没变化不记审计
    keep = next(r for r in rows if r['name'] == '开发者')
    keep.update(name='主创', color='红')
    body = _body(await pt.library_handler(_Req({'library': [keep]})))
    assert audits[-1][2] == '删除 「萌新 · 蓝 · 常驻」；修改 「开发者 · 橙 · 常驻」 → 「主创 · 红 · 常驻」'
    assert body['players'][0]['titles'] == [_t('主创', '红')] and body['players'][0]['equipped'] == '主创'


def test_get_data_is_safe_to_embed(pt, monkeypatch):
    titles.set_record(U1, [_t('A')], None)
    monkeypatch.setattr(pt.userinfo, 'get_name', lambda uid: '</script><b>')
    assert '</script>' not in pt.get_data()
    assert os.path.isabs(json.loads(pt.get_data())['path'])
