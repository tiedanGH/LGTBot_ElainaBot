#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""头衔系统 —— 颜色与分类 + 头衔库 + 每人持有的头衔与当前装备。

数据在 ``data/titles/`` 下:
  · ``colors.json``:``[{"name": 颜色名, "hex": "#RRGGBB"}, ...]``
  · ``categories.json``:``[{"name": 分类名}, ...]``;顺序即排序,文件不存在时用默认预设
  · ``library.json``:``[{"name": 头衔名, "color": 颜色名, "category": 分类名}, ...]``,头衔库
  · ``titles.json``:``{"<openid>": {"titles": [{"name", "color", "category"}, ...], "equipped": 头衔名 | null}}``

名字都唯一并直接按名引用:颜色 / 分类改名连同头衔库与玩家头衔一起换名,还有头衔在用的不能删除;头衔库改名、改颜色分类同步到持有者。
玩家头衔与头衔库同名即库内头衔,颜色分类跟随头衔库;不在库里的是限定头衔,只在本人的头衔列表里出现。
排序一律先分类、再颜色;序号按展示顺序(限定在前、库内在后)计。展示时用 KaTeX 行内上色。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

from core.base.logger import get_logger, PLUGIN
from .. import boot
from . import userinfo

log = get_logger(PLUGIN, 'LGTBot')

DEFAULT_COLORS = (
    {'name': '黄', 'hex': '#F1C40F'},
    {'name': '橙', 'hex': '#FF8C00'},
    {'name': '红', 'hex': '#E74C3C'},
    {'name': '粉', 'hex': '#E84393'},
    {'name': '紫', 'hex': '#8E44AD'},
    {'name': '蓝', 'hex': '#2980B9'},
    {'name': '青', 'hex': '#17A2B8'},
    {'name': '绿', 'hex': '#27AE60'},
)
DEFAULT_CATEGORIES = ({'name': '长期'},)
NAME_MAX_LEN = 12
PRESET_NAME_MAX_LEN = 8
_OPENID_RE = re.compile(r'^[0-9A-Za-z]{32}$')
_HEX_RE = re.compile(r'^#?([0-9A-Fa-f]{6})$')
# 颜色 / 分类文件损坏、被手改掉时头衔照常保留,按灰色、「未分类」兜底显示,可在面板重新选
UNKNOWN_COLOR = {'name': '未知', 'hex': '#808080'}
UNKNOWN_CATEGORY = '未分类'


class _JsonFile:
    """带缓存的 JSON 文件:按 (mtime, size) 失效,恢复备份 / 手改文件后下次读取即生效;解析失败的文件改名留证,从空续记,免得下次保存把它覆盖掉。"""

    def __init__(self, path: str, kind: type):
        self.path = path
        self._kind = kind
        self._cache: tuple | None = None    # (mtime_ns, size, data)

    def load(self):
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            self._cache = None
            return self._kind()
        if self._cache and self._cache[:2] == (st.st_mtime_ns, st.st_size):
            return self._cache[2]
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, self._kind):
                raise ValueError('根节点类型错误')
        except (OSError, ValueError) as e:
            bad = f'{self.path}.corrupt_{int(time.time())}'
            log.warning(f'{os.path.basename(self.path)} 读取失败,已改名为 {os.path.basename(bad)}: {e}')
            try:
                os.replace(self.path, bad)
            except OSError:
                pass
            self._cache = None
            return self._kind()
        self._cache = (st.st_mtime_ns, st.st_size, data)
        return data

    def save(self, data) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
        st = os.stat(self.path)
        self._cache = (st.st_mtime_ns, st.st_size, data)


_DIR = os.path.join(boot.DATA_DIR, 'titles')
_players = _JsonFile(os.path.join(_DIR, 'titles.json'), dict)
_colors = _JsonFile(os.path.join(_DIR, 'colors.json'), list)
_categories = _JsonFile(os.path.join(_DIR, 'categories.json'), list)
_library = _JsonFile(os.path.join(_DIR, 'library.json'), list)
# 全部文件共用一把锁;锁内只调 *_unlocked,threading.Lock 不可重入
_lock = threading.Lock()

