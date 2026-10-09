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
    # 日期模式无趋势但榜单 10 行,两种布局高度必不同
    assert h_date != h_normal


def test_render_month_mode_layout():
    """按月模式:走 date_mode 布局(无趋势/榜单 10 行),第 4 卡切换为
    「当月对局人次」+ 票根图标 —— 渲染不崩即分支与图标生效。"""
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

    不能直接在整图里找颜色:_GREEN / _RED 同时用于图标底色与配额告警,满图都有。
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
    """★ 配色契约:涨跌胶囊取**涨红跌绿**,与这两个常量在图标 / 配额处的语义相反。"""
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
    """好友 / 群聊总数那一行缺数据(历史日 / 月视图)时整行不画,画了就多一行高度。"""
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


def _first_bulk_y(png: bytes, rgb: tuple, min_run: int = 40):
    """该颜色**成片**出现(某一行里至少 ``min_run`` 个像素)的最靠上行号,没有则 None。

    只认成片填充:抗锯齿边缘会混出中间色,单个像素可能恰好撞上目标色值。
    """
    from io import BytesIO
    from PIL import Image
    im = Image.open(BytesIO(png)).convert('RGB')
    w, h = im.size
    px = im.load()
    for y in range(h):
        if sum(1 for x in range(w) if px[x, y] == rgb) >= min_run:
            return y
    return None


def test_bot_scale_delta_is_net_change_not_yesterday():
    """★ 这两张卡的胶囊语义与其它卡**不同**:传进来的已经是今日净变化本身,
    不是「昨日值」—— 当成昨日值去做减法会算出相反数。

    判据取 ``_RED`` 的有无:本样本里没有别的 _RED 使用者;不能用 _GREEN,person 图标就是它。
    """
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    base = dict(_sample_stats(with_trend=False, with_ranks=False),
                bot_groups=100, bot_friends=100, bot_friends_delta=None)
    red = stats_image._RED

    up = stats_image.render_stats_image(dict(base, bot_groups_delta=5), sub_title='x')
    assert red in _colors(up)                       # 净增 = 涨 = 红
    down = stats_image.render_stats_image(dict(base, bot_groups_delta=-5), sub_title='x')
    assert red not in _colors(down)                 # 净减不该出现红
    flat = stats_image.render_stats_image(dict(base, bot_groups_delta=0), sub_title='x')
    assert red not in _colors(flat)                 # 持平也不该出现红


def test_bot_scale_pill_is_outlined_today_pill_is_filled():
    """★ 两类角标形态必须不同(用户要求):累计总数用**描边**胶囊、今日指标用
    **实底**胶囊。判据是 ``_tint(_RED)``(实底胶囊的底色)—— 描边胶囊内部填的是
    卡片底色,不会产生这个色值。"""
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    filled = stats_image._tint(stats_image._RED)
    base = _sample_stats(with_trend=False, with_ranks=False)

    # 只有累计总数行带角标 → 描边,不该出现实底胶囊的底色
    only_scale = stats_image.render_stats_image(
        dict(base, bot_groups=100, bot_friends=100,
             bot_groups_delta=5, bot_friends_delta=None), sub_title='x')
    assert stats_image._RED in _colors(only_scale)
    assert filled not in _colors(only_scale)

    # 只有今日行带角标 → 实底
    only_today = stats_image.render_stats_image(
        dict(base, yesterday_matches_same_span=base['today_matches'] - 5),
        sub_title='x')
    assert filled in _colors(only_today)


def test_total_bg_is_visibly_distinct_from_today_bg():
    """★ 约束**色差本身**:_TOTAL_BG 与今日卡的 _PANEL2 至少有一个通道相差 16 个色阶,
    差得少了并排几乎分不出;色相可自由调整。"""
    d = [abs(a - b) for a, b in zip(stats_image._TOTAL_BG, stats_image._PANEL2)]
    assert max(d) >= 16, (stats_image._TOTAL_BG, d)
    # 边框也应比常规边框更明显,否则整块卡的轮廓会被卡面吃掉
    db = [abs(a - b) for a, b in zip(stats_image._TOTAL_BORDER, stats_image._BORDER)]
    assert max(db) >= 8, (stats_image._TOTAL_BORDER, db)


