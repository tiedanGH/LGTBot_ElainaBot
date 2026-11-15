#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「头衔列表」「我的头衔」的图片渲染(PIL)。

头衔排成网格,格子按头衔颜色铺底;未持有的去掉部分饱和度后调淡、仍看得出原色,装备中的加金色光环并标「装备中」。字体探测与配色沿用 stats_image。
不做文字回退:PIL 或中文字体缺失时 ``missing_dependency()`` 说明缺什么,由指令回复提示安装。
"""

from __future__ import annotations

from io import BytesIO

from core.base.logger import get_logger, PLUGIN
from ..stats import stats_image as si

log = get_logger(PLUGIN, 'LGTBot')

PAD, GAP = 40, 20
# 每种列数的格宽:三列与五列都让最长的头衔名在最小字号下放得下
CELL_W = {3: 320, 5: 248}
CELL_H, CELL_H_LABEL = 96, 124
EMPTY_H = 150                # 一个格子都没有时正文提示框的高度
_GOLD = (245, 179, 1)
_GRAY = (128, 128, 128)
_UNOWNED_SAT = 0.65          # 未持有:保留的饱和度,太低时相近的颜色分不出来
_UNOWNED_ALPHA = 0.29        # 未持有:去色后按这个比例叠在页底上
_UNOWNED_TEXT = tuple((a + b) // 2 for a, b in zip(si._TEXT_FAINT, si._TEXT_MUTED))


def width(cols: int) -> int:
    return PAD * 2 + CELL_W[cols] * cols + GAP * (cols - 1)


def missing_dependency() -> str:
    """渲染缺的依赖,齐全返回空串。"""
    try:
        import PIL  # noqa: F401
    except ImportError:
        return '缺少 Pillow'
    return '' if si._find_font() else '缺少中文字体'


def _rgb(hexcolor: str) -> tuple:
    try:
        n = int(str(hexcolor).lstrip('#'), 16)
    except ValueError:
        return _GRAY
    return (n >> 16 & 255, n >> 8 & 255, n & 255)


def _luma(c) -> float:
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def _on(c) -> tuple:
    """格子上的文字色:亮底用深色,暗底用白色(同面板 titlesOnColor)。"""
    return si._TEXT if _luma(c) > 160 else (255, 255, 255)


def _faded(c) -> tuple:
    """未持有的格子底色:去掉一部分饱和度,再淡淡地叠在页底上。"""
    g = _luma(c)
    grey = tuple(c[i] * _UNOWNED_SAT + g * (1 - _UNOWNED_SAT) for i in range(3))
    return tuple(int(si._BG[i] + (grey[i] - si._BG[i]) * _UNOWNED_ALPHA) for i in range(3))


def _fit(d, text: str, max_w: int):
    """头衔名按格宽挑字号(最长的中文名也能用最小号放下),还放不下就截断加省略号。"""
    for size in (40, 34, 30, 26, 22, 18):
        f = si._font(size)
        if si._text_w(d, text, f) <= max_w:
            return text, f
    while len(text) > 1 and si._text_w(d, text + '…', f) > max_w:
        text = text[:-1]
    return text + '…', f


def _center(d, cx: int, cy: int, text: str, font, fill, bold=False) -> None:
    """按墨迹居中:中文字体字框顶部留白大,按字框摆会整体偏下。"""
    left, top, right, bottom = d.textbbox((0, 0), text, font=font)
    xy = (cx - (left + right) // 2, cy - (top + bottom) // 2)
    if bold:
        si._bold_text(d, xy, text, font, fill)
    else:
        d.text(xy, text, font=font, fill=fill)


def _cell(d, x: int, y: int, w: int, h: int, t: dict) -> None:
    base = _rgb(t['hex'])
    fill = base if t['owned'] else _faded(base)
    fg = _on(base) if t['owned'] else _UNOWNED_TEXT
    if t.get('equipped'):
        # 金色光环画在格子外圈:描在格子里会和橙、黄这类颜色糊在一起
        d.rounded_rectangle((x - 6, y - 6, x + w + 6, y + h + 6), radius=21, outline=_GOLD, width=5)
    d.rounded_rectangle((x, y, x + w, y + h), radius=16, fill=fill)
    if t.get('equipped'):
        tag = si._font(18)
        tw = si._text_w(d, '装备中', tag)
        d.rounded_rectangle((x + w - tw - 26, y + 8, x + w - 8, y + 34), radius=13, fill=_GOLD)
        _center(d, x + w - 17 - tw // 2, y + 21, '装备中', tag, si._TEXT)
    if t.get('index'):
        d.text((x + 16, y + 12), str(t['index']), font=si._font(22), fill=fg)
    name, f = _fit(d, t['name'], w - 28)
    label = t.get('label')
    # 带分类的格子名字放在序号、角标那一行下面:五列格子窄,长名字会顶到序号和角标上
    _center(d, x + w // 2, y + (61 if label else h // 2 + 4), name, f, fg, bold=True)
    if label:
        _center(d, x + w // 2, y + h - 22, label, si._font(22), fg)


def render(title: str, sub: str, sections: list, footer: str, cols: int = 3, empty: str = '') -> bytes | None:
    """``sections`` 是 ``[(小标题, [{'name','hex','owned','equipped','index','label'}, ...]), ...]``,空的小节跳过;``label`` 写在头衔名下方,一张图里有一个带就都留出这一行。小节全空时把 ``empty`` 写在正文的提示框里。渲染异常返回 None。"""
    try:
        return _render(title, sub, [(label, items) for label, items in sections if items], footer, cols, empty)
    except Exception as e:
        log.warning(f'头衔图片渲染失败: {e}')
        return None


def _render(title: str, sub: str, sections: list, footer: str, cols: int, empty: str) -> bytes | None:
    from PIL import Image, ImageDraw
    if not si._find_font():
        return None
    cw = CELL_W[cols]
    ch = CELL_H_LABEL if any(t.get('label') for _label, items in sections for t in items) else CELL_H
    head_h, label_h, footer_h = 124, 58, 56
    rows = [-(-len(items) // cols) for _label, items in sections]
    total_w = width(cols)
    empty_h = EMPTY_H if empty and not sections else 0
    height = PAD + head_h + sum(GAP + label_h + n * ch + (n - 1) * GAP for n in rows) + (GAP + empty_h if empty_h else 0) + GAP + footer_h + PAD
    img = Image.new('RGB', (total_w, height), si._BG)
    d = ImageDraw.Draw(img)

    y = PAD
    si._section(d, (PAD, y, total_w - PAD, y + head_h), top_accent=True)
    si._bold_text(d, (PAD + 28, y + 22), title, si._font(40), si._ACCENT)
    if sub:
        d.text((PAD + 28, y + 82), sub, font=si._font(22), fill=si._TEXT_MUTED)
    y += head_h
    for (label, items), n in zip(sections, rows):
        y += GAP
        si._sec_title(d, PAD + 4, y + 8, label)
        y += label_h
        for i, t in enumerate(items):
            r, c = divmod(i, cols)
            _cell(d, PAD + c * (cw + GAP), y + r * (ch + GAP), cw, ch, t)
        y += n * ch + (n - 1) * GAP
    if empty_h:
        y += GAP
        si._section(d, (PAD, y, total_w - PAD, y + empty_h))
        _center(d, total_w // 2, y + empty_h // 2, empty, si._font(34), si._TEXT_MUTED)
        y += empty_h
    _center(d, total_w // 2, y + GAP + footer_h // 2, footer, si._font(22), si._TEXT_FAINT)

    buf = BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue()
