#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""stats_image 渲染 + 数据统计指令图片通道测试。

渲染用例依赖 PIL(importorskip;CI / 本机均装);指令通道用例全 mock,
不真渲染不真上传。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.LGTBot_ElainaBot.mod import dispatcher, stats_image, uploader


def _sample_stats(with_trend=True, with_ranks=True) -> dict:
    trend = []
    if with_trend:
        today = datetime.now().date()
        trend = [{'date': (today - timedelta(days=i)).strftime('%Y-%m-%d'),
                  'count': (10 - i) * 2, 'players': 10 - i}
                 for i in range(10)]                       # 新→旧
    return {
        'available': True, 'errors': [],
        'today_matches': 20, 'today_players': 10, 'today_groups': 4,
        'top_games_today': ([{'game_name': '决胜五子棋这个名字特别特别长会溢出',
                              'count': 8},
                             {'game_name': '大富翁', 'count': 5},
                             {'game_name': '狼人杀', 'count': 2}] if with_ranks else []),
        'top_players_today': ([{'display': '铁蛋', 'count': 6},
                               {'display': 'abc****xyz', 'count': 4}] if with_ranks else []),
        'trend_10d': trend,
    }


# ──────── 渲染 ────────────────────────────────────────────────────────────

def test_render_returns_png_with_sane_dimensions():
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    png = stats_image.render_stats_image(_sample_stats(), sub_title='截至 12:34')
    assert png and png[:8] == b'\x89PNG\r\n\x1a\n'
    w, h = uploader.get_image_size(png)
    assert w == 1000 and h > 800


def test_render_without_trend_and_ranks_still_renders():
    """空趋势 / 空榜单:趋势卡整体省略,榜单显示「暂无数据」,不崩。"""
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    png = stats_image.render_stats_image(
        _sample_stats(with_trend=False, with_ranks=False))
    assert png and png[:8] == b'\x89PNG\r\n\x1a\n'


def test_render_date_mode_layout():
    """历史日期模式:无趋势 section,rank_limit=10 → 10 行榜单。"""
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    g = {
        'available': True, 'date_mode': True, 'rank_limit': 10,
        'today_matches': 12, 'today_players': 5, 'today_groups': 3,
        'trailing10_matches': 88,
        'top_games_today': [{'game_name': f'g{i}', 'count': 11 - i}
                            for i in range(1, 12)],           # 11 条 → 截 10
        'top_players_today': [{'display': f'p{i}', 'count': 11 - i}
                              for i in range(1, 12)],
        'trend_10d': [],
    }
    png = stats_image.render_stats_image(g, sub_title='2026-08-02')
    assert png and png[:8] == b'\x89PNG\r\n\x1a\n'
    _w, h_date = uploader.get_image_size(png)

    normal = stats_image.render_stats_image(_sample_stats(), sub_title='x')
    _w2, h_normal = uploader.get_image_size(normal)
    # 日期模式无趋势但榜单 10 行,两种布局高度必不同——分支生效的低成本验证
    assert h_date != h_normal


def test_render_month_mode_layout():
    """按月模式:走 date_mode 布局(无趋势/榜单 10 行),第 4 卡切换为
    「当月对局人次」+ 票根图标 —— 渲染不崩即分支与新图标生效。"""
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    g = {
        'available': True, 'date_mode': True, 'month_mode': True,
        'rank_limit': 10,
        'today_matches': 42, 'today_players': 9, 'today_groups': 4,
        'month_attendances': 130,
        'top_games_today': [{'game_name': f'g{i}', 'count': 11 - i}
                            for i in range(1, 11)],
        'top_players_today': [{'display': f'p{i}', 'count': 11 - i}
                              for i in range(1, 11)],
        'trend_10d': [],
    }
    png = stats_image.render_stats_image(g, sub_title='2026-08 月度统计')
    assert png and png[:8] == b'\x89PNG\r\n\x1a\n'


def test_render_swallows_exceptions(monkeypatch):
    """渲染内部异常 → None(调用方回退文本),不抛。"""
    monkeypatch.setattr(stats_image, '_render',
                        lambda g, s: (_ for _ in ()).throw(RuntimeError('boom')))
    assert stats_image.render_stats_image({}, '') is None


# ──────── 数据统计指令的图片通道(全 mock)─────────────────────────────────

def _fake_event(is_group=True):
    ev = MagicMock()
    ev.user_id = 'USER1'
    ev.group_id = 'GROUP1' if is_group else ''
    ev.channel_id = ''
    ev.is_group = is_group
    ev.is_interaction = False
    ev.reply = AsyncMock()
    return ev


@pytest.fixture
def _stats_env(monkeypatch):
    monkeypatch.setattr(dispatcher.helpers, 'is_foreign_event', lambda e: False)
    monkeypatch.setattr(dispatcher.metrics, 'query_game_stats',
                        lambda: _sample_stats())
    yield


async def test_stats_command_replies_markdown_image(monkeypatch, _stats_env):
    """配了图床 + 渲染上传成功 → markdown 内嵌图回复(带 @ 与 #Wpx #Hpx)。"""
    monkeypatch.setattr(uploader, 'SELECTED_BACKEND', 'cos')
    png = b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\x0DIHDR' + \
        (640).to_bytes(4, 'big') + (480).to_bytes(4, 'big')
    monkeypatch.setattr(dispatcher.stats_image, 'render_stats_image',
                        lambda g, sub: png)
    seen = {}

    async def fake_upload(data, filename, user_id='', *, target_id='', target_is_uid=False):
        seen.update(filename=filename, target_id=target_id, target_is_uid=target_is_uid)
        return 'https://cdn.example/stats.png'
    monkeypatch.setattr(dispatcher.uploader, 'upload_image', fake_upload)

    ev = _fake_event(is_group=True)
    await dispatcher.lgtbot_data_stats(ev, None)
    ev.reply.assert_awaited_once()
    md = ev.reply.await_args.args[0]
    assert '<@USER1>' in md and 'https://cdn.example/stats.png' in md
    assert '#640px' in md and '#480px' in md
    assert seen == {'filename': 'lgtbot_stats.png',
                    'target_id': 'GROUP1', 'target_is_uid': False}


async def test_stats_command_falls_back_to_text(monkeypatch, _stats_env):
    """渲染失败(无 PIL / 字体)→ 回退原文本输出。"""
    monkeypatch.setattr(uploader, 'SELECTED_BACKEND', 'cos')
    monkeypatch.setattr(dispatcher.stats_image, 'render_stats_image',
                        lambda g, sub: None)
    ev = _fake_event()
    await dispatcher.lgtbot_data_stats(ev, None)
    text = ev.reply.await_args.args[0]
    assert '今日对局' in text and '![' not in text


async def test_stats_command_text_when_no_backend(monkeypatch, _stats_env):
    """未配置图床 → 不渲染不上传,直接文本。"""
    monkeypatch.setattr(uploader, 'SELECTED_BACKEND', '')
    called = []
    monkeypatch.setattr(dispatcher.stats_image, 'render_stats_image',
                        lambda g, sub: called.append(1) or b'x')
    ev = _fake_event()
    await dispatcher.lgtbot_data_stats(ev, None)
    assert called == []
    assert '今日对局' in ev.reply.await_args.args[0]
