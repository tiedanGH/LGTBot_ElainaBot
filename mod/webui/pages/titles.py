#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「头衔系统」标签 —— 头衔等级的新增 / 编辑,全部玩家的头衔库与当前装备的查看、编辑、切换与删除。

Python 侧职责:
  · ``TAB_HTML`` / ``TAB_CSS`` / ``TAB_JS`` 从 ``templates/titles/`` 加载
  · ``get_data()`` 首屏 payload(等级表 + 全部玩家记录,昵称 / 头像经 userinfo 门面查)
  · ``list_handler`` / ``lookup_handler`` / ``save_handler`` / ``delete_handler`` / ``levels_handler`` 五条真路由;改动类接口都回完整 payload,前端整页重绘

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
        'levels': titles.levels(),
        'name_max': titles.NAME_MAX_LEN,
        'level_name_max': titles.LEVEL_NAME_MAX_LEN,
        'max_titles': titles.MAX_TITLES,
        'max_levels': titles.MAX_LEVELS,
        'players': players,
        'path': titles._players.path,
        'query_time': int(time.time()),
    }


def get_data() -> str:
    """返回可嵌入 ``<script id="titles-data">`` 的 JSON。"""
    data_json = json.dumps(_payload(), ensure_ascii=False, default=str)
    return data_json.replace('</script>', '<\\/script>')


def _join(label: str, items: list) -> str:
    return f'{label} ' + '、'.join(items) if items else ''


def _diff(before: dict | None, after: dict | None) -> str:
    """头衔审计详情:新增 / 移除 / 改等级的头衔与装备变化;改名记作移除旧名、新增新名。"""
    b = {t['name']: t['color'] for t in (before or {}).get('titles', [])}
    a = {t['name']: t['color'] for t in (after or {}).get('titles', [])}
    parts = [_join('新增', [f'「{n}」' for n in a if n not in b]),
             _join('移除', [f'「{n}」' for n in b if n not in a]),
             _join('改等级', [f'「{n}」' for n in a if n in b and a[n] != b[n]])]
    eb, ea = (before or {}).get('equipped'), (after or {}).get('equipped')
    if eb != ea:
        parts.append(f'装备 {eb or "无"} → {ea or "无"}')
    return '；'.join(p for p in parts if p) or '无变化'


def _level_label(lv: dict) -> str:
    return f'「{lv["name"]} {lv["hex"]}」'


def _levels_diff(before: list, after: list) -> str:
    """等级审计详情:新增 / 删除 / 改名改色的等级。"""
    b = {lv['key']: lv for lv in before}
    a = {lv['key']: lv for lv in after}
    parts = [_join('新增', [_level_label(lv) for k, lv in a.items() if k not in b]),
             _join('删除', [_level_label(lv) for k, lv in b.items() if k not in a]),
             _join('修改', [f'{_level_label(b[k])} → {_level_label(lv)}' for k, lv in a.items() if k in b and b[k] != lv])]
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
    """``GET /api/ext/lgtbot/titles/list`` —— 重新读取全部数据(玩家在群里切换头衔后面板据此刷新)。"""
    return web.json_response({'success': True, **_payload()})


async def lookup_handler(request) -> 'web.Response':
    """``GET /api/ext/lgtbot/titles/lookup?openid=`` —— 添加玩家前按 OpenID 查昵称与头像。"""
    openid = str(request.query.get('openid') or '').strip()
    if not titles.valid_openid(openid):
        return _fail('OpenID 应为 32 位字母或数字')
    return web.json_response({'success': True, 'openid': openid, 'name': userinfo.get_name(openid),
                              'avatar': userinfo.avatar_url(openid), 'exists': titles.get(openid) is not None})


async def save_handler(request) -> 'web.Response':
    """``POST /api/ext/lgtbot/titles/save`` body ``{openid, titles: [{name, color}], equipped}``。

    整份覆盖该玩家的头衔库与当前装备,头衔库为空等于删除记录;校验不过时原样回 ``titles.set_record`` 的原因,不写盘;内容没变不记审计。
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
    return web.json_response({'success': True, 'message': '已保存' if after else '头衔库已清空，玩家记录已删除', **_payload()})


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


async def levels_handler(request) -> 'web.Response':
    """``POST /api/ext/lgtbot/titles/levels`` body ``{levels: [{key?, name, hex}]}`` —— 整份覆盖等级表,不带 key 的行是新增;内容没变不记审计。"""
    body = await _json_body(request)
    if body is None:
        return _fail('请求体不是 JSON 对象')
    before = titles.levels()
    err = titles.set_levels(body.get('levels'))
    if err:
        return _fail(err)
    after = titles.levels()
    if after != before:
        audit.record('title', '编辑头衔等级', _levels_diff(before, after))
    return web.json_response({'success': True, 'message': '等级已保存', **_payload()})
