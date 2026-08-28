#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「🏷️ 昵称审核」标签 —— 总开关、存量批量扫描、违规记录处置。

Python 侧职责:
  · ``TAB_HTML`` / ``TAB_CSS`` / ``TAB_JS`` 从 ``templates/review/`` 加载
  · ``get_data()`` 首屏 payload;``render_*`` 是无参 action(隐藏 action 协议)
  · ``verdict_handler`` / ``settings_handler`` 要接参数,所以走 ``web_pages.register_route`` 真路由

设置存 ``data/review/settings.json``;接口地址与 API Key 由中央「AI LLM 服务」模块保管。
"""

from __future__ import annotations

import html as _html
import json
import os
import time

from core.base.logger import get_logger, PLUGIN

from .. import audit, nickname_review as review

log = get_logger(PLUGIN, 'LGTBot')

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')


def _load(name: str) -> str:
    with open(os.path.join(_TEMPLATE_DIR, name), 'r', encoding='utf-8') as f:
        return f.read()


TAB_HTML = _load('review/review.html')
TAB_CSS = _load('review/review.css')
TAB_JS = _load('review/review.js')

# 违规记录列表一次下发的条数
LIST_LIMIT = 200


def _fragment(payload: dict) -> str:
    body = json.dumps(payload, ensure_ascii=False, default=str)
    return f'<pre id="result">{_html.escape(body)}</pre>'


def _providers() -> list:
    """接口与其可选模型,供面板的两个下拉框。"""
    return [{'id': str(p.get('id') or ''), 'name': str(p.get('name') or p.get('id') or ''),
             'models': review.provider_models(p)} for p in review.enabled_providers()]


def _payload() -> dict:
    """首屏与刷新共用的完整 payload。"""
    llm = review.llm_status()
    return {
        **review.settings(),
        'llm_available': llm['available'],
        'llm_message': llm['message'],
        'providers': _providers(),
        'stats': review.stats(),
        'scan': review.scan_status(),
        'entries': review.list_flagged(limit=LIST_LIMIT),
        'allowed': review.list_allowed(limit=LIST_LIMIT),
        'list_limit': LIST_LIMIT,
        'db_path': review.DB_PATH,
        'query_time': int(time.time()),
    }


def get_data() -> str:
    """返回可嵌入 ``<script id="review-data">`` 的 JSON。"""
    data_json = json.dumps(_payload(), ensure_ascii=False, default=str)
    return data_json.replace('</script>', '<\\/script>')


# ─────────────────────────────────────────────────────────────────────────
# Action 端点(无参)
# ─────────────────────────────────────────────────────────────────────────

def render_refresh() -> str:
    return _fragment({'success': True, **_payload()})


def render_toggle() -> str:
    """翻转总开关并写回 config。"""
    want = not review.ENABLED
    if want:
        llm = review.llm_status()
        if not llm['available']:
            return _fragment({'success': False, **_payload(),
                              'message': f'❌ 无法启用：{llm["message"]}'})
    ok, err = review.save_settings(enabled=want)
    if not ok:
        audit.record('config', '昵称审核开关', err, ok=False)
        return _fragment({'success': False, **_payload(), 'message': f'❌ {err}'})
    n = review.stats()['flagged']
    log.warning(f'🏷️ [昵称审核] 已{"启用" if want else "关闭"}（已有违规结论 {n} 条）')
    audit.record('config', '昵称审核开关',
                 ('已启用' if want else '已关闭') + f'；已有违规结论 {n} 条')
    return _fragment({'success': True, **_payload(), 'message': (
        '✅ 昵称审核已启用：违规昵称将在对局图片 / 播报 / 排行榜 / 面板里显示为匿名'
        if want else '✅ 昵称审核已关闭：所有昵称按原样显示（已有结论保留）')})


def render_scan_start() -> str:
    ok, msg = review.scan_start()
    if ok:
        audit.record('config', '昵称批量扫描', '启动')
    return _fragment({'success': ok, **_payload(),
                      'message': ('✅ ' if ok else '❌ ') + msg})


def render_scan_pause() -> str:
    ok, msg = review.scan_pause()
    return _fragment({'success': ok, **_payload(), 'message': '⏸ ' + msg})


def render_scan_reset() -> str:
    ok, msg = review.scan_reset()
    audit.record('config', '昵称批量扫描', '重置游标')
    return _fragment({'success': ok, **_payload(), 'message': '✅ ' + msg})


# ─────────────────────────────────────────────────────────────────────────
# 带参端点 —— 单条记录的处置
# ─────────────────────────────────────────────────────────────────────────

async def verdict_handler(request) -> 'object':
    """``GET /api/ext/lgtbot/review/verdict?key=<归一化键>&op=<动作>``

    · ``acquit``   转人工白名单(安全 + 已处理),批量重扫不会再标回违规
    · ``revoke``   撤销白名单,回到待处理的违规记录
    · ``condemn``  从白名单直接判违规并标记已处理
    · ``handled`` / ``reopen``   只改「已处理」标记,判定不变
    """
    from aiohttp import web

    key = str(request.query.get('key') or '').strip()
    op = str(request.query.get('op') or '').strip()
    if not key or op not in ('acquit', 'handled', 'reopen', 'revoke', 'condemn'):
        return web.json_response({'success': False, 'message': '参数缺失或非法'},
                                 status=400)
    cur = review.get_verdict(key)
    if cur is None:
        return web.json_response({'success': False, 'message': '记录不存在'},
                                 status=404)
    ops = {
        'acquit': (review.acquit, '翻案转白名单'),
        'revoke': (review.revoke, '撤销白名单，回到待处理'),
        'condemn': (review.condemn, '从白名单判为违规'),
        'handled': (lambda k: review.set_handled(k, True), '标记已处理'),
        'reopen': (lambda k: review.set_handled(k, False), '撤销处理'),
    }
    fn, label = ops[op]
    ok = fn(key)
    detail = f'{label}（{cur["sample"]!r}）'
    audit.record('config', '昵称审核处置', detail, ok=ok)
    if not ok:
        return web.json_response({'success': False, 'message': '写入结论库失败'},
                                 status=500)
    return web.json_response({'success': True, 'message': '已更新',
                              'pending': review.pending_count()})


async def settings_handler(request) -> 'object':
    """``GET /api/ext/lgtbot/review/settings?provider_id=&model=&fail_closed=&batch_size=``"""
    from aiohttp import web

    q = request.query
    changes = {}
    if 'provider_id' in q:
        changes['provider_id'] = str(q.get('provider_id') or '').strip()
    if 'model' in q:
        changes['model'] = str(q.get('model') or '').strip()
    if 'fail_closed' in q:
        changes['fail_closed'] = str(q.get('fail_closed')).lower() in ('1', 'true')
    if 'batch_size' in q:
        try:
            changes['batch_size'] = min(100, max(1, int(q.get('batch_size'))))
        except (TypeError, ValueError):
            return web.json_response({'success': False, 'message': '批量条数应为 1-100 的整数'},
                                     status=400)
    if not changes:
        return web.json_response({'success': False, 'message': '没有要保存的项'}, status=400)
    ok, err = review.save_settings(**changes)
    audit.record('config', '昵称审核设置',
                 '、'.join(f'{k}={v}' for k, v in changes.items()), ok=ok)
    if not ok:
        return web.json_response({'success': False, 'message': err}, status=500)
    return web.json_response({'success': True, 'message': '设置已保存',
                              **review.settings()})