def test_bot_scale_row_comes_first_and_has_distinct_bg():
    """★ 行序与底色(用户要求):累计总数行排在**数据总览标题正下方**,今日行在其下;
    且底色用更深一档的 ``_TOTAL_BG``,与今日行的 ``_PANEL2`` 区分。

    _TOTAL_BG 是这一行**专用**的色值,所以"它最靠上出现在哪一行"就等于这一行的位置。
    """
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    g = dict(_sample_stats(with_trend=False, with_ranks=False),
             bot_groups=1284, bot_friends=5391,
             bot_groups_delta=7, bot_friends_delta=-3)
    png = stats_image.render_stats_image(g, sub_title='')
    # 阈值取 200px:描边胶囊的内部也填 _TOTAL_BG,阈值太低胶囊就能冒充卡面
    y_total = _first_bulk_y(png, stats_image._TOTAL_BG, min_run=200)
    # 今日行的定位物:活跃玩家 person 图标的 52px 宽底块
    y_today = _first_bulk_y(png, stats_image._tint(stats_image._GREEN))
    assert y_total is not None and y_today is not None
    assert y_total < y_today, (y_total, y_today)               # 总数行在今日行之上

    # 没有累计总数时,_TOTAL_BG 不该出现在卡片区(证明它确实是这一行带来的)
    plain = stats_image.render_stats_image(
        _sample_stats(with_trend=False, with_ranks=False), sub_title='')
    assert _first_bulk_y(plain, stats_image._TOTAL_BG, min_run=200) is None


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
    好友总数和「今日活跃玩家」的图标一模一样。"""
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


@pytest.mark.parametrize('view, players, groups', [
    ({}, '今日活跃玩家', '今日活跃群聊'),
    ({'date_mode': True, 'total_mode': True, 'attendances': 9}, '累计玩家', '累计群聊'),
])
def test_bot_scale_tiles_sit_above_their_columns(monkeypatch, view, players, groups):
    """★ 好友总数在玩家卡正上方、群聊总数在群聊卡正上方(用户要求)—— 数字和角标跟着各自的卡走,不是只换了标签。"""
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    from PIL import ImageDraw
    texts = []
    real = ImageDraw.ImageDraw.text
    monkeypatch.setattr(ImageDraw.ImageDraw, 'text',
                        lambda self, xy, t, *a, **kw: texts.append((xy, t)) or real(self, xy, t, *a, **kw))
    stats_image.render_stats_image(
        dict(_sample_stats(with_trend=False, with_ranks=False), **view,
             bot_groups=1284, bot_friends=5391, bot_groups_delta=7, bot_friends_delta=-3),
        sub_title='x')
    monkeypatch.undo()
    at = {t: xy for xy, t in texts if xy[0] >= 0}          # 胶囊会先在画面外量一次宽度
    assert at['好友总数'][0] == at[players][0] < at['群聊总数'][0] == at[groups][0]
    assert at['好友总数'][1] == at['群聊总数'][1] < at[players][1]
    assert at['5,391'][0] == at['好友总数'][0] and at['1,284'][0] == at['群聊总数'][0]
    assert at['↓ 3'][0] < at['群聊总数'][0] < at['↑ 7'][0]
    assert '群组总数' not in at


def test_period_word_matches_dispatcher_span_views():
    """★ 跨模块措辞防漂移:图片的期间词必须与 dispatcher 文本保底的一致 —— 两条出口各有一份措辞表。"""
    for view, cfg in dispatcher._SPAN_VIEWS.items():
        g = dict(cfg['flags'], date_mode=True)
        assert stats_image._period_word(g) == cfg['period'], view
    # 今日视图(非 date_mode)不走期间词,默认值不该被当成它的文案
    assert stats_image._period_word({}) == '当日'


def test_total_mode_scale_row_shows_no_pill():
    """★ 累计视图的总数行**不带角标**(用户要求):delta 为 None 时一个胶囊都不画。

    判据取描边胶囊的边框色 ``_RED`` —— 关掉榜单 / 趋势后,这个色值只可能来自涨跌胶囊。
    """
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    base = dict(_sample_stats(with_trend=False, with_ranks=False),
                date_mode=True, total_mode=True, attendances=9,
                bot_groups=1284, bot_friends=5391)
    png = stats_image.render_stats_image(base, sub_title='累计总统计')
    assert png and stats_image._RED not in _colors(png)
    # 对照:同一视图给了 delta 就该出现胶囊(证明判据有效,不是恒不出现)
    with_delta = stats_image.render_stats_image(
        dict(base, bot_groups_delta=7), sub_title='累计总统计')
    assert stats_image._RED in _colors(with_delta)


def test_total_mode_still_draws_the_scale_row():
    """累计视图仍要有那一行(只是没角标)—— 少了它整块高度会塌一行。"""
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    base = dict(_sample_stats(with_trend=False, with_ranks=False),
                date_mode=True, total_mode=True, attendances=9)
    h_without = uploader.get_image_size(
        stats_image.render_stats_image(base, sub_title='x'))[1]
    h_with = uploader.get_image_size(stats_image.render_stats_image(
        dict(base, bot_groups=1284, bot_friends=5391), sub_title='x'))[1]
    assert h_with > h_without
    # 这一行的专用底色也要在(与今日行区分那套配色照旧生效)
    png = stats_image.render_stats_image(
        dict(base, bot_groups=1284, bot_friends=5391), sub_title='x')
    assert _first_bulk_y(png, stats_image._TOTAL_BG, min_run=200) is not None


def test_render_swallows_exceptions(monkeypatch):
    """渲染内部异常 → None(调用方回退文本),不抛。"""
    monkeypatch.setattr(stats_image, '_render',
                        lambda *a: (_ for _ in ()).throw(RuntimeError('boom')))
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
                        lambda g, sub, elapsed_ms=None: png)
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
    """渲染失败(无 PIL / 字体)→ 回退文本输出。"""
    monkeypatch.setattr(uploader, 'SELECTED_BACKEND', 'cos')
    monkeypatch.setattr(dispatcher.stats_image, 'render_stats_image',
                        lambda g, sub, elapsed_ms=None: None)
    ev = _fake_event()
    await dispatcher.lgtbot_data_stats(ev, None)
    text = ev.reply.await_args.args[0]
    assert '今日对局' in text and '![' not in text


async def test_stats_command_text_when_no_backend(monkeypatch, _stats_env):
    """未配置图床 → 不渲染不上传,直接文本。"""
    monkeypatch.setattr(uploader, 'SELECTED_BACKEND', '')
    called = []
    monkeypatch.setattr(dispatcher.stats_image, 'render_stats_image',
                        lambda g, sub, elapsed_ms=None: called.append(1) or b'x')
    ev = _fake_event()
    await dispatcher.lgtbot_data_stats(ev, None)
    assert called == []
    assert '今日对局' in ev.reply.await_args.args[0]


# ──────── @ 与图片之间的换行 ────────────────────

def test_stats_image_md_separates_mention_from_image():
    """★ @ 与图片之间空一行 —— 紧贴时 QQ 客户端会把两者挤成一块。"""
    md = dispatcher._stats_image_md('U1', 640, 480, 'https://cdn/x.png')
    assert md == '<@U1>\n![数据统计 #640px #480px](https://cdn/x.png)'


def test_stats_image_md_is_the_only_place_building_that_markdown():
    """图片回执的 markdown 只许有一处 —— 今日视图与四个窗口视图共用同一个出口,分开拼的话改格式容易只改一边。"""
    import inspect
    src = inspect.getsource(dispatcher)
    assert src.count('![数据统计 #') == 1, '数据统计图片 markdown 出现了多份拼装'


async def test_today_view_image_reply_has_the_newline(monkeypatch, _stats_env):
    """今日视图(无参数)的真实回执里带换行。"""
    monkeypatch.setattr(uploader, 'SELECTED_BACKEND', 'cos')
    png = b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\x0DIHDR' + \
        (640).to_bytes(4, 'big') + (480).to_bytes(4, 'big')
    monkeypatch.setattr(dispatcher.stats_image, 'render_stats_image',
                        lambda g, sub, elapsed_ms=None: png)

    async def fake_upload(data, filename, user_id='', *, target_id='', target_is_uid=False):
        return 'https://cdn.example/stats.png'
    monkeypatch.setattr(dispatcher.uploader, 'upload_image', fake_upload)

    ev = _fake_event()
    await dispatcher.lgtbot_data_stats(ev, None)
    assert ev.reply.await_args.args[0].startswith('<@USER1>\n![数据统计 ')


async def test_window_view_image_reply_has_the_newline(monkeypatch, _stats_env):
    """窗口视图(这里用「数据统计总」)走的是另一个出口,同样带换行。"""
    import re as _re
    monkeypatch.setattr(uploader, 'SELECTED_BACKEND', 'cos')
    png = b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\x0DIHDR' + \
        (640).to_bytes(4, 'big') + (480).to_bytes(4, 'big')
    monkeypatch.setattr(dispatcher.stats_image, 'render_stats_image',
                        lambda g, sub, elapsed_ms=None: png)
    monkeypatch.setattr(dispatcher.metrics, 'query_game_stats_total',
                        lambda: {'available': True, 'total_matches': 5,
                                 'total_players': 4, 'total_groups': 3,
                                 'total_attendances': 9,
                                 'top_games_total': [], 'top_players_total': []})

    async def fake_upload(data, filename, user_id='', *, target_id='', target_is_uid=False):
        return 'https://cdn.example/stats.png'
    monkeypatch.setattr(dispatcher.uploader, 'upload_image', fake_upload)

    ev = _fake_event()
    await dispatcher.lgtbot_data_stats(ev, _re.match(dispatcher._P_STATS, '数据统计总'))
    assert ev.reply.await_args.args[0].startswith('<@USER1>\n![数据统计 ')


def test_unranked_tag_is_actually_drawn():
    """★ 名字短到不会被截断,所以两张图的唯一差别只能是胶囊本身 —— 用长名字比会被「预留位置导致截得更短」混过去。"""
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    base = _sample_stats()
    base['top_games_today'] = [{'game_name': '斗地主', 'count': 8}]
    tagged = dict(base, top_games_today=[{'game_name': '斗地主', 'count': 8,
                                          'unranked': True}])
    a = stats_image.render_stats_image(tagged, sub_title='x')
    b = stats_image.render_stats_image(base, sub_title='x')
    assert a and b and a != b
    assert uploader.get_image_size(a) == uploader.get_image_size(b)


def test_rank_row_gives_the_name_everything_up_to_the_count():
    """★ 计数按实际文本宽右对齐。打标的行再从可用宽里扣掉胶囊。"""
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    from PIL import Image, ImageDraw
    d = ImageDraw.Draw(Image.new('RGB', (10, 10)))
    cf = stats_image._font(22)
    cx, half_w = 36, 453
    tag_w = stats_image._unranked_tag_w(d)
    assert tag_w > 0

    nx, nw, cnt_x = stats_image._rank_row_layout(d, cx, half_w, '8局', cf, True)
    # 计数右对齐到栏内边缘
    assert cnt_x + stats_image._text_w(d, '8局', cf) == cx + half_w - 28
    # 名字 + 胶囊合起来仍在计数之前
    assert nx + nw + 10 + tag_w <= cnt_x
    # 不打标时名字直接顶到计数之前,正好宽出一个胶囊
    _nx2, nw2, cnt_x2 = stats_image._rank_row_layout(d, cx, half_w, '8局', cf, False)
    assert cnt_x2 == cnt_x and nw2 - nw == tag_w + 10
    # 计数越长,留给名字的越少
    _nx3, nw3, _c3 = stats_image._rank_row_layout(d, cx, half_w, '12345局', cf, False)
    assert nw3 < nw2


def test_unranked_tag_trails_the_game_name():
    """★ 胶囊跟在游戏名后面,不是钉在计数旁边 —— 名字短时它就该靠着名字。"""
    import inspect
    src = inspect.getsource(stats_image._render)
    assert "_unranked_tag(d, name_x + _text_w(d, shown, nf) + 10, ry + 2)" in src


def test_fit_name_only_truncates_when_needed():
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    from PIL import Image, ImageDraw
    d = ImageDraw.Draw(Image.new('RGB', (10, 10)))
    f = stats_image._font(24)
    long_name = '这个游戏名字特别特别长长到必须被截断'
    assert stats_image._fit_name(d, long_name, 300, f).endswith('…')
    assert stats_image._text_w(d, stats_image._fit_name(d, long_name, 300, f), f) <= 300
    assert stats_image._fit_name(d, '斗地主', 300, f) == '斗地主'


def test_unranked_tag_absent_when_flag_is_false():
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    g = _sample_stats()
    g['top_games_today'] = [dict(t, unranked=False) for t in g['top_games_today']]
    assert stats_image.render_stats_image(g, sub_title='x') == \
           stats_image.render_stats_image(_sample_stats(), sub_title='x')


def _push_value_call(monkeypatch, limit):
    """渲染一张带主动消息行的图,返回那一行数字的 (xy, 字号)。"""
    calls = []
    real = stats_image._bold_text

    def spy(d, xy, text, font, fill):
        calls.append((xy, text, getattr(font, 'size', None)))
        return real(d, xy, text, font, fill)

    monkeypatch.setattr(stats_image, '_bold_text', spy)
    g = dict(_sample_stats(), push_quota={
        'shown': True, 'is_group': True, 'used': 312, 'limit': limit,
        'remaining': max(0, limit - 312), 'near_limit': False,
        'exhausted': False, 'no_permission': False})
    stats_image.render_stats_image(g)
    hits = [(xy, size) for xy, text, size in calls if text.startswith('312')]
    assert len(hits) == 1, calls
    monkeypatch.undo()
    return hits[0]


def test_unlimited_push_quota_number_matches_the_other_tiles(monkeypatch):
    """★ 不限量(limit=0)没有进度条 —— 数字要和普通指标卡一样大、一样的位置。"""
    pytest.importorskip('PIL')
    (x0, y0), size_unlimited = _push_value_call(monkeypatch, 0)
    (x1, y1), size_limited = _push_value_call(monkeypatch, 1000)
    assert size_unlimited == 48        # 普通指标卡的数字字号
    assert size_limited == 36          # 有上限:给进度条让出底部
    assert y0 - y1 == 6                # 不限量落回指标卡的 cy+60,而不是 cy+54


def _quota_draw_calls(monkeypatch, pq):
    """渲染一张带主动消息行的图,记下全部文字 (xy, 文本, 字体, 颜色)、圆角矩形与纸飞机图标。"""
    from PIL import ImageDraw
    texts, rects, icons = [], [], []
    real_text, real_rect = ImageDraw.ImageDraw.text, ImageDraw.ImageDraw.rounded_rectangle
    real_push = stats_image._push_icon

    def text(self, xy, t, *a, **kw):
        texts.append((xy, t, kw.get('font'), kw.get('fill')))
        return real_text(self, xy, t, *a, **kw)

    def rect(self, box, *a, **kw):
        rects.append((tuple(box), kw.get('fill')))
        return real_rect(self, box, *a, **kw)

    def push_icon(d, ix, iy, fg, size=52):
        icons.append((ix, iy, fg, size))
        return real_push(d, ix, iy, fg, size)

    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', text)
    monkeypatch.setattr(ImageDraw.ImageDraw, 'rounded_rectangle', rect)
    monkeypatch.setattr(stats_image, '_push_icon', push_icon)
    stats_image.render_stats_image(dict(_sample_stats(), push_quota=pq))
    monkeypatch.undo()
    return texts, rects, icons


@pytest.mark.parametrize('is_group, label', [(True, '群总计'), (False, '私信总计')])
def test_push_quota_row_trails_the_scene_total(monkeypatch, is_group, label):
    """★ 本会话用量后面跟今日总计:灰标签 + accent 数字 —— 额度用满变红时总计不跟着红,它不是本会话的额度。"""
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    texts, _, icons = _quota_draw_calls(monkeypatch, {
        'shown': True, 'is_group': is_group, 'used': 50, 'limit': 50, 'remaining': 0,
        'near_limit': False, 'exhausted': True, 'no_permission': False, 'total': 4321})
    at = {t: (xy, fill) for xy, t, _font, fill in texts}
    assert at['50 / 50'][1] == stats_image._RED
    assert at[label][1] == stats_image._TEXT_MUTED
    assert at['4,321'][1] == stats_image._ACCENT
    # 标签前是缩小版纸飞机
    assert [(fg, size) for _x, _y, fg, size in icons] == [(stats_image._TEAL, 30)]
    assert at['50 / 50'][0][0] < icons[0][0] < at[label][0][0] < at['4,321'][0][0]


@pytest.mark.parametrize('is_group, scope, label', [(True, '本群', '今日群主动总计'), (False, '本私信', '今日私信主动总计')])
def test_unlimited_push_quota_splits_into_two_tiles(monkeypatch, is_group, scope, label):
    """★ 不限量没有进度条:拆成和上面指标卡一样的左右两张 —— 左本会话用量,右今日总计配整张纸飞机。"""
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    texts, _, icons = _quota_draw_calls(monkeypatch, {
        'shown': True, 'is_group': is_group, 'used': 312, 'limit': 0, 'remaining': 0,
        'near_limit': False, 'exhausted': False, 'no_permission': False, 'total': 4321})
    at = {t: (xy, getattr(font, 'size', None), fill) for xy, t, font, fill in texts}
    (lx, ly), lsize, _ = at['312']
    (rx, ry), rsize, rfill = at['4,321']
    assert lsize == rsize == 48 and ly == ry and rfill == stats_image._ACCENT
    # 两张的横向位置与上面 2×2 指标卡的左右两列对齐
    assert lx == at[f'{scope}今日主动消息'][0][0] == at['今日活跃玩家'][0][0]
    assert rx == at[label][0][0] == at['今日活跃群聊'][0][0]
    assert [(fg, size) for _x, _y, fg, size in icons] == [(stats_image._TEAL, 52)]


def test_quota_tip_leaves_a_gap_above_the_long_value_row(monkeypatch):
    """★ 用量 + 总计一长,数字就伸到右上角提示胶囊正下方 —— 胶囊底边要比数字墨迹顶端高出一截,不能贴上。"""
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')
    texts, rects, icons = _quota_draw_calls(monkeypatch, {
        'shown': True, 'is_group': True, 'used': 20000, 'limit': 20000, 'remaining': 0,
        'near_limit': False, 'exhausted': True, 'no_permission': False, 'total': 1234567})
    tip = [box for box, fill in rects
           if fill == stats_image._tint(stats_image._RED) and box[2] - box[0] > 200]
    assert len(tip) == 1, rects
    # 粗体描边 1px,墨迹顶端再往上算 1px;总计前的小图标同样不能顶到胶囊
    tops = [xy[1] + font.getbbox(t)[1] - 1 for xy, t, font, _fill in texts
            if t in ('20,000 / 20,000', '1,234,567')]
    assert len(tops) == 2 and len(icons) == 1
    assert tip[0][3] + 4 <= min(tops + [icons[0][1]]), (tip, tops, icons)


# ──────── 单游戏统计卡片(数据统计<游戏名>)────────────────────────────────

def _game_sample(**over) -> dict:
    today = datetime.now().date()
    gs = {
        'available': True, 'found': True, 'game_name': '天赋云巢', 'game_count': 45,
        'rank': 2, 'week_rank': 3, 'matches': 635, 'attendances': 2706, 'avg_players': 4.26,
        'min_players': 2, 'max_players': 8, 'groups': 16, 'group_matches': 120,
        'group_players': 23, 'last_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'week_matches': 110, 'prev_week_matches': 115,
        'players': 105, 'week_players': 52, 'prev_week_players': 54,
        'trend_weeks': [{'start': (today - timedelta(days=7 * i + 6)).strftime('%Y-%m-%d'),
                         'matches': 10 * i, 'players': 3 * i} for i in range(12)],
        'top_players': [{'display': f'玩家{i}', 'count': 100 - i, 'me': False} for i in range(10)],
        'top_power': [{'display': f'玩家{i}', 'rate': 80.5 - i, 'count': 20 + i, 'me': False}
                      for i in range(10)],
        'power_min': 10,
        'me': {'display': '我自己', 'matches': 6, 'rate': 66.7, 'rank': 37, 'power_rank': None},
    }
    gs.update(over)
    return gs


def _need_font():
    pytest.importorskip('PIL')
    if not stats_image._find_font():
        pytest.skip('无中文字体')


def test_render_game_card_png():
    _need_font()
    png = stats_image.render_game_stats_image(_game_sample(), '游戏统计 · 截至 12:34')
    assert png and png[:8] == b'\x89PNG\r\n\x1a\n'
    w, h = uploader.get_image_size(png)
    assert w == 1000 and h > 1500


def test_game_card_me_row_only_when_off_the_boards():
    """★ 查询者两张榜都在 TOP10 → 就地高亮,不补行;只在一张或都不在 → 两张榜底部都补一行(图一样高,不留空白)。"""
    _need_font()
    base = _game_sample()
    both = _game_sample(
        top_players=[dict(p, me=(i == 3)) for i, p in enumerate(base['top_players'])],
        top_power=[dict(p, me=(i == 3)) for i, p in enumerate(base['top_power'])])
    one = _game_sample(
        top_players=[dict(p, me=(i == 3)) for i, p in enumerate(base['top_players'])])

    def _h(gs):
        return uploader.get_image_size(stats_image.render_game_stats_image(gs))[1]

    assert _h(base) == _h(one) > _h(both) == _h(_game_sample(me=None))


def test_game_board_rows_carry_my_row():
    count, power = stats_image._board_rows(_game_sample())
    assert (count['title'], count['tag']) == ('局数排行', '')
    assert (power['title'], power['tag']) == ('实力排行', '≥10局')
    assert count['full'] is None and power['full'] == 100     # 实力条按 0–100% 画,局数条以榜首为满格
    assert count['rows'][0] == (1, '玩家0', 100, '100局', False)
    assert power['rows'][0] == (1, '玩家0', 80.5, '80.50% · 20局', False)
    assert count['me_row'] == (37, '我自己', 6, '6局', True)
    assert power['me_row'] == (None, '我自己', 66.7, '66.70% · 6局', True)   # 没满门槛:名次画「—」


def test_game_board_row_uses_the_tight_insets():
    """★ 游戏卡双榜:名次章贴左内边距、计数右对齐到右内边距;比统计卡的榜两侧都收窄,同样的计数文案下名字放得下更多字。"""
    _need_font()
    from PIL import Image, ImageDraw
    rec = _DrawRecorder(ImageDraw.Draw(Image.new('RGB', (600, 200))))
    long_name, cnt = '蕾米莉亚斯卡雷特大小姐的女仆长', '80.00% · 179局'
    stats_image._board_row(rec, 0, 10, 453, (4, long_name, 60, cnt, False), 100, stats_image._ORANGE)
    texts = {t: xy for xy, t, _f, _fill in rec.texts}
    shown = next(t for t in texts if t.startswith('蕾米'))
    f22, f24 = stats_image._font(22), stats_image._font(24)
    assert texts['179局'][0] + stats_image._text_w(rec, '179局', f22) == 453 - stats_image._BOARD_INSET
    assert texts[shown][0] == stats_image._BOARD_INSET + 38 + 12
    old_name_x, old_w, old_cnt_x = stats_image._rank_row_layout(rec, 0, 453, cnt, f22, False)
    assert texts[shown][0] < old_name_x and texts['80.00%'][0] > old_cnt_x
    assert len(shown.rstrip('…')) > len(stats_image._fit_name(rec, long_name, old_w, f24).rstrip('…'))


def test_power_count_dot_gets_a_narrow_gap():
    """★ 实力排行的「比例 · 局数」:图里中点两侧各留 _DOT_GAP,比整个空格窄、又不贴着字;量宽与画法一致。"""
    _need_font()
    from PIL import Image, ImageDraw
    rec = _DrawRecorder(ImageDraw.Draw(Image.new('RGB', (600, 100))))
    f = stats_image._font(22)
    txt = stats_image.fmt_power(80, 179)
    assert txt == '80.00% · 179局'
    stats_image._dot_text(rec, (10, 10), txt, f, stats_image._ORANGE)
    assert [t for _xy, t, _f, _fill in rec.texts] == ['80.00%', '·', '179局']
    (x0, _y0), (xd, _yd), (x1, _y1) = [xy for xy, _t, _f, _fill in rec.texts]
    gap = stats_image._DOT_GAP
    w = lambda s: stats_image._text_w(rec, s, f)        # noqa: E731
    assert xd - (x0 + w('80.00%')) == gap == x1 - (xd + w('·'))
    assert 0 < gap < w(' ')
    assert x1 + w('179局') - 10 == stats_image._dot_text_w(rec, txt, f)
    assert stats_image._dot_text_w(rec, '179局', f) == w('179局')       # 没有中点的计数照常量宽


def test_zero_weeks_are_a_thin_line_and_low_weeks_stay_visible():
    """★ 0 只画一条细线;非零最矮也有 6px,两者一眼分得开(游戏卡双柱)。"""
    _need_font()
    trend = [{'start': '2026-07-01', 'matches': m, 'players': p}
             for m, p in zip([0, 1, 1000] + [0] * 9, [0, 1, 900] + [0] * 9)]
    bars, _nums = _draw_trend(trend)
    heights = [box[3] - box[1] for box, _f in bars]
    assert heights.count(stats_image._ZERO_BAR_H) == 20                     # 只有 0 那 10 周
    assert min(h for h in heights if h != stats_image._ZERO_BAR_H) == 6     # 1 对 1000 也有 6px


def test_old_trend_zero_days_are_a_thin_line(monkeypatch):
    """统计卡的近 10 日趋势同样:0 是细线,1 局对 500 局也有 6px。"""
    _need_font()
    from collections import Counter
    from PIL import ImageDraw
    recs = []
    real = ImageDraw.Draw
    monkeypatch.setattr(ImageDraw, 'Draw', lambda im: recs.append(_DrawRecorder(real(im))) or recs[-1])
    g = _sample_stats()
    g['trend_10d'] = [dict(t, count=c) for t, c in zip(g['trend_10d'], [0, 1] + [500] * 8)]
    assert stats_image.render_stats_image(g, 'x')
    dim = stats_image._tint(stats_image._ACCENT, base=stats_image._PANEL, alpha=0.35)
    cand = [box for box, fill in recs[0].rects if fill in (stats_image._ACCENT, dim)]
    base_y = Counter(b[3] for b in cand).most_common(1)[0][0]
    hs = [b[3] - b[1] for b in cand if b[3] == base_y]
    assert len(hs) == 10
    assert hs[-1] == stats_image._ZERO_BAR_H and hs[-2] == 6        # 今天 0 局在最右,昨天 1 局


def test_my_row_on_both_boards_when_only_one_lists_me():
    """★ 只在一张榜上时两张榜底部都补行;在榜的那张写榜上的名次(前三照样画奖牌色)。"""
    base = _game_sample()
    gs = _game_sample(top_players=[dict(p, me=(i == 1)) for i, p in enumerate(base['top_players'])])
    count, power = stats_image._board_rows(gs)
    assert count['me_row'][0] == 2                           # 榜上第 2,不是 me['rank'] 的 37
    assert power['me_row'][0] is None
    gs = _game_sample(top_power=[dict(p, me=(i == 0)) for i, p in enumerate(base['top_power'])])
    count, power = stats_image._board_rows(gs)
    assert (count['me_row'][0], power['me_row'][0]) == (37, 1)


def test_game_overview_tiles(monkeypatch):
    """总览 4×2 的标签顺序与角标:人次挂在累计对局上,累计玩家与游戏群聊挂本群人数 / 局数,私信里这两个不挂。
    角标里的数字 / 名次单独成段加粗;平均人数与最后一局的角标整段不加粗。"""
    _need_font()
    seen = []
    real = stats_image._game_tile

    def spy(d, box, label, value, icon, fg, **kw):
        seen.append((label, kw.get('pill')))
        return real(d, box, label, value, icon, fg, **kw)

    monkeypatch.setattr(stats_image, '_game_tile', spy)
    stats_image.render_game_stats_image(_game_sample())
    assert [label for label, _p in seen] == ['累计对局', '累计玩家', '近7日对局', '近7日玩家',
                                            '平均人数', '游戏群聊', '热度排名', '最后一局']
    pills = dict(seen)
    assert pills['累计对局'] == ('info', ('', '2,706', ' 人次'))     # 奇数位的段加粗
    assert pills['累计玩家'] == ('info', ('本群 ', '23', ' 人'))
    assert pills['游戏群聊'] == ('info', ('本群 ', '120', ' 局'))
    assert pills['热度排名'] == ('info', ('近7日 ', '#3'))
    assert pills['平均人数'] == ('info', '2–8 人') and isinstance(pills['最后一局'][1], str)

    seen.clear()
    stats_image.render_game_stats_image(_game_sample(group_matches=None, group_players=None,
                                                     week_rank=None))
    pills = dict(seen)
    assert pills['累计玩家'] == ('info', '') and pills['游戏群聊'] == ('info', '')
    assert pills['热度排名'] == ('info', ('近7日 ', '无对局'))   # 近 7 日没有对局也挂胶囊


@pytest.mark.parametrize('parts', [('近7日 ', '无对局'), ('本群 ', '23', ' 人'), ('', '2,706', ' 人次')])
def test_info_pill_bolds_odd_segments(monkeypatch, parts):
    """胶囊分段画,奇数位的段加粗:粗体描边多出的那点算进占宽,段与段首尾相接不重叠,两端仍各留 13px。"""
    _need_font()
    from PIL import Image, ImageDraw
    rec = _DrawRecorder(ImageDraw.Draw(Image.new('RGB', (400, 100))))
    bold = []
    real = stats_image._bold_text
    monkeypatch.setattr(stats_image, '_bold_text',
                        lambda d_, xy, t, f, fill: bold.append(t) or real(d_, xy, t, f, fill))
    stats_image._info_pill(rec, 10, 10, parts)
    assert bold == list(parts[1::2])
    f = stats_image._font(22)
    want, x = [], 10 + 13
    for i, p in enumerate(parts):
        if p:
            want.append((x + 1 if i % 2 else x, p))
        x += stats_image._text_w(rec, p, f) + (2 if i % 2 else 0)
    assert [(xy[0], t) for xy, t, _f, _fill in rec.texts] == want
    pill_w = stats_image._pill_w(rec, parts)
    assert x + 13 == 10 + pill_w and rec.rects[0][0][2] == 10 + pill_w


def test_power_board_shows_its_threshold_tag(monkeypatch):
    """实力排行标题后要画出这次的上榜门槛,局数排行不画。"""
    _need_font()
    seen = []
    real = stats_image._tag
    monkeypatch.setattr(stats_image, '_tag',
                        lambda d, x, y, text, *a, **kw: seen.append(text) or real(d, x, y, text, *a, **kw))
    stats_image.render_game_stats_image(_game_sample(power_min=5))
    assert seen.count('≥5局') == 1


def test_game_card_without_trend_is_shorter():
    _need_font()
    full = stats_image.render_game_stats_image(_game_sample())
    bare = stats_image.render_game_stats_image(_game_sample(trend_weeks=[]))
    assert uploader.get_image_size(bare)[1] < uploader.get_image_size(full)[1]


def test_game_tile_value_fits_and_clears_the_pill(monkeypatch):
    """★ 「最后一局」的日期时间最长,会伸到右上角胶囊正下方:不能出卡,胶囊底边也不能贴着数字墨迹顶端。"""
    _need_font()
    from PIL import Image, ImageDraw
    d = ImageDraw.Draw(Image.new('RGB', (500, 200)))
    seen = []
    real = stats_image._bold_text

    def spy(d_, xy, text, font, fill):
        seen.append((xy, text, font))
        return real(d_, xy, text, font, fill)

    monkeypatch.setattr(stats_image, '_bold_text', spy)
    # 427 是总览卡的实际宽度;380 窄到 48 号字放不下,验证逐级缩字号
    for tile_w in (427, 380):
        for value in ('12-31 23:59', '2025-12-31', '昨天 23:59'):
            seen.clear()
            stats_image._game_tile(d, (0, 0, tile_w, 138), '最后一局', value, 'clock',
                                   stats_image._PINK, pill=('info', '11 个月前'))
            (x, y), _t, f = seen[0]
            assert x + stats_image._text_w(d, value, f) <= tile_w - 24, (tile_w, value)
            # 胶囊占 24..64;粗体描边 1px,墨迹顶端再往上算 1px
            assert 24 + 40 + 4 <= y + f.getbbox(value)[1] - 1, (tile_w, value)


class _DrawRecorder:
    """包一层 ImageDraw,记下画过的文字 / 圆角矩形,其余调用原样转发。"""

    def __init__(self, d):
        self._d, self.texts, self.rects = d, [], []

    def __getattr__(self, name):
        return getattr(self._d, name)

    def text(self, xy, text, font=None, fill=None, **kw):
        self.texts.append((xy, text, font, fill))
        return self._d.text(xy, text, font=font, fill=fill, **kw)

    def rounded_rectangle(self, box, *a, **kw):
        self.rects.append((box, kw.get('fill')))
        return self._d.rounded_rectangle(box, *a, **kw)


def _ink_box(xy, text, font):
    x0, top, x1, bottom = font.getbbox(text)
    return (xy[0] + x0, xy[1] + top, xy[0] + x1, xy[1] + bottom)


def _overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _draw_trend(trend):
    from PIL import Image, ImageDraw
    rec = _DrawRecorder(ImageDraw.Draw(Image.new('RGB', (1000, 400))))
    stats_image._game_trend(rec, (36, 0, 964, stats_image._GAME_TREND_H), trend)
    base_y = stats_image._BAR_BASE
    bars = [(box, fill) for box, fill in rec.rects if box[3] == base_y]
    # 标题行(含图例)在 y < 56,日期在柱底之下,都不算
    nums = [(_ink_box(xy, t, f), t) for xy, t, f, _fill in rec.texts if 56 < xy[1] < base_y]
    return bars, nums


def test_game_trend_pairs_share_one_axis_and_numbers_stay_clear():
    """★ 双柱:一桶一对,两组共用纵轴(最高的那根顶满);柱顶数字不压到任何柱子,也不互相叠
    —— 矮柱上的四位数最容易探到旁边那根高柱上(第 2 桶:1000 局 / 1500 人)。"""
    _need_font()
    matches = [5, 1000, 100, 7, 0, 64, 100, 12, 33, 80, 1, 58]      # 新→旧
    players = [9, 1500, 120, 3, 0, 60, 2, 12, 40, 7, 1, 999]
    trend = [{'start': f'2026-07-{i + 1:02d}', 'matches': m, 'players': p}
             for i, (m, p) in enumerate(zip(matches, players))]
    bars, nums = _draw_trend(trend)
    assert len(bars) == 24 and len(nums) == 24
    heights = [box[3] - box[1] for box, _f in bars]
    assert max(heights) == stats_image._BAR_H                         # 1500 那根顶满
    for box, t in nums:
        for bar, _f in bars:
            assert not _overlap(box, bar), (t, box, bar)
        for other, t2 in nums:
            assert other is box or not _overlap(box, other), (t, t2)


def test_game_trend_without_players_draws_only_match_bars():
    """玩家侧查询失败(players 为 None):只画对局柱,不出现绿色柱子和数字。"""
    _need_font()
    trend = [{'start': '2026-07-01', 'matches': 3, 'players': None} for _ in range(12)]
    bars, nums = _draw_trend(trend)
    assert len(bars) == 12 and len(nums) == 12
    green = (stats_image._GREEN, stats_image._tint(stats_image._GREEN, base=stats_image._PANEL, alpha=0.35))
    assert not [f for _b, f in bars if f in green]


def test_render_game_swallows_exceptions(monkeypatch):
    monkeypatch.setattr(stats_image, '_render_game',
                        lambda *a: (_ for _ in ()).throw(RuntimeError('boom')))
    assert stats_image.render_game_stats_image({}, '') is None


def test_footer_carries_query_time():
    assert stats_image._footer_text('X', None) == 'X'
    assert stats_image._footer_text('X', 1234) == 'X · 查询耗时 1,234ms'


def test_cards_write_the_query_time_into_the_footer(monkeypatch):
    """★ 两种卡片都把查询耗时写进底部署名行;游戏卡底部只剩署名和耗时。"""
    _need_font()
    seen = []
    real = stats_image._footer_text
    monkeypatch.setattr(stats_image, '_footer_text',
                        lambda base, ms: seen.append((base, ms)) or real(base, ms))
    stats_image.render_stats_image(_sample_stats(), '截至 12:34', 12)
    stats_image.render_game_stats_image(_game_sample(), '游戏统计', 345)
    assert seen == [('LGTBot × ElainaBot · 数据统计', 12), ('LGTBot × ElainaBot · 游戏统计', 345)]


async def test_stats_views_pass_the_query_time_to_the_renderer(monkeypatch, _stats_env):
    """今日视图、窗口视图、游戏统计都把查询耗时(整数毫秒)交给渲染。"""
    import re as _re
    monkeypatch.setattr(uploader, 'SELECTED_BACKEND', 'cos')
    got = []
    monkeypatch.setattr(dispatcher.stats_image, 'render_stats_image',
                        lambda g, sub, elapsed_ms=None: got.append(elapsed_ms))
    monkeypatch.setattr(dispatcher.stats_image, 'render_game_stats_image',
                        lambda gs, sub, elapsed_ms=None: got.append(elapsed_ms))
    monkeypatch.setattr(dispatcher.metrics, 'query_game_stats_total',
                        lambda: {'available': True, 'total_matches': 5, 'total_players': 4,
                                 'total_groups': 3, 'total_attendances': 9,
                                 'top_games_total': [], 'top_players_total': []})
    monkeypatch.setattr(dispatcher.metrics, 'query_game_detail',
                        lambda q, uid, gid: _game_sample())
    for cmd in ('数据统计', '数据统计总', '数据统计 天赋云巢'):
        await dispatcher.lgtbot_data_stats(
            _fake_event(), _re.search(dispatcher._P_STATS, cmd, _re.DOTALL))
    assert len(got) == 3 and all(isinstance(ms, int) and ms >= 0 for ms in got)


_NOW = datetime(2026, 8, 8, 18, 0, 0)


@pytest.mark.parametrize('ts, want', [
    ('2026-08-08 15:52:10', '今天 15:52'),
    ('2026-08-07 23:59:00', '昨天 23:59'),
    ('2026-01-02 03:04:05', '01-02 03:04'),
    ('2025-12-31 23:00:00', '2025-12-31'),
    ('garbage', 'garbage'),
])
def test_fmt_when(ts, want):
    assert stats_image.fmt_when(ts, _NOW) == want


def test_last_match_from_a_past_year_shows_only_the_date(monkeypatch):
    """★ 跨年只给日期、不带时分(只有「昨天」这种相对说法带时间);数值和右上角胶囊都不出卡、不重叠。"""
    _need_font()
    now = datetime(2026, 1, 1, 10, 0, 0)
    assert stats_image.fmt_when('2025-12-31 23:00:00', now) == '昨天 23:00'
    assert stats_image.fmt_when('2025-12-30 21:14:00', now) == '2025-12-30'
    from PIL import Image, ImageDraw
    rec = _DrawRecorder(ImageDraw.Draw(Image.new('RGB', (500, 200))))
    labels = stats_image.game_labels({'last_time': '2024-03-02 21:14:00'}, datetime(2026, 10, 9, 22, 0, 0))
    assert (labels['last'], labels['ago']) == ('2024-03-02', '2 年前')
    stats_image._game_tile(rec, (0, 0, 427, 138), '最后一局', labels['last'], 'clock',
                           stats_image._PINK, pill=('info', labels['ago']))
    (vx, vy), _t, vf, _fill = next(t for t in rec.texts if t[1] == '2024-03-02')
    assert vx + stats_image._text_w(rec, '2024-03-02', vf) <= 427 - 24
    pill = next(box for box, fill in rec.rects if fill == stats_image._TAG_BG)
    assert pill[2] <= 427 - 24 and pill[3] + 4 <= vy + vf.getbbox('2024-03-02')[1] - 1


@pytest.mark.parametrize('ts, want', [
    ('2026-08-08 17:59:30', '刚刚'),
    ('2026-08-08 19:00:00', '刚刚'),          # 时钟回拨导致的未来时间不出负数
    ('2026-08-08 17:59:00', '1 分钟前'),
    ('2026-08-08 17:00:00', '1 小时前'),
    ('2026-08-07 18:00:00', '1 天前'),
    ('2026-07-01 18:00:00', '1 个月前'),
    ('2025-08-01 18:00:00', '1 年前'),
    ('', ''),
])
def test_fmt_ago(ts, want):
    assert stats_image.fmt_ago(ts, _NOW) == want


def test_game_labels_avg_and_range():
    two = stats_image.game_labels({'avg_players': 2.0, 'min_players': 2, 'max_players': 2})
    assert (two['avg'], two['range']) == ('2', '固定 2 人')          # 固定人数不带小数
    many = stats_image.game_labels({'avg_players': 4.26, 'min_players': 2, 'max_players': 8})
    assert (many['avg'], many['range']) == ('4.26', '2–8 人')
    assert stats_image.game_labels({}) == {'last': '—', 'last_full': '—',
                                           'ago': '', 'avg': '—', 'range': ''}


async def test_game_stats_command_replies_markdown_image(monkeypatch):
    """配了图床 + 渲染上传成功 → 与其他视图同一个 markdown 出口(@ 与图片之间换行)。"""
    import re as _re
    monkeypatch.setattr(dispatcher.helpers, 'is_foreign_event', lambda e: False)
    monkeypatch.setattr(uploader, 'SELECTED_BACKEND', 'cos')
    monkeypatch.setattr(dispatcher.metrics, 'query_game_detail',
                        lambda q, uid, gid: _game_sample())
    png = b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\x0DIHDR' + \
        (640).to_bytes(4, 'big') + (480).to_bytes(4, 'big')
    monkeypatch.setattr(dispatcher.stats_image, 'render_game_stats_image',
                        lambda gs, sub, elapsed_ms=None: png)
    seen = {}

    async def fake_upload(data, filename, user_id='', *, target_id='', target_is_uid=False):
        seen.update(filename=filename, target_id=target_id, target_is_uid=target_is_uid)
        return 'https://cdn.example/game.png'
    monkeypatch.setattr(dispatcher.uploader, 'upload_image', fake_upload)

    ev = _fake_event()
    await dispatcher.lgtbot_data_stats(
        ev, _re.search(dispatcher._P_STATS, '数据统计 天赋云巢', _re.DOTALL))
    assert ev.reply.await_args.args[0] == \
        '<@USER1>\n![数据统计 #640px #480px](https://cdn.example/game.png)'
    assert seen == {'filename': 'lgtbot_game_stats.png',
                    'target_id': 'GROUP1', 'target_is_uid': False}


async def test_game_stats_command_falls_back_to_text_when_render_fails(monkeypatch):
    import re as _re
    monkeypatch.setattr(dispatcher.helpers, 'is_foreign_event', lambda e: False)
    monkeypatch.setattr(uploader, 'SELECTED_BACKEND', 'cos')
    monkeypatch.setattr(dispatcher.metrics, 'query_game_detail',
                        lambda q, uid, gid: _game_sample())
    monkeypatch.setattr(dispatcher.stats_image, 'render_game_stats_image',
                        lambda gs, sub, elapsed_ms=None: None)
    ev = _fake_event()
    await dispatcher.lgtbot_data_stats(
        ev, _re.search(dispatcher._P_STATS, '数据统计天赋云巢', _re.DOTALL))
    txt = ev.reply.await_args.args[0]
    assert '《天赋云巢》游戏统计' in txt and '![' not in txt
