#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「💾 数据备份」标签 —— 列出 / 创建 / 恢复 / 删除 备份 zip。

★ 安全准则 ★
  · 没有任何「自动恢复 / 自动删除」UI。create / restore / delete 全部用户
    主动触发(restore / delete 还要 confirm);``backup.schedule_on_load_check()``
    是后端 @on_load 钩子里的自动任务，本模块只让用户可见它的产物 + 提供手动
    覆盖入口。
  · ``restore_handler`` 在解压前先调
    ``boot.LGTBot_ElainaBot.release_bot_if_not_processing_games()`` —— 有活跃
    游戏直接拒绝，避免数据库写一半被覆盖。

Python 侧职责:
  · ``TAB_HTML`` / ``TAB_JS`` 从 ``templates/backup/`` 加载
  · ``get_data()`` 返回首屏状态(目录路径、备份列表、容量统计)
  · ``render_create`` / ``render_list`` —— 无参 endpoint,沿用本插件的
    ``<pre id="result">JSON</pre>`` fragment 协议，由 ``webui/main.py`` 用
    ``_register_hidden_action`` 注册到 ``web_pages._registry`` 隐藏列表
  · ``restore_handler`` / ``delete_handler`` —— 带 ``?name=<zip_name>``
    query 参数的 HTTP route(``/api/ext/lgtbot/backup/{restore,delete}``),
    由 ``webui/main.py`` 用 ``web_pages.register_route`` 注册到 aiohttp 路由表
"""

from __future__ import annotations

import html as _html
import json
import os
import time

from aiohttp import web

from core.base.logger import get_logger, PLUGIN
from .. import backup, boot

log = get_logger(PLUGIN, 'LGTBot')

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')


def _load(name: str) -> str:
    with open(os.path.join(_TEMPLATE_DIR, name), 'r', encoding='utf-8') as f:
        return f.read()


TAB_HTML = _load('backup/backup.html')
TAB_CSS = _load('backup/backup.css')
TAB_JS = _load('backup/backup.js')


def _fragment(payload: dict) -> str:
    """把 ``payload`` 包成 ``<pre id="result">…</pre>`` —— 同 page_dashboard。"""
    body = json.dumps(payload, ensure_ascii=False, default=str)
    return f'<pre id="result">{_html.escape(body)}</pre>'


# ─────────────────────────────────────────────────────────────────────────
# 数据入口 —— 每次页面渲染调用一次
# ─────────────────────────────────────────────────────────────────────────

def get_data() -> str:
    """返回可嵌入 ``<script id="backup-data">`` 的 JSON。

    含字段:
      · backup_dir       —— 完整绝对路径(展示给用户验证 / 排查)
      · retention_count  —— 保留份数上限(常量)
      · auto_interval_h  —— 自动备份间隔小时数(常量)
      · query_time       —— 服务端拍照时刻
      · backups[]        —— 备份列表(name / size_bytes / mtime_ts)
      · total_size_bytes —— 所有备份占用合计
    """
    backups = backup.list_backups()
    total = sum(b['size_bytes'] for b in backups)
    payload = {
        'backup_dir': backup.BACKUP_DIR,
        'retention_count': backup.RETENTION_COUNT,
        'auto_interval_h': int(backup.AUTO_INTERVAL_S / 3600),
        'query_time': int(time.time()),
        'backups': backups,
        'total_size_bytes': total,
    }
    data_json = json.dumps(payload, ensure_ascii=False, default=str)
    return data_json.replace('</script>', '<\\/script>')


# ─────────────────────────────────────────────────────────────────────────
# 无参 action endpoints —— 沿用 _register_hidden_action 注册到 _registry
# ─────────────────────────────────────────────────────────────────────────

def render_create() -> str:
    """触发一次手动备份。后端实际跑 backup.create_backup(),返回结果。"""
    try:
        result = backup.create_backup()
    except Exception as e:
        log.error(f'手动备份异常: {e}')
        return _fragment({'success': False, 'message': f'备份异常: {e}'})
    return _fragment(result)


def render_list() -> str:
    """返回最新备份列表(用于「🔄 刷新列表」按钮)。"""
    backups = backup.list_backups()
    total = sum(b['size_bytes'] for b in backups)
    return _fragment({
        'success': True,
        'backups': backups,
        'total_size_bytes': total,
        'query_time': int(time.time()),
    })


# ─────────────────────────────────────────────────────────────────────────
# 带参 HTTP route handlers —— 用 web_pages.register_route 挂到
# /api/ext/lgtbot/backup/{restore,delete}, 从 request.query 拿 name
# ─────────────────────────────────────────────────────────────────────────

async def restore_handler(request: 'web.Request') -> 'web.Response':
    """``GET /api/ext/lgtbot/backup/restore?name=<zip>`` —— 恢复指定备份。

    预检:若 LGTBot 引擎正在运行 + 有活跃游戏 → 拒绝(避免数据库写一半被覆盖)。
    """
    name = (request.query.get('name') or '').strip()
    if not name:
        return web.json_response(
            {'success': False, 'message': '缺少备份文件名参数(?name=...)'},
            status=400,
        )

    # 引擎活跃游戏预检 —— 复用 boot 的原子接口
    try:
        if boot.LGTBOT_AVAILABLE and boot.is_engine_running():
            released = boot.LGTBot_ElainaBot.release_bot_if_not_processing_games()
            if not released:
                return web.json_response({
                    'success': False,
                    'message': ('检测到有进行中的游戏，拒绝恢复以保护数据。'
                                '请等所有对局结束后再试，或在「🔁 重启 LGTBot」'
                                '强制释放引擎后重试。'),
                })
            # release 成功 —— 引擎已停,可以安全恢复
    except Exception as e:
        log.warning(f'恢复前引擎状态预检异常，仍继续: {e}')

    try:
        result = backup.restore_backup(name)
    except Exception as e:
        log.error(f'恢复备份 {name} 异常: {e}')
        return web.json_response({'success': False, 'message': f'恢复异常: {e}'})
    return web.json_response(result)


async def delete_handler(request: 'web.Request') -> 'web.Response':
    """``GET /api/ext/lgtbot/backup/delete?name=<zip>`` —— 删除指定备份文件。"""
    name = (request.query.get('name') or '').strip()
    if not name:
        return web.json_response(
            {'success': False, 'message': '缺少备份文件名参数(?name=...)'},
            status=400,
        )
    try:
        result = backup.delete_backup(name)
    except Exception as e:
        log.error(f'删除备份 {name} 异常: {e}')
        return web.json_response({'success': False, 'message': f'删除异常: {e}'})
    return web.json_response(result)
