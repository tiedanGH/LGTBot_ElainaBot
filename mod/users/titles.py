#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""头衔系统 —— 每人可持有多个、同时只装备一个;头衔按等级上色,可新增 / 编辑,数据由面板维护。

数据在 ``data/titles/`` 下的两份文件:
  · ``titles.json``:``{"<openid>": {"titles": [{"name": 头衔名, "color": 等级键}, ...], "equipped": 头衔名 | null}}``
  · ``levels.json``:``[{"key": 等级键, "name": 等级名, "hex": "#RRGGBB"}, ...]``,文件不存在时用默认的六个等级

头衔按等级键引用等级,改等级的名称或颜色对所有头衔立即生效;还有头衔在用的等级不能删除。
装备按名字记,面板增删、调序头衔后照样对得上。展示时用 KaTeX 行内上色,头衔名与等级名因此不收会打断公式或 markdown 的字符。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid

from core.base.logger import get_logger, PLUGIN
from .. import boot
from . import userinfo

log = get_logger(PLUGIN, 'LGTBot')

# 默认等级:顺序即稀有度由低到高,粉色是限定款;键沿用颜色名
DEFAULT_LEVELS = (
    {'key': 'green',  'name': '普通', 'hex': '#27AE60'},
    {'key': 'blue',   'name': '稀有', 'hex': '#2980B9'},
    {'key': 'purple', 'name': '史诗', 'hex': '#8E44AD'},
    {'key': 'orange', 'name': '传说', 'hex': '#FF8C00'},
    {'key': 'red',    'name': '神话', 'hex': '#E74C3C'},
    {'key': 'pink',   'name': '限定', 'hex': '#E84393'},
)
NAME_MAX_LEN = 12
LEVEL_NAME_MAX_LEN = 8
MAX_TITLES = 20
MAX_LEVELS = 20
# KaTeX \text{} 与 markdown 里有特殊含义的字符
_FORBIDDEN = set('\\{}$%#&_^~*`[]<>|')
_OPENID_RE = re.compile(r'^[0-9A-Za-z]{32}$')
_HEX_RE = re.compile(r'^#?([0-9A-Fa-f]{6})$')
# 等级文件损坏、被手改掉时头衔照常保留,按灰色兜底显示,可在面板重新选等级
_UNKNOWN_LEVEL = {'key': '', 'name': '未知', 'hex': '#808080'}


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
_levels = _JsonFile(os.path.join(_DIR, 'levels.json'), list)
# 两份文件共用一把锁;锁内只调 *_unlocked,threading.Lock 不可重入
_lock = threading.Lock()


def valid_openid(openid) -> bool:
    """QQ 的 OpenID 是 32 位字母数字串。"""
    return isinstance(openid, str) and bool(_OPENID_RE.match(openid))


def _check_text(text, label: str, max_len: int) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return f'{label}不能为空'
    text = text.strip()
    if len(text) > max_len:
        return f'{label}最多 {max_len} 个字'
    if any(c in _FORBIDDEN or c in '\r\n\t' for c in text):
        return f'{label}不能包含 \\ {{ }} $ % # & _ ^ ~ * ` [ ] < > | 等符号'
    return None


def validate_name(name) -> str | None:
    """头衔名不合法时返回原因,合法返回 None。"""
    return _check_text(name, '头衔名', NAME_MAX_LEN)


def validate_level_name(name) -> str | None:
    """等级名不合法时返回原因,合法返回 None。"""
    return _check_text(name, '等级名', LEVEL_NAME_MAX_LEN)


def normalize_hex(value) -> str | None:
    """「#RRGGBB」或「RRGGBB」统一成大写的「#RRGGBB」;不合法返回 None。"""
    m = _HEX_RE.match(value.strip()) if isinstance(value, str) else None
    return f'#{m.group(1).upper()}' if m else None


# ──────── 等级 ────────────────────────────────────────────────────────────

def _levels_unlocked() -> list[dict]:
    """读到的等级表规整成合法形状(丢掉坏项);一个可用的都没有时退回默认等级。"""
    raw = _levels.load()
    out, keys, names = [], set(), set()
    for lv in raw:
        if not isinstance(lv, dict):
            continue
        key, name, hexcolor = lv.get('key'), lv.get('name'), normalize_hex(lv.get('hex'))
        if not isinstance(key, str) or not key or key in keys or validate_level_name(name) or not hexcolor:
            continue
        if name.strip() in names:
            continue
        keys.add(key)
        names.add(name.strip())
        out.append({'key': key, 'name': name.strip(), 'hex': hexcolor})
    return out or [dict(lv) for lv in DEFAULT_LEVELS]


def levels() -> list[dict]:
    """全部等级(副本),顺序即面板里的顺序。"""
    with _lock:
        return _levels_unlocked()


def level_of(title: dict) -> dict:
    """头衔所属的等级;等级已不存在时返回灰色的「未知」兜底。"""
    key = title.get('color')
    return next((lv for lv in levels() if lv['key'] == key), _UNKNOWN_LEVEL)


def _usage_unlocked() -> dict[str, int]:
    """各等级被多少个头衔引用。"""
    usage: dict[str, int] = {}
    for raw in _players.load().values():
        for t in (_clean_record(raw) or {}).get('titles', []):
            usage[t['color']] = usage.get(t['color'], 0) + 1
    return usage


