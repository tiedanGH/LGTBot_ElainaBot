#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""玩家头衔测试 —— users.titles 数据层(颜色、分类、头衔库、玩家头衔)、头衔图片、「头衔」帮助与「头衔 我的 / 列表 / 切换」指令、新建 / 加入房间后的头衔播报。

被测行为:
  · 玩家头衔整份保存时逐项校验(32 位 OpenID / 名字字符 / 颜色与分类存在 / 重名 / 装备必须持有),清空等于删除,数量不设上限;与头衔库同名的颜色分类以库为准
  · 颜色、分类按行序整份保存:头衔按名引用,改名连同头衔库与玩家头衔一起换(互换名字不串)、改色立即生效、在用的不能删、至少留一个
  · 头衔库整份保存:改名、改颜色分类同步到持有者(装备跟着换名),同名的限定头衔并入库内,删掉的头衔持有者照常保留成限定头衔
  · 排序先分类再颜色;装备按序号或名字,序号按展示顺序(限定在前、库内在后)计,越界再按名字找;颜色分类已不存在的头衔照常保留并兜底;坏文件改名留证
  · 只发「头衔」回子指令说明;「头衔 列表」五列、限定头衔在「限定」、其余按分类分组;「头衔 我的」三列(满 15 个五列)、「限定」与「已收集」两组并写分类;有图床走 markdown,没有直接回图;缺依赖提示安装
  · 播报「【头衔名】」加粗上色,后接展示昵称(查不到才 @);播报对象:建房取派发线程上记下的发起人,加入取加入的玩家,退出不播;标记只在打标的线程上可见
  · 播报排在回复限制 / 主动私信提示之后
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.LGTBot_ElainaBot.mod import callbacks, dispatcher, state
from plugins.LGTBot_ElainaBot.mod.users import titles

U1 = '0123456789ABCDEF0123456789ABCDEF'
U2 = 'FEDCBA9876543210FEDCBA9876543210'
HOST = 'A1B2C3D4E5F60718293A4B5C6D7E8F90'


@pytest.fixture(autouse=True)
def _titles_files(tmp_path, monkeypatch):
    """每个用例一份独立的数据目录。"""
    for attr, name, kind in (('_players', 'titles.json', dict), ('_colors', 'colors.json', list),
                             ('_categories', 'categories.json', list), ('_library', 'library.json', list)):
        monkeypatch.setattr(titles, attr, titles._JsonFile(str(tmp_path / name), kind))
    callbacks._title_mark.kind = ''
    state.engine_request.uid = ''
    yield
    callbacks._title_mark.kind = ''
    state.engine_request.uid = ''


def _t(name, color='蓝', category='常驻'):
    return {'name': name, 'color': color, 'category': category}


def _rows(kind):
    """当前颜色 / 分类表原样作为编辑行(每行带原名)。"""
    return [dict(p, orig=p['name']) for p in (titles.colors() if kind == 'color' else titles.categories())]


def _pos(rows, name):
    """编辑行里某个名字所在的位置。"""
    return next(i for i, r in enumerate(rows) if r['name'] == name)


# ─────────────────────────────────────────────────────────────────────────
# 数据层:头衔
# ─────────────────────────────────────────────────────────────────────────


def test_set_record_validates_every_field_and_writes_nothing_on_error():
    bad = [
        ('U1', [_t('A')], None),
        (U1[:-1] + '!', [_t('A')], None),
        (U1 + '0', [_t('A')], None),
        (U1, 'A', None),
        (U1, ['A'], None),
        (U1, [_t('')], None),
        (U1, [_t('一' * (titles.NAME_MAX_LEN + 1))], None),
        (U1, [_t('a$b')], None),
        (U1, [_t('a\\b')], None),
        (U1, [_t('a}b')], None),
        (U1, [_t('a\nb')], None),
        (U1, [_t('A', '金')], None),
        (U1, [_t('A', category='限时')], None),
        (U1, [_t('A', ['蓝'])], None),
        (U1, [_t('A'), _t(' A ', '红')], None),
        (U1, [_t('A')], 'B'),
    ]
    for openid, ts, eq in bad:
        assert titles.set_record(openid, ts, eq), (openid, ts, eq)
    assert not os.path.exists(titles._players.path)

    assert titles.set_record(U1, [_t(' 铁蛋 '), _t('Ace', '粉')], '铁蛋') is None
    assert titles.get(U1) == {'titles': [_t('铁蛋'), _t('Ace', '粉')], 'equipped': '铁蛋'}
    assert titles.equipped(U1) == _t('铁蛋')
    assert titles.set_record(U1, [_t('Ace', '粉')], '') is None
    assert titles.equipped(U1) is None
    many = [_t(f'T{i}') for i in range(30)]                      # 管理端维护,数量不设上限
    assert titles.set_record(U2, many, 'T29') is None and titles.get(U2)['titles'] == many


def test_empty_library_deletes_the_record():
    titles.set_record(U1, [_t('A')], 'A')
    assert titles.set_record(U1, [], None) is None
    assert titles.get(U1) is None
    assert titles.delete(U1) is False
    titles.set_record(U2, [_t('B')], None)
    assert titles.delete(U2) is True and titles.all_records() == {}


