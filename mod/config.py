#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""插件配置（data/config.yaml）—— 通过 ElainaBot 标准配置体系存取。

字段（按 yaml 中出现顺序）：
  · bind_bot_appid: str              绑定机器人 appid（留空 = 框架第一个 bot），其他 bot 的事件静默忽略
  · admin_uids: list[str]            LGTBot 内部管理员 openid 列表
  · image_hosting: str               markdown 图片内嵌使用的图床名（默认 any = 自动依次尝试；留空 = 禁用）
  · refresh_wait_timeout: float      被动消息配额耗尽后等待刷新按钮的秒数
  · active_push_daily_limit: int     单个群 / 用户每日主动消息条数上限（0 = 不限）
  · image_upload_dedup_ttl: float    同份图片重复上传去重 TTL（秒），0 = 关闭去重
  · crash_notify_group: str          严重问题通知群 openid（崩溃时向此群主动推报告）
  · blocked_commands: list[str]      追加屏蔽指令（与 dispatcher 内置屏蔽表共同生效，命中的消息不转发给引擎）
  · sandbox_dm_users: list[str]      沙箱测试用户 openid 列表（私信主动直推；["all"] = 全员直推模式）
  · menu_game_buttons: list[str]     欢迎菜单的游戏快捷按钮列表（自动按每行 3 个排版）
  · sponsor_enabled: bool            赞助功能总开关（默认 False = 完全隐藏赞助入口与「赞助支持」指令）
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
    'bind_bot_appid': '',
    'admin_uids': [],
    'image_hosting': 'any',
    'refresh_wait_timeout': 15.0,
    'active_push_daily_limit': 1000,
    'image_upload_dedup_ttl': 60.0,
    'crash_notify_group': '',
    'blocked_commands': [],
    'sandbox_dm_users': [],
    'menu_game_buttons': list(_DEFAULT_MENU_GAMES),
    'sponsor_enabled': False,
}
CONFIG_COMMENTS = {
    'bind_bot_appid': '绑定机器人 appid（可在仪表盘配置）。留空 = 自动使用框架第一个 bot；绑定后仅处理该 bot 的消息，其他 bot 的事件静默忽略',
    'admin_uids': 'LGTBot 内部管理员 openid 列表，这些用户可执行 LGTBot 管理命令（如 %帮助 等）',
    'image_hosting': '游戏图片走 markdown 内嵌时使用的图床。默认 any 自动依次尝试全部可用图床；也可指定单个提升效率，可选值以主框架 image_hosting 模块为准：cos / bilibili / chatglm / xingye / nature / qq_file；留空 = 不启用图床。失败回退 msg_type=7',
    'refresh_wait_timeout': '被动消息配额耗尽时，等待用户点击「刷新」按钮的最长秒数，超时后改走主动消息',
    'active_push_daily_limit': '单个群 / 用户每日主动消息条数上限（QQ 官方接口限制，默认 1000）。用满后该群 / 用户当日退回「刷新按钮」被动机制，次日 0 点自动恢复；设 0 = 不限制',
    'image_upload_dedup_ttl': '同份图片重复上传去重 TTL（秒），并发请求会共享上传结果；设 0 关闭去重，负数自动归 0',
    'crash_notify_group': 'LGTBot 引擎严重问题通知群 openid，该群需要全量消息权限',
    'blocked_commands': '屏蔽指令列表：命中的消息不再转发给引擎，用于化解与其他插件的指令冲突',
    'sandbox_dm_users': '沙箱用户 openid 列表，列表内用户私信走主动消息直推；填 ["all"]（仅此一项）= 全员直推模式',
    'menu_game_buttons': '欢迎菜单里「游戏快捷开局」按钮列表，游戏名需与 /游戏列表 输出一致',
    'sponsor_enabled': '赞助功能总开关（默认 false）。开启后「更多功能」/「关于」/「更新公告」下会出现「赞助支持」按钮，并启用「赞助支持」指令；关闭时完全隐藏。第三方部署请保持 false',
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


# 解析后的引擎管理员 openid 列表(与传给 LGTBot_ElainaBot.start 的 admins 串同源)。
# 由 load_plugin_config 每次 @on_load 覆写;dispatcher 的「%中断」代理需要用其中
# 一个身份替换请求者 uid(引擎 HasAdmin 只认这个集合),故在此暴露给同包模块。
ADMIN_UIDS: tuple[str, ...] = ()


def load_plugin_config() -> str:
    """加载 / 创建 data/config.yaml，返回 LGTBot 引擎需要的逗号分隔 admin 字符串

    - 不存在则创建带注释的默认模板（此时 Web UI 才能看到该配置文件）
    - 存在但缺字段则自动补齐
    - admin_uids 字段非法时降级为空（不阻断启动）
    - 同时把解析结果写入模块级 ``ADMIN_UIDS``(供 dispatcher 的 %中断 代理用)
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

    global ADMIN_UIDS
    uids = cfg.get('admin_uids', [])
    if not isinstance(uids, list):
        log.warning('config.yaml 中 admin_uids 应为列表，已忽略')
        uids = []
    clean_uids = tuple(str(u).strip() for u in uids if str(u).strip())
    ADMIN_UIDS = clean_uids
    admins_str = ','.join(clean_uids)
    if admins_str:
        log.info(f'LGTBot 管理员配置：{len(clean_uids)} 人')

    # 把运行时可调字段套用到 quota 模块（每次 @on_load 都重新读取，
    # 改完 config.yaml 在 Web UI reload 插件即生效，无需重启进程）
    _apply_runtime_tunables(cfg)

    return admins_str


def _apply_runtime_tunables(cfg: dict):
    """把 config.yaml 中的可调字段下发到对应运行时模块。

    下发顺序与 ``DEFAULT_CONFIG`` / yaml 中字段顺序一致(admin_uids 由
    ``load_plugin_config`` 处理,不在此函数内):
      bind_bot_appid → image_hosting → refresh_wait_timeout →
      active_push_daily_limit → image_upload_dedup_ttl → crash_notify_group → blocked_commands →
      sandbox_dm_users → menu_game_buttons → sponsor_enabled
    """
    from . import helpers, quota, uploader, buttons as _buttons, callbacks as _callbacks
    from . import dispatcher as _dispatcher

    # ── bind_bot_appid ────────────────────────────────────────────────────
    # 绑定机器人:所有出站消息 / 数据读取固定走该 bot,其他 bot 的事件被 dispatcher 静默忽略。
    # 这里只落配置原值到 state,真正解析(在线校验 / 回退第一个)由 helpers.get_bound_appid() 每次调用惰性完成。
    raw_bind = cfg.get('bind_bot_appid', '')
    # appid 是纯数字,不带引号时 yaml 解析成 int(手工 / 框架通用编辑器写入的
    # 常见形态)—— 按字符串接受;若直接忽略,绑定会在重启后静默回退第一个 bot
    if isinstance(raw_bind, int) and not isinstance(raw_bind, bool):
        raw_bind = str(raw_bind)
    if not isinstance(raw_bind, str):
        log.warning(f'bind_bot_appid 应为字符串，已忽略 (got {type(raw_bind).__name__})')
        raw_bind = ''
    bind_appid = raw_bind.strip()
    if state.bind_bot_appid != bind_appid:
        old = state.bind_bot_appid or '(自动第一个)'
        new = bind_appid or '(自动第一个)'
        log.info(f'bind_bot_appid: {old} → {new}')
        state.bind_bot_appid = bind_appid
    # 每次加载都从绑定 bot 的 data.db 重载全量群集合。bot 未就绪时返回 -1,保留现状。
    seeded = helpers.seed_full_volume_groups_from_db()
    if seeded >= 0:
        log.info(f'全量群集合已从绑定 bot({helpers.get_bound_appid() or "?"}) 数据库载入: {seeded} 个')

    # ── image_hosting ─────────────────────────────────────────────────────
    # 图床名单**动态**取自主框架模块 status()(≥2.0.0 beds/ 自动发现);模块
    # 未加载时无法校验,保留原值 —— 运行时 _do_upload 会按 status 早退,
    # 可用性徽章也会如实显示 unknown / module_off。'any' 恒为合法值。
    backend = cfg.get('image_hosting', '')
    if not isinstance(backend, str):
        log.warning(f'image_hosting 应为字符串，已忽略 (got {backend!r})')
        backend = ''
    backend = backend.strip().lower()
    if backend and backend != 'any':
        hosting = uploader._get_hosting()
        valid: set = set()
        if hosting is not None and hasattr(hosting, 'status'):
            try:
                valid = set((hosting.status() or {}).keys())
            except Exception:
                valid = set()
        if valid and backend not in valid:
            log.warning(f'image_hosting 未知图床 {backend!r}，'
                        f'可选值：{sorted(valid)} 或 any；已禁用')
            backend = ''
    if uploader.SELECTED_BACKEND != backend:
        old = uploader.SELECTED_BACKEND or '(未启用)'
        new = backend or '(未启用)'
        log.info(f'image_hosting: {old} → {new}')
        uploader.SELECTED_BACKEND = backend

    # ── refresh_wait_timeout ──────────────────────────────────────────────
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

    # ── active_push_daily_limit ───────────────────────────────────────────
    # 单群 / 单用户每日主动消息上限(QQ 官方接口限制)。用满后该目标当日退回「刷新按钮」被动机制
    # (callbacks._active_push_allowed),次日 0 点随日分桶自动重置。0 = 不限制;非法值忽略并保留现值。
    limit = cfg.get('active_push_daily_limit', 1000)
    try:
        limit_i = int(limit)
    except (TypeError, ValueError):
        log.warning(f'active_push_daily_limit 应为整数，已忽略 (got {limit!r})')
    else:
        if limit_i < 0:
            log.warning(f'active_push_daily_limit 不能为负数，已忽略 (got {limit_i})')
        elif _callbacks.ACTIVE_PUSH_DAILY_LIMIT != limit_i:
            log.info(f'active_push_daily_limit: '
                     f'{_callbacks.ACTIVE_PUSH_DAILY_LIMIT} → {limit_i}'
                     + ('（不限制）' if limit_i == 0 else ''))
            _callbacks.ACTIVE_PUSH_DAILY_LIMIT = limit_i

    # ── image_upload_dedup_ttl ────────────────────────────────────────────
    # 同份图片重复上传去重 TTL。0 = 关闭去重(每次都重新上传,仍保留 filename
    # 唯一化避免 cos_key 冲突);负数自动归 0;非数值忽略保留旧值。
    raw_ttl = cfg.get('image_upload_dedup_ttl', 60.0)
    try:
        ttl_f = float(raw_ttl)
    except (TypeError, ValueError):
        log.warning(f'image_upload_dedup_ttl 应为数值，已忽略 (got {raw_ttl!r})')
    else:
        if ttl_f < 0:
            log.info(f'image_upload_dedup_ttl 负数已归 0 (关闭去重) (原值 {ttl_f})')
            ttl_f = 0.0
        if uploader.URL_CACHE_TTL != ttl_f:
            old_desc = '关闭' if uploader.URL_CACHE_TTL <= 0 else f'{uploader.URL_CACHE_TTL}s'
            new_desc = '关闭' if ttl_f <= 0 else f'{ttl_f}s'
            log.info(f'image_upload_dedup_ttl: {old_desc} → {new_desc}')
            uploader.URL_CACHE_TTL = ttl_f

    # ── crash_notify_group ────────────────────────────────────────────────
    # 引擎崩溃时往这里推送主动消息。非 str 或空白 → 视为未配置(空字符串);
    # callbacks.CRASH_NOTIFY_GROUP 在崩溃善后路径里读这个值,empty 跳过推送。
    raw_notify = cfg.get('crash_notify_group', '')
    # 同 bind_bot_appid:纯数字群 id 不带引号会解析成 int,按字符串接受
    if isinstance(raw_notify, int) and not isinstance(raw_notify, bool):
        raw_notify = str(raw_notify)
    if not isinstance(raw_notify, str):
        log.warning(f'crash_notify_group 应为字符串，已忽略 (got {type(raw_notify).__name__})')
        raw_notify = ''
    notify_group = raw_notify.strip()
    if _callbacks.CRASH_NOTIFY_GROUP != notify_group:
        old = _callbacks.CRASH_NOTIFY_GROUP or '(未配置)'
        new = notify_group or '(未配置)'
        log.info(f'crash_notify_group: {old} → {new}')
        _callbacks.CRASH_NOTIFY_GROUP = notify_group

    # ── blocked_commands ──────────────────────────────────────────────────
    # 追加屏蔽指令:与 dispatcher.BUILTIN_BLOCKED_COMMANDS 共同组成屏蔽表(两个 catch-all 派发前调 _is_blocked_command 检查)。
    # 规范化:strip + 去空 + 去重保序;严格按配置匹配。非法 / 缺失 → 空表(仅内置屏蔽项生效)。
    raw_blocked = cfg.get('blocked_commands', None)
    if isinstance(raw_blocked, list):
        seen: set = set()
        blocked: list = []
        for c in raw_blocked:
            cmd = str(c).strip()
            if cmd and cmd not in seen:
                seen.add(cmd)
                blocked.append(cmd)
        blocked_t = tuple(blocked)
    else:
        if raw_blocked is not None:
            log.warning(f'blocked_commands 应为字符串列表，已忽略 (got {type(raw_blocked).__name__})')
        blocked_t = ()
    if _dispatcher.BLOCKED_COMMANDS != blocked_t:
        log.info(f'blocked_commands: {len(_dispatcher.BLOCKED_COMMANDS)} → {len(blocked_t)} 条'
                 + (f' {list(blocked_t)}' if blocked_t else ''))
        _dispatcher.BLOCKED_COMMANDS = blocked_t

    # ── sandbox_dm_users ──────────────────────────────────────────────────
    # 列表内用户私信跳过被动配额,直接主动直推。非法 / 缺失 → 空集合(所有私信按正式环境规则:无有效 msg_id 直接丢弃)。
    # 特例:恰好只有一项 'all' → 全员直推模式(callbacks.DM_PUSH_ALL),对全部用户主动私信、不再丢弃。
    # 白名单老语义原样保留,官方收回权限时把配置改回白名单即可整体还原。
    # 混入其他项(如 ['all', openid])按普通白名单处理:字面 'all' 不匹配任何真实 openid,等于无效项。
    raw_sandbox = cfg.get('sandbox_dm_users', None)
    if isinstance(raw_sandbox, list):
        cleaned = [str(u).strip() for u in raw_sandbox if str(u).strip()]
    else:
        if raw_sandbox is not None:
            log.warning(f'sandbox_dm_users 应为字符串列表，已忽略 (got {type(raw_sandbox).__name__})')
        cleaned = []
    push_all = (cleaned == ['all'])
    sandbox_set = frozenset() if push_all else frozenset(cleaned)
    if _callbacks.DM_PUSH_ALL != push_all:
        log.info(f'sandbox_dm_users: 全员主动私信直推 {"开启 (all 模式)" if push_all else "关闭 (白名单模式)"}')
        _callbacks.DM_PUSH_ALL = push_all
    if _callbacks.SANDBOX_DM_USERS != sandbox_set:
        log.info(f'sandbox_dm_users: {len(_callbacks.SANDBOX_DM_USERS)} → {len(sandbox_set)} 人')
        _callbacks.SANDBOX_DM_USERS = sandbox_set

    # ── menu_game_buttons ─────────────────────────────────────────────────
    # 非法 / 缺失时回退到默认 6 个;
    # buttons.build_menu_buttons() 每次调用都读这个列表,所以下发后下一次回欢迎菜单即生效。
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

    # ── sponsor_enabled ───────────────────────────────────────────────────
    # 赞助功能总开关,**默认关闭**(插件市场里的第三方部署看不到任何收款引导)。
    # 关闭时:三个入口都不生成「赞助支持」按钮,「赞助支持」指令转发给引擎。
    # 只接受真正的布尔值 —— yaml 里写 'true' / 1 这类近似值一律按非法忽略并保留现值。
    raw_sponsor = cfg.get('sponsor_enabled', None)
    if raw_sponsor is None:
        sponsor_on = False
    elif isinstance(raw_sponsor, bool):
        sponsor_on = raw_sponsor
    else:
        log.warning(f'sponsor_enabled 应为布尔值 true / false，已忽略 (got {raw_sponsor!r})')
        sponsor_on = _buttons.SPONSOR_ENABLED
    if _buttons.SPONSOR_ENABLED != sponsor_on:
        log.info(f'sponsor_enabled: {"关闭" if not sponsor_on else "开启"}'
                 f'（原 {"开启" if _buttons.SPONSOR_ENABLED else "关闭"}）')
        _buttons.SPONSOR_ENABLED = sponsor_on


def persist_bind_bot_appid(appid: str) -> tuple[bool, str]:
    """把面板选择的绑定 bot 写回 ``data/config.yaml`` 并即时应用到运行时。

    用**行级文本替换**而非 yaml 全量重写 —— 保住 ensure_config 生成的注释和
    用户手写内容。key 不存在(老配置文件)时带注释追加到文件末尾。写盘成功后
    同步 ``state.bind_bot_appid`` + 从新绑定 bot 的 data.db 重载全量群集合。
    """
    import os
    import re as _re

    from . import helpers, boot as _boot

    appid = (appid or '').strip()
    path = os.path.join(_boot.DATA_DIR, 'config.yaml')
    try:
        if os.path.isfile(path):
            with open(path, 'r', encoding='utf-8') as f:
                text = f.read()
        else:
            text = ''
        new_line = f"bind_bot_appid: '{appid}'"
        if _re.search(r'(?m)^bind_bot_appid:', text):
            text = _re.sub(r'(?m)^bind_bot_appid:.*$', new_line, text, count=1)
        else:
            if text and not text.endswith('\n'):
                text += '\n'
            text += (f"# {CONFIG_COMMENTS['bind_bot_appid']}\n{new_line}\n")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
    except Exception as e:
        log.error(f'写回 bind_bot_appid 失败: {e}')
        return False, f'写入 config.yaml 失败: {e}'

    old = state.bind_bot_appid or '(自动第一个)'
    state.bind_bot_appid = appid
    log.info(f'🤖 [面板换绑] bind_bot_appid: {old} → {appid or "(自动第一个)"}')
    seeded = helpers.seed_full_volume_groups_from_db()
    if seeded >= 0:
        log.info(f'   全量群集合已从新绑定 bot 重载: {seeded} 个')
    resolved = helpers.get_bound_appid()
    return True, f'已绑定 {resolved or "(无可用 bot)"}；全量群 {max(seeded, 0)} 个'
