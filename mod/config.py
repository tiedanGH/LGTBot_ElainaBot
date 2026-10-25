#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""插件配置（data/config.yaml）—— 通过 ElainaBot 标准配置体系存取。

字段（按 yaml 中出现顺序）：
  · bind_bot_appid: str              绑定机器人 appid（留空 = 框架第一个 bot），其他 bot 的事件静默忽略
  · image_hosting: str               markdown 图片内嵌使用的图床名（默认 any = 自动依次尝试；留空 = 禁用）
  · refresh_wait_timeout: float      被动消息配额耗尽后等待刷新按钮的秒数
  · button_cooldown: float           回调按钮按用户共用的冷却秒数（刷新按钮除外），0 = 关闭
  · image_upload_dedup_ttl: float    同份图片重复上传去重 TTL（秒），0 = 关闭去重
  · active_push_daily_limit: int     单个群 / 用户每日主动消息条数上限（0 = 不限）
  · admin_uids: list[str]            LGTBot 内部管理员 openid 列表
  · notify_groups: list[str]         通知群 openid 列表（崩溃报告 / 熔断告警 / 自动重启说明向全部群主动推送）
  · sandbox_dm_users: list[str]      沙箱测试用户 openid 列表（私信主动直推；["all"] = 全员直推模式）
  · blocked_commands: list[str]      追加屏蔽指令（与 dispatcher 内置屏蔽表共同生效，命中的消息不转发给引擎）
  · text_at_as_mention: bool        全量群里「@机器人名称」文本前缀是否算 @
  · sponsor_enabled: bool            赞助功能总开关（默认 False = 完全隐藏赞助入口与「赞助支持」指令）
  · menu_game_buttons: list[str]     欢迎菜单的游戏快捷按钮列表（自动按每行 3 个排版）
