#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「📈 指标面板」标签 —— 数据统计 + 运行指标 + 游戏数据。

顶部独立标题栏放统一刷新按钮与查询时间;三个区共用一个刷新端点
(``render_refresh``,一次返回完整 payload):
  1. 📊 数据统计 —— 用户总数(user_cache.db)+ lgtbot.db 四个基础 COUNT
  2. 📈 运行指标 —— mod/metrics.py 的持久计数器(图床上传成功率 / 主动重启次数 / 配额压力 / 引擎崩溃重启)
  3. 🎮 游戏数据 —— lgtbot.db 只读统计(今日对局 / 活跃玩家 / 活跃群聊、
     游戏局数总榜、本周游戏榜、本周玩家参与榜、近 10 日趋势)+ 今日主动消息
     (群聊 / 私信,metrics 按日分桶计数)

纯只读展示:唯一 action 是刷新;运行指标为运维数据,不进全员指令(/数据统计 只输出游戏数据区内容)。
"""

from __future__ import annotations

import html as _html
import json
import os
import time

from .. import metrics, uploader, userdb

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')


def _load(name: str) -> str:
    with open(os.path.join(_TEMPLATE_DIR, name), 'r', encoding='utf-8') as f:
        return f.read()


TAB_HTML = _load('metrics/metrics.html')
TAB_CSS = _load('metrics/metrics.css')
TAB_JS = _load('metrics/metrics.js')


def _fragment(payload: dict) -> str:
    """把 ``payload`` 包成 ``<pre id="result">…</pre>`` —— 同 page_audit。"""
    body = json.dumps(payload, ensure_ascii=False, default=str)
    return f'<pre id="result">{_html.escape(body)}</pre>'


def _payload() -> dict:
    """首屏与统一刷新共用的完整 payload(三个区一次取齐)。"""
    game = metrics.query_game_stats()
    snap = metrics.snapshot()
    total = snap.get('upload_total') or 0
    fail = snap.get('upload_fail') or 0
    return {
        # ① 数据统计:user_cache 总数 + lgtbot 基础 COUNT
        'stats': {
            'user_cache_total': userdb.count_users(),
            'lgtbot_users': game.get('lgtbot_users'),
            'lgtbot_matches': game.get('lgtbot_matches'),
            'lgtbot_match_attendances': game.get('lgtbot_match_attendances'),
            'lgtbot_achievements': game.get('lgtbot_achievements'),
        },
        # ② 运行指标:计数器 + 服务端算好的成功率
        # (4 位小数,百万级样本下才能区分极高成功率:1e6 次里失败 1 次 = 99.9999%;
        #  总数 0 时 None → 前端显 —。round 后经 JSON 传输,末尾 0 天然省略:99.99900 → 99.999)
        'runtime': {
            **snap,
            'upload_rate': None if total == 0 else round((total - fail) / total * 100, 4),
            # 图床可用性:仅查配置 + 主框架 status(),不做真实上传探测
            'hosting': uploader.hosting_availability(),
            'metrics_path': metrics.METRICS_PATH,
        },
        # ③ 游戏数据(含 errors / available)+ 今日主动消息概况
        'game': game,
        'active_push': metrics.active_push_today(),
        'query_time': int(time.time()),
    }


def get_data() -> str:
    """返回可嵌入 ``<script id="metrics-data">`` 的 JSON。"""
    data_json = json.dumps(_payload(), ensure_ascii=False, default=str)
    return data_json.replace('</script>', '<\\/script>')


def render_refresh() -> str:
    """统一刷新端点(「🔄 刷新」按钮用)—— 三个区的数据一次全量返回。"""
    return _fragment({'success': True, **_payload()})