# 两套预设:字段名 = 头衔里引用它的键;file 存模块属性名,测试替换文件后照样生效
_KINDS = {
    'color': {'label': '颜色', 'file': '_colors', 'defaults': DEFAULT_COLORS, 'hex': True},
    'category': {'label': '分类', 'file': '_categories', 'defaults': DEFAULT_CATEGORIES, 'hex': False},
}


def data_dir() -> str:
    """数据目录(面板展示用)。"""
    return os.path.dirname(_players.path)


def valid_openid(openid) -> bool:
    """QQ 的 OpenID 是 32 位字母数字串。"""
    return isinstance(openid, str) and bool(_OPENID_RE.match(openid))


def _check_text(text, label: str, max_len: int) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return f'{label}不能为空'
    text = text.strip()
    if len(text) > max_len:
        return f'{label}最多 {max_len} 个字'
    if any(c in '\r\n\t' for c in text):
        return f'{label}不能包含换行或制表符'
    return None


def validate_name(name) -> str | None:
    """头衔名不合法时返回原因,合法返回 None。"""
    return _check_text(name, '头衔名', NAME_MAX_LEN)


def normalize_hex(value) -> str | None:
    """「#RRGGBB」或「RRGGBB」统一成大写的「#RRGGBB」;不合法返回 None。"""
    m = _HEX_RE.match(value.strip()) if isinstance(value, str) else None
    return f'#{m.group(1).upper()}' if m else None


def _clean_titles(items) -> list[dict]:
    """头衔列表规整成合法形状:丢掉名字不合法、重名的项;颜色或分类已不存在的照常保留,读一次不会把它弄丢。"""
    out, seen = [], set()
    for t in items or []:
        if not isinstance(t, dict):
            continue
        name, color, category = t.get('name'), t.get('color'), t.get('category')
        if validate_name(name) or name.strip() in seen or not all(isinstance(v, str) and v for v in (color, category)):
            continue
        seen.add(name.strip())
        out.append({'name': name.strip(), 'color': color, 'category': category})
    return out


def _sorter(colors: list[dict], categories: list[dict]):
    """排序键:先分类、再颜色,都按预设的顺序,已不存在的排最后;配合稳定排序,同分类同颜色保持原顺序。"""
    ci = {c['name']: i for i, c in enumerate(colors)}
    ki = {c['name']: i for i, c in enumerate(categories)}
    return lambda t: (ki.get(t['category'], len(ki)), ci.get(t['color'], len(ci)))


# ──────── 颜色与分类 ──────────────────────────────────────────────────────

def _presets_unlocked(kind: str) -> list[dict]:
    """读到的预设规整成合法形状(丢掉坏项与重名);一个可用的都没有时退回默认。"""
    cfg = _KINDS[kind]
    out, names = [], set()
    for p in globals()[cfg['file']].load():
        if not isinstance(p, dict):
            continue
        name = p.get('name')
        if _check_text(name, cfg['label'] + '名', PRESET_NAME_MAX_LEN) or name.strip() in names:
            continue
        item = {'name': name.strip()}
        if cfg['hex']:
            item['hex'] = normalize_hex(p.get('hex'))
            if not item['hex']:
                continue
        names.add(item['name'])
        out.append(item)
    return out or [dict(p) for p in cfg['defaults']]


def colors() -> list[dict]:
    """全部颜色(副本),顺序即排序。"""
    with _lock:
        return _presets_unlocked('color')


def categories() -> list[dict]:
    """全部分类(副本),顺序即排序。"""
    with _lock:
        return _presets_unlocked('category')


