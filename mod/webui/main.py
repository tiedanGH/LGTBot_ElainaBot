#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
LGTBot WebUI 入口 —— 注册「LGTBot 机器人」侧边栏页面并组装多标签布局。

骨架与拼装:
  · ``PAGE_KEY = 'lgtbot'``  唯一对用户可见的侧边栏入口
  · 多个 ``_HIDDEN_KEYS`` —— 内部 action 端点(重启 / 检查更新 / git pull /
    清理各缓存),通过 wrap ``web_pages.get_pages`` 从侧边栏列表过滤;前端
    用 ``fetch(apiUrl(key))`` 触发,响应是单 HTML 片段(``<div id="msg">`` /
    ``<pre id="result">``),JS 用 DOMParser 解析。
  · 顶部标题栏右侧放「🔁 重启 LGTBot」按钮(整页通用,不属于任一标签)
  · 三个标签:仪表盘(``page_dashboard``,最左侧)/ 消息日志(``page_logs``)/
    用户数据(``page_users``);各自的 HTML / JS / 数据生成都委托给对应模块,
    本文件只做组装

HTML / CSS / JS 全部抽到 ``templates/`` 子目录的 ``main.html`` /
``main.css`` / ``main.js`` 中,本文件只保留 Python 逻辑;模板在 import 时
一次性读入并缓存,插件热重载会随之自动重新读盘。

每次 HTTP 请求 ``_render_html()`` 跑一次,把三个标签的 HTML/JS 片段和数据
JSON 都拼进同一份 HTML —— 这样无论用户当前在哪个标签上,刷新都能就地更新。

设计注意点:
  · ``_LazyHtmlDict.get('html')`` 返回 truthy 占位而非真调 provider,避免框架
    ``core.plugin.web_pages.get_page_html`` 的「先 truthy 后取值」双次访问
    把有副作用的 provider 跑两遍 → 例如 ``_render_restart`` 释放 C++ 引擎
    导致 tcache double-free
  · ``_ensure_get_pages_filters_hidden`` 一次性 wrap ``web_pages.get_pages``
    把所有 ``_HIDDEN_KEYS`` 从侧边栏列表里隐去,链式 wrap 不与其它插件冲突
  · ``_render_restart`` 内部延迟 import ``dispatcher``,断开循环依赖(本模块
    被 dispatcher 间接 import)
  · Dashboard 的「保存引擎配置」复用主框架 ``/api/config-file/save`` 端点
    (接受 plugins/ 下绝对路径,本插件 webui 不再为此自建端点)