def test_equip_by_index_or_name():
    assert titles.equip(U1, '1') == (None, 'none')
    titles.set_record(U1, [_t('2048', '绿'), _t('A'), _t('B', '红')], None)
    assert titles.equip(U1, '1') == (_t('B', '红'), '')            # 序号按颜色顺序:红、蓝、绿
    assert titles.get(U1)['equipped'] == 'B'
    assert titles.equip(U1, ' A ') == (_t('A'), '')
    assert titles.equip(U1, '３')[0] == _t('2048', '绿')          # 全角数字照样当序号
    assert titles.equip(U1, '2048')[0] == _t('2048', '绿')        # 序号越界再按名字找
    assert titles.equip(U1, '0') == (None, 'missing')
    assert titles.equip(U1, '9' * 5000) == (None, 'missing')
    assert titles.equip(U1, '不存在') == (None, 'missing')
    assert titles.get(U1)['equipped'] == '2048'
    titles.set_record(U2, [_t(f'T{i}') for i in range(1, 1201)], None)
    assert titles.equip(U2, '1200')[0] == _t('T1200')             # 头衔多到四位数也能按序号切


def test_render_uses_inline_katex_colour():
    t = _t('铁蛋', '蓝')
    assert titles.render(t) == '$\\textcolor{#2980B9}{\\text{铁蛋}}$'
    assert titles.join_notice(t, '玩家甲') == '$\\textcolor{#2980B9}{\\textbf{【铁蛋】}}$ 玩家甲 加入游戏'
    assert [c['name'] for c in titles.colors()] == ['黄', '橙', '红', '粉', '紫', '蓝', '青', '绿']
    assert titles.categories() == [{'name': '常驻'}]


def test_dirty_file_is_cleaned_but_unknown_styles_survive():
    with open(titles._players.path, 'w', encoding='utf-8') as f:
        json.dump({U1: {'titles': [_t('A'), _t('A', '红'), _t('B', '已删除', '旧分类'), 'x', _t('a{b'), {'name': 'C', 'color': '蓝'}], 'equipped': 'B'},
                   U2: 'garbage'}, f, ensure_ascii=False)
    assert titles.get(U1) == {'titles': [_t('A'), _t('B', '已删除', '旧分类')], 'equipped': 'B'}
    assert titles.get(U2) is None
    assert set(titles.all_records()) == {U1}
    # 颜色已不存在:灰色兜底,玩家切换装备时也不会把它弄丢
    assert titles.render(_t('B', '已删除')) == '$\\textcolor{#808080}{\\text{B}}$'
    titles.equip(U1, 'A')
    assert titles.get(U1)['titles'] == [_t('A'), _t('B', '已删除', '旧分类')]


def test_corrupt_file_is_set_aside_and_saving_starts_fresh(tmp_path):
    with open(titles._players.path, 'w', encoding='utf-8') as f:
        f.write('{not json')
    assert titles.get(U1) is None
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith('titles.json.corrupt_')]
    assert titles.set_record(U1, [_t('A')], 'A') is None
    assert titles.equipped(U1) == _t('A')


def test_file_changed_on_disk_is_picked_up():
    titles.set_record(U1, [_t('A')], 'A')
    assert titles.equipped(U1) == _t('A')
    with open(titles._players.path, 'w', encoding='utf-8') as f:
        json.dump({U1: {'titles': [_t('Longer', '红')], 'equipped': 'Longer'}}, f)
    assert titles.equipped(U1) == _t('Longer', '红')


# ─────────────────────────────────────────────────────────────────────────
# 数据层:颜色与分类
# ─────────────────────────────────────────────────────────────────────────


def test_colors_can_be_added_renamed_recoloured_and_reordered():
    titles.set_record(U1, [_t('A', '紫'), _t('B', '红')], 'A')
    titles.set_record(U2, [_t('C', '蓝')], None)
    rows = _rows('color')
    i = _pos(rows, '紫')
    rows[i].update(name='深紫', hex='3498db')                      # 改名改色,不带 # 也认
    rows.append({'name': ' 金 ', 'hex': '#ffd700'})
    assert titles.set_colors(rows) is None
    got = titles.colors()
    assert got[i] == {'name': '深紫', 'hex': '#3498DB'} and got[-1] == {'name': '金', 'hex': '#FFD700'}
    # 头衔里的颜色名跟着改,颜色立即生效;别的头衔不动
    assert titles.get(U1) == {'titles': [_t('A', '深紫'), _t('B', '红')], 'equipped': 'A'}
    assert titles.render(titles.equipped(U1)) == '$\\textcolor{#3498DB}{\\text{A}}$'
    assert titles.get(U2)['titles'] == [_t('C', '蓝')]
    # 两个颜色互换名字:一次映射,不会串成同一个
    rows = _rows('color')
    rows[_pos(rows, '蓝')]['name'], rows[_pos(rows, '深紫')]['name'] = '深紫', '蓝'
    assert titles.set_colors(rows) is None
    assert titles.get(U1)['titles'][0] == _t('A', '蓝') and titles.get(U2)['titles'] == [_t('C', '深紫')]
    # 行序即排序
    order = [c['name'] for c in titles.colors()]
    assert titles.set_colors(list(reversed(_rows('color')))) is None
    assert [c['name'] for c in titles.colors()] == order[::-1]