def color_of(title: dict) -> dict:
    """头衔的颜色 ``{name, hex}``;颜色已不存在时返回灰色的「未知」兜底。"""
    name = title.get('color')
    return next((c for c in colors() if c['name'] == name), UNKNOWN_COLOR)


def _usage_unlocked(field: str) -> dict[str, int]:
    """各颜色 / 分类被多少个头衔引用:头衔库每项算一个,玩家头衔只算限定的(库内头衔跟随头衔库)。"""
    lib = _library_unlocked()
    lib_names = {t['name'] for t in lib}
    usage: dict[str, int] = {}
    for t in lib:
        usage[t[field]] = usage.get(t[field], 0) + 1
    for raw in _players.load().values():
        for t in (_clean_record(raw) or {}).get('titles', []):
            if t['name'] not in lib_names:
                usage[t[field]] = usage.get(t[field], 0) + 1
    return usage


def _rename_unlocked(field: str, moved: dict[str, str]) -> None:
    """颜色 / 分类改名后把头衔库与玩家头衔里的旧名换成新名;一次映射,两个互换名字也不会串。"""
    def remap(items):
        return [dict(t, **{field: moved[t[field]]}) if isinstance(t, dict) and isinstance(t.get(field), str) and t[field] in moved else t
                for t in items]

    data, changed = {}, False
    for uid, raw in _players.load().items():
        if isinstance(raw, dict) and isinstance(raw.get('titles'), list):
            ts = remap(raw['titles'])
            if ts != raw['titles']:
                raw, changed = dict(raw, titles=ts), True
        data[uid] = raw
    if changed:
        _players.save(data)
    lib = _library.load()
    if remap(lib) != lib:
        _library.save(remap(lib))


def _set_presets(kind: str, rows) -> str | None:
    cfg = _KINDS[kind]
    label = cfg['label']
    if not isinstance(rows, list) or not rows:
        return f'至少保留一个{label}'
    with _lock:
        current = {p['name'] for p in _presets_unlocked(kind)}
        out, names, renames = [], set(), {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('orig') or '', str):
                return f'{label}格式错误'
            err = _check_text(row.get('name'), label + '名', PRESET_NAME_MAX_LEN)
            if err:
                return err
            item = {'name': row['name'].strip()}
            if cfg['hex']:
                item['hex'] = normalize_hex(row.get('hex'))
                if not item['hex']:
                    return f'颜色「{item["name"]}」需为 #RRGGBB 格式的 HEX'
            if item['name'] in names:
                return f'{label}「{item["name"]}」重复'
            orig = row.get('orig') or ''
            if orig and (orig not in current or orig in renames):
                return f'{label}「{item["name"]}」已被删除或重复，请刷新后再编辑'
            if orig:
                renames[orig] = item['name']
            names.add(item['name'])
            out.append(item)
        # 被删掉的名字若又被新行用上,头衔就挂到新的那项上,不算失去颜色 / 分类
        for name, n in _usage_unlocked(kind).items():
            if name in current and renames.get(name, name) not in names:
                return f'{label}「{name}」还有 {n} 个头衔在用，先给这些头衔换一个再删除'
        moved = {o: n for o, n in renames.items() if o != n}
        if moved:
            _rename_unlocked(kind, moved)
        globals()[cfg['file']].save(out)
    return None


def set_colors(rows) -> str | None:
    """面板保存:整份覆盖颜色表,行序即排序;行带 ``orig``(原名)是编辑,不带是新增;不合法时返回原因(不写盘)。"""
    return _set_presets('color', rows)


def set_categories(rows) -> str | None:
    """面板保存:整份覆盖分类表,规则同 ``set_colors``。"""
    return _set_presets('category', rows)


# ──────── 头衔库 ──────────────────────────────────────────────────────────

def _library_unlocked() -> list[dict]:
    return _clean_titles(_library.load())


def _library_sorted_unlocked() -> list[dict]:
    return sorted(_library_unlocked(), key=_sorter(_presets_unlocked('color'), _presets_unlocked('category')))