"""

from __future__ import annotations

import json
import os
import re

import yaml

from core.base.logger import get_logger, PLUGIN
from . import state, boot

log = get_logger(PLUGIN, 'LGTBot')

# 与 buttons.DEFAULT_MENU_GAMES 同源;复制一份是为了让 ensure_config 写出的 config.yaml 模板直接呈现给用户。
_DEFAULT_MENU_GAMES = [
    '数字蜂巢', '天赋云巢', '炼金术士',
    '差值投标', '决胜五子', '彩虹奇兵',
]

# 字段顺序即 config.yaml 的字段顺序,也是面板「插件配置」两列逐行的排布(相邻两项一行)
DEFAULT_CONFIG = {
    'bind_bot_appid': '',
    'image_hosting': 'any',
    'refresh_wait_timeout': 15.0,
    'button_cooldown': 3.0,
    'image_upload_dedup_ttl': 60.0,
    'active_push_daily_limit': 1000,
    'admin_uids': [],
    'notify_groups': [],
    'sandbox_dm_users': [],
    'blocked_commands': [],
    'text_at_as_mention': True,
    'sponsor_enabled': False,
    'menu_game_buttons': list(_DEFAULT_MENU_GAMES),
}
CONFIG_COMMENTS = {
    'bind_bot_appid': '绑定机器人 appid（可在仪表盘配置）：绑定后仅处理该 bot 的消息，其他 bot 的事件静默忽略；留空 = 自动使用第一个 bot',
    'image_hosting': '游戏图片走 Markdown 内嵌时使用的图床：默认自动选择可用图床，单选提升效率。失败回退 msg_type=7；留空 = 不启用图床',
    'refresh_wait_timeout': '被动消息配额耗尽时，等待用户点击「刷新」按钮的最长秒数，超时后改走主动消息',
    'button_cooldown': '回调按钮的用户级冷却秒数（刷新按钮除外）：冷却中点任意回调按钮机器人不回复；设 0 = 关闭',
    'image_upload_dedup_ttl': '同份图片重复上传去重 TTL（秒）：并发请求会共享上传结果；设 0 = 关闭去重',
    'active_push_daily_limit': '单个群 / 用户每日主动消息条数上限（官方接口限制）：用满后退回「刷新按钮」被动机制，次日 0 点自动恢复；设 0 = 不限制',
    'admin_uids': '每行填写一个 LGTBot 超级管理员 OpenID：这些用户可执行 LGTBot 管理命令（如 %帮助 等）',
    'notify_groups': '每行填写一个 LGTBot 重要通知群 OpenID：引擎崩溃 / 引擎告警 / 自动重启 会同时推送给全部群。通知群需要主动消息权限',
    'sandbox_dm_users': '每行填写一个沙箱用户 OpenID：列表内用户私信走主动消息直推；填 all（仅此一项）= 全员直推模式',
    'blocked_commands': '每行填写一个屏蔽指令：命中的消息不再转发给引擎，用于化解与其他插件的指令冲突',
    'text_at_as_mention': '把复制粘贴出来的「@机器人名称」文本前缀也当成 @（仅影响全量群）；关闭则只认真实 @',
    'sponsor_enabled': '赞助功能总开关（默认关闭）：开启后启用「赞助支持」指令，更多功能 / 关于 / 更新公告 下添加「赞助支持」按钮。第三方部署请保持 false',
    'menu_game_buttons': '每行填写一个游戏名称：欢迎菜单里「游戏快捷开局」按钮列表，游戏名需与 /游戏列表 输出一致',
}
# 面板可视化编辑的字段名;说明文字直接用 CONFIG_COMMENTS
CONFIG_LABELS = {
    'bind_bot_appid': '绑定机器人',
    'image_hosting': '图床',
    'refresh_wait_timeout': '刷新等待（秒）',
    'button_cooldown': '按钮冷却（秒）',
    'image_upload_dedup_ttl': '图片去重（秒）',
    'active_push_daily_limit': '每日主动消息上限',
    'admin_uids': 'LGTBot 管理员',
    'notify_groups': '通知群',
    'sandbox_dm_users': '私信直推用户',
    'blocked_commands': '屏蔽指令',
    'text_at_as_mention': '文本 @ 视为 @',
    'sponsor_enabled': '赞助功能',
    'menu_game_buttons': '欢迎菜单快捷开局',
}


def _config_path() -> str:
    return os.path.join(boot.DATA_DIR, 'config.yaml')


def _ordered(keys) -> list:
    """按 DEFAULT_CONFIG 的字段顺序排序,本插件不读取的字段保持原相对顺序排在最后(sorted 是稳定排序)。"""
    rank = {k: i for i, k in enumerate(DEFAULT_CONFIG)}
    return sorted(keys, key=lambda k: rank.get(k, len(rank)))


def _yaml_scalar(value) -> str:
    """单个值的行内 yaml 写法。加不加引号交给 PyYAML;含换行的字符串改用 JSON 风格的双引号(同样是合法 yaml)保持单行。"""
    if isinstance(value, str) and ('\n' in value or '\r' in value):
        return json.dumps(value, ensure_ascii=False)
    text = yaml.safe_dump(value, allow_unicode=True, width=1 << 20, default_flow_style=True)
    # 顶层纯标量会带文档结束标记「...」
    return text[:-4].rstrip('\n') if text.endswith('...\n') else text.rstrip('\n')


def render_config_yaml(cfg: dict) -> str:
    """按 DEFAULT_CONFIG 的字段顺序渲染 config.yaml,注释取 CONFIG_COMMENTS;本插件不读取的字段排在最后。

    版式同 ensure_config 生成的模板:标量的注释跟在行尾,列表的注释单独一行写在上方。
    """
    lines: list = []
    for key in _ordered(cfg):
        value, comment, name = cfg[key], CONFIG_COMMENTS.get(key, ''), _yaml_scalar(key)
        if isinstance(value, (list, dict)) and value:
            if comment:
                lines.append(f'# {comment}')
            lines.append(f'{name}:')
            if isinstance(value, list):
                lines.extend(f'  - {_yaml_scalar(v)}' for v in value)
            else:
                lines.extend(f'  {_yaml_scalar(k)}: {_yaml_scalar(v)}' for k, v in value.items())
        elif isinstance(value, list):
            if comment:
                lines.append(f'# {comment}')
            lines.append(f'{name}: []')
        else:
            line = f'{name}: {_yaml_scalar(value)}'
            lines.append(f'{line}  # {comment}' if comment else line)
    return '\n'.join(lines) + '\n'


# 顶层字段所在行:顶格,且不是注释、列表项或文档标记
_TOP_KEY_RE = re.compile(r'^(?![\s#-])([^:]+?)\s*:(?=\s|$)')


def _config_blocks(text: str) -> tuple:
    """按顶层字段把 yaml 文本切块:每块 = 紧邻其上的注释与空行 + 字段行 + 其后的缩进行 / 列表项。

    返回 ``(首个字段之前的内容, [(字段名, 块文本), ...], 文件末尾游离的注释与空行)``。
    """
    head: list = []
    blocks: list = []
    pending: list = []
    for line in text.splitlines(keepends=True):
        if not line.endswith('\n'):
            line += '\n'
        m = _TOP_KEY_RE.match(line)
        if m:
            blocks.append((m.group(1), pending + [line]))
            pending = []
        elif not line.strip() or line.startswith('#'):
            pending.append(line)
        else:
            (blocks[-1][1] if blocks else head).extend(pending + [line])
            pending = []
    return ''.join(head), [(k, ''.join(ls)) for k, ls in blocks], ''.join(pending)


def order_config_yaml(text: str) -> str:
    """把 config.yaml 文本的顶层字段按 DEFAULT_CONFIG 顺序重排,本插件不读取的字段排在最后;已经有序时原样返回。

    每个字段连同紧邻其上的注释整块挪动,原有写法与手写注释都保留。挪动后解析结果对不上
    (带引号的字段名这类切不准块的写法)时改按 render_config_yaml 重新渲染。文本解析不了或根不是映射时原样返回,交给校验报错。
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return text
    if not isinstance(data, dict):
        return text
    want = _ordered(data)
    if list(data) == want:
        return text
    head, blocks, tail = _config_blocks(text)
    rank = {k: i for i, k in enumerate(want)}
    out = head + ''.join(b for _, b in sorted(blocks, key=lambda kb: rank.get(kb[0], len(rank)))) + tail
    try:
        again = yaml.safe_load(out)
    except yaml.YAMLError:
        again = None
    if isinstance(again, dict) and again == data and list(again) == want:
        return out
    return render_config_yaml(data)