def test_categories_follow_the_same_rules():
    titles.set_record(U1, [_t('A')], None)
    rows = _rows('category') + [{'name': '活动'}]
    rows[0]['name'] = '常驻款'
    assert titles.set_categories(rows) is None
    assert titles.categories() == [{'name': '常驻款'}, {'name': '活动'}]
    assert titles.get(U1)['titles'] == [_t('A', category='常驻款')]
    err = titles.set_categories([{'name': '活动', 'orig': '活动'}])
    assert err and '常驻款' in err and '1' in err                 # 还有头衔在用:不能删
    assert titles.set_categories([]) and titles.set_categories([{'name': 'a*b'}]) and titles.set_categories([{'name': 'X'}, {'name': ' X '}])
    assert len(titles.categories()) == 2


def test_presets_have_no_upper_limit():
    rows = _rows('color') + [{'name': f'C{i}', 'hex': '#000000'} for i in range(30)]
    assert titles.set_colors(rows) is None and len(titles.colors()) == len(titles.DEFAULT_COLORS) + 30


def test_style_in_use_cannot_be_deleted():
    titles.set_record(U1, [_t('A', '红'), _t('B', '红')], None)
    rest = [r for r in _rows('color') if r['name'] != '红']
    err = titles.set_colors(rest)
    assert err and '红' in err and '2' in err
    assert any(c['name'] == '红' for c in titles.colors())
    # 删掉又新建同名颜色:头衔挂到新颜色上,不算失去颜色
    assert titles.set_colors(rest + [{'name': '红', 'hex': '#000000'}]) is None
    assert titles.render(_t('A', '红')) == '$\\textcolor{#000000}{\\text{A}}$'
    titles.set_record(U1, [_t('A', '蓝')], None)
    assert titles.set_colors([r for r in _rows('color') if r['name'] != '红']) is None
    assert all(c['name'] != '红' for c in titles.colors())


def test_set_colors_validates_and_writes_nothing_on_error():
    base = titles.colors()
    bad = [
        [], 'x',
        [_rows('color')[0]] * 2,
        [{'name': '', 'hex': '#000000'}],
        [{'name': '一' * (titles.PRESET_NAME_MAX_LEN + 1), 'hex': '#000000'}],
        [{'name': 'a*b', 'hex': '#000000'}],
        [{'name': 'A', 'hex': 'red'}], [{'name': 'A', 'hex': '#12345'}], [{'name': 'A', 'hex': '#1234567'}],
        [{'name': 'A', 'hex': '#000000'}, {'name': ' A ', 'hex': '#111111'}],
        [{'name': 'A', 'hex': '#000000', 'orig': '不存在'}],
        [{'name': 'A', 'hex': '#000000', 'orig': ['蓝']}],
        [{'name': 'A', 'hex': '#000000', 'orig': '蓝'}, {'name': 'B', 'hex': '#000000', 'orig': '蓝'}],
        ['A'],
    ]
    for rows in bad:
        assert titles.set_colors(rows), rows
    assert not os.path.exists(titles._colors.path)
    assert titles.colors() == base


def test_broken_preset_files_fall_back_to_defaults(tmp_path):
    with open(titles._colors.path, 'w', encoding='utf-8') as f:
        f.write('[oops')
    assert titles.colors() == list(titles.DEFAULT_COLORS)
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith('colors.json.corrupt_')]
    with open(titles._colors.path, 'w', encoding='utf-8') as f:
        json.dump([{'name': 'A', 'hex': 'zz'}, 'x', {'name': '金', 'hex': '#ffd700'}, {'name': '金', 'hex': '#000000'}], f)
    assert titles.colors() == [{'name': '金', 'hex': '#FFD700'}]
    with open(titles._categories.path, 'w', encoding='utf-8') as f:
        json.dump([{'name': ''}, 'x'], f)                          # 一项能用的都没有
    assert titles.categories() == list(titles.DEFAULT_CATEGORIES)


# ─────────────────────────────────────────────────────────────────────────
# 数据层:头衔库
# ─────────────────────────────────────────────────────────────────────────


def _lib(name, color='蓝', category='常驻', orig=''):
    return {'orig': orig, 'name': name, 'color': color, 'category': category}


def _lib_rows():
    """当前头衔库原样作为编辑行(每行带原名)。"""
    return [dict(t, orig=t['name']) for t in titles.library()]


def test_library_validates_sorts_by_category_then_colour():
    bad = ['x', [_lib('')], [_lib('a$b')], [_lib('A', '金')], [_lib('A', category='限时')], [_lib('A', ['蓝'])],
           [_lib('A'), _lib(' A ', '红')], [_lib('A', orig='不存在')], [_lib('A', orig=['A'])], ['A']]
    for rows in bad:
        assert titles.set_library(rows), rows
    assert not os.path.exists(titles._library.path)
    titles.set_categories(_rows('category') + [{'name': '活动'}])
    assert titles.set_library([_lib('活动橙', '橙', '活动'), _lib('开发者', '红'), _lib(' Bug猎人 ', '紫'), _lib('萌新', '紫'), _lib('活动蓝', '蓝', '活动')]) is None
    # 先分类(常驻、活动)再颜色(黄橙红粉紫蓝青绿),同分类同颜色保持录入顺序
    assert [t['name'] for t in titles.library()] == ['开发者', 'Bug猎人', '萌新', '活动橙', '活动蓝']
    assert titles.set_library([]) is None and titles.library() == []