def library() -> list[dict]:
    """头衔库(副本),先分类再颜色排序,同分类同颜色保持录入顺序。"""
    with _lock:
        return _library_sorted_unlocked()


def _sync_players_unlocked(renames: dict[str, str], lib: dict[str, dict]) -> None:
    """头衔库改动同步到玩家:按 renames 换名(装备跟着换),与库同名的头衔改用库里的颜色分类;换名撞上已有的同名头衔时只留一个。"""
    data, changed = {}, False
    for uid, raw in _players.load().items():
        rec = _clean_record(raw)
        if rec is not None:
            ts, seen = [], set()
            for t in rec['titles']:
                name = renames.get(t['name'], t['name'])
                if name not in seen:
                    seen.add(name)
                    ts.append(dict(lib[name]) if name in lib else dict(t, name=name))
            new = {'titles': ts, 'equipped': renames.get(rec['equipped'], rec['equipped'])}
            if new != rec:
                raw, changed = new, True
        data[uid] = raw
    if changed:
        _players.save(data)


def _check_style(t: dict, name: str, color_names: set, category_names: set) -> str | None:
    if not isinstance(t.get('color'), str) or t['color'] not in color_names:
        return f'头衔「{name}」的颜色不存在，请重新选择'
    if not isinstance(t.get('category'), str) or t['category'] not in category_names:
        return f'头衔「{name}」的分类不存在，请重新选择'
    return None


def set_library(rows) -> str | None:
    """面板保存:整份覆盖头衔库,不合法时返回原因(不写盘)。

    行带 ``orig``(原名)是编辑,不带是新增;改名、改颜色分类同步到持有者,删掉的头衔持有者照常保留,成为限定头衔。
    """
    if not isinstance(rows, list):
        return '头衔库格式错误'
    with _lock:
        current = {t['name'] for t in _library_unlocked()}
        color_names = {c['name'] for c in _presets_unlocked('color')}
        category_names = {c['name'] for c in _presets_unlocked('category')}
        out, renames = {}, {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('orig') or '', str):
                return '头衔格式错误'
            err = validate_name(row.get('name'))
            if err:
                return err
            name = row['name'].strip()
            err = _check_style(row, name, color_names, category_names)
            if err:
                return err
            if name in out:
                return f'头衔「{name}」重复'
            orig = row.get('orig') or ''
            if orig and (orig not in current or orig in renames):
                return f'头衔「{name}」已被删除或重复，请刷新后再编辑'
            if orig:
                renames[orig] = name
            out[name] = {'name': name, 'color': row['color'], 'category': row['category']}
        _sync_players_unlocked({o: n for o, n in renames.items() if o != n}, out)
        _library.save(list(out.values()))
    return None


# ──────── 玩家头衔 ────────────────────────────────────────────────────────

def _clean_record(raw) -> dict | None:
    """把读到的记录规整成合法形状;装备的头衔不在持有的头衔里时视为没装备。"""
    if not isinstance(raw, dict):
        return None
    out = _clean_titles(raw.get('titles'))
    equipped = raw.get('equipped')
    return {'titles': out, 'equipped': equipped if any(t['name'] == equipped for t in out) else None}


def _display_order(titles: list[dict], lib_sorted: list[dict], key) -> list[dict]:
    """展示顺序:限定头衔在前(先分类再颜色),库内头衔按头衔库的顺序。"""
    pos = {t['name']: i for i, t in enumerate(lib_sorted)}
    custom = sorted((t for t in titles if t['name'] not in pos), key=key)
    return custom + sorted((t for t in titles if t['name'] in pos), key=lambda t: pos[t['name']])


def get(openid: str) -> dict | None:
    """玩家的头衔记录 ``{titles, equipped}``(副本);没有记录返回 None。"""
    if not openid:
        return None
    with _lock:
        return _clean_record(_players.load().get(openid))


