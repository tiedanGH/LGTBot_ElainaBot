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
        'attendances': 88,
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
        'attendances': 130,
        'top_games_today': [{'game_name': f'g{i}', 'count': 11 - i}
                            for i in range(1, 11)],
        'top_players_today': [{'display': f'p{i}', 'count': 11 - i}
                              for i in range(1, 11)],
        'trend_10d': [],
    }
    png = stats_image.render_stats_image(g, sub_title='2026-08 月度统计')
    assert png and png[:8] == b'\x89PNG\r\n\x1a\n'


def _delta_only_stats(diff) -> dict:
    """只让「今日对局」一张卡带涨跌胶囊,其余卡不给对比数据(不画胶囊)。

    ``diff=None`` → 连这张卡也不画胶囊,用作像素差分的基准版。
    """
    g = {
        'available': True,
        'today_matches': 100, 'today_players': 10, 'today_groups': 3,
        'top_games_today': [], 'top_players_today': [], 'trend_10d': [],
    }
    if diff is not None:
        g['yesterday_matches_same_span'] = 100 - diff
    return g


def _pill_colors(diff: int) -> set:
    """渲染「带胶囊」与「无胶囊」两版做像素差分,返回**只属于胶囊**的颜色集。

    不能直接在整图里找颜色:_GREEN / _RED 同时用于图标底色与配额告警,满图都有,分辨不出胶囊到底画成了哪种。
    两版布局完全一致(``_delta_pill`` 在 diff 为 None 时什么都不画),所以差异像素精确等于胶囊本身。
    """
    from io import BytesIO
    from PIL import Image, ImageChops
    base = Image.open(BytesIO(stats_image.render_stats_image(
        _delta_only_stats(None), sub_title='x'))).convert('RGB')
    shot = Image.open(BytesIO(stats_image.render_stats_image(
        _delta_only_stats(diff), sub_title='x'))).convert('RGB')
    assert base.size == shot.size, '两版布局应一致,差分才有意义'
    mask = ImageChops.difference(base, shot).convert('L')
    box = mask.getbbox()
    assert box, '带 diff 的一版应当多画出胶囊'
    return {shot.getpixel((x, y))
            for x in range(box[0], box[2]) for y in range(box[1], box[3])
            if mask.getpixel((x, y))}


@pytest.mark.parametrize('diff,want,other', [
    (20, '_RED', '_GREEN'),        # 涨 → 红
    (-20, '_GREEN', '_RED'),       # 跌 → 绿
])
def test_delta_pill_is_up_red_down_green(diff, want, other):
    """★ 配色契约:涨跌胶囊同 dau 卡片取**涨红跌绿**(证券风格),与这两个常量
    在图标 / 配额处的语义正好相反 —— 极易在后续改动里被"顺手改回来"。
    写反了图上只是颜色互换,没有任何报错,所以直接查胶囊像素。"""
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    colors = _pill_colors(diff)
    want_fg, other_fg = getattr(stats_image, want), getattr(stats_image, other)
    assert want_fg in colors                          # 箭头 + 数字
    assert stats_image._tint(want_fg) in colors       # 胶囊底(18% 透明底)
    assert other_fg not in colors and stats_image._tint(other_fg) not in colors


def test_delta_pill_flat_is_neutral_grey():
    """持平(diff == 0)既不红也不绿 —— 用中性灰,避免 0 变化被误读成趋势。"""
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    colors = _pill_colors(0)
    assert stats_image._tint(stats_image._TEXT_MUTED) in colors
    for c in (stats_image._RED, stats_image._GREEN):
        assert c not in colors and stats_image._tint(c) not in colors



def test_bot_scale_row_only_when_data_present():
    """群组 / 好友总数那一行由 dispatcher 只在今日视图注入 —— 缺数据(历史日 /
    月视图)时整行不画,画了就多一行高度。"""
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    base = _sample_stats()
    h_without = uploader.get_image_size(
        stats_image.render_stats_image(base, sub_title='x'))[1]
    withrow = dict(base, bot_groups=1284, bot_friends=5391,
                   bot_groups_delta=7, bot_friends_delta=-3)
    h_with = uploader.get_image_size(
        stats_image.render_stats_image(withrow, sub_title='x'))[1]
    assert h_with > h_without


def _colors(png: bytes) -> set:
    from io import BytesIO
    from PIL import Image
    im = Image.open(BytesIO(png)).convert('RGB')
    return {c for _n, c in im.getcolors(1 << 20)}


def test_bot_scale_delta_is_net_change_not_yesterday():
    """★ 这两张卡的胶囊语义与其它卡**不同**:传进来的已经是今日净变化本身,
    不是「昨日值」—— 当成昨日值去做减法会算出相反数。

    判据取 ``_tint(_RED)`` 的有无:本样本里没有别的 _RED 使用者(配额未告警),
    所以「净增出现红底 / 净减不出现红底」能同时钉住两个方向。
    (不用 _GREEN 判:活跃玩家的 person 图标底色就是它,满图都有。)
    """
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    base = dict(_sample_stats(with_trend=False, with_ranks=False),
                bot_groups=100, bot_friends=100, bot_friends_delta=None)
    red = stats_image._tint(stats_image._RED)

    up = stats_image.render_stats_image(dict(base, bot_groups_delta=5), sub_title='x')
    assert red in _colors(up)                       # 净增 = 涨 = 红
    down = stats_image.render_stats_image(dict(base, bot_groups_delta=-5), sub_title='x')
    assert red not in _colors(down)                 # 净减不该出现红
    flat = stats_image.render_stats_image(dict(base, bot_groups_delta=0), sub_title='x')
    assert stats_image._tint(stats_image._TEXT_MUTED) in _colors(flat)
    assert red not in _colors(flat)


def test_friend_icon_distinct_from_person():
    """'friend' 与 'person' 必须画得不一样 —— 前者是后者加了爱心。"""
    pytest.importorskip('PIL')
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (120, 60), (255, 255, 255))
    d = ImageDraw.Draw(img)
    stats_image._icon(d, 'person', 0, 4, (0, 0, 0))
    stats_image._icon(d, 'friend', 56, 4, (0, 0, 0))
    assert img.crop((0, 0, 56, 60)).tobytes() != img.crop((56, 0, 112, 60)).tobytes()


def test_bot_scale_row_uses_friend_icon_not_person(monkeypatch):
    """★ 光有 'friend' 图标不够,渲染时**真的要用它** —— 顺手写回 'person' 会让
    好友总数和「今日活跃玩家」的图标一模一样。这里记下 _icon 实际收到的 kind。"""
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    kinds: list = []
    real = stats_image._icon
    monkeypatch.setattr(stats_image, '_icon',
                        lambda d, kind, *a, **k: kinds.append(kind) or real(d, kind, *a, **k))
    stats_image.render_stats_image(
        dict(_sample_stats(with_trend=False, with_ranks=False),
             bot_groups=1, bot_friends=2, bot_groups_delta=0, bot_friends_delta=0),
        sub_title='x')
    # 前两行已经用掉 person / group;bot 规模行必须是 group + friend
    assert kinds.count('friend') == 1, kinds
    assert kinds.count('person') == 1, kinds     # 只有「今日活跃玩家」那一张


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