def test_library_titles_follow_the_library_style_and_name():
    titles.set_categories(_rows('category') + [{'name': '活动'}])
    titles.set_library([_lib('开发者', '橙'), _lib('Bug猎人', '紫')])
    titles.set_record(U1, [_t('开发者', '绿', '活动'), _t('自创', '蓝')], '开发者')
    assert titles.get(U1)['titles'] == [_t('开发者', '橙'), _t('自创', '蓝')]        # 库内头衔的颜色分类以库为准
    titles.set_record(U2, [_t('金牌', '绿')], None)
    rows = _lib_rows()
    rows[_pos(rows, '开发者')].update(name='主创', color='红', category='活动')     # 开发者 → 主创,改颜色分类
    rows.append(_lib('金牌', '粉'))                               # 与 U2 的限定头衔同名:并入库内
    assert titles.set_library(rows) is None
    assert titles.get(U1) == {'titles': [_t('主创', '红', '活动'), _t('自创', '蓝')], 'equipped': '主创'}
    assert titles.get(U2)['titles'] == [_t('金牌', '粉')]
    # 删掉库里的头衔:持有者照常保留,成为限定头衔
    assert titles.set_library([r for r in _lib_rows() if r['name'] != '主创']) is None
    assert titles.get(U1)['titles'][0] == _t('主创', '红', '活动')
    assert titles.board(U1)['custom'] == [_t('自创', '蓝'), _t('主创', '红', '活动')]     # 限定头衔也先分类再颜色


def test_library_rename_onto_a_held_name_keeps_one_title():
    titles.set_library([_lib('B')])
    titles.set_record(U1, [_t('A', '紫'), _t('B')], 'B')
    assert titles.set_library([_lib('A', '红', orig='B')]) is None
    assert titles.get(U1) == {'titles': [_t('A', '红')], 'equipped': 'A'}


def test_styles_used_by_the_library_cannot_be_deleted_and_renames_reach_it():
    titles.set_library([_lib('开发者', '红')])
    titles.set_record(U1, [_t('开发者')], None)
    err = titles.set_colors([r for r in _rows('color') if r['name'] != '红'])
    assert err and '红' in err and '1' in err                     # 头衔库一项,持有者不重复计
    rows = _rows('color')
    rows[_pos(rows, '红')]['name'] = '大红'
    assert titles.set_colors(rows) is None
    rows = _rows('category')
    rows[0]['name'] = '常驻款'
    assert titles.set_categories(rows) is None
    assert titles.library() == [_t('开发者', '大红', '常驻款')] and titles.get(U1)['titles'] == [_t('开发者', '大红', '常驻款')]


def test_display_order_puts_custom_first_and_drives_equip_index():
    titles.set_categories(_rows('category') + [{'name': '活动'}])
    titles.set_library([_lib('L橙', '橙'), _lib('L蓝', '蓝'), _lib('L活动', '蓝', '活动')])
    titles.set_record(U1, [_t('L活动', '蓝', '活动'), _t('L橙', '橙'), _t('限定蓝', '蓝'), _t('限定活动', '橙', '活动'), _t('L蓝', '蓝')], 'L蓝')
    b = titles.board(U1)
    assert [t['name'] for t in b['mine']] == ['限定蓝', '限定活动', 'L橙', 'L蓝', 'L活动']
    assert [t['name'] for t in b['custom']] == ['限定蓝', '限定活动'] and b['equipped'] == 'L蓝'
    assert [t['name'] for t in b['library']] == ['L橙', 'L蓝', 'L活动']
    assert b['colors']['橙'] == '#FF8C00' and b['categories'] == ['常驻', '活动']
    assert titles.equip(U1, '1')[0]['name'] == '限定蓝'
    assert titles.equip(U1, '5')[0]['name'] == 'L活动'
    assert titles.board(U2) == dict(b, custom=[], mine=[], equipped=None)


# ─────────────────────────────────────────────────────────────────────────
# 指令
# ─────────────────────────────────────────────────────────────────────────


def _ev(content, *, group=True, uid=U1):
    ev = MagicMock()
    ev.event_type = dispatcher.GROUP_AT_MESSAGE_CREATE if group else dispatcher.C2C_MESSAGE_CREATE
    ev.is_group, ev.is_direct = group, not group
    ev.group_id = 'G1' if group else ''
    ev.channel_id = ''
    ev.user_id = uid
    ev.content = content
    ev.appid = ''
    ev.is_interaction = False
    ev.reply = AsyncMock()
    ev.reply_image = AsyncMock()
    return ev


async def _call(handler, pattern, content, **kw):
    ev = _ev(content, **kw)
    await handler(ev, re.search(pattern, content))
    return ev.reply.await_args.args[0]