def set_levels(rows) -> str | None:
    """面板保存:整份覆盖等级表,不合法时返回原因(不写盘)。

    行带现有等级的 key 是编辑,不带 key 是新增(在这里生成);删掉的等级还有头衔在用时拒绝保存。
    """
    if not isinstance(rows, list) or not rows:
        return '至少保留一个等级'
    if len(rows) > MAX_LEVELS:
        return f'最多 {MAX_LEVELS} 个等级'
    with _lock:
        current = {lv['key']: lv for lv in _levels_unlocked()}
        out, names, kept = [], set(), set()
        for row in rows:
            if not isinstance(row, dict):
                return '等级格式错误'
            err = validate_level_name(row.get('name'))
            if err:
                return err
            name = row['name'].strip()
            hexcolor = normalize_hex(row.get('hex'))
            if not hexcolor:
                return f'等级「{name}」颜色需为 #RRGGBB 格式的 HEX'
            if name in names:
                return f'等级「{name}」重复'
            key = row.get('key') or ''
            if key and (key not in current or key in kept):
                return f'等级「{name}」已被删除或重复，请刷新后再编辑'
            if not key:
                # 随机键:不会撞上被删等级的旧键(手改文件残留的头衔可能还引用着)
                key = uuid.uuid4().hex[:8]
            kept.add(key)
            names.add(name)
            out.append({'key': key, 'name': name, 'hex': hexcolor})
        usage = _usage_unlocked()
        for key, lv in current.items():
            if key not in kept and usage.get(key):
                return f'等级「{lv["name"]}」还有 {usage[key]} 个头衔在用，先给这些头衔换个等级再删除'
        _levels.save(out)
    return None


# ──────── 玩家头衔 ────────────────────────────────────────────────────────

def _clean_record(raw) -> dict | None:
    """把读到的记录规整成合法形状:丢掉名字不合法、重名的头衔;等级已不存在的头衔照常保留,读一次不会把它弄丢。"""
    if not isinstance(raw, dict):
        return None
    out, seen = [], set()
    for t in raw.get('titles') or []:
        if not isinstance(t, dict):
            continue
        name, color = t.get('name'), t.get('color')
        if validate_name(name) or not isinstance(color, str) or not color or name.strip() in seen:
            continue
        seen.add(name.strip())
        out.append({'name': name.strip(), 'color': color})
    equipped = raw.get('equipped')
    return {'titles': out, 'equipped': equipped if equipped in seen else None}


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
    """玩家当前装备的头衔 ``{name, color}``;没装备返回 None。"""
    rec = get(openid)
    if not rec or not rec['equipped']:
        return None
    return next((t for t in rec['titles'] if t['name'] == rec['equipped']), None)


def equip(openid: str, ref: str) -> tuple[dict | None, str]:
    """按序号(从 1 起)或头衔名装备,返回 ``(头衔, '')``;失败返回 ``(None, 'none' 没有头衔 | 'missing' 找不到)``。"""
    ref = (ref or '').strip()
    with _lock:
        data = _players.load()
        rec = _clean_record(data.get(openid))
        if not rec or not rec['titles']:
            return None, 'none'
        titles = rec['titles']
        title = None
        if ref.isdecimal() and len(ref) <= 3 and 1 <= int(ref) <= len(titles):
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
    """面板保存:整份覆盖玩家的头衔库与当前装备,不合法时返回原因(不写盘);头衔库为空等于删除记录。"""
    if not valid_openid(openid):
        return 'OpenID 应为 32 位字母或数字'
    if not isinstance(titles, list):
        return '头衔列表格式错误'
    if len(titles) > MAX_TITLES:
        return f'每人最多 {MAX_TITLES} 个头衔'
    with _lock:
        keys = {lv['key'] for lv in _levels_unlocked()}
        clean, seen = [], set()
        for t in titles:
            if not isinstance(t, dict):
                return '头衔格式错误'
            err = validate_name(t.get('name'))
            if err:
                return err
            name = t['name'].strip()
            if t.get('color') not in keys:
                return f'头衔「{name}」的等级不存在，请重新选择'
            if name in seen:
                return f'头衔「{name}」重复'
            seen.add(name)
            clean.append({'name': name, 'color': t['color']})
        if equipped_name not in (None, '') and equipped_name not in seen:
            return '当前装备的头衔不在头衔库里'
        data = dict(_players.load())
        if clean:
            data[openid] = {'titles': clean, 'equipped': equipped_name or None}
        else:
            data.pop(openid, None)
        _players.save(data)
    return None


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

def rarity(title: dict) -> str:
    return level_of(title)['name']


def render(title: dict) -> str:
    """KaTeX 行内上色的头衔名,色值可以直接放进公式。"""
    return f'$\\textcolor{{{level_of(title)["hex"]}}}{{\\text{{{title["name"]}}}}}$'


def join_notice(title: dict) -> str:
    """新建 / 加入游戏时的头衔播报。"""
    return f'[头衔] {render(title)} 加入游戏'


def player_label(openid: str) -> str:
    """审计详情里的玩家:有昵称写「昵称 (OpenID)」,没有只写 OpenID。"""
    name = userinfo.get_name(openid)
    return f'{name} ({openid})' if name else openid
