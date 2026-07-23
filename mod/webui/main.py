#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
LGTBot WebUI 入口 —— 注册「LGTBot 机器人」侧边栏页面并组装多标签布局。

骨架与拼装:
  · ``PAGE_KEY = 'lgtbot'``  唯一对用户可见的侧边栏入口
  · 多个 ``_HIDDEN_KEYS`` —— 内部 action 端点(重启 / 检查更新 / git pull /
    子模块 update / 清理各缓存 / 引擎编译启停),通过 wrap ``web_pages.get_pages``
    从侧边栏列表过滤;前端用 ``fetch(apiUrl(key))`` 触发，响应是单 HTML 片段
    (``<div id="msg">`` / ``<pre id="result">``),JS 用 DOMParser 解析。
  · 顶部标题栏右侧放「🔁 重启 LGTBot」按钮(整页通用，不属于任一标签)
  · 面板标签:仪表盘(``page_dashboard``)/ 指标面板(``page_metrics``)/
    配置管理(``page_config``)/ 引擎编译(``page_build``)/ 数据备份
    (``page_backup``)/ 消息日志(``page_logs``)/ 操作审计(``page_audit``)/
    用户数据(``page_users``);各自的 HTML / JS / 数据生成都委托给对应模块,
    本文件只做组装

HTML / CSS / JS 全部抽到 ``templates/`` 子目录的 ``main.html`` /
``main.css`` / ``main.js`` 中，本文件只保留 Python 逻辑;模板在 import 时
一次性读入并缓存，插件热重载会随之自动重新读盘。

每次 HTTP 请求 ``_render_html()`` 跑一次，把三个标签的 HTML/JS 片段和数据
JSON 都拼进同一份 HTML —— 这样无论用户当前在哪个标签上，刷新都能就地更新。

设计注意点:
  · ``_LazyHtmlDict.get('html')`` 返回 truthy 占位而非真调 provider,避免框架
    ``core.plugin.web_pages.get_page_html`` 的「先 truthy 后取值」双次访问
    把有副作用的 provider 跑两遍 → 例如 ``_render_restart`` 释放 C++ 引擎
    导致 tcache double-free
  · ``_ensure_get_pages_filters_hidden`` 一次性 wrap ``web_pages.get_pages``
    把所有 ``_HIDDEN_KEYS`` 从侧边栏列表里隐去，链式 wrap 不与其它插件冲突
  · ``_render_restart`` 内部延迟 import ``dispatcher``,断开循环依赖(本模块
    被 dispatcher 间接 import)
  · Dashboard 的「保存引擎配置」复用主框架 ``/api/config-file/save`` 端点
    (接受 plugins/ 下绝对路径，本插件 webui 不再为此自建端点)
