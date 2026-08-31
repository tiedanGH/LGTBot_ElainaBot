#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「数据统计」指令的图片渲染(PIL)。

样式对齐本插件 Web 面板的浅色主题(webui templates/main/main.css 的
light 变量:浅灰页底 + 白色细边框圆角卡 + #5b6ee8 强调色 + 左侧色条标题),
内容为 lgtbot 游戏数据:
  · 数据总览,自上而下**先累计总数、再今日数据**:
      1. 群组总数 / 好友总数(``g['bot_groups']`` / ``['bot_friends']``)——
         这一行不是今日数据,所以底色换成 accent 系浅蓝紫 ``_TOTAL_BG``、角标用**描边**
         胶囊,与下面的今日行一眼区分;角标是**今日净变化**本身,不是与昨日对比
      2. 今日活跃玩家 / 今日活跃群聊(实底胶囊,对比昨日同时段)
      3. 今日对局(同上)/ 近 10 日对局(对比上一个 10 日整期)
      4. 可选的主动消息额度通栏
  · 近 10 日对局趋势:通栏条形图(当日高亮在最右)
  · 今日游戏榜 / 玩家参与榜:TOP5,金银铜奖牌 + 比例条。榜单条目带 ``unranked``
    时(当天的对局全是不计分的)在名称后跟一个灰色「不计分」胶囊;既有计分又有不计分的游戏只汇总,不打标

窗口模式(``g['date_mode']`` 真):无涨跌胶囊、无主动消息行、无趋势图;第 4 卡换成「对局人次」
(``g['attendances']``,user_with_match 计次不去重,票根图标);双榜 TOP10(``g['rank_limit']``);卡片文案去「今日」。
在此之上叠一个子模式开关决定期间词(见 ``_period_word``,与 dispatcher._SPAN_VIEWS 同措辞):

    (无)                 当日  「数据统计MMDD」
    ``month_mode``       当月  「数据统计MM」
    ``year_mode``        当年  「数据统计YYYY」
    ``total_mode``       累计  「数据统计总」(前两卡也改「累计玩家 / 累计群聊」)

第一行的群组 / 好友总数只在调用方注入 ``bot_groups`` / ``bot_friends`` 时出现 ——
即今日视图(带今日净变化角标)与累计总计视图(**无角标**,``*_delta`` 留空)。

PIL 未安装或找不到中文字体时返回 None,调用方(dispatcher 数据统计指令)回退纯文本输出 —— 渲染是增强,不是依赖。
"""

from __future__ import annotations

import os
from io import BytesIO

from core.base.logger import get_logger, PLUGIN

log = get_logger(PLUGIN, 'LGTBot')

# ──────── 配色(取自面板 main.css 浅色主题变量)────────────────────────────
_BG = (246, 247, 251)           # --bg      #f6f7fb
_PANEL = (255, 255, 255)        # --panel   #ffffff
_PANEL2 = (250, 251, 255)       # --panel-2 #fafbff
_BORDER = (230, 232, 240)       # --border  #e6e8f0
_BORDER2 = (217, 220, 232)      # --border-2 #d9dce8
_TEXT = (31, 36, 51)            # --text    #1f2433
_TEXT_MUTED = (107, 114, 128)   # --text-muted #6b7280
_TEXT_FAINT = (154, 161, 173)   # --text-faint #9aa1ad
_ACCENT = (91, 110, 232)        # --accent  #5b6ee8
_ORANGE = (224, 134, 0)         # --img     #e08600
_GREEN = (22, 163, 74)          # 涨(对局变多是积极信号)
_RED = (220, 53, 69)            # 跌 / 面板 crash 红 #dc3545
_WARN = (230, 162, 60)          # 警告黄(同面板计划重启按钮高亮 #e6a23c)
_TAG_BG = (238, 240, 247)       # --tag-bg  #eef0f7
# 累计总数卡的底色 / 边框 —— accent(#5b6ee8)按 15% / 30% 兑白得到的浅蓝紫。
_TOTAL_BG = (230, 233, 251)
_TOTAL_BORDER = (205, 211, 248)
_RANK_COLORS = ((255, 172, 20), (160, 174, 192), (219, 154, 108))  # 金银铜

# 排行榜条数上限(文本回退仍为 3 条控制消息长度)
RANK_LIMIT = 5


def _tint(fg, base=_PANEL2, alpha=0.18):
    """模拟面板 color-mix(in srgb, fg 18%, transparent) 的徽章底色。"""
    return tuple(int(base[i] + (fg[i] - base[i]) * alpha) for i in range(3))


# ──────── 中文字体探测(一次缓存)─────────────────────────────────────────
_FONT_PATHS = [
    '/usr/share/fonts/truetype/msyh.ttc',
    'C:/Windows/Fonts/msyh.ttc',
    'C:/Windows/Fonts/msyh.ttf',
    'C:/Windows/Fonts/simhei.ttf',
    '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
    '/usr/share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc',
    '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf',
    '/System/Library/Fonts/PingFang.ttc',
]
_CJK_NAME_HINTS = (
    'cjk', 'wqy', 'msyh', 'yahei', 'simhei', 'simsun', 'pingfang',
    'sourcehan', 'source-han', 'notosanssc', 'notoserifsc', 'sarasa',
    'harmonyos', 'alibaba', 'fallback', 'uming', 'ukai', 'zenhei',
)

_font_file: str | None = None
_font_cache: dict = {}


def _find_font() -> str:
    """定位一个中文字体;找不到返回 ''(调用方应回退文本)。"""
    global _font_file
    if _font_file is not None:
        return _font_file
    found = next((p for p in _FONT_PATHS if os.path.isfile(p)), '')
    if not found:
        for base in ('/usr/share/fonts', '/usr/local/share/fonts',
                     os.path.expanduser('~/.fonts'),
                     os.path.expanduser('~/.local/share/fonts')):
            if found or not os.path.isdir(base):
                continue
            for root, _dirs, files in os.walk(base):
                for name in sorted(files):
                    low = name.lower()
                    if low.endswith(('.ttf', '.ttc', '.otf')) and \
                            any(h in low for h in _CJK_NAME_HINTS):
                        found = os.path.join(root, name)
                        break
                if found:
                    break
    _font_file = found
    return found


def _font(size: int):
    from PIL import ImageFont
    f = _font_cache.get(size)
    if f is None:
        f = ImageFont.truetype(_font_file, size)
        _font_cache[size] = f
    return f


def _fmt(n) -> str:
    if n is None:
        return '—'
    try:
        return f'{int(n):,}'
    except (TypeError, ValueError):
        return str(n)


def _text_w(d, text, font) -> int:
    box = d.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _bold_text(d, xy, text, font, fill):
    """粗体:stroke_width=1 模拟(CJK 常无独立 bold 文件)。"""
    d.text(xy, text, font=font, fill=fill, stroke_width=1, stroke_fill=fill)


def _section(d, box, radius=12, top_accent=False):
    """面板式区块卡:panel 底 + 1px border(深色主题下阴影不可见,不画投影)。"""
    d.rounded_rectangle(box, radius=radius, fill=_PANEL, outline=_BORDER, width=1)
    if top_accent:
        x0, y0, x1, _ = box
        d.rounded_rectangle((x0, y0, x1, y0 + 4), radius=2, fill=_ACCENT)


def _tile(d, box, radius=8, fill=_PANEL2, outline=_BORDER):
    """区块内的小卡(默认 panel-2 底,同面板 .metrics-status-card)。

    ``fill`` / ``outline`` 用于区分数据口径:今日类指标用默认的近白底,累计总数类
    用 ``_TOTAL_BG`` + ``_TOTAL_BORDER``(浅蓝紫),一眼能看出那一行不是"今天"的数字。
    """
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=1)


def _sec_title(d, x, y, text):
    """左侧 accent 色条 + 标题(同面板 dash-section-title 的视觉记号)。"""
    d.rounded_rectangle((x, y + 2, x + 8, y + 32), radius=4, fill=_ACCENT)
    _bold_text(d, (x + 22, y), text, _font(28), _TEXT)


def _delta_pill(d, x, y, diff, h=40, *, outline=False, bg=None) -> int:
    """涨跌胶囊,返回宽度。配色照搬主框架 dau 卡片:**涨红跌绿**、平灰。

    两种形态,区分两类不同口径的角标:
      · 实底(默认)—— 今日指标 vs「昨日同时段 / 上个区间」
      · 描边(``outline=True``)—— 累计总数的**今日净增减**,只描边不填色;
        ``bg`` 传所在卡片的底色,让胶囊内部与卡面齐平(看着是空心的)
    """
    if diff is None:
        return 0
    if diff > 0:
        txt, fg = f'↑ {_fmt(diff)}', _RED
    elif diff < 0:
        txt, fg = f'↓ {_fmt(abs(diff))}', _GREEN
    else:
        txt, fg = '· 0', _TEXT_MUTED
    f = _font(22)
    w = _text_w(d, txt, f) + 26
    box = (x, y, x + w, y + h)
    if outline:
        d.rounded_rectangle(box, radius=h // 2, fill=(bg or _PANEL2), outline=fg, width=2)
    else:
        d.rounded_rectangle(box, radius=h // 2, fill=_tint(fg))
    d.text((x + 13, y + (h - 22) // 2 - 4), txt, font=f, fill=fg)
    return w


_UNRANKED_TAG = '不计分'


def _unranked_tag_w(d) -> int:
    """「不计分」胶囊本身的宽度(不含左右间距)。"""
    return _text_w(d, _UNRANKED_TAG, _font(18)) + 20


def _unranked_tag(d, x, y) -> None:
    """游戏名后的灰色「不计分」胶囊 —— 该游戏当天的对局全是不计分的。"""
    f = _font(18)
    w = _text_w(d, _UNRANKED_TAG, f) + 20
    d.rounded_rectangle((x, y, x + w, y + 26), radius=13, fill=_TAG_BG)
    d.text((x + 10, y + 1), _UNRANKED_TAG, font=f, fill=_TEXT_MUTED)


def _fit_name(d, full: str, max_w: int, font) -> str:
    """把榜单名字截到放得下,超出补省略号。"""
    name = full
    while name and _text_w(d, name + '…', font) > max_w:
        name = name[:-1]
    return name + ('…' if name != full else '')


def _rank_row_layout(d, cx: int, half_w: int, cnt_txt: str, cnt_font, tagged: bool):
    """一行榜单的横向布局 → ``(名字起点, 名字可用宽, 计数起点)``。"""
    name_x = cx + 84
    cnt_x = cx + half_w - 28 - _text_w(d, cnt_txt, cnt_font)
    right = cnt_x - 12 - ((_unranked_tag_w(d) + 10) if tagged else 0)
    return name_x, max(0, right - name_x), cnt_x


def _icon(d, kind: str, ix: int, iy: int, fg) -> None:
    """52×52 圆角图标块(fg 实色图形 + 18% 透明底,同面板徽章配色法)。"""
    d.rounded_rectangle((ix, iy, ix + 52, iy + 52), radius=14, fill=_tint(fg))
    if kind == 'mail':      # 信封(主动消息额度)
        d.rounded_rectangle((ix + 11, iy + 16, ix + 41, iy + 38), radius=4, fill=fg)
        d.line([(ix + 11, iy + 17), (ix + 26, iy + 29), (ix + 41, iy + 17)],
               fill=_tint(fg), width=3)
        return
    if kind == 'warn':      # 感叹号三角(无全量权限警告)
        d.polygon([(ix + 26, iy + 10), (ix + 44, iy + 42), (ix + 8, iy + 42)], fill=fg)
        d.rounded_rectangle((ix + 24, iy + 20, ix + 28, iy + 33), radius=2,
                            fill=_tint(fg))
        d.ellipse((ix + 24, iy + 35, ix + 28, iy + 39), fill=_tint(fg))
        return
    if kind == 'ticket':    # 票根(对局人次 —— 参与计次,不去重)
        d.rounded_rectangle((ix + 9, iy + 15, ix + 43, iy + 37), radius=5, fill=fg)
        # 两侧半圆缺口 + 中缝虚线,画出票券撕口的记号
        d.ellipse((ix + 5, iy + 22, ix + 13, iy + 30), fill=_tint(fg))
        d.ellipse((ix + 39, iy + 22, ix + 47, iy + 30), fill=_tint(fg))
        for dy in (19, 25, 31):
            d.rectangle((ix + 30, iy + dy, ix + 32, iy + dy + 2), fill=_tint(fg))
        return
    if kind == 'die':
        d.rounded_rectangle((ix + 12, iy + 12, ix + 40, iy + 40), radius=7, fill=fg)
        for px, py in ((19, 19), (33, 33), (19, 33), (33, 19), (26, 26)):
            d.ellipse((ix + px - 3, iy + py - 3, ix + px + 3, iy + py + 3),
                      fill=_tint(fg))
    elif kind == 'person':
        d.ellipse((ix + 18, iy + 10, ix + 34, iy + 26), fill=fg)
        d.pieslice((ix + 10, iy + 27, ix + 42, iy + 55), 180, 360, fill=fg)
    elif kind == 'group':
        d.ellipse((ix + 11, iy + 13, ix + 23, iy + 25), fill=fg)
        d.pieslice((ix + 5, iy + 27, ix + 29, iy + 49), 180, 360, fill=fg)
        d.ellipse((ix + 29, iy + 13, ix + 41, iy + 25), fill=fg)
        d.pieslice((ix + 23, iy + 27, ix + 47, iy + 49), 180, 360, fill=fg)
    elif kind == 'friend':
        d.ellipse((ix + 13, iy + 13, ix + 27, iy + 27), fill=fg)
        d.pieslice((ix + 6, iy + 28, ix + 34, iy + 54), 180, 360, fill=fg)
        d.ellipse((ix + 31, iy + 12, ix + 39, iy + 20), fill=fg)
        d.ellipse((ix + 37, iy + 12, ix + 45, iy + 20), fill=fg)
        d.polygon([(ix + 31, iy + 17), (ix + 45, iy + 17), (ix + 38, iy + 28)], fill=fg)
    else:  # calendar
        d.rounded_rectangle((ix + 11, iy + 14, ix + 41, iy + 42), radius=5, fill=fg)
        d.rectangle((ix + 11, iy + 22, ix + 41, iy + 24), fill=_tint(fg))
        for tx in (19, 33):
            d.rounded_rectangle((ix + tx - 2, iy + 9, ix + tx + 2, iy + 18),
                                radius=2, fill=fg)


def render_stats_image(g: dict, sub_title: str = '') -> bytes | None:
    """把 ``metrics.query_game_stats()`` 的结果渲染成统计卡片 PNG。

    PIL 未安装 / 无中文字体 / 渲染异常 → 返回 None(调用方回退文本)。
    """
    try:
        return _render(g, sub_title)
    except ImportError:
        return None
    except Exception as e:
        log.warning(f'数据统计图片渲染失败,回退文本: {e}')
        return None


def _period_word(g: dict) -> str:
    """窗口视图的期间词 —— ``date_mode`` 下按子模式开关取(默认当日)。

    与 dispatcher._SPAN_VIEWS 的 period 同措辞:图片与文本保底两条出口的用词必须一致。
    """
    if g.get('total_mode'):
        return '累计'
    if g.get('year_mode'):
        return '当年'
    if g.get('month_mode'):
        return '当月'
    return '当日'


def _render(g: dict, sub_title: str) -> bytes | None:
    from PIL import Image, ImageDraw
    if not _find_font():
        return None

    width, pad, gap = 1000, 36, 22
    date_mode = bool(g.get('date_mode'))
    rank_limit = int(g.get('rank_limit') or RANK_LIMIT)
    trend = list(g.get('trend_10d') or [])                  # 新→旧
    top_games = (g.get('top_games_today') or [])[:rank_limit]
    top_players = (g.get('top_players_today') or [])[:rank_limit]
    list_rows = max(len(top_games), len(top_players), 1)

    # 主动消息额度(dispatcher 按当前会话目标注入;无则不画该行)
    pq = g.get('push_quota') or {}
    show_pq = bool(pq.get('shown'))
    # bot 规模行(群组总数 / 好友总数)—— 同样由 dispatcher 只在今日视图注入
    show_scale = g.get('bot_groups') is not None or g.get('bot_friends') is not None

    head_h = 118                                            # 顶栏
    tile_h, tile_gap = 138, 18                              # 指标小卡
    overview_h = (76 + tile_h * 2 + tile_gap + 2
                  + ((tile_h + tile_gap) if show_scale else 0)
                  + ((tile_h + tile_gap) if show_pq else 0))
    trend_h = 268 if trend else 0
    rank_h = 88 + list_rows * 74 + 14                       # 标题 + 行 ×N
    footer_h = 56
    height = pad + head_h + gap + overview_h + gap \
        + (trend_h + gap if trend else 0) + rank_h + footer_h

    img = Image.new('RGB', (width, height), _BG)
    d = ImageDraw.Draw(img)
    inner_w = width - pad * 2

    # ── 顶栏(accent 顶条 + accent 标题,同面板 topbar h1 用 accent 色)──
    y = pad
    _section(d, (pad, y, width - pad, y + head_h), top_accent=True)
    _bold_text(d, (pad + 28, y + 30), 'LGT-Bot 数据统计', _font(40), _ACCENT)
    tw = _text_w(d, 'LGT-Bot 数据统计', _font(40))
    if sub_title:
        stf = _font(22)
        sw = _text_w(d, sub_title, stf) + 28
        sx = pad + 28 + tw + 24
        sy = y + 42
        d.rounded_rectangle((sx, sy, sx + sw, sy + 36), radius=18, fill=_TAG_BG)
        d.text((sx + 14, sy + 4), sub_title, font=stf, fill=_TEXT_MUTED)
    wm_font = _font(20)
    wm = 'GAME DASHBOARD'
    d.text((width - pad - 28 - _text_w(d, wm, wm_font), y + 48), wm,
           font=wm_font, fill=_TEXT_FAINT)
    y += head_h + gap

    # ── 数据总览:2×2 指标小卡(值用 accent,同 .metrics-status-value)──
    _section(d, (pad, y, width - pad, y + overview_h))
    _sec_title(d, pad + 28, y + 24, '数据总览')
    if date_mode:
        # 历史日 / 月 / 年 / 累计总计:无涨跌;第 4 卡为该期**对局人次**(user_with_match 计次,不去重)。
        # 总计视图前两卡也改「累计」——全量口径下"活跃玩家"会被误读成"当前还在玩的人"。
        period = _period_word(g)
        who = '累计' if g.get('total_mode') else '活跃'
        cards = [
            (f'{who}玩家', g.get('today_players'), None, 'person', _GREEN),
            (f'{who}群聊', g.get('today_groups'), None, 'group', _ORANGE),
            (f'{period}对局', g.get('today_matches'), None, 'die', _ACCENT),
            (f'{period}对局人次', g.get('attendances'), None, 'ticket', (232, 121, 249)),
        ]
    else:
        # 涨跌胶囊:前三卡对比「昨日同时段」(窗口与今日等长,见 metrics._YDAY_*);
        # 近10日对局对比「上一个 10 日」整期(metrics.prev10_matches,不做时段对齐)
        total10 = sum(t['count'] for t in trend) if trend else None
        cards = [
            ('今日活跃玩家', g.get('today_players'),
             g.get('yesterday_players_same_span'), 'person', _GREEN),
            ('今日活跃群聊', g.get('today_groups'),
             g.get('yesterday_groups_same_span'), 'group', _ORANGE),
            ('今日对局', g.get('today_matches'),
             g.get('yesterday_matches_same_span'), 'die', _ACCENT),
            ('近10日对局', total10, g.get('prev10_matches'), 'calendar', (232, 121, 249)),
        ]
    tile_w = (inner_w - 28 * 2 - tile_gap) // 2
    ty0 = y + 68

    # ── bot 规模:群组总数 / 好友总数 —— 排在**第一行**(标题正下方)。
    # 这两个是累计总数、不是今天的数字,所以放最上面先给全局盘子,再往下看今日。
    # 数据来自框架绑定 bot 的 data.db(userinfo.count_groups / count_friends)。
    scale_rows = 0
    if show_scale:
        scale_rows = 1
        # 图标 / 配色与今日行错开:群组用 accent 蓝(区别于活跃群聊的橙)
        srow = [('群组总数', g.get('bot_groups'), g.get('bot_groups_delta'),
                 'group', _ACCENT),
                ('好友总数', g.get('bot_friends'), g.get('bot_friends_delta'),
                 'friend', (232, 121, 249))]
        cy = ty0
        for i, (label, val, delta, icon, fg) in enumerate(srow):
            cx = pad + 28 + i * (tile_w + tile_gap)
            _tile(d, (cx, cy, cx + tile_w, cy + tile_h),
                  fill=_TOTAL_BG, outline=_TOTAL_BORDER)
            _icon(d, icon, cx + 24, cy + (tile_h - 52) // 2, fg)
            d.text((cx + 96, cy + 24), label, font=_font(24), fill=_TEXT_MUTED)
            _bold_text(d, (cx + 96, cy + 60), _fmt(val), _font(48), _ACCENT)
            # 这里的 delta 已经是净变化本身(不是"昨日值"),0 也画出来表示今日无变化。
            # 用**描边**胶囊而非实底 —— 这一行的角标是「累计总数的今日净增减」,与"今日 vs 昨日同时段"不是一回事。
            if delta is not None:
                pw = _delta_pill(d, -1000, -1000, int(delta), outline=True)
                _delta_pill(d, cx + tile_w - 24 - pw, cy + 24, int(delta),
                            outline=True, bg=_TOTAL_BG)

    # ── 今日指标 2×2(值用 accent,同 .metrics-status-value)——排在总数行之下 ──
    for i, (label, val, y_val, icon, fg) in enumerate(cards):
        cx = pad + 28 + (i % 2) * (tile_w + tile_gap)
        cy = ty0 + (scale_rows + i // 2) * (tile_h + tile_gap)
        _tile(d, (cx, cy, cx + tile_w, cy + tile_h))
        _icon(d, icon, cx + 24, cy + (tile_h - 52) // 2, fg)
        d.text((cx + 96, cy + 24), label, font=_font(24), fill=_TEXT_MUTED)
        _bold_text(d, (cx + 96, cy + 60), _fmt(val), _font(48), _ACCENT)
        if y_val is not None and val is not None:
            diff = int(val) - int(y_val)
            pw = _delta_pill(d, -1000, -1000, diff)         # 预算宽度
            _delta_pill(d, cx + tile_w - 24 - pw, cy + 24, diff)

    # ── 主动消息额度(通栏一行:用量 / 上限 + 进度条;用满转红)──
    # 非全量群走**黄色警告**变体:该群没有主动推送权限,额度数字没有意义,
    # 直接给授权指引(与文本输出同一判定 dispatcher._push_quota_view)。
    if show_pq and pq.get('no_permission'):
        cx = pad + 28
        cy = ty0 + (2 + scale_rows) * (tile_h + tile_gap)
        full_w = inner_w - 28 * 2
        _tile(d, (cx, cy, cx + full_w, cy + tile_h))
        d.rounded_rectangle((cx, cy, cx + 5, cy + tile_h), radius=2, fill=_WARN)
        _icon(d, 'warn', cx + 24, cy + (tile_h - 52) // 2, _WARN)
        _bold_text(d, (cx + 96, cy + 30), '本群未开启全量消息权限', _font(28), _WARN)
        d.text((cx + 96, cy + 76), '无法推送主动消息 —— 请 @机器人 发送「全量申请」完成授权',
               font=_font(23), fill=_TEXT_MUTED)
    elif show_pq:
        cx = pad + 28
        cy = ty0 + (2 + scale_rows) * (tile_h + tile_gap)
        full_w = inner_w - 28 * 2
        _tile(d, (cx, cy, cx + full_w, cy + tile_h))
        limit = int(pq.get('limit') or 0)
        used = int(pq.get('used') or 0)
        exhausted = bool(pq.get('exhausted'))
        near = bool(pq.get('near_limit'))
        # 三态:正常 accent / 即将用尽(≥85%)警告黄 / 已用满红
        fg = _RED if exhausted else (_WARN if near else _ACCENT)
        if exhausted or near:
            d.rounded_rectangle((cx, cy, cx + 5, cy + tile_h), radius=2, fill=fg)
        _icon(d, 'warn' if near else 'mail', cx + 24, cy + (tile_h - 52) // 2, fg)
        scope = '本群' if pq.get('is_group') else '本私信'
        d.text((cx + 96, cy + 24), f'{scope}今日主动消息',
               font=_font(24), fill=_TEXT_MUTED)
        val_txt = f'{_fmt(used)} / {_fmt(limit)}' if limit else f'{_fmt(used)}'
        _bold_text(d, (cx + 96, cy + 54), val_txt, _font(36), fg)
        tip = ''
        if exhausted:
            tip = '已用满 · 改用刷新按钮，次日 0 点恢复'
        elif near:
            tip = f'即将用尽 · 剩余 {_fmt(pq.get("remaining") or 0)} 条'
        if tip:
            tf = _font(22)
            d.rounded_rectangle(
                (cx + full_w - 24 - _text_w(d, tip, tf) - 26, cy + 24,
                 cx + full_w - 24, cy + 62), radius=19, fill=_tint(fg))
            d.text((cx + full_w - 24 - _text_w(d, tip, tf) - 13, cy + 32),
                   tip, font=tf, fill=fg)
        if limit:
            # 进度条:用量占比(用满为满格红)。y 要与上方数值留出间距
            bar_x, bar_y = cx + 96, cy + tile_h - 26
            bar_w = full_w - 96 - 24
            d.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + 12),
                                radius=6, fill=_TAG_BG)
            ratio = min(1.0, used / limit) if limit else 0.0
            if ratio > 0:
                d.rounded_rectangle(
                    (bar_x, bar_y, bar_x + max(10, int(bar_w * ratio)), bar_y + 12),
                    radius=6, fill=fg)
    y += overview_h + gap

    # ── 近 10 日对局趋势(当日 accent 高亮在最右,历史柱 35% 淡色)──
    if trend:
        _section(d, (pad, y, width - pad, y + trend_h))
        _sec_title(d, pad + 28, y + 24, '近 10 日对局趋势')
        bars = list(reversed(trend))                        # 旧→新
        max_c = max((t['count'] for t in bars), default=0) or 1
        area_x, area_w = pad + 48, inner_w - 96
        base_y, area_h = y + trend_h - 62, 110
        slot = area_w // len(bars)
        bw = min(46, slot - 16)
        nf, df = _font(20), _font(18)
        dim = _tint(_ACCENT, base=_PANEL, alpha=0.35)       # 历史柱淡色
        d.line([(area_x - 8, base_y), (area_x + area_w + 8 - (area_w % slot), base_y)],
               fill=_BORDER, width=1)
        for j, t in enumerate(bars):
            bx = area_x + j * slot + (slot - bw) // 2
            bh = max(6, int(area_h * (t['count'] / max_c))) if t['count'] else 6
            color = _ACCENT if j == len(bars) - 1 else dim
            d.rounded_rectangle((bx, base_y - bh, bx + bw, base_y), radius=6,
                                fill=color)
            cnt = str(t['count'])
            d.text((bx + (bw - _text_w(d, cnt, nf)) // 2, base_y - bh - 28),
                   cnt, font=nf, fill=_TEXT_MUTED)
            day = (t.get('date') or '')[5:]                 # MM-DD
            d.text((bx + (bw - _text_w(d, day, df)) // 2, base_y + 10),
                   day, font=df, fill=_TEXT_FAINT)
        y += trend_h + gap

    # ── 排行榜:今日游戏榜(accent)/ 玩家参与榜(橙);历史日期为当日双榜 ──
    half_w = (inner_w - gap) // 2
    games_label = f'{_period_word(g)}游戏榜' if date_mode else '今日游戏榜'
    ranks = ((games_label, top_games, 'game_name', _ACCENT),
             ('玩家参与榜', top_players, 'display', _ORANGE))
    for i, (label, items, key, fg) in enumerate(ranks):
        cx = pad + i * (half_w + gap)
        _section(d, (cx, y, cx + half_w, y + rank_h))
        d.rounded_rectangle((cx + 28, y + 26, cx + 36, y + 56), radius=4, fill=fg)
        _bold_text(d, (cx + 50, y + 24), label, _font(28), _TEXT)
        if not items:
            d.text((cx + 28, y + 92), '暂无数据', font=_font(24), fill=_TEXT_FAINT)
            continue
        max_c = max(it.get('count', 0) or 1 for it in items)
        for j, it in enumerate(items):
            ry = y + 88 + j * 74
            cnt = it.get('count', 0)
            # 名次:前三金银铜奖牌,4/5 名 tag 底灰字
            if j < 3:
                d.ellipse((cx + 28, ry, cx + 66, ry + 38), fill=_RANK_COLORS[j])
                rf = _font(22)
                _bold_text(d, (cx + 28 + (38 - _text_w(d, str(j + 1), rf)) // 2,
                               ry + 3), str(j + 1), rf, (255, 255, 255))
            else:
                d.ellipse((cx + 28, ry, cx + 66, ry + 38), fill=_TAG_BG)
                rf = _font(22)
                d.text((cx + 28 + (38 - _text_w(d, str(j + 1), rf)) // 2, ry + 3),
                       str(j + 1), font=rf, fill=_TEXT_MUTED)
            tagged = bool(it.get('unranked'))
            nf, cf = _font(24), _font(22)
            cnt_txt = f'{_fmt(cnt)}局'
            name_x, name_w, cnt_x = _rank_row_layout(
                d, cx, half_w, cnt_txt, cf, tagged)
            shown = _fit_name(d, str(it.get(key, '') or ''), name_w, nf)
            d.text((name_x, ry), shown, font=nf, fill=_TEXT)
            if tagged:
                _unranked_tag(d, name_x + _text_w(d, shown, nf) + 10, ry + 2)
            _bold_text(d, (cnt_x, ry + 2), cnt_txt, cf, fg)
            bar_x, bar_y = cx + 84, ry + 40
            bar_w = half_w - 84 - 28
            d.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + 10), radius=5,
                                fill=_TAG_BG)
            fill_w = max(10, int(bar_w * (cnt / max_c)))
            d.rounded_rectangle((bar_x, bar_y, bar_x + fill_w, bar_y + 10), radius=5,
                                fill=fg)
    y += rank_h

    footer = 'LGTBot × ElainaBot · 数据统计'
    ff = _font(20)
    d.text(((width - _text_w(d, footer, ff)) // 2, y + 18), footer, font=ff,
           fill=_TEXT_FAINT)

    buf = BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()