def _migrate_legacy(data: dict) -> tuple:
    """旧字段 crash_notify_group(单个群)迁到 notify_groups:新字段没配群时填进去,配了就只删旧字段(运行时本来就不用它)。

    返回 ``(迁移后的新 dict, 迁移说明)``;没有旧字段时原样返回。
    """
    if 'crash_notify_group' not in data:
        return data, []
    out = {k: v for k, v in data.items() if k != 'crash_notify_group'}
    raw = data['crash_notify_group']
    legacy = '' if raw is None or isinstance(raw, bool) else str(raw).strip()
    groups = out.get('notify_groups')
    if legacy and not (isinstance(groups, list) and any(str(g).strip() for g in groups)):
        out['notify_groups'] = [legacy]
        return out, ['crash_notify_group → notify_groups']
    return out, ['crash_notify_group 已删除' + ('（notify_groups 已有配置，旧值未生效）' if legacy else '')]


def normalize_config_yaml(text: str) -> tuple:
    """迁移旧字段并按代码顺序整理 config.yaml 文本,返回 ``(整理后的文本, 迁移说明)``。

    迁移只删旧字段那一块、按需重写 notify_groups 那一块,其余字段原样保留;改完解析结果对不上时按 render_config_yaml 重新渲染。
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return text, []
    if not isinstance(data, dict):
        return text, []
    migrated, notes = _migrate_legacy(data)
    if notes:
        head, blocks, tail = _config_blocks(text)
        kept = [(k, b) for k, b in blocks if k != 'crash_notify_group']
        if migrated.get('notify_groups') != data.get('notify_groups'):
            block = render_config_yaml({'notify_groups': migrated['notify_groups']})
            at = next((i for i, (k, _) in enumerate(kept) if k == 'notify_groups'), None)
            if at is None:
                kept.append(('notify_groups', block))
            else:
                kept[at] = ('notify_groups', block)
        text = head + ''.join(b for _, b in kept) + tail
        try:
            again = yaml.safe_load(text)
        except yaml.YAMLError:
            again = None
        if again != migrated:
            text = render_config_yaml(migrated)
    return order_config_yaml(text), notes


def _write_atomic(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def _normalize_config_file() -> None:
    """加载时整理 config.yaml:迁移旧字段,字段顺序与 DEFAULT_CONFIG 不一致时按代码顺序排(ensure_config 把新增字段补在文件末尾)。"""
    path = _config_path()
    try:
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
        new, notes = normalize_config_yaml(text)
        if new != text:
            _write_atomic(path, new)
            if notes:
                log.info(f'config.yaml 已迁移旧字段：{"；".join(notes)}')
            if order_config_yaml(text) != text:
                log.info('config.yaml 字段顺序已按代码顺序整理')
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning(f'整理 config.yaml 失败: {e}')


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


# 解析后的引擎管理员 openid 列表(与传给 LGTBot_ElainaBot.start 的 admins 串同源),每次 @on_load 覆写。
# dispatcher 的「%中断」代理要用其中一个身份替换请求者 uid(引擎 HasAdmin 只认这个集合)。
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
            _normalize_config_file()
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

    # 每次 @on_load 都重新下发，改完 config.yaml 在 Web UI reload 插件即生效，无需重启进程
    _apply_runtime_tunables(cfg)

    return admins_str


def _apply_runtime_tunables(cfg: dict):
    """把 config.yaml 中的可调字段下发到对应运行时模块。

    下发顺序与 ``DEFAULT_CONFIG`` / yaml 中字段顺序一致(admin_uids 由
    ``load_plugin_config`` 处理,不在此函数内)。
    """
    from . import helpers, quota, uploader, buttons as _buttons, callbacks as _callbacks
    from . import dispatcher as _dispatcher

    cfg = _migrate_legacy(cfg)[0]

    # ── bind_bot_appid ────────────────────────────────────────────────────
    # 这里只落配置原值到 state,真正解析(在线校验 / 回退第一个)由 helpers.get_bound_appid() 每次调用惰性完成。
    raw_bind = cfg.get('bind_bot_appid', '')
    # appid 是纯数字,不带引号时 yaml 解析成 int —— 按字符串接受;若直接忽略,绑定会在重启后静默回退第一个 bot
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
    # 主动推送权限是 per-bot 的,绑定可能变化时把 helpers.can_push_group 的 TTL 缓存打掉
    helpers.invalidate_push_cache()

    # ── image_hosting ─────────────────────────────────────────────────────
    # 图床名单**动态**取自主框架模块 status();模块未加载时无法校验,保留原值 ——
    # 运行时 _do_upload 会按 status 早退。'any' 恒为合法值。
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

    # ── button_cooldown ───────────────────────────────────────────────────
    raw_cd = cfg.get('button_cooldown', 3.0)
    try:
        cd_f = float(raw_cd)
    except (TypeError, ValueError):
        log.warning(f'button_cooldown 应为数值，已忽略 (got {raw_cd!r})')
    else:
        if cd_f < 0:
            log.info(f'button_cooldown 负数已归 0 (关闭冷却) (原值 {cd_f})')
            cd_f = 0.0
        if _dispatcher.BUTTON_COOLDOWN != cd_f:
            log.info(f'button_cooldown: {_dispatcher.BUTTON_COOLDOWN}s → {cd_f}s'
                     + ('（关闭）' if cd_f == 0 else ''))
            _dispatcher.BUTTON_COOLDOWN = cd_f

    # ── image_upload_dedup_ttl ────────────────────────────────────────────
    # 0 = 关闭去重(每次都重新上传,仍保留 filename 唯一化避免 cos_key 冲突)。
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

    # ── active_push_daily_limit ───────────────────────────────────────────
    # 用满后该目标当日退回「刷新按钮」被动机制(callbacks._active_push_allowed),次日 0 点随日分桶自动重置。
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

    # ── notify_groups ─────────────────────────────────────────────────────
    # 崩溃报告 / 崩溃熔断告警 / 自动重启说明都向**全部**群推送(见 callbacks.broadcast_notify)。
    # 纯数字群 id 不带引号时 yaml 解析成 int,str() 统一收下(同 bind_bot_appid)。
    raw_groups = cfg.get('notify_groups', None)
    groups: list = []
    if isinstance(raw_groups, list):
        seen_g: set = set()
        for g in raw_groups:
            gid = str(g).strip()
            if gid and gid not in seen_g:
                seen_g.add(gid)
                groups.append(gid)
    elif raw_groups not in (None, ''):
        log.warning(f'notify_groups 应为列表，已忽略 (got {type(raw_groups).__name__})')
    new_groups = tuple(groups)
    if _callbacks.NOTIFY_GROUPS != new_groups:
        old = ', '.join(_callbacks.NOTIFY_GROUPS) or '(未配置)'
        new = ', '.join(new_groups) or '(未配置)'
        log.info(f'notify_groups: {old} → {new}')
        _callbacks.NOTIFY_GROUPS = new_groups

    # ── sandbox_dm_users ──────────────────────────────────────────────────
    # 列表内用户私信跳过被动配额直接主动推送;恰好只有一项 'all' → 全员直推模式(callbacks.DM_PUSH_ALL)。
    # 混入其他项时按普通白名单处理:字面 'all' 不匹配任何真实 openid,等于无效项。
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

    # ── blocked_commands ──────────────────────────────────────────────────
    # 与 dispatcher.BUILTIN_BLOCKED_COMMANDS 共同组成屏蔽表;斜杠原样保留,严格按配置匹配(见 _is_blocked_command)。
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

    # ── text_at_as_mention ────────────────────────────────────────────────
    # 只认真正的布尔值,'true' / 1 这类近似值按非法忽略并保留现值。
    raw_text_at = cfg.get('text_at_as_mention', None)
    if raw_text_at is None:
        text_at_on = True
    elif isinstance(raw_text_at, bool):
        text_at_on = raw_text_at
    else:
        log.warning(f'text_at_as_mention 应为布尔值 true / false，已忽略 (got {raw_text_at!r})')
        text_at_on = _dispatcher.TEXT_AT_AS_MENTION
    if _dispatcher.TEXT_AT_AS_MENTION != text_at_on:
        log.info(f'text_at_as_mention: {"开启" if text_at_on else "关闭"}'
                 f'（原 {"开启" if _dispatcher.TEXT_AT_AS_MENTION else "关闭"}）')
        _dispatcher.TEXT_AT_AS_MENTION = text_at_on

    # ── sponsor_enabled ───────────────────────────────────────────────────
    # **默认关闭**:插件市场里的第三方部署看不到任何收款引导。
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

    # ── menu_game_buttons ─────────────────────────────────────────────────
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


def persist_bind_bot_appid(appid: str) -> tuple[bool, str]:
    """把面板选择的绑定 bot 写回 ``data/config.yaml`` 并即时应用到运行时。

    用**行级文本替换**而非 yaml 全量重写 —— 保住 ensure_config 生成的注释和
    用户手写内容。key 不存在(老配置文件)时带注释补进来,再按代码顺序挪到该在的位置。
    写盘成功后同步 ``state.bind_bot_appid`` 并作废主动推送权限缓存。
    """
    from . import helpers

    appid = (appid or '').strip()
    path = _config_path()
    try:
        if os.path.isfile(path):
            with open(path, 'r', encoding='utf-8') as f:
                text = f.read()
        else:
            text = ''
        new_line = f"bind_bot_appid: '{appid}'"
        if re.search(r'(?m)^bind_bot_appid:', text):
            text = re.sub(r'(?m)^bind_bot_appid:.*$', new_line, text, count=1)
        else:
            if text and not text.endswith('\n'):
                text += '\n'
            text = order_config_yaml(text + f"# {CONFIG_COMMENTS['bind_bot_appid']}\n{new_line}\n")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
    except Exception as e:
        log.error(f'写回 bind_bot_appid 失败: {e}')
        return False, f'写入 config.yaml 失败: {e}'

    old = state.bind_bot_appid or '(自动第一个)'
    state.bind_bot_appid = appid
    log.info(f'🤖 [面板换绑] bind_bot_appid: {old} → {appid or "(自动第一个)"}')
    helpers.invalidate_push_cache()      # 权限 per-bot,换绑后旧结论全部作废
    resolved = helpers.get_bound_appid()
    perms = helpers._count_group_perms(resolved) if resolved else {'full': None, 'push': None}
    return True, (f'已绑定 {resolved or "(无可用 bot)"}；'
                  f'全量群 {perms.get("full") or 0} 个、'
                  f'可主动推送 {perms.get("push") or 0} 个')
