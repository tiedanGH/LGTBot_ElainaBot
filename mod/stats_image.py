#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「数据统计」指令的图片渲染(PIL)。

样式对齐本插件 Web 面板的浅色主题(webui templates/main/main.css 的
light 变量:浅灰页底 + 白色细边框圆角卡 + #5b6ee8 强调色 + 左侧色条标题),
内容为 lgtbot 游戏数据:
  · 数据总览 2×2:今日对局 / 今日活跃玩家(带昨日涨跌胶囊,数据来自
    trend_10d)/ 今日活跃群聊 / 近 10 日对局
  · 近 10 日对局趋势:通栏条形图(当日高亮在最右)
  · 今日游戏榜 / 玩家参与榜:TOP5,金银铜奖牌 + 比例条

PIL 未安装或找不到中文字体时返回 None,调用方(dispatcher 数据统计指令)
回退纯文本输出 —— 渲染是增强,不是依赖。
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


def _tile(d, box, radius=8):
    """区块内的小卡(panel-2 底,同面板 .metrics-status-card)。"""
    d.rounded_rectangle(box, radius=radius, fill=_PANEL2, outline=_BORDER, width=1)


def _sec_title(d, x, y, text):
    """左侧 accent 色条 + 标题(同面板 dash-section-title 的视觉记号)。"""
    d.rounded_rectangle((x, y + 2, x + 8, y + 32), radius=4, fill=_ACCENT)
    _bold_text(d, (x + 22, y), text, _font(28), _TEXT)


def _delta_pill(d, x, y, diff, h=38) -> int:
    """涨跌胶囊(对比昨日;涨=绿、跌=红、平=灰,徽章式 18% 透明底),返回宽度。"""
    if diff is None:
        return 0
    if diff > 0:
        txt, fg = f'↑ {_fmt(diff)}', _GREEN
    elif diff < 0:
        txt, fg = f'↓ {_fmt(abs(diff))}', _RED
    else:
        txt, fg = '· 0', _TEXT_MUTED
    f = _font(22)
    w = _text_w(d, txt, f) + 26
    d.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=_tint(fg))
    d.text((x + 13, y + (h - 22) // 2 - 4), txt, font=f, fill=fg)
    return w


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


def _render(g: dict, sub_title: str) -> bytes | None:
    from PIL import Image, ImageDraw
    if not _find_font():
        return None

    width, pad, gap = 1000, 36, 22
    trend = list(g.get('trend_10d') or [])                  # 新→旧
    top_games = (g.get('top_games_today') or [])[:RANK_LIMIT]
    top_players = (g.get('top_players_today') or [])[:RANK_LIMIT]
    list_rows = max(len(top_games), len(top_players), 1)

    # 主动消息额度(dispatcher 按当前会话目标注入;无则不画该行)
    pq = g.get('push_quota') or {}
    show_pq = bool(pq.get('shown'))

    head_h = 118                                            # 顶栏
    tile_h, tile_gap = 138, 18                              # 指标小卡
    # 标题区 + 2 行小卡(+ 额度通栏卡一行)
    overview_h = 76 + tile_h * 2 + tile_gap + ((tile_h + tile_gap) if show_pq else 0)
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
    y_matches = trend[1]['count'] if len(trend) >= 2 else None
    y_players = trend[1]['players'] if len(trend) >= 2 else None
    total10 = sum(t['count'] for t in trend) if trend else None
    cards = [
        ('今日对局', g.get('today_matches'), y_matches, 'die', _ACCENT),
        ('今日活跃玩家', g.get('today_players'), y_players, 'person', _GREEN),
        ('今日活跃群聊', g.get('today_groups'), None, 'group', _ORANGE),
        ('近10日对局', total10, None, 'calendar', (232, 121, 249)),
    ]
    tile_w = (inner_w - 28 * 2 - tile_gap) // 2
    ty0 = y + 68
    for i, (label, val, y_val, icon, fg) in enumerate(cards):
        cx = pad + 28 + (i % 2) * (tile_w + tile_gap)
        cy = ty0 + (i // 2) * (tile_h + tile_gap)
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
        cy = ty0 + 2 * (tile_h + tile_gap)
        full_w = inner_w - 28 * 2
        _tile(d, (cx, cy, cx + full_w, cy + tile_h))
        d.rounded_rectangle((cx, cy, cx + 5, cy + tile_h), radius=2, fill=_WARN)
        _icon(d, 'warn', cx + 24, cy + (tile_h - 52) // 2, _WARN)
        _bold_text(d, (cx + 96, cy + 30), '本群未开启全量消息权限', _font(28), _WARN)
        d.text((cx + 96, cy + 76), '无法推送主动消息 —— 请 @机器人 发送「全量申请」完成授权',
               font=_font(23), fill=_TEXT_MUTED)
    elif show_pq:
        cx = pad + 28
        cy = ty0 + 2 * (tile_h + tile_gap)
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

    # ── 排行榜 TOP5:今日游戏榜(accent)/ 玩家参与榜(橙)──
    half_w = (inner_w - gap) // 2
    ranks = (('今日游戏榜', top_games, 'game_name', _ACCENT),
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
        name_max_w = half_w - 88 - 28 - 100                 # 名次 + 计数留白
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
            full = str(it.get(key, '') or '')
            name, nf = full, _font(24)
            while name and _text_w(d, name + '…', nf) > name_max_w:
                name = name[:-1]
            shown = name + ('…' if name != full else '')
            d.text((cx + 84, ry), shown, font=nf, fill=_TEXT)
            cf = _font(22)
            cnt_txt = f'{_fmt(cnt)}局'
            _bold_text(d, (cx + half_w - 28 - _text_w(d, cnt_txt, cf), ry + 2),
                       cnt_txt, cf, fg)
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