def all_records() -> dict:
    """全部玩家的头衔记录 ``{openid: {titles, equipped}}``(副本),给面板用。"""
    with _lock:
        data = _players.load()
    return {uid: rec for uid, rec in ((u, _clean_record(r)) for u, r in data.items()) if rec}


def equipped(openid: str) -> dict | None:
    """玩家当前装备的头衔 ``{name, color, category}``;没装备返回 None。"""
    rec = get(openid)
    if not rec or not rec['equipped']:
        return None
    return next((t for t in rec['titles'] if t['name'] == rec['equipped']), None)


def board(openid: str) -> dict:
    """头衔图片用的数据:``library`` 头衔库(已排序),``custom`` 玩家的限定头衔与 ``mine`` 持有的头衔(都按展示顺序),``equipped`` 装备的头衔名,``colors`` 颜色名 → HEX,``categories`` 分类名(按顺序)。"""
    with _lock:
        color_list, category_list = _presets_unlocked('color'), _presets_unlocked('category')
        key = _sorter(color_list, category_list)
        lib = sorted(_library_unlocked(), key=key)
        rec = _clean_record(_players.load().get(openid)) or {'titles': [], 'equipped': None}
    names = {t['name'] for t in lib}
    mine = _display_order(rec['titles'], lib, key)
    return {'library': lib, 'custom': [t for t in mine if t['name'] not in names], 'mine': mine, 'equipped': rec['equipped'],
            'colors': {c['name']: c['hex'] for c in color_list}, 'categories': [c['name'] for c in category_list]}


def equip(openid: str, ref: str) -> tuple[dict | None, str]:
    """按序号(从 1 起,展示顺序)或头衔名装备,返回 ``(头衔, '')``;失败返回 ``(None, 'none' 没有头衔 | 'missing' 找不到)``。"""
    ref = (ref or '').strip()
    with _lock:
        data = _players.load()
        rec = _clean_record(data.get(openid))
        if not rec or not rec['titles']:
            return None, 'none'
        key = _sorter(_presets_unlocked('color'), _presets_unlocked('category'))
        titles = _display_order(rec['titles'], sorted(_library_unlocked(), key=key), key)
        title = None
        # 位数超过头衔总数的位数就不当序号,超长数字串也不会进 int()
        if ref.isdecimal() and len(ref) <= len(str(len(titles))) and 1 <= int(ref) <= len(titles):
            title = titles[int(ref) - 1]
        if title is None:
            title = next((t for t in titles if t['name'] == ref), None)
        if title is None:
            return None, 'missing'
        rec['equipped'] = title['name']
        data = dict(data)
        data[openid] = rec
        _players.save(data)
    return title, ''


def unequip(openid: str) -> tuple[dict | None, str]:
    """取消装备,返回 ``(卸下的头衔, '')``;失败返回 ``(None, 'none' 没有头衔 | 'already' 本来就没装备)``。"""
    with _lock:
        data = _players.load()
        rec = _clean_record(data.get(openid))
        if not rec or not rec['titles']:
            return None, 'none'
        title = next((t for t in rec['titles'] if t['name'] == rec['equipped']), None)
        if title is None:
            return None, 'already'
        rec['equipped'] = None
        data = dict(data)
        data[openid] = rec
        _players.save(data)
    return title, ''