"""

from __future__ import annotations

import html
import os

from core.plugin import web_pages
from .. import audit, metrics
from .. import state as _plugin_state
from . import (page_audit, page_backup, page_build, page_config, page_dashboard,
               page_logs, page_metrics, page_prebuilt, page_users)


PAGE_KEY = 'lgtbot'
RESTART_KEY = '__lgtbot_restart'
PLANNED_RESTART_KEY = '__lgtbot_planned_restart'

# Dashboard 的各 action 端点(JS 侧 DASH_KEYS 与此一一对应)
_DASH_CHECK_UPDATE_KEY      = '__lgtbot_dash_check_update'
_DASH_DO_UPDATE_KEY         = '__lgtbot_dash_do_update'           # 更新桥接层 (git pull --ff-only origin main)
_DASH_DO_UPDATE_FORCE_KEY   = '__lgtbot_dash_do_update_force'     # 强制更新桥接层 (git reset --hard origin/main)
_DASH_UPDATE_SUBMODULE_KEY  = '__lgtbot_dash_update_submodule'    # 更新 / 初始化 lgtbot 子模块
_DASH_INIT_REPO_KEY         = '__lgtbot_dash_init_repo'           # 市场用户:把插件目录初始化为 git 仓库
_DASH_CLEAR_AVATAR_KEY      = '__lgtbot_dash_clear_avatar'
_DASH_CLEAR_AVATAR_7D_KEY   = '__lgtbot_dash_clear_avatar_7d'
_DASH_CLEAR_GEN_KEY         = '__lgtbot_dash_clear_gen'
_DASH_CLEAR_GEN_7D_KEY      = '__lgtbot_dash_clear_gen_7d'
_DASH_CLEAR_MATCH_ALL_KEY   = '__lgtbot_dash_clear_match_all'
_DASH_CLEAR_MATCH_7D_KEY    = '__lgtbot_dash_clear_match_7d'
_DASH_RELOAD_CONFIG_KEY     = '__lgtbot_dash_reload_config'        # 插件配置热重载
_DASH_MATCHES_KEY           = '__lgtbot_dash_matches'             # 进行中对局列表(只读,前端实时轮询)

# 引擎编译标签的 action 端点(JS 侧 BUILD_KEYS 与此一一对应)
_BUILD_FULL_KEY    = '__lgtbot_dash_build_full'
_BUILD_INCR_KEY    = '__lgtbot_dash_build_incr'
_BUILD_BRIDGE_KEY  = '__lgtbot_dash_build_bridge'
_BUILD_LIST_KEY    = '__lgtbot_dash_build_list'
_BUILD_CUSTOM_KEY  = '__lgtbot_dash_build_custom'
_BUILD_KILL_KEY    = '__lgtbot_dash_build_kill'
_BUILD_CLEAN_KEY   = '__lgtbot_dash_build_clean'
_BUILD_REMOVE_KEY  = '__lgtbot_dash_build_remove'
_BUILD_LOG_KEY     = '__lgtbot_dash_build_log'

# 数据备份标签的 action 端点(JS 侧 BACKUP_KEYS 与此一一对应)
_BACKUP_CREATE_KEY  = '__lgtbot_backup_create'
_BACKUP_LIST_KEY    = '__lgtbot_backup_list'
# 操作审计标签的唯一 action 端点(只读刷新)
_AUDIT_LIST_KEY     = '__lgtbot_audit_list'
# 指标面板标签的唯一 action 端点(统一刷新:数据统计 + 运行指标 + 游戏数据)
_METRICS_REFRESH_KEY = '__lgtbot_metrics_refresh'
# 预编译部署标签的无参 action 端点(JS 侧 PREBUILT_KEYS 与此一一对应)
_PREBUILT_LIST_KEY            = '__lgtbot_prebuilt_list'        # 远程包列表(网络)
_PREBUILT_STATE_KEY           = '__lgtbot_prebuilt_state'       # 下载进度(轮询)
_PREBUILT_SWITCH_LOCAL_KEY    = '__lgtbot_prebuilt_switch_local'    # 切回本地编译
_PREBUILT_SWITCH_PREBUILT_KEY = '__lgtbot_prebuilt_switch_prebuilt'  # 切到预编译
# register_route 的 path,必须以 /api/ext/ 开头(core/plugin/web_pages.py 要求)
_BACKUP_RESTORE_ROUTE = '/api/ext/lgtbot/backup/restore'
_BACKUP_DELETE_ROUTE  = '/api/ext/lgtbot/backup/delete'
# 仪表盘「机器人绑定」换绑端点(要接 ?appid= 参数,同样走 register_route)
_BIND_BOT_ROUTE       = '/api/ext/lgtbot/bind-bot'
# 带 schema 校验的配置保存(config.yaml / lgtbot.json),POST body 传内容
_CONFIG_SAVE_ROUTE    = '/api/ext/lgtbot/config/save'
# 预编译下载(POST {name,mirror?},后台起 task)/ 镜像测速(POST {customs?})/ 记住下载镜像(POST {mirror})
_PREBUILT_DOWNLOAD_ROUTE    = '/api/ext/lgtbot/prebuilt/download'
_PREBUILT_TESTMIRRORS_ROUTE = '/api/ext/lgtbot/prebuilt/test-mirrors'
_PREBUILT_MIRROR_ROUTE      = '/api/ext/lgtbot/prebuilt/mirror'
_PREBUILT_UPLOAD_ROUTE      = '/api/ext/lgtbot/prebuilt/upload'   # 手动上传包(multipart)

# 所有「不该出现在侧边栏列表」的 key —— filter wrap 据此过滤
_HIDDEN_KEYS = frozenset({
    RESTART_KEY,
    PLANNED_RESTART_KEY,
    _DASH_CHECK_UPDATE_KEY,
    _DASH_DO_UPDATE_KEY,
    _DASH_DO_UPDATE_FORCE_KEY,
    _DASH_UPDATE_SUBMODULE_KEY,
    _DASH_INIT_REPO_KEY,
    _DASH_CLEAR_AVATAR_KEY,
    _DASH_CLEAR_AVATAR_7D_KEY,
    _DASH_CLEAR_GEN_KEY,
    _DASH_CLEAR_GEN_7D_KEY,
    _DASH_CLEAR_MATCH_ALL_KEY,
    _DASH_CLEAR_MATCH_7D_KEY,
    _DASH_RELOAD_CONFIG_KEY,
    _DASH_MATCHES_KEY,
    _BUILD_FULL_KEY,
    _BUILD_INCR_KEY,
    _BUILD_BRIDGE_KEY,
    _BUILD_LIST_KEY,
    _BUILD_CUSTOM_KEY,
    _BUILD_KILL_KEY,
    _BUILD_CLEAN_KEY,
    _BUILD_REMOVE_KEY,
    _BUILD_LOG_KEY,
    _BACKUP_CREATE_KEY,
    _BACKUP_LIST_KEY,
    _AUDIT_LIST_KEY,
    _METRICS_REFRESH_KEY,
    _PREBUILT_LIST_KEY,
    _PREBUILT_STATE_KEY,
    _PREBUILT_SWITCH_LOCAL_KEY,
    _PREBUILT_SWITCH_PREBUILT_KEY,
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
    """每次访问页面调用，生成最新 HTML(含四个标签的内容和数据)。"""
    return (_MAIN_HTML
            .replace('__MAIN_CSS__', _MAIN_CSS)
            .replace('__DASHBOARD_CSS__', page_dashboard.TAB_CSS)
            .replace('__METRICS_CSS__', page_metrics.TAB_CSS)
            .replace('__CONFIG_CSS__', page_config.TAB_CSS)
            .replace('__BUILD_CSS__', page_build.TAB_CSS)
            .replace('__LOGS_CSS__', page_logs.TAB_CSS)
            .replace('__USERS_CSS__', page_users.TAB_CSS)
            .replace('__BACKUP_CSS__', page_backup.TAB_CSS)
            .replace('__AUDIT_CSS__', page_audit.TAB_CSS)
            .replace('__PREBUILT_CSS__', page_prebuilt.TAB_CSS)
            .replace('__DASHBOARD_HTML__', page_dashboard.TAB_HTML)
            .replace('__METRICS_HTML__', page_metrics.TAB_HTML)
            .replace('__CONFIG_HTML__', page_config.TAB_HTML)
            .replace('__BUILD_HTML__', page_build.TAB_HTML)
            .replace('__LOGS_HTML__', page_logs.TAB_HTML)
            .replace('__USERS_HTML__', page_users.TAB_HTML)
            .replace('__BACKUP_HTML__', page_backup.TAB_HTML)
            .replace('__AUDIT_HTML__', page_audit.TAB_HTML)
            .replace('__PREBUILT_HTML__', page_prebuilt.TAB_HTML)
            .replace('__DASHBOARD_DATA__', page_dashboard.get_data())
            .replace('__METRICS_DATA__', page_metrics.get_data())
            .replace('__CONFIG_DATA__', page_config.get_data())
            .replace('__BUILD_DATA__', page_build.get_data())
            .replace('__LOG_DATA__', page_logs.get_data())
            .replace('__USER_DATA__', page_users.get_data())
            .replace('__BACKUP_DATA__', page_backup.get_data())
            .replace('__AUDIT_DATA__', page_audit.get_data())
            .replace('__PREBUILT_DATA__', page_prebuilt.get_data())
            .replace('__MAIN_JS__', _MAIN_JS)
            .replace('__DASHBOARD_JS__', page_dashboard.TAB_JS)
            .replace('__METRICS_JS__', page_metrics.TAB_JS)
            .replace('__CONFIG_JS__', page_config.TAB_JS)
            .replace('__BUILD_JS__', page_build.TAB_JS)
            .replace('__LOGS_JS__', page_logs.TAB_JS)
            .replace('__USERS_JS__', page_users.TAB_JS)
            .replace('__BACKUP_JS__', page_backup.TAB_JS)
            .replace('__AUDIT_JS__', page_audit.TAB_JS)
            .replace('__PREBUILT_JS__', page_prebuilt.TAB_JS)
            .replace('__PAGE_KEY__', PAGE_KEY)
            .replace('__RESTART_KEY__', RESTART_KEY)
            .replace('__PLANNED_RESTART_KEY__', PLANNED_RESTART_KEY)
            .replace('__PLANNED_ON__', '1' if _plugin_state.is_planned_restart() else '0'))


# ──────── 重启 action 端点(隐藏,仅按钮 GET) ──────────────────────────────
# 复用 dispatcher 里命令 /重启 路径的 check_and_prepare_restart +
# schedule_exec_after,两条入口语义完全一致 —— 包括「有活跃对局则拒绝」原子
# 预检和「0.5s 后 os.execv 整进程」的换进程动作,确保 C++ 二进制真正被新进程
# 重新 dlopen。

def _render_restart() -> str:
    """触发重启 + 返回单个 ``<div id="msg">…</div>``。

    只用做 JS 的回执片段:主页 main.js 的「🔁 重启 LGTBot」按钮 fetch 后用
    DOMParser 抠 #msg.textContent 显示成顶部横幅，完整 HTML 外壳(DOCTYPE /
    卡片 / hint) 都用不到。这个 key 又被 get_pages 过滤掉，用户也不会以独立
    页面身份打开它，所以连 <html><body> 都省了。
    """
    # 延迟 import 断开循环依赖(dispatcher 间接 import 本模块)
    from .. import dispatcher
    ok, msg = dispatcher.check_and_prepare_restart()
    # record 同步写盘,返回即已持久化 —— 先落审计与重启计数再调度 execv,换进程后记录仍在
    audit.record('restart', '重启 LGTBot', '' if ok else msg,
                 ok=ok, src=audit.SRC_PANEL)
    if ok:
        metrics.record_restart()
        dispatcher.schedule_exec_after(0.5)
    return f'<div id="msg">{html.escape(msg)}</div>'


def _render_planned_restart() -> str:
    """切换「计划重启」维护模式,与命令 /计划重启 共用 dispatcher 的 toggle。

    返回 ``#msg``(提示文案)+ ``#state``(1/0 新状态),main.js 据此更新
    顶栏按钮的文案与高亮。
    """
    from .. import dispatcher
    on, msg = dispatcher.toggle_planned_restart()
    audit.record('restart', '计划重启模式',
                 '已开启维护模式' if on else '已取消维护模式', src=audit.SRC_PANEL)
    return (f'<div id="msg">{html.escape(msg)}</div>'
            f'<div id="state">{1 if on else 0}</div>')


# ──────── LazyHtmlDict ──────────────────────────────────────────────────

class _LazyHtmlDict(dict):
    """字典子类:访问 'html' key 时调用 provider 动态生成;其他键正常字典行为。

    框架 ``get_page_html`` 内部对 'html' 字段先做 truthy 检查再取值。两次访问
    若都直传 provider,有副作用的 provider(此处 ``_render_restart`` 释放 C++ 引擎)
    会跑两遍 → 第二次 deref 已 freed 的 ``g_bot_core`` 触发 tcache double-free。
    本类 ``.get('html')`` 只返回 truthy 占位，真正生成留给 ``__getitem__``。
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
    """把 ``web_pages.get_pages`` 包一层，从侧边栏列表里过滤掉所有 ``_HIDDEN_KEYS``。

    幂等(``_lgtbot_wrapped`` 标记防重复包);链式(``_lgtbot_inner`` 保留对原
    函数的引用，与其它插件后续的 wrap 兼容)。
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
    """注册一个隐藏的 action 端点(不出现在侧边栏，只供 JS fetch 触发)。"""
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

    隐藏(被 filter wrap 屏蔽，不出现在侧边栏列表):
      · ``__lgtbot_restart`` —— 整页通用「重启 LGTBot」按钮
      · ``__lgtbot_dash_check_update``      —— Dashboard「检查更新」(同时查桥接层 + 子模块上游)
      · ``__lgtbot_dash_do_update``         —— Dashboard「更新桥接层」(git pull --ff-only origin main)
      · ``__lgtbot_dash_do_update_force``   —— Dashboard「强制更新」(git reset --hard origin/main,丢工作区)
      · ``__lgtbot_dash_update_submodule``  —— Dashboard「更新 / 初始化 lgtbot 子模块」
      · ``__lgtbot_dash_init_repo``         —— Dashboard 市场用户「把插件目录初始化为 git 仓库」
      · ``__lgtbot_dash_clear_avatar`` / ``_7d`` —— Dashboard 头像缓存「清理全部 / 保留 7 天」
      · ``__lgtbot_dash_clear_gen``    / ``_7d`` —— Dashboard 图片缓存「清理全部 / 保留 7 天」
      · ``__lgtbot_dash_clear_match_all`` / ``__lgtbot_dash_clear_match_7d``
        —— Dashboard 赛况缓存「清理全部 / 保留 7 天」
      · ``__lgtbot_dash_reload_config`` —— Dashboard「插件配置」热重载 yaml 到运行时
      · ``__lgtbot_dash_build_full / incr / bridge / list / custom / kill /
         clean / remove / log`` —— 引擎编译标签的 9 个动作 + 轮询端点
      · ``__lgtbot_backup_create / list`` —— 数据备份标签的创建 / 列表端点
      · ``__lgtbot_audit_list`` —— 操作审计标签的列表刷新(只读)
      · ``__lgtbot_metrics_refresh`` —— 指标面板的统一刷新(只读)
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
    # 计划重启(维护模式)切换端点
    _register_hidden_action(PLANNED_RESTART_KEY, _render_planned_restart)

    # Dashboard action 端点
    _register_hidden_action(_DASH_CHECK_UPDATE_KEY,      page_dashboard.render_check_update)
    _register_hidden_action(_DASH_DO_UPDATE_KEY,         page_dashboard.render_do_update)
    _register_hidden_action(_DASH_DO_UPDATE_FORCE_KEY,   page_dashboard.render_do_update_force)
    _register_hidden_action(_DASH_UPDATE_SUBMODULE_KEY,  page_dashboard.render_update_submodule)
    _register_hidden_action(_DASH_INIT_REPO_KEY,         page_dashboard.render_init_repo)
    _register_hidden_action(_DASH_CLEAR_AVATAR_KEY,      page_dashboard.render_clear_avatar)
    _register_hidden_action(_DASH_CLEAR_AVATAR_7D_KEY,   page_dashboard.render_clear_avatar_7d)
    _register_hidden_action(_DASH_CLEAR_GEN_KEY,         page_dashboard.render_clear_gen)
    _register_hidden_action(_DASH_CLEAR_GEN_7D_KEY,      page_dashboard.render_clear_gen_7d)
    _register_hidden_action(_DASH_CLEAR_MATCH_ALL_KEY,   page_dashboard.render_clear_match_all)
    _register_hidden_action(_DASH_CLEAR_MATCH_7D_KEY,    page_dashboard.render_clear_match_7d)
    # 注:_DASH_RELOAD_CONFIG_KEY 历史 key 不变(JS / 文档兼容),provider 已搬到 page_config
    _register_hidden_action(_DASH_RELOAD_CONFIG_KEY,     page_config.render_reload_config)
    # 进行中对局列表(只读,前端每几秒实时轮询)
    _register_hidden_action(_DASH_MATCHES_KEY,           page_dashboard.render_matches)

    # 引擎编译 action 端点
    _register_hidden_action(_BUILD_FULL_KEY,    page_build.render_build_full)
    _register_hidden_action(_BUILD_INCR_KEY,    page_build.render_build_incr)
    _register_hidden_action(_BUILD_BRIDGE_KEY,  page_build.render_build_bridge)
    _register_hidden_action(_BUILD_LIST_KEY,    page_build.render_build_list)
    _register_hidden_action(_BUILD_CUSTOM_KEY,  page_build.render_build_custom)
    _register_hidden_action(_BUILD_KILL_KEY,    page_build.render_build_kill)
    _register_hidden_action(_BUILD_CLEAN_KEY,   page_build.render_build_clean)
    _register_hidden_action(_BUILD_REMOVE_KEY,  page_build.render_build_remove)
    _register_hidden_action(_BUILD_LOG_KEY,     page_build.render_build_log)

    # 数据备份 action 端点
    # · create / list:无参,沿用 _register_hidden_action 的 fragment 协议
    # · restore / delete:要从 ?name= 拿参数,走 web_pages.register_route 真路由
    _register_hidden_action(_BACKUP_CREATE_KEY, page_backup.render_create)
    _register_hidden_action(_BACKUP_LIST_KEY,   page_backup.render_list)

    # 操作审计 action 端点(只读刷新;审计流不设清空 / 删除端点)
    _register_hidden_action(_AUDIT_LIST_KEY, page_audit.render_list)

    # 指标面板 action 端点(统一刷新:数据统计 + 运行指标 + 游戏数据)
    _register_hidden_action(_METRICS_REFRESH_KEY, page_metrics.render_refresh)

    # 预编译部署 action 端点(远程列表 / 进度轮询 / 本地·预编译切换)
    _register_hidden_action(_PREBUILT_LIST_KEY,            page_prebuilt.render_list)
    _register_hidden_action(_PREBUILT_STATE_KEY,           page_prebuilt.render_state)
    _register_hidden_action(_PREBUILT_SWITCH_LOCAL_KEY,    page_prebuilt.render_switch_local)
    _register_hidden_action(_PREBUILT_SWITCH_PREBUILT_KEY, page_prebuilt.render_switch_prebuilt)

    web_pages.register_route('GET', _BACKUP_RESTORE_ROUTE, page_backup.restore_handler, auth=True)
    web_pages.register_route('GET', _BACKUP_DELETE_ROUTE, page_backup.delete_handler, auth=True)
    web_pages.register_route('GET', _BIND_BOT_ROUTE, page_dashboard.bind_bot_handler, auth=True)
    web_pages.register_route('POST', _CONFIG_SAVE_ROUTE, page_config.save_config_handler, auth=True)
    # 预编译:下载 / 镜像测速 / 记住下载镜像(均 POST)
    web_pages.register_route('POST', _PREBUILT_DOWNLOAD_ROUTE, page_prebuilt.download_handler, auth=True)
    web_pages.register_route('POST', _PREBUILT_TESTMIRRORS_ROUTE, page_prebuilt.test_mirrors_handler, auth=True)
    web_pages.register_route('POST', _PREBUILT_MIRROR_ROUTE, page_prebuilt.mirror_select_handler, auth=True)
    web_pages.register_route('POST', _PREBUILT_UPLOAD_ROUTE, page_prebuilt.upload_handler, auth=True)

    _ensure_get_pages_filters_hidden()


def unregister():
    web_pages.unregister_page(PAGE_KEY)
    for k in _HIDDEN_KEYS:
        web_pages.unregister_page(k)
    # 注销 backup register_route 路由(其余 ``_register_hidden_action`` 注册的
    # action 端点都是放在 ``web_pages._registry``,前面循环已清掉;register_route
    # 是另一张表 ``web_pages._routes``,要显式 unregister)
    web_pages.unregister_route('GET', _BACKUP_RESTORE_ROUTE)
    web_pages.unregister_route('GET', _BACKUP_DELETE_ROUTE)
    web_pages.unregister_route('GET', _BIND_BOT_ROUTE)
    web_pages.unregister_route('POST', _CONFIG_SAVE_ROUTE)
    web_pages.unregister_route('POST', _PREBUILT_DOWNLOAD_ROUTE)
    web_pages.unregister_route('POST', _PREBUILT_TESTMIRRORS_ROUTE)
    web_pages.unregister_route('POST', _PREBUILT_MIRROR_ROUTE)
    web_pages.unregister_route('POST', _PREBUILT_UPLOAD_ROUTE)
    # get_pages 的 wrap 不主动 unwrap:其它插件可能后续也加了包装,贸然恢复会断链。
    # 留着的副作用仅是过滤一组已不存在的 key,无害。