"""

from __future__ import annotations

import html
import os

from core.plugin import web_pages
from . import page_dashboard, page_logs, page_users


PAGE_KEY = 'lgtbot'
RESTART_KEY = '__lgtbot_restart'

# Dashboard 的各 action 端点(JS 侧 DASH_KEYS 与此一一对应)
_DASH_CHECK_UPDATE_KEY     = '__lgtbot_dash_check_update'
_DASH_DO_UPDATE_KEY        = '__lgtbot_dash_do_update'
_DASH_CLEAR_AVATAR_KEY     = '__lgtbot_dash_clear_avatar'
_DASH_CLEAR_AVATAR_7D_KEY  = '__lgtbot_dash_clear_avatar_7d'
_DASH_CLEAR_GEN_KEY        = '__lgtbot_dash_clear_gen'
_DASH_CLEAR_GEN_7D_KEY     = '__lgtbot_dash_clear_gen_7d'
_DASH_CLEAR_MATCH_ALL_KEY  = '__lgtbot_dash_clear_match_all'
_DASH_CLEAR_MATCH_7D_KEY   = '__lgtbot_dash_clear_match_7d'

# 所有「不该出现在侧边栏列表」的 key —— filter wrap 据此过滤
_HIDDEN_KEYS = frozenset({
    RESTART_KEY,
    _DASH_CHECK_UPDATE_KEY,
    _DASH_DO_UPDATE_KEY,
    _DASH_CLEAR_AVATAR_KEY,
    _DASH_CLEAR_AVATAR_7D_KEY,
    _DASH_CLEAR_GEN_KEY,
    _DASH_CLEAR_GEN_7D_KEY,
    _DASH_CLEAR_MATCH_ALL_KEY,
    _DASH_CLEAR_MATCH_7D_KEY,
})

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')


def _load(name: str) -> str:
    """读取 templates/ 下的纯文本模板。"""
    with open(os.path.join(_TEMPLATE_DIR, name), 'r', encoding='utf-8') as f:
        return f.read()


# import 时一次性读入,缓存为模块常量。热重载会重新执行 import → 重新读盘,
# 改完模板存盘后下次插件热重载就能看到新版本(无须重启进程)。
_MAIN_HTML = _load('main/main.html')
_MAIN_CSS = _load('main/main.css')
_MAIN_JS = _load('main/main.js')


def _render_html() -> str:
    """每次访问页面调用,生成最新 HTML(含三个标签的内容和数据)。"""
    return (_MAIN_HTML
            .replace('__MAIN_CSS__', _MAIN_CSS)
            .replace('__DASHBOARD_HTML__', page_dashboard.TAB_HTML)
            .replace('__LOGS_HTML__', page_logs.TAB_HTML)
            .replace('__USERS_HTML__', page_users.TAB_HTML)
            .replace('__DASHBOARD_DATA__', page_dashboard.get_data())
            .replace('__LOG_DATA__', page_logs.get_data())
            .replace('__USER_DATA__', page_users.get_data())
            .replace('__MAIN_JS__', _MAIN_JS)
            .replace('__DASHBOARD_JS__', page_dashboard.TAB_JS)
            .replace('__LOGS_JS__', page_logs.TAB_JS)
            .replace('__USERS_JS__', page_users.TAB_JS)
            .replace('__PAGE_KEY__', PAGE_KEY)
            .replace('__RESTART_KEY__', RESTART_KEY))


# ──────── 重启 action 端点(隐藏,仅按钮 GET) ──────────────────────────────
# 复用 dispatcher 里命令 /重启 路径的 check_and_prepare_restart +
# schedule_exec_after,两条入口语义完全一致 —— 包括「有活跃对局则拒绝」原子
# 预检和「0.5s 后 os.execv 整进程」的换进程动作,确保 C++ 二进制真正被新进程
# 重新 dlopen。

def _render_restart() -> str:
    """触发重启 + 返回单个 ``<div id="msg">…</div>``。

    只用做 JS 的回执片段:主页 main.js 的「🔁 重启 LGTBot」按钮 fetch 后用
    DOMParser 抠 #msg.textContent 显示成顶部横幅,完整 HTML 外壳(DOCTYPE /
    卡片 / hint) 都用不到。这个 key 又被 get_pages 过滤掉,用户也不会以独立
    页面身份打开它,所以连 <html><body> 都省了。
    """
    # 延迟 import 断开循环依赖(dispatcher 间接 import 本模块)
    from .. import dispatcher
    ok, msg = dispatcher.check_and_prepare_restart()
    if ok:
        dispatcher.schedule_exec_after(0.5)
    return f'<div id="msg">{html.escape(msg)}</div>'


# ──────── LazyHtmlDict ──────────────────────────────────────────────────

class _LazyHtmlDict(dict):
    """字典子类:访问 'html' key 时调用 provider 动态生成;其他键正常字典行为。

    框架 ``get_page_html`` 内部对 'html' 字段先做 truthy 检查再取值。两次访问
    若都直传 provider,有副作用的 provider(此处 ``_render_restart`` 释放 C++ 引擎)
    会跑两遍 → 第二次 deref 已 freed 的 ``g_bot_core`` 触发 tcache double-free。
    本类 ``.get('html')`` 只返回 truthy 占位,真正生成留给 ``__getitem__``。
    """

    def __init__(self, base: dict, html_provider):
        super().__init__(base)
        self._provider = html_provider

    def get(self, key, default=None):
        if key == 'html':
            return True
        return super().get(key, default)

    def __getitem__(self, key):
        if key == 'html':
            return self._provider()
        return super().__getitem__(key)


# ──────── 侧边栏过滤 wrap ────────────────────────────────────────────────

def _ensure_get_pages_filters_hidden():
    """把 ``web_pages.get_pages`` 包一层,从侧边栏列表里过滤掉所有 ``_HIDDEN_KEYS``。

    幂等(``_lgtbot_wrapped`` 标记防重复包);链式(``_lgtbot_inner`` 保留对原
    函数的引用,与其它插件后续的 wrap 兼容)。
    """
    cur = web_pages.get_pages
    if getattr(cur, '_lgtbot_wrapped', False):
        return
    inner = cur

    def filtered():
        return [p for p in inner() if p.get('key') not in _HIDDEN_KEYS]

    filtered._lgtbot_wrapped = True
    filtered._lgtbot_inner = inner
    web_pages.get_pages = filtered


# ──────── 注册 / 注销 ────────────────────────────────────────────────────

def _register_hidden_action(key: str, provider):
    """注册一个隐藏的 action 端点(不出现在侧边栏,只供 JS fetch 触发)。"""
    base = {
        'key': key,
        'label': '',
        'source': 'plugin',
        'source_name': 'LGTBot_ElainaBot',
        'html': '',
        'html_file': '',
        'icon': '',
    }
    web_pages._registry[key] = _LazyHtmlDict(base, provider)


def register():
    """在 ``web_pages._registry`` 中注册主页与所有 action 端点。

    可见:
      · ``lgtbot`` —— 「LGTBot 机器人」侧边栏入口(展示三标签内容)

    隐藏(被 filter wrap 屏蔽,不出现在侧边栏列表):
      · ``__lgtbot_restart`` —— 整页通用「重启 LGTBot」按钮
      · ``__lgtbot_dash_check_update`` —— Dashboard「检查更新」
      · ``__lgtbot_dash_do_update``    —— Dashboard「立即更新」(git pull)
      · ``__lgtbot_dash_clear_avatar`` / ``_7d`` —— Dashboard 头像缓存「清理全部 / 保留 7 天」
      · ``__lgtbot_dash_clear_gen``    / ``_7d`` —— Dashboard 图片缓存「清理全部 / 保留 7 天」
      · ``__lgtbot_dash_clear_match_all`` / ``__lgtbot_dash_clear_match_7d``
        —— Dashboard 赛况缓存「清理全部 / 保留 7 天」
    """
    # 主页(可见)
    log_base = {
        'key': PAGE_KEY,
        'label': 'LGTBot 机器人',
        'source': 'plugin',
        'source_name': 'LGTBot_ElainaBot',
        'html': '',          # 占位,会被 _LazyHtmlDict 覆盖
        'html_file': '',
        'icon': '',
    }
    web_pages._registry[PAGE_KEY] = _LazyHtmlDict(log_base, _render_html)

    # 重启 action 端点
    _register_hidden_action(RESTART_KEY, _render_restart)

    # Dashboard action 端点
    _register_hidden_action(_DASH_CHECK_UPDATE_KEY,     page_dashboard.render_check_update)
    _register_hidden_action(_DASH_DO_UPDATE_KEY,        page_dashboard.render_do_update)
    _register_hidden_action(_DASH_CLEAR_AVATAR_KEY,     page_dashboard.render_clear_avatar)
    _register_hidden_action(_DASH_CLEAR_AVATAR_7D_KEY,  page_dashboard.render_clear_avatar_7d)
    _register_hidden_action(_DASH_CLEAR_GEN_KEY,        page_dashboard.render_clear_gen)
    _register_hidden_action(_DASH_CLEAR_GEN_7D_KEY,     page_dashboard.render_clear_gen_7d)
    _register_hidden_action(_DASH_CLEAR_MATCH_ALL_KEY,  page_dashboard.render_clear_match_all)
    _register_hidden_action(_DASH_CLEAR_MATCH_7D_KEY,   page_dashboard.render_clear_match_7d)

    _ensure_get_pages_filters_hidden()


def unregister():
    web_pages.unregister_page(PAGE_KEY)
    for k in _HIDDEN_KEYS:
        web_pages.unregister_page(k)
    # get_pages 的 wrap 不主动 unwrap:其它插件可能后续也加了包装,贸然恢复会断链。
    # 留着的副作用仅是过滤一组已不存在的 key,无害。
