#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按钮组(group_id)约定:回调按钮除蓝底外都成组 —— 每颗单独一组(点一次就失效),数据统计候选共用一组;刷新按钮不分组。"""

from __future__ import annotations

import pytest

from plugins.LGTBot_ElainaBot.mod.messaging import buttons, quota


@pytest.fixture(autouse=True)
def _sponsor_on(monkeypatch):
    """赞助入口打开,覆盖到最多的按钮。"""
    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', True)


def _keyboards() -> dict:
    b = buttons
    return {
        'menu': b.build_menu_buttons(),
        'more_features': b.build_more_features_buttons(),
        'about': b.build_about_buttons(),
        'notice': b.build_notice_buttons(older=True),
        'room': b.build_game_action_buttons('数字蜂巢', include_rule=True),
        'game_over': b.build_game_over_buttons('数字蜂巢'),
        'unknown_meta': b.build_unknown_meta_buttons(),
        'unknown_config': b.build_unknown_config_buttons(),
        'unknown_game': b.build_unknown_game_buttons(),
        'game_help': b.build_game_help_buttons(),
        'dissolve': b.build_dissolve_buttons(),
        'suggest': b.build_game_suggest_buttons(['五子棋', '困兽棋', '天赋云巢']),
        'force_interrupt': b.build_force_interrupt_buttons(),
        'sponsor': b.build_sponsor_buttons(),
        'support': b.build_support_buttons(),
    }


def test_callback_buttons_are_grouped_except_blue_ones():
    """★ 回调按钮除蓝底外都要成组:漏了的话,用户翻到旧消息点按钮,回复出现在最底下看不到,会一直点同一颗;蓝底(style=4)置灰几乎看不出区别,不作要求。"""
    for name, kb in _keyboards().items():
        for row in kb:
            for x in row:
                if x.get('type') == 1 and x['style'] != 4:
                    assert x.get('group_id'), (name, x['text'])


def test_different_buttons_never_share_a_group():
    """★ 同一条消息里不同的按钮各占一组:共用一组的话,点了一颗另一颗也跟着置灰。只有数据统计候选是有意共用。"""
    for name, kb in _keyboards().items():
        groups: dict = {}
        for row in kb:
            for x in row:
                if x.get('group_id'):
                    groups.setdefault(x['group_id'], set()).add(x['data'])
        for gid, datas in groups.items():
            if gid != 'stats_pick':
                assert len(datas) == 1, (name, gid, datas)


def test_stats_candidates_share_one_group():
    rows = buttons.build_game_suggest_buttons(['五子棋', '困兽棋', '天赋云巢'])
    assert {x['group_id'] for row in rows[:-1] for x in row} == {'stats_pick'}
    assert 'group_id' not in rows[-1][0]          # 末排「游戏列表」是回填按钮,不成组


def test_refresh_button_stays_repeatable():
    """刷新按钮不分组:每次点击都要续一轮被动额度。"""
    for is_last in (False, True):
        assert 'group_id' not in quota.build_refresh_button(is_last)[0]


@pytest.mark.parametrize('kw', [{'type': 2}, {'type': 0}, {'link': 'https://example.com'}])
def test_group_only_allowed_on_callback_buttons(kw):
    """官方按钮组只对 type=1 生效:其他类型传了直接报错,免得以为成组了其实没有。"""
    with pytest.raises(ValueError):
        buttons.btn('x', 'y', group='g', **kw)