@pytest.fixture
def renders(monkeypatch):
    """假的头衔图片渲染:记下传进来的参数,回固定字节;依赖齐全、不配图床。"""
    from plugins.LGTBot_ElainaBot.mod.messaging import uploader
    calls = []

    def fake_render(title, sub, sections, footer, cols):
        calls.append({'title': title, 'sub': sub, 'sections': sections, 'footer': footer, 'cols': cols})
        return b'PNG'

    monkeypatch.setattr(dispatcher.titles_image, 'render', fake_render)
    monkeypatch.setattr(dispatcher.titles_image, 'missing_dependency', lambda: '')
    monkeypatch.setattr(uploader, 'SELECTED_BACKEND', '')
    monkeypatch.setattr(dispatcher.userinfo, 'display_name', lambda uid: {U1: '铁蛋玩家'}.get(uid, ''))
    return calls


def _cells(section):
    return [(c['name'], c['hex'], c['owned'], c['equipped'], c['index'], c['label']) for c in section[1]]


async def test_my_titles_groups_custom_as_limited_and_collected_with_categories(renders):
    titles.set_categories(_rows('category') + [{'name': '活动'}])
    titles.set_library([_lib('L橙', '橙'), _lib('L活动', '蓝', '活动'), _lib('没拿到', '红')])
    titles.set_record(U1, [_t('L活动', '蓝', '活动'), _t('限定款', '粉'), _t('L橙', '橙')], 'L橙')
    ev = _ev('头衔 我的')
    await dispatcher.lgtbot_my_titles(ev, None)
    ev.reply.assert_not_awaited()
    ev.reply_image.assert_awaited_once_with(b'PNG')                 # 没配图床:直接回图片
    call = renders[0]
    assert call['title'] == '我的头衔' and call['cols'] == 3 and call['sub'] == '铁蛋玩家　共 3 个头衔 · 装备中：L橙'
    assert [s[0] for s in call['sections']] == ['限定', '已收集']
    assert _cells(call['sections'][0]) == [('限定款', '#E84393', True, False, 1, '常驻')]
    assert _cells(call['sections'][1]) == [('L橙', '#FF8C00', True, True, 2, '常驻'), ('L活动', '#2980B9', True, False, 3, '活动')]
    assert titles.equip(U1, '3')[0]['name'] == 'L活动'            # 图上的序号就是切换序号
    assert await _call(dispatcher.lgtbot_my_titles, dispatcher._P_MY_TITLES, '头衔 我的', uid=U2) == f'<@{U2}>\n您还没有头衔'


async def test_my_titles_switch_to_five_columns_from_fifteen_titles(renders):
    assert dispatcher._MY_TITLES_WIDE == 15
    titles.set_library([_lib(f'L{i}') for i in range(10)])
    held = [_t(f'L{i}') for i in range(10)] + [_t(f'限定{i}') for i in range(4)]
    titles.set_record(U1, held, None)                               # 头衔库与限定头衔合计 14 个
    await dispatcher.lgtbot_my_titles(_ev('头衔 我的'), None)
    titles.set_record(U1, held + [_t('限定4')], None)
    await dispatcher.lgtbot_my_titles(_ev('头衔 我的'), None)
    assert [c['cols'] for c in renders] == [3, 5]


async def test_title_list_groups_by_category_in_five_columns_without_labels(renders):
    assert (await _call(dispatcher.lgtbot_title_list, dispatcher._P_TITLE_LIST, '头衔 列表')).endswith('头衔库里还没有头衔')
    titles.set_categories(_rows('category') + [{'name': '活动'}])
    titles.set_library([_lib('L蓝', '蓝'), _lib('L橙', '橙'), _lib('L活动', '紫', '活动')])
    titles.set_record(U1, [_t('限定款', '粉'), _t('L橙', '橙')], '限定款')
    ev = _ev('/头衔 列表', group=False)
    await dispatcher.lgtbot_title_list(ev, None)
    call = renders[0]
    assert call['title'] == '头衔列表' and call['cols'] == 5 and call['sub'] == '铁蛋玩家　已收集 1 / 3 · 限定 1'
    assert [s[0] for s in call['sections']] == ['限定', '常驻', '活动', titles.UNKNOWN_CATEGORY]
    assert _cells(call['sections'][0]) == [('限定款', '#E84393', True, True, 0, '')]
    assert _cells(call['sections'][1]) == [('L橙', '#FF8C00', True, False, 0, ''), ('L蓝', '#2980B9', False, False, 0, '')]
    assert _cells(call['sections'][2]) == [('L活动', '#8E44AD', False, False, 0, '')] and call['sections'][3][1] == []
    ev.reply_image.assert_awaited_once_with(b'PNG')
    # 别人的限定头衔不出现在自己的头衔列表里
    await dispatcher.lgtbot_title_list(_ev('头衔 列表', uid=U2), None)
    assert renders[1]['sections'][0] == ('限定', [])


