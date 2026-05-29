#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""插件配置（data/config.yaml）—— 通过 ElainaBot 标准配置体系存取。

字段：
  · admin_uids: list[str]            LGTBot 内部管理员 openid 列表
  · refresh_wait_timeout: float      被动消息配额耗尽后等待刷新按钮的秒数
  · image_hosting: str               markdown 图片内嵌使用的单个图床名（留空 = 禁用）
  · menu_game_buttons: list[str]     欢迎菜单的游戏快捷按钮列表（自动按每行 3 个排版）
  · crash_notify_group: str          严重问题通知群 openid（崩溃时向此群主动推报告）
  · sandbox_dm_users: list[str]      沙箱测试用户 openid 列表（这些用户私信走主动消息直推）
"""

from __future__ import annotations

from core.base.logger import get_logger, PLUGIN
from . import state, boot

log = get_logger(PLUGIN, 'LGTBot')

# 默认游戏快捷按钮列表 —— 与 buttons.DEFAULT_MENU_GAMES 同源,这里复制一份是
# 为了让 ensure_config 写出 config.yaml 模板时直接呈现给用户。
_DEFAULT_MENU_GAMES = [
    '数字蜂巢', '天赋云巢', '炼金术士',
    '差值投标', '决胜五子', '彩虹奇兵',
]

DEFAULT_CONFIG = {
    'admin_uids': [],
    'refresh_wait_timeout': 15.0,
    'image_hosting': '',
    'menu_game_buttons': list(_DEFAULT_MENU_GAMES),
    'crash_notify_group': '',
    'sandbox_dm_users': [],
}
CONFIG_COMMENTS = {
    'admin_uids': 'LGTBot 内部管理员 openid 列表，这些用户可执行 LGTBot 管理命令（如 %帮助 等）',
    'refresh_wait_timeout': '被动消息配额（5 条）耗尽时，等待用户点击「刷新」按钮的最长秒数，超时后改走主动消息',
    'image_hosting': '游戏图片走 markdown 内嵌时使用的图床（可选值：cos / nature / bilibili / chatglm / ukaka / xingye）。上传失败回退 msg_type=7',
    'menu_game_buttons': '欢迎菜单里「游戏快捷开局」按钮列表，游戏名需与 /游戏列表 输出一致',
    'crash_notify_group': 'LGTBot 引擎严重问题通知群 openid，该群需要全量消息权限',
    'sandbox_dm_users': '沙箱测试用户 openid 列表，列表内用户私信走主动消息直推（仅沙箱私信可发主动消息）',
}


def _get_ctx():
    """三层降级取 PluginContext，保证 config.yaml 总能落地

      ① main.py 在 import 阶段捕获到的 state.plugin_ctx（最可靠）
      ② 通过 BotManager → PluginManager 反查（应对热重载等情形）
      ③ 直接用 plugin 目录构造一个 PluginContext（兜底）
    """
    if state.plugin_ctx is not None:
        return state.plugin_ctx

    try:
        from core.bot.manager import _bot_manager_ref
        if _bot_manager_ref is not None:
            pm = getattr(_bot_manager_ref, 'plugin_manager', None)
            if pm is not None:
                info = pm.get_plugin('LGTBot_ElainaBot') if hasattr(pm, 'get_plugin') else None
                if info and getattr(info, 'ctx', None):
                    return info.ctx
    except Exception:
        pass

    try:
        from core.plugin.context import PluginContext
        return PluginContext('LGTBot_ElainaBot', boot.PLUGIN_DIR)
    except Exception as e:
        log.warning(f'构造 PluginContext 失败: {e}')
        return None


def load_plugin_config() -> str:
    """加载 / 创建 data/config.yaml，返回 LGTBot 引擎需要的逗号分隔 admin 字符串

    - 不存在则创建带注释的默认模板（此时 Web UI 才能看到该配置文件）
    - 存在但缺字段则自动补齐
    - admin_uids 字段非法时降级为空（不阻断启动）
    """
    ctx = _get_ctx()
    try:
        if ctx is not None:
            cfg = ctx.ensure_config(DEFAULT_CONFIG, filename='config.yaml',
                                     comments=CONFIG_COMMENTS)
        else:
            log.warning('PluginContext 完全不可用，使用默认配置（Web UI 将看不到配置文件）')
            cfg = dict(DEFAULT_CONFIG)
    except Exception as e:
        log.warning(f'加载配置异常，使用默认值: {e}')
        cfg = dict(DEFAULT_CONFIG)

    uids = cfg.get('admin_uids', [])
    if not isinstance(uids, list):
        log.warning('config.yaml 中 admin_uids 应为列表，已忽略')
        uids = []
    admins_str = ','.join(str(u).strip() for u in uids if str(u).strip())
    if admins_str:
        log.info(f'LGTBot 管理员配置：{len(uids)} 人')

    # 把运行时可调字段套用到 quota 模块（每次 @on_load 都重新读取，
    # 改完 config.yaml 在 Web UI reload 插件即生效，无需重启进程）
    _apply_runtime_tunables(cfg)

    return admins_str


def _apply_runtime_tunables(cfg: dict):
    """把 config.yaml 中的可调字段下发到对应运行时模块"""
    from . import quota, uploader, buttons as _buttons, callbacks as _callbacks

    timeout = cfg.get('refresh_wait_timeout', 15.0)
    try:
        timeout_f = float(timeout)
    except (TypeError, ValueError):
        log.warning(f'refresh_wait_timeout 应为数值，已忽略 (got {timeout!r})')
    else:
        if timeout_f <= 0:
            log.warning(f'refresh_wait_timeout 应为正数，已忽略 (got {timeout_f})')
        elif quota.REFRESH_WAIT_TIMEOUT != timeout_f:
            log.info(f'refresh_wait_timeout: {quota.REFRESH_WAIT_TIMEOUT}s → {timeout_f}s')
            quota.REFRESH_WAIT_TIMEOUT = timeout_f

    backend = cfg.get('image_hosting', '')
    if not isinstance(backend, str):
        log.warning(f'image_hosting 应为字符串，已忽略 (got {backend!r})')
        backend = ''
    backend = backend.strip().lower()
    valid = {name for name, _ in uploader._UPLOADERS}
    if backend and backend not in valid:
        log.warning(f'image_hosting 未知图床 {backend!r}，可选值：{sorted(valid)}；已禁用')
        backend = ''
    if uploader.SELECTED_BACKEND != backend:
        old = uploader.SELECTED_BACKEND or '(未启用)'
        new = backend or '(未启用)'
        log.info(f'image_hosting: {old} → {new}')
        uploader.SELECTED_BACKEND = backend

    # 欢迎菜单的游戏快捷按钮列表 —— 非法 / 缺失时回退到默认 6 个,buttons.py
    # 的 build_menu_buttons() 每次调用都读这个列表,所以下发后下一次回欢迎菜单
    # 即生效。
    raw_games = cfg.get('menu_game_buttons', None)
    if raw_games is None:
        games = list(_buttons.DEFAULT_MENU_GAMES)
    elif isinstance(raw_games, list):
        games = [str(g).strip() for g in raw_games if str(g).strip()]
    else:
        log.warning(f'menu_game_buttons 应为字符串列表，已忽略 (got {type(raw_games).__name__})')
        games = list(_buttons.DEFAULT_MENU_GAMES)
    if _buttons.MENU_GAMES != games:
        log.info(f'menu_game_buttons: {len(_buttons.MENU_GAMES)} → {len(games)} 个游戏')
        _buttons.MENU_GAMES = games

    # 严重问题通知群 openid —— 引擎崩溃时往这里推送主动消息。
    # 非 str 或空白 → 视为未配置(空字符串);callbacks.CRASH_NOTIFY_GROUP
    # 在崩溃善后路径里读这个值,empty 跳过推送。
    raw_notify = cfg.get('crash_notify_group', '')
    if not isinstance(raw_notify, str):
        log.warning(f'crash_notify_group 应为字符串，已忽略 (got {type(raw_notify).__name__})')
        raw_notify = ''
    notify_group = raw_notify.strip()
    if _callbacks.CRASH_NOTIFY_GROUP != notify_group:
        old = _callbacks.CRASH_NOTIFY_GROUP or '(未配置)'
        new = notify_group or '(未配置)'
        log.info(f'crash_notify_group: {old} → {new}')
        _callbacks.CRASH_NOTIFY_GROUP = notify_group

    # 沙箱私信用户 openid 列表 —— 列表内用户私信跳过被动配额,直接主动直推。
    # 非法 / 缺失 → 空集合(所有私信按正式环境规则:无有效 msg_id 直接丢弃)。
    # callbacks.SANDBOX_DM_USERS 在 _send_text/image_quota_managed 里读取。
    raw_sandbox = cfg.get('sandbox_dm_users', None)
    if isinstance(raw_sandbox, list):
        sandbox_set = frozenset(
            str(u).strip() for u in raw_sandbox if str(u).strip())
    else:
        if raw_sandbox is not None:
            log.warning(f'sandbox_dm_users 应为字符串列表，已忽略 (got {type(raw_sandbox).__name__})')
        sandbox_set = frozenset()
    if _callbacks.SANDBOX_DM_USERS != sandbox_set:
        log.info(f'sandbox_dm_users: {len(_callbacks.SANDBOX_DM_USERS)} → {len(sandbox_set)} 人')
        _callbacks.SANDBOX_DM_USERS = sandbox_set
