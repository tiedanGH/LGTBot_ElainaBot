#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「头衔系统」标签 —— 颜色、分类与头衔库的新增 / 编辑 / 排序,全部玩家持有的头衔与当前装备的查看、编辑、装备与删除。

Python 侧职责:
  · ``TAB_HTML`` / ``TAB_CSS`` / ``TAB_JS`` 从 ``templates/titles/`` 加载
  · ``get_data()`` 首屏 payload(颜色 + 分类 + 头衔库 + 全部玩家记录,昵称 / 头像经 userinfo 门面查)
  · ``list`` / ``lookup`` / ``search`` / ``save`` / ``delete`` / ``colors`` / ``categories`` / ``library`` 八条真路由(``*_handler``);改动类接口都回完整 payload,前端整页重绘

数据读写与校验都在 ``users.titles``,这里只做请求解析、审计与组装。
"""

from __future__ import annotations

import json
import os
import time

from aiohttp import web

from ...ops import audit
from ...users import titles, userinfo

_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'templates')


def _load(name: str) -> str:
    with open(os.path.join(_TEMPLATE_DIR, name), 'r', encoding='utf-8') as f:
        return f.read()


TAB_HTML = _load('titles/titles.html')
TAB_CSS = _load('titles/titles.css')
TAB_JS = _load('titles/titles.js')


def _payload() -> dict:
    """首屏与各接口共用的完整 payload;玩家按昵称排序,没有昵称的排在最后。"""
    players = [{'openid': uid, 'name': userinfo.get_name(uid), 'avatar': userinfo.avatar_url(uid),
                'titles': rec['titles'], 'equipped': rec['equipped']}
               for uid, rec in titles.all_records().items()]
    players.sort(key=lambda p: (not p['name'], p['name'], p['openid']))
    return {
        'colors': titles.colors(),
        'categories': titles.categories(),
        'library': titles.library(),
        'name_max': titles.NAME_MAX_LEN,
        'preset_name_max': titles.PRESET_NAME_MAX_LEN,
        'players': players,
        'path': titles.data_dir(),
        'query_time': int(time.time()),
    }


def get_data() -> str:
    """返回可嵌入 ``<script id="titles-data">`` 的 JSON。"""
    data_json = json.dumps(_payload(), ensure_ascii=False, default=str)
    return data_json.replace('</script>', '<\\/script>')


def _join(label: str, items: list) -> str:
    return f'{label} ' + '、'.join(items) if items else ''


def _diff(before: dict | None, after: dict | None) -> str:
    """头衔审计详情:新增 / 移除 / 改颜色分类的头衔与装备变化;改名记作移除旧名、新增新名。"""
    b = {t['name']: t for t in (before or {}).get('titles', [])}
    a = {t['name']: t for t in (after or {}).get('titles', [])}
    parts = [_join('新增', [f'「{n}」' for n in a if n not in b]),
             _join('移除', [f'「{n}」' for n in b if n not in a]),
             _join('改样式', [f'「{n}」' for n in a if n in b and a[n] != b[n]])]
    eb, ea = (before or {}).get('equipped'), (after or {}).get('equipped')
    if eb != ea:
        parts.append(f'装备 {eb or "无"} → {ea or "无"}')
    return '；'.join(p for p in parts if p) or '无变化'


def _preset_label(p: dict) -> str:
    return f'「{p["name"]} {p["hex"]}」' if 'hex' in p else f'「{p["name"]}」'


def _presets_diff(before: list, after: list, origs: list) -> str:
    """颜色 / 分类审计详情:新增 / 删除 / 修改与排序变化;``origs[i]`` 是 ``after[i]`` 原来的名称,新增的为空。"""
    b = {p['name']: p for p in before}
    kept = [(b[o], p) for o, p in zip(origs, after) if o in b]
    parts = [_join('新增', [_preset_label(p) for o, p in zip(origs, after) if o not in b]),
             _join('删除', [_preset_label(p) for n, p in b.items() if n not in origs]),
             _join('修改', [f'{_preset_label(old)} → {_preset_label(p)}' for old, p in kept if old != p])]
    if [old['name'] for old, _ in kept] != [n for n in b if n in origs]:
        parts.append('调整排序')
    return '；'.join(p for p in parts if p) or '无变化'


def _title_label(t: dict) -> str:
    return f'「{t["name"]} · {t["color"]} · {t["category"]}」'


def _library_diff(before: list, rows: list) -> str:
    """头衔库审计详情:新增 / 删除 / 修改;``rows`` 是已通过校验的请求行(``orig`` 为原名,新增的为空)。"""
    b = {t['name']: t for t in before}
    pairs = [(r.get('orig') or '', {'name': r['name'].strip(), 'color': r['color'], 'category': r['category']}) for r in rows]
    origs = {o for o, _ in pairs}
    parts = [_join('新增', [_title_label(t) for o, t in pairs if o not in b]),
             _join('删除', [_title_label(t) for n, t in b.items() if n not in origs]),
             _join('修改', [f'{_title_label(b[o])} → {_title_label(t)}' for o, t in pairs if o in b and b[o] != t])]
    return '；'.join(p for p in parts if p) or '无变化'


async def _json_body(request) -> dict | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _fail(message: str, status: int = 400) -> 'web.Response':
    return web.json_response({'success': False, 'message': message}, status=status)


async def list_handler(request) -> 'web.Response':
    """``GET /api/ext/lgtbot/titles/list`` —— 重新读取全部数据(玩家在群里装备头衔后面板据此刷新)。"""
    return web.json_response({'success': True, **_payload()})


async def lookup_handler(request) -> 'web.Response':
    """``GET /api/ext/lgtbot/titles/lookup?openid=`` —— 添加玩家前按 OpenID 查昵称与头像。"""
    openid = str(request.query.get('openid') or '').strip()
    if not titles.valid_openid(openid):
        return _fail('OpenID 应为 32 位字母或数字')
    return web.json_response({'success': True, 'openid': openid, 'name': userinfo.get_name(openid),
                              'avatar': userinfo.avatar_url(openid), 'exists': titles.get(openid) is not None})


async def search_handler(request) -> 'web.Response':
    """``GET /api/ext/lgtbot/titles/search?q=`` —— 按昵称片段找玩家(最多 20 个),``exists`` 表示已有头衔记录。"""
    q = str(request.query.get('q') or '').strip()
    if not q:
        return _fail('请输入要搜索的昵称')
    if len(q) > 32:
        return _fail('昵称关键词最多 32 个字')
    records = titles.all_records()
    return web.json_response({'success': True, 'results': [dict(u, exists=u['openid'] in records) for u in userinfo.search_users(q, 20)]})


async def save_handler(request) -> 'web.Response':
    """``POST /api/ext/lgtbot/titles/save`` body ``{openid, titles: [{name, color, category}], equipped}``。

    整份覆盖该玩家持有的头衔与当前装备,头衔清空等于删除记录;校验不过时原样回 ``titles.set_record`` 的原因,不写盘;内容没变不记审计。
    """
    body = await _json_body(request)
    if body is None:
        return _fail('请求体不是 JSON 对象')
    openid = str(body.get('openid') or '').strip()
    before = titles.get(openid)
    err = titles.set_record(openid, body.get('titles'), body.get('equipped'))
    if err:
        return _fail(err)
    after = titles.get(openid)
    if after != before:
        audit.record('title', '编辑头衔', f'{titles.player_label(openid)}：{_diff(before, after)}')
    return web.json_response({'success': True, 'message': '已保存' if after else '头衔已清空，玩家记录已删除', **_payload()})


async def delete_handler(request) -> 'web.Response':
    """``POST /api/ext/lgtbot/titles/delete`` body ``{openid}`` —— 删除玩家的整条头衔记录。"""
    body = await _json_body(request)
    if body is None:
        return _fail('请求体不是 JSON 对象')
    openid = str(body.get('openid') or '').strip()
    rec = titles.get(openid)
    if rec is None or not titles.delete(openid):
        return _fail('记录不存在', 404)
    audit.record('title', '删除头衔记录', f'{titles.player_label(openid)}：共 {len(rec["titles"])} 个头衔')
    return web.json_response({'success': True, 'message': '已删除', **_payload()})


async def _presets_handler(request, key: str, label: str, read, write) -> 'web.Response':
    body = await _json_body(request)
    if body is None:
        return _fail('请求体不是 JSON 对象')
    rows = body.get(key)
    before = read()
    err = write(rows)
    if err:
        return _fail(err)
    after = read()
    if after != before:
        audit.record('title', f'编辑头衔{label}', _presets_diff(before, after, [r.get('orig') or '' for r in rows]))
    return web.json_response({'success': True, 'message': f'{label}已保存', **_payload()})


async def colors_handler(request) -> 'web.Response':
    """``POST /api/ext/lgtbot/titles/colors`` body ``{colors: [{orig?, name, hex}]}`` —— 按行序整份覆盖颜色表,不带 orig 的行是新增;内容没变不记审计。"""
    return await _presets_handler(request, 'colors', '颜色', titles.colors, titles.set_colors)


async def categories_handler(request) -> 'web.Response':
    """``POST /api/ext/lgtbot/titles/categories`` body ``{categories: [{orig?, name}]}`` —— 同颜色表。"""
    return await _presets_handler(request, 'categories', '分类', titles.categories, titles.set_categories)


async def library_handler(request) -> 'web.Response':
    """``POST /api/ext/lgtbot/titles/library`` body ``{library: [{orig?, name, color, category}]}`` —— 整份覆盖头衔库,不带 orig 的行是新增;内容没变不记审计。"""
    body = await _json_body(request)
    if body is None:
        return _fail('请求体不是 JSON 对象')
    rows = body.get('library')
    before = titles.library()
    err = titles.set_library(rows)
    if err:
        return _fail(err)
    diff = _library_diff(before, rows)
    if diff != '无变化':
        audit.record('title', '编辑头衔库', diff)
    return web.json_response({'success': True, 'message': '头衔库已保存', **_payload()})