async def test_title_images_go_through_the_image_host_when_configured(renders, monkeypatch):
    from plugins.LGTBot_ElainaBot.mod.messaging import uploader
    monkeypatch.setattr(uploader, 'SELECTED_BACKEND', 'qq_file')
    monkeypatch.setattr(uploader, 'upload_image', AsyncMock(return_value='https://img/t.png'))
    monkeypatch.setattr(uploader, 'get_image_size', lambda data: (1080, 640))
    titles.set_record(U1, [_t('A')], 'A')
    ev = _ev('头衔 我的')
    await dispatcher.lgtbot_my_titles(ev, None)
    ev.reply.assert_awaited_once_with(f'<@{U1}>\n![我的头衔 #1080px #640px](https://img/t.png)')
    ev.reply_image.assert_not_awaited()
    uploader.upload_image.return_value = None                       # 图床上传失败:退回直接回图
    ev = _ev('头衔 我的')
    await dispatcher.lgtbot_my_titles(ev, None)
    ev.reply_image.assert_awaited_once_with(b'PNG')


async def test_title_images_ask_for_missing_dependencies_without_a_text_fallback(renders, monkeypatch):
    monkeypatch.setattr(dispatcher.titles_image, 'missing_dependency', lambda: '缺少 Pillow')
    titles.set_record(U1, [_t('A')], 'A')
    for handler, pattern, text in ((dispatcher.lgtbot_my_titles, dispatcher._P_MY_TITLES, '头衔 我的'),
                                   (dispatcher.lgtbot_title_list, dispatcher._P_TITLE_LIST, '头衔 列表')):
        assert await _call(handler, pattern, text) == f'<@{U1}>\n❌ 头衔图片渲染缺少 Pillow，请联系管理员安装'
    assert renders == []


def test_title_image_renders_grids_with_and_without_labels():
    pytest.importorskip('PIL')
    from plugins.LGTBot_ElainaBot.mod.users import titles_image as ti
    if ti.missing_dependency():
        pytest.skip('本机没有中文字体')
    from io import BytesIO
    from PIL import Image
    cell = {'name': '一个很长很长的头衔名字', 'hex': '#FF8C00', 'owned': True, 'equipped': True, 'index': 0, 'label': ''}
    faded = dict(cell, name='未持有', owned=False, equipped=False)
    png = ti.render('头衔列表', '已收集 1 / 6', [('限定', [cell]), ('常驻', [cell] + [faded] * 5), ('空', [])], '底部提示', 5)
    w, h = Image.open(BytesIO(png)).size
    section = ti.GAP + 58                                           # 小节间距 + 小标题
    assert w == ti.width(5) == 1400 and h == 40 + 124 + (section + ti.CELL_H) + (section + 2 * ti.CELL_H + ti.GAP) + 20 + 56 + 40
    png = ti.render('我的头衔', '', [('限定', [dict(cell, index=1, label='常驻')])], '底部提示', 3)
    w, h = Image.open(BytesIO(png)).size
    assert w == ti.width(3) == 1080 and h == 40 + 124 + section + ti.CELL_H_LABEL + 20 + 56 + 40


async def test_bare_title_command_replies_with_the_subcommand_help():
    text = await _call(dispatcher.lgtbot_title_help, dispatcher._P_TITLE_HELP, '头衔')
    for cmd in ('/头衔 我的', '/头衔 列表', '/头衔 切换 '):
        assert f'text="{cmd}"' in text, cmd
    assert '头衔 切换 无' in text
    # 帮助只认单独的「头衔」,子指令各走各的 handler
    pats = (dispatcher._P_TITLE_HELP, dispatcher._P_MY_TITLES, dispatcher._P_TITLE_LIST, dispatcher._P_EQUIP_TITLE)
    for content, hit in (('/头衔', 0), ('头衔 我的', 1), ('头衔我的', 1), ('头衔 列表', 2), ('/头衔列表', 2), ('头衔 切换 3', 3), ('头衔切换无', 3)):
        assert [bool(re.match(p, content)) for p in pats] == [i == hit for i in range(4)], content


async def test_equip_title_replies_and_never_echoes_input():
    p = dispatcher._P_EQUIP_TITLE
    h = dispatcher.lgtbot_equip_title
    assert (await _call(h, p, '头衔 切换 1')).endswith('您还没有头衔')
    titles.set_record(U1, [_t('铁蛋', '蓝'), _t('Ace', '橙')], None)
    assert (await _call(h, p, '头衔 切换 1')).endswith('✅ 已装备头衔：$\\textcolor{#FF8C00}{\\text{Ace}}$')
    assert titles.get(U1)['equipped'] == 'Ace'
    assert (await _call(h, p, '头衔 切换 Ace')).endswith('您已经装备了此头衔')
    assert (await _call(h, p, '/头衔切换铁蛋')).endswith('✅ 已装备头衔：$\\textcolor{#2980B9}{\\text{铁蛋}}$')
    for arg in ('奇怪的输入', '99'):
        text = await _call(h, p, f'头衔 切换 {arg}')
        assert arg not in text and '头衔 我的' in text
    assert titles.get(U1)['equipped'] == '铁蛋'
    usage = await _call(h, p, '头衔 切换')
    assert '用法' in usage and '头衔 我的' in usage and '头衔 切换 无' in usage