def set_record(openid: str, titles, equipped_name) -> str | None:
    """面板保存:整份覆盖玩家持有的头衔与当前装备,不合法时返回原因(不写盘);头衔清空等于删除记录。与头衔库同名的头衔颜色分类以头衔库为准。"""
    if not valid_openid(openid):
        return 'OpenID 应为 32 位字母或数字'
    if not isinstance(titles, list):
        return '头衔列表格式错误'
    with _lock:
        color_names = {c['name'] for c in _presets_unlocked('color')}
        category_names = {c['name'] for c in _presets_unlocked('category')}
        lib = {t['name']: t for t in _library_unlocked()}
        clean, seen = [], set()
        for t in titles:
            if not isinstance(t, dict):
                return '头衔格式错误'
            err = validate_name(t.get('name'))
            if err:
                return err
            name = t['name'].strip()
            item = dict(lib[name]) if name in lib else {'name': name, 'color': t.get('color'), 'category': t.get('category')}
            err = _check_style(item, name, color_names, category_names)
            if err:
                return err
            if name in seen:
                return f'头衔「{name}」重复'
            seen.add(name)
            clean.append(item)
        if equipped_name not in (None, '') and equipped_name not in seen:
            return '当前装备的头衔不在玩家持有的头衔里'
        data = dict(_players.load())
        if clean:
            data[openid] = {'titles': clean, 'equipped': equipped_name or None}
        else:
            data.pop(openid, None)
        _players.save(data)
    return None


def grant(openids, title) -> tuple[dict | None, str | None]:
    """面板批量授予:把一个头衔发给多位玩家,返回 ``({title, custom, granted, equipped, held}, None)``,不合法时返回 ``(None, 原因)`` 且不写盘。

    与头衔库同名的按库内头衔发放(颜色分类以头衔库为准),否则是限定头衔;已持有的跳过(``held``),原本没有头衔的玩家顺带装备上(``equipped``)。
    """
    if not isinstance(openids, list) or not openids:
        return None, '请至少选择一位玩家'
    if not all(valid_openid(u) for u in openids):
        return None, 'OpenID 应为 32 位字母或数字'
    if not isinstance(title, dict):
        return None, '头衔格式错误'
    err = validate_name(title.get('name'))
    if err:
        return None, err
    name = title['name'].strip()
    with _lock:
        lib = {t['name']: t for t in _library_unlocked()}
        item = dict(lib[name]) if name in lib else {'name': name, 'color': title.get('color'), 'category': title.get('category')}
        err = _check_style(item, name, {c['name'] for c in _presets_unlocked('color')}, {c['name'] for c in _presets_unlocked('category')})
        if err:
            return None, err
        data = dict(_players.load())
        out = {'title': item, 'custom': name not in lib, 'granted': [], 'equipped': [], 'held': []}
        for uid in dict.fromkeys(openids):
            rec = _clean_record(data.get(uid)) or {'titles': [], 'equipped': None}
            if any(t['name'] == name for t in rec['titles']):
                out['held'].append(uid)
                continue
            first = not rec['titles']
            if first:
                out['equipped'].append(uid)
            data[uid] = {'titles': rec['titles'] + [dict(item)], 'equipped': name if first else rec['equipped']}
            out['granted'].append(uid)
        if out['granted']:
            _players.save(data)
    return out, None


def delete(openid: str) -> bool:
    """删除玩家的头衔记录;本来就没有返回 False。"""
    with _lock:
        data = _players.load()
        if openid not in data:
            return False
        data = dict(data)
        del data[openid]
        _players.save(data)
    return True


# ──────── 展示 ────────────────────────────────────────────────────────────

def render(title: dict) -> str:
    """KaTeX 行内上色的头衔名,色值可以直接放进公式。"""
    return f'$\\textcolor{{{color_of(title)["hex"]}}}{{\\text{{{title["name"]}}}}}$'


def join_notice(title: dict, player: str) -> str:
    """新建 / 加入游戏时的头衔播报:「【头衔名】」加粗并上色,``player`` 是已转义的玩家名;公式后留空格,玩家名以数字开头时公式也能闭合。"""
    return f'$\\textcolor{{{color_of(title)["hex"]}}}{{\\textbf{{【{title["name"]}】}}}}$ {player} 加入游戏'


def player_label(openid: str) -> str:
    """审计详情里的玩家:有昵称写「昵称 (OpenID)」,没有只写 OpenID。"""
    name = userinfo.get_name(openid)
    return f'{name} ({openid})' if name else openid
