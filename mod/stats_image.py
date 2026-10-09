#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「数据统计」指令的图片渲染(PIL)。

样式对齐本插件 Web 面板的浅色主题(webui templates/main/main.css 的
light 变量:浅灰页底 + 白色细边框圆角卡 + #5b6ee8 强调色 + 左侧色条标题),
内容为 lgtbot 游戏数据:
  · 数据总览,自上而下**先累计总数、再今日数据**:
      1. 好友总数 / 群聊总数(``g['bot_friends']`` / ``['bot_groups']``)——
         不是今日数据,所以底色换成浅蓝紫 ``_TOTAL_BG``、角标用**描边**胶囊以示区分;
         角标是**今日净变化**本身,不是与昨日对比
      2. 今日活跃玩家 / 今日活跃群聊(实底胶囊,对比昨日同时段)
      3. 今日对局(同上)/ 近 10 日对局(对比上一个 10 日整期)
      4. 可选的主动消息额度行:有上限时一整条(用量 / 上限 + 进度条,今日总计跟在后面),
         不限量时拆成左右两张(本会话用量 / 今日全部群或全部私信的总计)
  · 近 10 日对局趋势:通栏条形图(当日高亮在最右)
  · 今日游戏榜 / 玩家参与榜:TOP5,金银铜奖牌 + 比例条。榜单条目带 ``unranked``
    时(当天的对局全是不计分的)在名称后跟一个灰色「不计分」胶囊

窗口模式(``g['date_mode']`` 真):无涨跌胶囊、无主动消息行、无趋势图;第 4 卡换成「对局人次」
(``g['attendances']``,user_with_match 计次不去重,票根图标);双榜 TOP10(``g['rank_limit']``);卡片文案去「今日」。
在此之上叠一个子模式开关决定期间词(见 ``_period_word``,与 dispatcher._SPAN_VIEWS 同措辞):

    (无)                 当日  「数据统计MMDD」
    ``month_mode``       当月  「数据统计MM」
    ``year_mode``        当年  「数据统计YYYY」
    ``total_mode``       累计  「数据统计总」(前两卡也改「累计玩家 / 累计群聊」)

第一行的好友 / 群聊总数只在调用方注入 ``bot_friends`` / ``bot_groups`` 时出现 ——
即今日视图(带今日净变化角标)与累计总计视图(**无角标**,``*_delta`` 留空)。

「数据统计<游戏名>」的单游戏卡片由 ``render_game_stats_image`` 渲染,版式沿用上面这套(见文件末尾一节)。

PIL 未安装或找不到中文字体时返回 None,调用方(dispatcher 数据统计指令)回退纯文本输出。
"""

from __future__ import annotations

import os
from datetime import datetime
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
_GREEN = (22, 163, 74)          # 跌
_RED = (220, 53, 69)            # 涨 / 面板 crash 红 #dc3545
_WARN = (230, 162, 60)          # 警告黄(同面板计划重启按钮高亮 #e6a23c)
_TEAL = (20, 184, 166)          # 今日主动消息总计的纸飞机图标
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


def _tag_w(d, text: str) -> int:
    """榜单名字后小胶囊本身的宽度(不含左右间距)。"""
    return _text_w(d, text, _font(18)) + 20


def _tag(d, x, y, text: str, fg=_TEXT_MUTED, bg=_TAG_BG) -> None:
    f = _font(18)
    d.rounded_rectangle((x, y, x + _tag_w(d, text), y + 26), radius=13, fill=bg)
    d.text((x + 10, y + 1), text, font=f, fill=fg)


def _unranked_tag_w(d) -> int:
    """「不计分」胶囊本身的宽度(不含左右间距)。"""
    return _tag_w(d, _UNRANKED_TAG)


def _unranked_tag(d, x, y) -> None:
    """游戏名后的灰色「不计分」胶囊 —— 该游戏当天的对局全是不计分的。"""
    _tag(d, x, y, _UNRANKED_TAG)


def _fit_name(d, full: str, max_w: int, font) -> str:
    """把榜单名字截到放得下,超出补省略号。"""
    name = full
    while name and _text_w(d, name + '…', font) > max_w:
        name = name[:-1]
    return name + ('…' if name != full else '')


def _rank_row_layout(d, cx: int, half_w: int, cnt_txt: str, cnt_font, tagged: bool,
                     tag_w: int | None = None):
    """一行榜单的横向布局 → ``(名字起点, 名字可用宽, 计数起点)``。

    ``tagged`` 时给名字后的胶囊留位,``tag_w`` 缺省按「不计分」胶囊算。
    """
    name_x = cx + 84
    cnt_x = cx + half_w - 28 - _text_w(d, cnt_txt, cnt_font)
    if tag_w is None:
        tag_w = _unranked_tag_w(d)
    right = cnt_x - 12 - ((tag_w + 10) if tagged else 0)
    return name_x, max(0, right - name_x), cnt_x


def _rank_badge(d, x: int, y: int, rank: int | None) -> None:
    """名次圆章:前三金银铜白字,其余 tag 底灰字;``rank`` 为 None 画「—」(查询者自己那行未上榜)。"""
    txt = '—' if rank is None else str(rank)
    size = 22 if len(txt) <= 2 else (18 if len(txt) == 3 else 15)
    rf, ty = _font(size), y + 3 + (22 - size) // 2
    tx = x + (38 - _text_w(d, txt, rf)) // 2
    if rank is not None and 1 <= rank <= 3:
        d.ellipse((x, y, x + 38, y + 38), fill=_RANK_COLORS[rank - 1])
        _bold_text(d, (tx, ty), txt, rf, (255, 255, 255))
    else:
        d.ellipse((x, y, x + 38, y + 38), fill=_TAG_BG)
        d.text((tx, ty), txt, font=rf, fill=_TEXT_MUTED)


def _rank_bar(d, x: int, y: int, w: int, ratio: float, fg) -> None:
    """榜单行下方的比例条(满长 = 榜首;再小也留 10px 起步,零值行也看得出在榜上)。"""
    d.rounded_rectangle((x, y, x + w, y + 10), radius=5, fill=_TAG_BG)
    d.rounded_rectangle((x, y, x + max(10, int(w * ratio)), y + 10), radius=5, fill=fg)


def _push_icon(d, ix: int, iy: int, fg, size: int = 52) -> None:
    """纸飞机(今日主动消息总计)。按 size 等比缩放:有额度的那一条里,总计前放 30px 的缩小版。"""
    k = size / 52
    p = lambda x, y: (ix + round(x * k), iy + round(y * k))
    d.rounded_rectangle((ix, iy, ix + size, iy + size), radius=round(14 * k), fill=_tint(fg))
    d.polygon([p(8, 26), p(44, 10), p(22, 31)], fill=fg)                          # 上翼
    # 下翼压暗一档:两片翼靠颜色分开,缩到 30px 也看得出折起的立体感
    d.polygon([p(44, 10), p(22, 31), p(31, 43)], fill=tuple(int(c * 0.75) for c in fg))


def _icon(d, kind: str, ix: int, iy: int, fg) -> None:
    """52×52 圆角图标块(fg 实色图形 + 18% 透明底,同面板徽章配色法)。"""
    if kind == 'push':
        _push_icon(d, ix, iy, fg)
        return
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
    if kind == 'trophy':    # 奖杯(热度排名):杯身 + 两侧把手 + 杯脚
        d.rectangle((ix + 16, iy + 11, ix + 36, iy + 19), fill=fg)
        d.pieslice((ix + 16, iy + 5, ix + 36, iy + 31), 0, 180, fill=fg)
        d.arc((ix + 9, iy + 13, ix + 21, iy + 25), 90, 270, fill=fg, width=3)
        d.arc((ix + 31, iy + 13, ix + 43, iy + 25), 270, 90, fill=fg, width=3)
        d.rectangle((ix + 24, iy + 30, ix + 28, iy + 36), fill=fg)
        d.rounded_rectangle((ix + 17, iy + 36, ix + 35, iy + 42), radius=2, fill=fg)
        return
    if kind == 'clock':     # 时钟(最后一局)
        d.ellipse((ix + 11, iy + 11, ix + 41, iy + 41), fill=fg)
        d.line([(ix + 26, iy + 26), (ix + 26, iy + 17)], fill=_tint(fg), width=3)
        d.line([(ix + 26, iy + 26), (ix + 33, iy + 26)], fill=_tint(fg), width=3)
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
    # bot 规模行(好友总数 / 群聊总数)—— 同样由 dispatcher 只在今日 / 累计视图注入
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

    # ── bot 规模:好友总数 / 群聊总数 —— 排在**第一行**:累计总数先给全局盘子,再往下看今日。
    # 数据来自框架绑定 bot 的 data.db(userinfo.count_friends / count_groups)。
    scale_rows = 0
    if show_scale:
        scale_rows = 1
        # 图标 / 配色与今日行错开:群聊用 accent 蓝
        srow = [('好友总数', g.get('bot_friends'), g.get('bot_friends_delta'),
                 'friend', (232, 121, 249)),
                ('群聊总数', g.get('bot_groups'), g.get('bot_groups_delta'),
                 'group', _ACCENT)]
        cy = ty0
        for i, (label, val, delta, icon, fg) in enumerate(srow):
            cx = pad + 28 + i * (tile_w + tile_gap)
            _tile(d, (cx, cy, cx + tile_w, cy + tile_h),
                  fill=_TOTAL_BG, outline=_TOTAL_BORDER)
            _icon(d, icon, cx + 24, cy + (tile_h - 52) // 2, fg)
            d.text((cx + 96, cy + 24), label, font=_font(24), fill=_TEXT_MUTED)
            _bold_text(d, (cx + 96, cy + 60), _fmt(val), _font(48), _ACCENT)
            # 这里的 delta 已经是净变化本身(不是"昨日值"),0 也画出来表示今日无变化。
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

    # ── 主动消息额度(有上限:通栏一行 用量 / 上限 + 进度条,用满转红;不限量:左右两张)──
    # 非全量群走**黄色警告**变体:没有主动推送权限,额度数字没有意义(与文本输出同一判定 dispatcher._push_quota_view)。
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
        limit = int(pq.get('limit') or 0)
        used = int(pq.get('used') or 0)
        total = pq.get('total')
        scope, kind = ('本群', '群') if pq.get('is_group') else ('本私信', '私信')
        if not limit:
            # 不限量没有进度条,拆成和上面指标卡一样的左右两张
            halves = [(f'{scope}今日主动消息', used, 'mail', _ACCENT)]
            if total is not None:
                halves.append((f'今日{kind}主动总计', total, 'push', _TEAL))
            for i, (label, val, icon, ic) in enumerate(halves):
                hx = cx + i * (tile_w + tile_gap)
                _tile(d, (hx, cy, hx + tile_w, cy + tile_h))
                _icon(d, icon, hx + 24, cy + (tile_h - 52) // 2, ic)
                d.text((hx + 96, cy + 24), label, font=_font(24), fill=_TEXT_MUTED)
                _bold_text(d, (hx + 96, cy + 60), _fmt(val), _font(48), _ACCENT)
        else:
            full_w = inner_w - 28 * 2
            _tile(d, (cx, cy, cx + full_w, cy + tile_h))
            exhausted = bool(pq.get('exhausted'))
            near = bool(pq.get('near_limit'))
            # 三态:正常 accent / 即将用尽(≥85%)警告黄 / 已用满红
            fg = _RED if exhausted else (_WARN if near else _ACCENT)
            if exhausted or near:
                d.rounded_rectangle((cx, cy, cx + 5, cy + tile_h), radius=2, fill=fg)
            _icon(d, 'warn' if near else 'mail', cx + 24, cy + (tile_h - 52) // 2, fg)
            d.text((cx + 96, cy + 24), f'{scope}今日主动消息',
                   font=_font(24), fill=_TEXT_MUTED)
            # 数字比指标卡小一号、上移,把卡片底部让给进度条
            val_txt = f'{_fmt(used)} / {_fmt(limit)}'
            vf, vy = _font(36), cy + 54
            _bold_text(d, (cx + 96, vy), val_txt, vf, fg)
            # 今日总计跟在后面,不随本会话的额度状态变色
            if total is not None:
                tx = cx + 96 + _text_w(d, val_txt, vf) + 36
                ink_top, ink_bottom = vf.getbbox(val_txt)[1::2]
                _push_icon(d, tx, vy + (ink_top + ink_bottom) // 2 - 15, _TEAL, size=30)
                tx += 30 + 10
                lf = _font(24)
                d.text((tx, vy + vf.getmetrics()[0] - lf.getmetrics()[0]), f'{kind}总计',
                       font=lf, fill=_TEXT_MUTED)
                _bold_text(d, (tx + _text_w(d, f'{kind}总计', lf) + 10, vy),
                           _fmt(total), vf, _ACCENT)
            tip = ''
            if exhausted:
                tip = '已用满 · 改用刷新按钮，次日 0 点恢复'
            elif near:
                tip = f'即将用尽 · 剩余 {_fmt(pq.get("remaining") or 0)} 条'
            if tip:
                # 胶囊压在 cy+20..56:数字大时下面那行「用量 + 总计」会伸到胶囊正下方,得留出空隙
                tf = _font(22)
                px0, py0, ph = cx + full_w - 24 - _text_w(d, tip, tf) - 26, cy + 20, 36
                d.rounded_rectangle((px0, py0, cx + full_w - 24, py0 + ph),
                                    radius=ph // 2, fill=_tint(fg))
                d.text((px0 + 13, py0 + (ph - 22) // 2 - 4), tip, font=tf, fill=fg)
            # 进度条:用量占比(用满为满格红)。y 要与上方数值留出间距
            bar_x, bar_y = cx + 96, cy + tile_h - 26
            bar_w = full_w - 96 - 24
            d.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + 12),
                                radius=6, fill=_TAG_BG)
            ratio = min(1.0, used / limit)
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
            _rank_badge(d, cx + 28, ry, j + 1)
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
            _rank_bar(d, cx + 84, ry + 40, half_w - 84 - 28, cnt / max_c, fg)
    y += rank_h

    footer = 'LGTBot × ElainaBot · 数据统计'
    ff = _font(20)
    d.text(((width - _text_w(d, footer, ff)) // 2, y + 18), footer, font=ff,
           fill=_TEXT_FAINT)

    buf = BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


# ──────── 单游戏统计卡片(「数据统计<游戏名>」)──────────────────────────────
# 数据来自 metrics.query_game_detail,版式沿用上面的统计卡片:顶栏 + 数据总览(2 列小卡)+ 趋势 + 双榜。
#   · 总览第一行是累计数(浅蓝紫底,同 bot 规模行);右上角的灰色胶囊只补充事实,涨跌只出现在近 7 日两卡
#   · 趋势每 7 天一组两根柱:对局与活跃玩家并排,共用一个纵轴,高度可以直接比
#   · 局数排行 / 实力排行(场均击败对手比例,标题后标出上榜门槛):查询者在 TOP10 里时那一行浅底高亮并跟「我」胶囊,
#     不在时钉在两张榜底部单独补一行

_PINK = (232, 121, 249)
_ME_TAG = '我'
_ME_BG = _tint(_ACCENT, base=_PANEL, alpha=0.08)
_ME_TAG_BG = _tint(_ACCENT, base=_PANEL, alpha=0.18)
_ME_ROW_H = 74 + 18             # 虚线分隔 + 一行


def _parse_ts(ts):
    try:
        return datetime.strptime(str(ts)[:19], '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


def fmt_when(ts: str, now: datetime | None = None) -> str:
    """库里的时间串 → 短写:今天 / 昨天 HH:MM,今年 MM-DD HH:MM,更早只给日期;解析不了原样返回。"""
    t = _parse_ts(ts)
    if t is None:
        return str(ts or '')
    now = now or datetime.now()
    days = (now.date() - t.date()).days
    if days == 0:
        return f'今天 {t:%H:%M}'
    if days == 1:
        return f'昨天 {t:%H:%M}'
    return f'{t:%m-%d %H:%M}' if t.year == now.year else f'{t:%Y-%m-%d}'


def fmt_ago(ts: str, now: datetime | None = None) -> str:
    """距今多久:刚刚 / N 分钟前 / N 小时前 / N 天前 / N 个月前 / N 年前;解析不了返回空串。"""
    t = _parse_ts(ts)
    if t is None:
        return ''
    s = int(((now or datetime.now()) - t).total_seconds())
    if s < 60:                  # 时钟回拨出现的未来时间也落在这里
        return '刚刚'
    if s < 3600:
        return f'{s // 60} 分钟前'
    if s < 86400:
        return f'{s // 3600} 小时前'
    days = s // 86400
    if days < 30:
        return f'{days} 天前'
    return f'{days // 30} 个月前' if days < 365 else f'{days // 365} 年前'


def game_labels(gs: dict, now: datetime | None = None) -> dict:
    """单游戏统计里要换算的几项展示文本 —— 图片与文本保底共用,两条出口措辞一致。"""
    last = str(gs.get('last_time') or '')
    lo, hi = gs.get('min_players'), gs.get('max_players')
    avg = gs.get('avg_players')
    fixed = lo is not None and lo == hi
    return {
        'last': fmt_when(last, now) if last else '—',
        'last_full': last[:16] or '—',
        'ago': fmt_ago(last, now) if last else '',
        # 每局人数固定时平均数就是那个整数,不带小数
        'avg': '—' if avg is None else (str(lo) if fixed else f'{float(avg):.2f}'),
        'range': '' if lo is None or hi is None else (f'固定 {lo} 人' if fixed else f'{lo}–{hi} 人'),
    }


def fmt_rate(rate) -> str:
    """击败比例保留 1 位小数(图片与文本保底共用)。"""
    return f'{float(rate):.1f}%'


def fmt_power(rate, n) -> str:
    """实力排行一行右侧的文案:比例 + 局数。"""
    return f'{fmt_rate(rate)} · {_fmt(n)}局'


def render_game_stats_image(gs: dict, sub_title: str = '') -> bytes | None:
    """把 ``metrics.query_game_detail()`` 的结果渲染成游戏统计卡片 PNG(失败语义同 render_stats_image)。"""
    try:
        return _render_game(gs, sub_title)
    except ImportError:
        return None
    except Exception as e:
        log.warning(f'游戏统计图片渲染失败,回退文本: {e}')
        return None


def _fit_font(d, text: str, max_w: int, sizes=(48, 42, 36, 30)):
    """放得下 ``text`` 的最大字号,都放不下就用最小的。"""
    for s in sizes:
        if _text_w(d, text, _font(s)) <= max_w:
            return _font(s)
    return _font(sizes[-1])


def _pill_w(d, text: str) -> int:
    return _text_w(d, text, _font(22)) + 26


def _info_pill(d, x, y, text: str, bg=_TAG_BG, h: int = 40) -> None:
    """说明胶囊:与涨跌胶囊同尺寸同位置,灰字,不带涨跌色。"""
    d.rounded_rectangle((x, y, x + _pill_w(d, text), y + h), radius=h // 2, fill=bg)
    d.text((x + 13, y + (h - 22) // 2 - 4), text, font=_font(22), fill=_TEXT_MUTED)


def _game_tile(d, box, label: str, value: str, icon: str, fg, *, suffix: str = '',
               pill=None, total: bool = False) -> None:
    """总览小卡:图标 + 标签 + 大号数值(可跟灰色后缀)+ 右上角胶囊。

    ``pill`` 是 ``('delta', 差值)`` 或 ``('info', 文案)``,值为 None / 空串时不画;``total`` 真时用累计数的浅蓝紫底。
    数值放不下时逐级缩小字号(「最后一局」的日期时间最长)。
    """
    x0, y0, x1, y1 = box
    fill, outline = (_TOTAL_BG, _TOTAL_BORDER) if total else (_PANEL2, _BORDER)
    _tile(d, box, fill=fill, outline=outline)
    _icon(d, icon, x0 + 24, y0 + (y1 - y0 - 52) // 2, fg)
    d.text((x0 + 96, y0 + 24), label, font=_font(24), fill=_TEXT_MUTED)
    sf = _font(26)
    suf_w = _text_w(d, suffix, sf) + 8 if suffix else 0
    vf = _fit_font(d, value, x1 - x0 - 96 - 24 - suf_w)
    vy = y0 + 60 + (48 - vf.size) // 2
    _bold_text(d, (x0 + 96, vy), value, vf, _ACCENT)
    if suffix:
        # 后缀与数值同一条基线
        d.text((x0 + 96 + _text_w(d, value, vf) + 8, vy + vf.getmetrics()[0] - sf.getmetrics()[0]),
               suffix, font=sf, fill=_TEXT_MUTED)
    kind, v = pill or ('', None)
    if kind == 'delta' and v is not None:
        pw = _delta_pill(d, -1000, -1000, v)            # 预算宽度
        _delta_pill(d, x1 - 24 - pw, y0 + 24, v)
    elif kind == 'info' and v:
        _info_pill(d, x1 - 24 - _pill_w(d, v), y0 + 24, v, bg=_PANEL if total else _TAG_BG)


def _game_header(d, box, name: str, sub_title: str) -> None:
    """顶栏:游戏名作大标题,子标题胶囊跟在后面;右侧水印放不下就省掉。"""
    x0, y0, x1, _y1 = box
    _section(d, box, top_accent=True)
    tf, stf, wf = _font(40), _font(22), _font(20)
    sw = (_text_w(d, sub_title, stf) + 28) if sub_title else 0
    shown = _fit_name(d, name, x1 - x0 - 56 - (sw + 24 if sw else 0), tf)
    _bold_text(d, (x0 + 28, y0 + 30), shown, tf, _ACCENT)
    px = x0 + 28 + _text_w(d, shown, tf) + 24
    if sw:
        d.rounded_rectangle((px, y0 + 42, px + sw, y0 + 78), radius=18, fill=_TAG_BG)
        d.text((px + 14, y0 + 46), sub_title, font=stf, fill=_TEXT_MUTED)
        px += sw
    wm = 'GAME STATS'
    wx = x1 - 28 - _text_w(d, wm, wf)
    if wx >= px + 24:
        d.text((wx, y0 + 48), wm, font=wf, fill=_TEXT_FAINT)


# 趋势卡:柱区最高 _BAR_H,柱顶数字占 _NUM_H;柱底往下是日期
_BAR_H, _NUM_H = 140, 22
_BAR_BASE = 76 + _NUM_H + _BAR_H + 20
_GAME_TREND_H = _BAR_BASE + 52
_PAIR_BW, _PAIR_GAP = 23, 4         # 一组两根柱的柱宽 / 柱间距


def _game_trend(d, box, trend: list) -> None:
    """近 N 周趋势(``trend`` 新→旧):每桶两根柱并排 —— 对局(accent)+ 活跃玩家(绿),共用一个纵轴。

    最新一桶实色在最右,往前的淡色;数字写在各自柱顶,同色。玩家侧查询失败(players 为 None)时只画对局柱。
    """
    x0, y0, x1, _y1 = box
    _section(d, box)
    _sec_title(d, x0 + 28, y0 + 24, f'近 {len(trend)} 周趋势')
    lf = _font(20)
    lx = x1 - 28
    for text, fg in (('活跃玩家', _GREEN), ('对局', _ACCENT)):     # 图例从右往左排
        lx -= _text_w(d, text, lf)
        d.text((lx, y0 + 30), text, font=lf, fill=_TEXT_MUTED)
        lx -= 22
        d.rounded_rectangle((lx, y0 + 35, lx + 14, y0 + 49), radius=3, fill=fg)
        lx -= 24
    bars = list(reversed(trend))                        # 旧→新
    series = [(_ACCENT, 'matches')]
    if all(t.get('players') is not None for t in bars):
        series.append((_GREEN, 'players'))
    top = max((int(t.get(k) or 0) for t in bars for _c, k in series), default=0) or 1
    area_x, area_w = x0 + 48, (x1 - x0) - 96
    base_y = y0 + _BAR_BASE
    slot = area_w // len(bars)
    df = _font(18)
    d.line([(area_x - 8, base_y), (area_x + slot * len(bars) + 8, base_y)], fill=_BORDER, width=1)
    for j, t in enumerate(bars):
        mid = area_x + j * slot + slot // 2
        latest = j == len(bars) - 1
        for k, (fg, key) in enumerate(series):
            v = int(t.get(key) or 0)
            if len(series) == 1:
                bx = mid - _PAIR_BW // 2
            else:
                bx = mid - _PAIR_GAP // 2 - _PAIR_BW if k == 0 else mid + _PAIR_GAP // 2
            bh = max(5, int(_BAR_H * v / top)) if v else 5
            d.rounded_rectangle((bx, base_y - bh, bx + _PAIR_BW, base_y), radius=5,
                                fill=fg if latest else _tint(fg, base=_PANEL, alpha=0.35))
            # 数字不能宽过「柱宽 + 柱间距」,否则会探到旁边那根柱子上方
            s = str(v)
            nf = _fit_font(d, s, _PAIR_BW + _PAIR_GAP, sizes=(16, 14, 12))
            d.text((bx + _PAIR_BW // 2 - _text_w(d, s, nf) // 2, base_y - bh - _NUM_H), s,
                   font=nf, fill=fg if v else _TEXT_FAINT)
        day = str(t.get('start') or '')[5:]             # MM-DD
        d.text((mid - _text_w(d, day, df) // 2, base_y + 10), day, font=df, fill=_TEXT_FAINT)


def _board_rows(gs: dict) -> list:
    """两张榜的绘制数据,每张 ``{title, fg, tag, full, rows, me_row}``。

    行是 ``(名次, 名字, 比例条取值, 右侧文案, 是不是查询者)``;``full`` 是比例条满格对应的值,None 表示以榜首为满格;
    ``tag`` 跟在标题后(实力排行的上榜门槛)。补行是查询者自己那行,只在他玩过这个游戏、又不在该榜 TOP10 里时才有,
    实力排行没满门槛时名次为 None。
    """
    me = gs.get('me')
    players = gs.get('top_players') or []
    power = gs.get('top_power') or []
    need = gs.get('power_min')
    return [
        {'title': '局数排行', 'fg': _ACCENT, 'tag': '', 'full': None,
         'rows': [(j, it.get('display', ''), it.get('count', 0), f'{_fmt(it.get("count", 0))}局',
                   bool(it.get('me'))) for j, it in enumerate(players, 1)],
         'me_row': ((me['rank'], me['display'], me['matches'], f'{_fmt(me["matches"])}局', True)
                    if me and not any(it.get('me') for it in players) else None)},
        {'title': '实力排行', 'fg': _ORANGE, 'tag': f'≥{need}局' if need else '', 'full': 100,
         'rows': [(j, it.get('display', ''), it.get('rate', 0),
                   fmt_power(it.get('rate', 0), it.get('count', 0)), bool(it.get('me')))
                  for j, it in enumerate(power, 1)],
         'me_row': ((me.get('power_rank'), me['display'], me['rate'],
                     fmt_power(me['rate'], me['matches']), True)
                    if me and not any(it.get('me') for it in power) else None)},
    ]


def _board_row(d, cx: int, ry: int, half_w: int, row: tuple, max_c: int, fg) -> None:
    rank, name, cnt, cnt_txt, is_me = row
    if is_me:
        d.rounded_rectangle((cx + 14, ry - 12, cx + half_w - 14, ry + 60), radius=10, fill=_ME_BG)
    _rank_badge(d, cx + 28, ry, rank)
    nf, cf = _font(24), _font(22)
    name_x, name_w, cnt_x = _rank_row_layout(d, cx, half_w, cnt_txt, cf, is_me,
                                             tag_w=_tag_w(d, _ME_TAG))
    shown = _fit_name(d, str(name or ''), name_w, nf)
    d.text((name_x, ry), shown, font=nf, fill=_TEXT)
    if is_me:
        _tag(d, name_x + _text_w(d, shown, nf) + 10, ry + 2, _ME_TAG, fg=_ACCENT, bg=_ME_TAG_BG)
    _bold_text(d, (cnt_x, ry + 2), cnt_txt, cf, fg)
    _rank_bar(d, cx + 84, ry + 40, half_w - 84 - 28, min(1.0, (cnt or 0) / max_c), fg)


def _game_boards(d, x0: int, y: int, inner_w: int, gap: int, h: int, boards: list, rows: int) -> None:
    half_w = (inner_w - gap) // 2
    for i, b in enumerate(boards):
        cx, fg, items = x0 + i * (half_w + gap), b['fg'], b['rows']
        _section(d, (cx, y, cx + half_w, y + h))
        d.rounded_rectangle((cx + 28, y + 26, cx + 36, y + 56), radius=4, fill=fg)
        tf = _font(28)
        _bold_text(d, (cx + 50, y + 24), b['title'], tf, _TEXT)
        if b['tag']:
            _tag(d, cx + 50 + _text_w(d, b['title'], tf) + 12, y + 30, b['tag'])
        if not items:
            d.text((cx + 28, y + 92), '暂无数据', font=_font(24), fill=_TEXT_FAINT)
        max_c = b['full'] or max([int(it[2] or 0) for it in items] + [1])
        for j, row in enumerate(items):
            _board_row(d, cx, y + 88 + j * 74, half_w, row, max_c, fg)
        if b['me_row']:
            # 补行钉在两张榜共同的底部(左右对齐),上方一条虚线与 TOP10 隔开
            sep_y = y + 88 + rows * 74 - 8
            for sx in range(cx + 28, cx + half_w - 28, 12):
                d.line([(sx, sep_y), (min(sx + 6, cx + half_w - 28), sep_y)], fill=_BORDER2, width=2)
            _board_row(d, cx, sep_y + 26, half_w, b['me_row'], max_c, fg)


def _render_game(gs: dict, sub_title: str) -> bytes | None:
    from PIL import Image, ImageDraw
    if not _find_font():
        return None

    width, pad, gap = 1000, 36, 22
    inner_w = width - pad * 2
    trend = list(gs.get('trend_weeks') or [])
    boards = _board_rows(gs)
    rows = max(max(len(b['rows']) for b in boards), 1)

    head_h, tile_h, tile_gap = 118, 138, 18
    overview_h = 76 + tile_h * 4 + tile_gap * 3 + 2
    trend_h = _GAME_TREND_H if trend else 0
    rank_h = 88 + rows * 74 + (_ME_ROW_H if any(b['me_row'] for b in boards) else 0) + 14
    footer_h = 56
    height = (pad + head_h + gap + overview_h + gap
              + (trend_h + gap if trend else 0) + rank_h + footer_h)

    img = Image.new('RGB', (width, height), _BG)
    d = ImageDraw.Draw(img)
    y = pad
    _game_header(d, (pad, y, width - pad, y + head_h), str(gs.get('game_name') or ''), sub_title)
    y += head_h + gap

    # ── 数据总览 4×2:累计 / 近 7 日 / 人数与群聊 / 热度与最后一局 ──
    _section(d, (pad, y, width - pad, y + overview_h))
    _sec_title(d, pad + 28, y + 24, '数据总览')
    lab = game_labels(gs)

    def _diff(cur, base):
        return None if cur is None or base is None else int(cur) - int(base)

    gm, rank, n_games, wr = (gs.get('group_matches'), gs.get('rank'),
                             gs.get('game_count'), gs.get('week_rank'))
    att, gp = gs.get('attendances'), gs.get('group_players')
    tiles = [
        ('累计对局', _fmt(gs.get('matches')), '', 'die', _ACCENT,
         ('info', f'{_fmt(att)} 人次' if att is not None else ''), True),
        ('累计玩家', _fmt(gs.get('players')), '', 'person', _GREEN,
         ('info', f'本群 {_fmt(gp)} 人' if gp is not None else ''), True),
        ('近7日对局', _fmt(gs.get('week_matches')), '', 'calendar', _PINK,
         ('delta', _diff(gs.get('week_matches'), gs.get('prev_week_matches'))), False),
        ('近7日玩家', _fmt(gs.get('week_players')), '', 'person', _TEAL,
         ('delta', _diff(gs.get('week_players'), gs.get('prev_week_players'))), False),
        ('平均人数', lab['avg'], '', 'ticket', _ACCENT, ('info', lab['range']), False),
        ('游戏群聊', _fmt(gs.get('groups')), '', 'group', _ORANGE,
         ('info', f'本群 {_fmt(gm)} 局' if gm is not None else ''), False),
        ('热度排名', f'#{rank}' if rank else '—', f'/ {n_games}' if rank and n_games else '',
         'trophy', _RANK_COLORS[0], ('info', f'近7日 #{wr}' if wr else ''), False),
        ('最后一局', lab['last'], '', 'clock', _PINK, ('info', lab['ago']), False),
    ]
    tile_w = (inner_w - 28 * 2 - tile_gap) // 2
    for i, (label, value, suffix, icon, fg, pill, total) in enumerate(tiles):
        cx = pad + 28 + (i % 2) * (tile_w + tile_gap)
        cy = y + 68 + (i // 2) * (tile_h + tile_gap)
        _game_tile(d, (cx, cy, cx + tile_w, cy + tile_h), label, value, icon, fg,
                   suffix=suffix, pill=pill, total=total)
    y += overview_h + gap

    if trend:
        _game_trend(d, (pad, y, width - pad, y + trend_h), trend)
        y += trend_h + gap

    _game_boards(d, pad, y, inner_w, gap, rank_h, boards, rows)
    y += rank_h

    footer = 'LGTBot × ElainaBot · 游戏统计 · 仅含计分对局'
    ff = _font(20)
    d.text(((width - _text_w(d, footer, ff)) // 2, y + 18), footer, font=ff, fill=_TEXT_FAINT)

    buf = BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()