async def test_equip_title_by_command_is_audited_only_when_it_changes(monkeypatch):
    audits = []
    monkeypatch.setattr(dispatcher.audit, 'record', lambda *a, **k: audits.append((a, k)))
    monkeypatch.setattr(titles.userinfo, 'get_name', lambda uid: {U1: '铁蛋'}.get(uid, ''))
    p, h = dispatcher._P_EQUIP_TITLE, dispatcher.lgtbot_equip_title
    titles.set_record(U1, [_t('A'), _t('B', '红')], None)
    await _call(h, p, '头衔 切换 1')
    assert audits == [(('title', '切换头衔', f'铁蛋 ({U1})：装备 无 → B'), {'src': dispatcher.audit.SRC_CMD})]
    assert (await _call(h, p, '头衔 切换 B')).endswith('您已经装备了此头衔')
    for bad in ('头衔 切换 不存在', '头衔 切换'):
        await _call(h, p, bad)
    assert len(audits) == 1                                      # 切的就是当前这个、或没切换成:不记
    await _call(h, p, '头衔 切换 2')
    assert audits[-1][0][2] == f'铁蛋 ({U1})：装备 B → A'
    titles.set_record(U2, [_t('C')], None)
    await _call(h, p, '头衔 切换 C', uid=U2)
    assert audits[-1][0][2] == f'{U2}：装备 无 → C'               # 没有昵称只写 OpenID


async def test_equip_title_none_takes_the_title_off(monkeypatch):
    audits = []
    monkeypatch.setattr(dispatcher.audit, 'record', lambda *a, **k: audits.append((a, k)))
    monkeypatch.setattr(titles.userinfo, 'get_name', lambda uid: {U1: '铁蛋'}.get(uid, ''))
    p, h = dispatcher._P_EQUIP_TITLE, dispatcher.lgtbot_equip_title
    assert (await _call(h, p, '头衔 切换 无')).endswith('您还没有头衔')
    titles.set_record(U1, [_t('A'), _t('B', '红')], None)
    assert (await _call(h, p, '头衔 切换 无')).endswith('您当前没有装备头衔')
    assert audits == []                                          # 本来就没装备:不记
    titles.equip(U1, 'B')
    assert (await _call(h, p, '头衔切换无')).endswith('✅ 已取消装备头衔')
    assert titles.get(U1) == {'titles': [_t('A'), _t('B', '红')], 'equipped': None}
    assert audits == [(('title', '切换头衔', f'铁蛋 ({U1})：装备 B → 无'), {'src': dispatcher.audit.SRC_CMD})]


def test_title_commands_never_reach_the_engine():
    for text in ('头衔', '/头衔', '头衔 我的', '/头衔我的', '头衔 列表', '/头衔 列表', '头衔 切换', '头衔 切换 2', '/头衔 切换 铁 蛋', '头衔切换2'):
        assert dispatcher._is_exclusive_command(text), text
    for text in ('头衔们', '头衔 我的们', '头衔真好看', '我的', '列表', '我的头衔', '切换头衔 2'):
        assert not dispatcher._is_exclusive_command(text), text


def test_engine_thread_records_the_requester_on_its_own_thread():
    seen = {}
    state.engine_request.uid = 'MAIN'

    def fake_engine(content, uid, gid):
        seen['uid'] = state.engine_request.uid

    t = threading.Thread(target=dispatcher._run_engine, args=(fake_engine, ('/新游戏 五子棋', HOST, 'G1'), HOST))
    t.start()
    t.join()
    assert seen == {'uid': HOST}
    assert state.engine_request.uid == 'MAIN'


# ─────────────────────────────────────────────────────────────────────────
# 播报对象
# ─────────────────────────────────────────────────────────────────────────

_JOIN = f'玩家 <@{U2}> 加入了游戏\n\n游戏名称：五子棋\n- 当前用户数：2'
_LEAVE = f'玩家 <@{U2}> 退出了游戏\n\n游戏名称：五子棋\n- 当前用户数：1'
_NEW = '现在玩家可以在群里通过「/加入」报名比赛\n\n游戏名称：五子棋'


def test_new_game_broadcast_picks_the_requester_once():
    state.engine_request.uid = HOST
    callbacks.cb_match_event('G1', False, 'new_game', '五子棋')
    assert callbacks._take_title_uid(_NEW) == HOST
    assert callbacks._take_title_uid(_NEW) == ''


def test_join_broadcast_picks_the_joiner_and_leave_picks_nobody():
    state.engine_request.uid = 'SOMEONE_ELSE'
    callbacks.cb_match_event('G1', False, 'join_leave', '五子棋')
    assert callbacks._take_title_uid(_JOIN) == U2
    callbacks.cb_match_event('G1', False, 'join_leave', '五子棋')
    assert callbacks._take_title_uid(_LEAVE) == ''


def test_other_broadcasts_reset_the_mark():
    state.engine_request.uid = HOST
    for kind in ('game_started', 'announce', 'all_left'):
        callbacks.cb_match_event('G1', False, 'new_game', '五子棋')
        callbacks.cb_match_event('G1', False, kind, '')
        assert callbacks._take_title_uid(_NEW) == '', kind
    callbacks.cb_match_event('G1', False, 'game_started', '')
    assert callbacks._take_title_uid(_NEW) == ''


def test_mark_is_invisible_to_other_threads():
    callbacks.cb_match_event('G1', False, 'join_leave', '五子棋')
    seen = []
    t = threading.Thread(target=lambda: seen.append(callbacks._take_title_uid(_JOIN)))
    t.start()
    t.join()
    assert seen == ['']
    assert callbacks._take_title_uid(_JOIN) == U2


async def test_send_callbacks_hand_the_title_uid_to_their_task(monkeypatch):
    monkeypatch.setattr(state, 'event_loop', asyncio.get_running_loop())
    got = {}

    async def fake_text(key, tid, is_uid, msg, btns, title_uid=''):
        got['text'] = title_uid

    async def fake_mixed(key, tid, is_uid, segs, images, plain, btns, title_uid=''):
        got['mixed'] = title_uid

    monkeypatch.setattr(callbacks, '_serialized_text_send', fake_text)
    monkeypatch.setattr(callbacks, '_serialized_mixed_send', fake_mixed)
    monkeypatch.setattr(callbacks, '_read_rendered_image', lambda p: b'png')
    callbacks.cb_match_event('G1', False, 'join_leave', '五子棋')
    callbacks.cb_send_text_message('G1', False, _JOIN)
    callbacks.cb_match_event('G1', False, 'join_leave', '五子棋')
    callbacks.cb_send_image_message('G1', False, ['/x.png'], _JOIN + '\x01IMG0\x01')
    for _ in range(5):
        await asyncio.sleep(0)
    assert got == {'text': U2, 'mixed': U2}


# ─────────────────────────────────────────────────────────────────────────
# 播报顺序
# ─────────────────────────────────────────────────────────────────────────


async def _run_send(monkeypatch, *, push: bool, game: str, title_uid=HOST, mixed=False):
    """跑一次建房广播的串行发送,返回按送达顺序排列的消息首行。"""
    from plugins.LGTBot_ElainaBot.mod import helpers
    monkeypatch.setattr(state, 'event_loop', asyncio.get_running_loop())
    # 每个用例一个新 loop,上个用例建的锁绑在已关闭的 loop 上,争用时会让排队的 task 直接出错
    monkeypatch.setattr(callbacks, '_send_locks', {})
    monkeypatch.setattr(helpers, 'can_push_group', lambda gid: push)
    monkeypatch.setattr(callbacks.page_logs, 'log_outgoing', lambda *a, **k: None)
    sent = []

    async def fake_send(target_id, is_uid, msg, extra):
        await asyncio.sleep(0)
        sent.append(msg.split('\n')[0])

    monkeypatch.setattr(callbacks, '_send_text_quota_managed', fake_send)
    monkeypatch.setattr(callbacks, '_send_mixed_message', lambda tid, is_uid, segs, images, plain, extra: fake_send(tid, is_uid, plain, extra))
    callbacks._pending_tip_keys.clear()
    callbacks._pending_dm_warn_keys.clear()
    try:
        callbacks.cb_match_event('G1', False, 'new_game', game)
        callbacks._title_mark.kind = ''
        if mixed:
            await callbacks._serialized_mixed_send('g:G1', 'G1', False, [('text', _NEW)], {}, _NEW, None, title_uid)
        else:
            await callbacks._serialized_text_send('g:G1', 'G1', False, _NEW, None, title_uid)
        for _ in range(20):
            await asyncio.sleep(0)
    finally:
        callbacks._pending_tip_keys.clear()
        callbacks._pending_dm_warn_keys.clear()
    return sent


async def test_title_notice_follows_the_reply_limit_tip(monkeypatch):
    monkeypatch.setattr(callbacks.userinfo, 'display_name', lambda uid: {HOST: '房主*'}.get(uid, ''))
    titles.set_record(HOST, [_t('铁蛋', '紫')], '铁蛋')
    sent = await _run_send(monkeypatch, push=False, game='五子棋')
    # 玩家名是展示昵称,按 markdown 转义
    assert sent == [_NEW.split('\n')[0], '## ⚠️ 消息回复限制', '$\\textcolor{#8E44AD}{\\textbf{【铁蛋】}}$ 房主\\* 加入游戏']


async def test_title_notice_follows_the_dm_warning(monkeypatch):
    monkeypatch.setattr(callbacks.userinfo, 'display_name', lambda uid: '')
    titles.set_record(HOST, [_t('铁蛋', '紫')], '铁蛋')
    game = next(iter(callbacks._DM_LIMITED_GAMES))
    sent = await _run_send(monkeypatch, push=True, game=game, mixed=True)
    # 查不到昵称时退回 @,由出站改写成各场景的写法
    assert len(sent) == 3 and sent[1].startswith('## ') and sent[2] == f'$\\textcolor{{#8E44AD}}{{\\textbf{{【铁蛋】}}}}$ <@{HOST}> 加入游戏'


async def test_no_notice_without_an_equipped_title(monkeypatch):
    titles.set_record(HOST, [_t('铁蛋', '紫')], None)
    assert len(await _run_send(monkeypatch, push=True, game='五子棋')) == 1
    assert len(await _run_send(monkeypatch, push=True, game='五子棋', title_uid='')) == 1
