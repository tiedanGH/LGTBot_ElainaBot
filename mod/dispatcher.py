#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""消息派发 + INTERACTION 处理（@handler 注册入口）

模块 import 时通过装饰器把 handler 注册进框架的 _pending_handlers 列表，
随后 PluginManager 收集到本插件名下。
"""

from __future__ import annotations
import os
import re
import sys
import time
import asyncio
import threading

from core.plugin.decorators import handler
from core.base.logger import get_logger, PLUGIN
from core.message.event import (
    GROUP_AT_MESSAGE_CREATE, GROUP_MESSAGE_CREATE, C2C_MESSAGE_CREATE,
    AT_MESSAGE_CREATE, DIRECT_MESSAGE_CREATE,
    INTERACTION_CREATE,
)

from . import state, quota, helpers, boot, buttons, uploader, userdb
from .webui import page_logs

log = get_logger(PLUGIN, 'LGTBot')

# 菜单 logo 文件路径（仓库内置）
# 用 ``_images/`` 而非 ``images/``: 加载器(``core/plugin/_loader.py:_import_plugin``)
# 跳过所有 ``_`` 开头的子目录,不会把它注册成 ``plugins.LGTBot_ElainaBot.images``
# 的伪 sub-package。纯资源目录这样标记最干净。
_MENU_LOGO_PATH = os.path.join(boot.PLUGIN_DIR, '_images', 'logo_transparent_colorful.png')


async def _resolve_menu_logo() -> dict | None:
    """读取 _images/logo_transparent_colorful.png 并通过图床上传 + 23h 缓存。

    任何异常都吞掉返回 None：菜单 logo 仅是装饰，不应阻断欢迎菜单回复。
    返回的字典含 ``url`` / ``width`` / ``height``，可直接拼 markdown。
    """
    try:
        if not os.path.isfile(_MENU_LOGO_PATH):
            return None
        with open(_MENU_LOGO_PATH, 'rb') as f:
            data = f.read()
        return await uploader.upload_image_cached(
            data, 'menu_logo.png', cache_key='menu:logo')
    except Exception as e:
        log.debug(f'菜单 logo 解析失败: {e}')
        return None


async def _send_welcome_menu(event) -> None:
    """发出欢迎菜单回复(logo + 按钮组)。三个入口共用本函数:
      · ``lgtbot_dispatch`` 空消息分支(单独 @bot)
      · ``lgtbot_welcome_menu`` 接到 ``菜单`` 文本
      · ``lgtbot_welcome_menu`` 接到 ``菜单`` callback INTERACTION

    本函数只负责发送 + 出向日志;**inbound 日志由 caller 写**,因为不同入口
    的日志标签需要区分(「空消息」「菜单按钮」「菜单文本」)。
    """
    try:
        logo = await _resolve_menu_logo()
        # 「logo/标题下方,真按钮上方」内容由 buttons.MENU_HEADER_EXTRA_MD 提供,
        # 图床有 / 无 logo 两条路径都拼上去 —— 「快速查看最近更新」超链接照常显示。
        if logo and logo.get('url'):
            md = (f'![logo #{logo["width"]}px #{logo["height"]}px]'
                  f'({logo["url"]})\n\n'
                  + buttons.MENU_TEXT_BODY
                  + buttons.MENU_HEADER_EXTRA_MD)
        else:
            md = (buttons.MENU_TEXT_HEADER
                  + buttons.MENU_TEXT_BODY
                  + buttons.MENU_HEADER_EXTRA_MD)
        await event.reply(md, buttons=buttons.build_menu_buttons(event.appid or ''))
        uid = event.user_id or ''
        gid = event.group_id or event.channel_id or ''
        page_logs.log_outgoing(gid or uid, not (event.is_group and gid),
                                 '[欢迎菜单]')
    except Exception as e:
        log.warning(f'菜单回复失败: {e}')

# 本插件监听的消息事件类型
# 加 GROUP_MESSAGE_CREATE 是为了适配「全量群」场景:某些 QQ 部署下,即便用户
# @了 bot,事件也会走 GROUP_MESSAGE_CREATE + is_at_self=True 而非 GROUP_AT_*。
# 但配合 ignore_at_check=True 后,框架也会把日常对话(is_at_self=False) 投递过来,
# handler 内必须再过一道 is_at_self 闸,见 lgtbot_dispatch 第一段。
_LGT_MSG_EVENTS = frozenset({
    GROUP_AT_MESSAGE_CREATE, GROUP_MESSAGE_CREATE, C2C_MESSAGE_CREATE,
    AT_MESSAGE_CREATE, DIRECT_MESSAGE_CREATE,
})


# ──────── 专属指令排除表 ──────────────────────────────────────────────────
# 这些指令都有专属 handler(priority > 0 且 block=True),不该再落进 LGTBot 引擎的 catch-all。
# 防线有两层:
#   ① 框架层:block=True —— 新版框架把**所有**命中的 handler 按优先级顺序全部
#     执行,只有 block=True 的 handler 命中后才拦截后续。
#   ② 插件层:catch-all(lgtbot_dispatch / lgtbot_interaction_dispatch)派发前
#     调 _is_exclusive_command 主动跳过 —— 即便框架链语义改变,引擎也不会重复收到这些指令。
# pattern 常量与对应 @handler 装饰器共用,新增专属指令时在此加常量即可,
# 两处不会漂移。
_P_QUERY_ID = r'^(?i:查询id)\s+(\S+)$'
_P_MENU     = r'^/?菜单$'
_P_MORE     = r'^/?更多功能$'
_P_NOTICE   = r'^/?更新公告$'
_P_TROUBLE  = r'^/?疑难解答$'
_P_ABOUT    = r'^/?关于$'
_P_RESTART  = r'^重启$'

_EXCLUSIVE_RES = tuple(re.compile(p, re.DOTALL) for p in (
    _P_QUERY_ID, _P_MENU, _P_MORE, _P_NOTICE, _P_TROUBLE, _P_ABOUT, _P_RESTART,
))


def _is_exclusive_command(text: str) -> bool:
    """content 是否命中任一专属指令(含框架「加/去斜杠」重试的变体)。"""
    if not text:
        return False
    alt = text[1:] if text.startswith('/') else '/' + text
    return any(p.search(text) or p.search(alt) for p in _EXCLUSIVE_RES)


# ──────── 用户查询(所有人可用) ────────────────────────────────────────────

@handler(_P_QUERY_ID,
         name='查询用户',
         desc='查缓存中的昵称 / 头像 / 上次活跃',
         priority=50,
         block=True,
         event_types=_LGT_MSG_EVENTS)
async def lgtbot_query_user(event, match):
    """以输入的 openid 在用户缓存 DB 中查单条记录,markdown 输出。

    - openid 长度必须严格 32 位,否则只回报错不查库
    - 查不到 → 「查询失败:该 ID 不存在」
    - 命中 → ``## 查询成功`` + 一行内嵌头像 + 昵称 + 一行上次活跃时间戳
    """
    target = match.group(1).strip()
    if len(target) != 32:
        await event.reply(f'❌ ID 长度不正确（需 32 位，当前 {len(target)} 位）')
        return

    user = userdb.get_user(target)
    if user is None:
        await event.reply('❌ 查询失败：该 ID 不存在')
        return

    name = user.get('name') or '[未知昵称]'
    avatar = user.get('avatar') or ''
    last_seen = user.get('last_seen', 0) or 0
    time_str = (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_seen))
                if last_seen else '从未活跃')
    # 内嵌头像 —— QQ markdown 用 `#WIDTHpx #HEIGHTpx` alt-text 控制尺寸,
    # 40px 是「小头像」典型尺寸,行内 + 昵称 一目了然
    avatar_md = (f'![头像 #40px #40px]({avatar})' if avatar else '[未知]')
    md = (
        '## ✅ 查询成功\n'
        '\n'
        f'{avatar_md} | **{name}**\n'
        '\n'
        f'```上次活跃\n'
        f'{time_str}\n'
        f'```'
    )
    await event.reply(md)


# ──────── 「📋 更多功能」子菜单 + 更新公告(所有人可用) ─────────────────────
# 这两个 handler 都同时监听消息事件和 INTERACTION_CREATE,因为「更多功能」
# 和「更新公告」按钮在 ``buttons.py`` 里是 type=2:
#   · 默认行为:点击 → 文字回填到输入框 → 用户手动发送 → 走消息事件路径
#   · 若 bot.yaml 配了 ``button_enter_to_send: true``:type=2 被框架转 type=1
#     → 点击直接触发 INTERACTION → 走 INTERACTION_CREATE 路径
# 一个 handler 接两种事件,优先级 50 高于 ``lgtbot_interaction_dispatch``(-100),
# 抢在被派回 LGTBot 引擎之前响应,免得引擎不认得这俩元指令报「未预料的元指令」。

_UPDATE_NOTICE_PATH = os.path.join(boot.DATA_DIR, 'update_notice.txt')
# 首次访问时写入的默认内容
_DEFAULT_UPDATE_NOTICE = '暂无更新公告'

# 「重要更新」置顶区域 —— 与「更新公告」配合显示在常规代码块的**上方**。
# 与 update_notice 的关键区别:
#   · 内容为空 / 文件不存在 → 完全不渲染该区块(避免出现空白小标题)
#   · 不自动写入默认占位 —— 这是"按需添加的置顶提示",平时应处于"不存在"状态
_IMPORTANT_UPDATE_PATH = os.path.join(boot.DATA_DIR, 'important_update.txt')

_TROUBLESHOOTING_PATH = os.path.join(boot.DATA_DIR, 'troubleshooting.txt')
# 首次访问时写入的默认 Q&A
_DEFAULT_TROUBLESHOOTING = (
    'Q：为什么 bot 只回了几条就停了？\n'
    'A：QQ 协议限制 —— 普通群里 bot 对同一条消息最多回 5 条，且仅 5 分钟内有效；私信场景同理。\n'
    '   ▸ 群主可@bot发送「全量申请」，并根据提示开通全量权限，bot 在全量群可推送主动消息，不受限制\n'
    '   ▸ 私信主动消息有名额限制，目前只内测对部分游戏开放，如有需求请联系开发者铁蛋\n'
    '\n'
    'Q：游戏中部分图片显示不出来/加载失败？\n'
    'A：极少数情况下图床偶发故障，导致已上传图片访问失败。可在游戏中发送「赛况」查看当前游戏进展'
)


def _read_txt_with_default(path: str, default: str, label: str) -> str:
    """实时读取 ``path`` 内容,文件不存在时用 ``default`` 创建并返回。

    · 读取成功但内容为空(全空白)→ 按 ``default`` 兜底
    · 任何异常吞掉返回 ``default``,handler 不会因文件 IO 问题崩
    · 每次调用都重新打开文件 —— 这就是「热更新」:管理员直接编辑 txt,
      下条命令就拿到新内容,无需重启进程
    """
    try:
        if not os.path.isfile(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(default)
            return default
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read().rstrip()
        return content or default
    except Exception as e:
        log.warning(f'读取 {label} 异常: {e}')
        return default


def _read_update_notice() -> str:
    return _read_txt_with_default(
        _UPDATE_NOTICE_PATH, _DEFAULT_UPDATE_NOTICE, 'update_notice.txt')


def _read_important_update() -> str:
    """读 ``important_update.txt`` 并 strip。文件缺失 / 内容全空白 → 返回 ``''``。

    跟 ``_read_txt_with_default`` 不同:**不自动创建**也不返回默认值 —— 这是
    "按需置顶提示",不该有自动写入的占位文件污染 ``data/``。空返回让
    ``lgtbot_update_notice`` 把整个「重要更新」区块跳过,不留空标题。
    """
    if not os.path.isfile(_IMPORTANT_UPDATE_PATH):
        return ''
    try:
        with open(_IMPORTANT_UPDATE_PATH, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception as e:
        log.warning(f'读取 important_update.txt 失败: {e}')
        return ''


def _read_troubleshooting() -> str:
    return _read_txt_with_default(
        _TROUBLESHOOTING_PATH, _DEFAULT_TROUBLESHOOTING, 'troubleshooting.txt')


@handler(_P_MENU,
         name='LGTBot 欢迎菜单',
         desc='触发欢迎菜单 (等同于单独 @bot)',
         priority=50,
         block=True,
         event_types=_LGT_MSG_EVENTS | {INTERACTION_CREATE})
async def lgtbot_welcome_menu(event, match):
    """收到「菜单」(文本或按钮)→ 回欢迎菜单。

    与 lgtbot_dispatch 空消息分支共享 ``_send_welcome_menu``;入口日志在这里
    打,标签区分文本 / 按钮路径,方便排查。state.started=False(引擎崩溃 30s
    窗口)时静默不回 —— 菜单按钮指向的命令都依赖引擎,提前发出去也没用。
    """
    if event.is_interaction:
        try:
            await event.ack_interaction(code=0)
        except Exception:
            pass
    if not state.started:
        return
    uid = event.user_id or ''
    gid = event.group_id or event.channel_id or ''
    label = '(菜单按钮)' if event.is_interaction else '菜单'
    page_logs.log_incoming(uid, gid if event.is_group else '', label)
    await _send_welcome_menu(event)


@handler(_P_MORE,
         name='LGTBot 更多功能',
         desc='展示「更多功能」子菜单',
         priority=50,
         block=True,
         event_types=_LGT_MSG_EVENTS | {INTERACTION_CREATE})
async def lgtbot_more_features(event, match):
    """收到「更多功能」(文本或按钮)→ 回一条带子菜单按钮的 markdown。

    INTERACTION 路径要先 ack 抑制客户端 3s「请求超时」toast。
    """
    if event.is_interaction:
        try:
            await event.ack_interaction(code=0)
        except Exception:
            pass
    md = (
        '## 🧩 更多功能\n'
        '\n'
        '---\n'
        '\n'
        f'{buttons.cmd_input("/排行大图 本群", "🏆 本群排行")} | {buttons.cmd_input("/战绩", "📊 我的战绩")}\n'
        f'{buttons.cmd_input("/随机游戏", "🎲 随机游戏")} | {buttons.cmd_input("/规则", "📖 查询规则")}\n'
        f'{buttons.cmd_input("/游戏信息", "查看本群房间")} | {buttons.cmd_input("/赛事列表", "查询房间列表")}\n'
    )
    await event.reply(md, buttons=buttons.build_more_features_buttons())


@handler(_P_NOTICE,
         name='LGTBot 更新公告',
         desc='读取 data/update_notice.txt 实时返回公告内容',
         priority=50,
         block=True,
         event_types=_LGT_MSG_EVENTS | {INTERACTION_CREATE})
async def lgtbot_update_notice(event, match):
    """收到「更新公告」(文本或按钮)→ 把 txt 文件内容包在代码块里 reply。

    若 ``important_update.txt`` 非空,在常规「公告详情」代码块上方再渲染一个
    ``重要更新`` 代码块作为置顶提示;为空时该区块不出现,UI 与旧版完全一致。
    """
    if event.is_interaction:
        try:
            await event.ack_interaction(code=0)
        except Exception:
            pass
    notice = _read_update_notice()
    important = _read_important_update()
    # 代码块包裹 —— 保留换行 / 缩进 / 特殊字符原样显示,管理员可以贴格式化文本。
    # 「重要更新」代码块标题与「公告详情」对称,留出代码块标签作小标题,QQ
    # 客户端会显著区分两段内容。
    parts = ['## 📢 更新公告', '', '---', '']
    if important:
        parts.append(f'```重要更新\n{important}\n```')
        parts.append('')
    parts.append(f'```公告详情\n{notice}\n```')
    md = '\n'.join(parts)
    await event.reply(md)


@handler(_P_TROUBLE,
         name='LGTBot 疑难解答',
         desc='读取 data/troubleshooting.txt 实时返回常见问题与解答',
         priority=50,
         block=True,
         event_types=_LGT_MSG_EVENTS | {INTERACTION_CREATE})
async def lgtbot_troubleshooting(event, match):
    """收到「疑难解答」(文本或按钮)→ 把 txt 文件内容包在代码块里 reply。

    结构与 ``lgtbot_update_notice`` 完全对称(标题 + 分隔线 + 代码块),
    管理员通过编辑 ``data/troubleshooting.txt`` 热更新内容。首次访问时
    文件不存在,会自动用 ``_DEFAULT_TROUBLESHOOTING`` 中预置的 Q&A 创建。
    """
    if event.is_interaction:
        try:
            await event.ack_interaction(code=0)
        except Exception:
            pass
    content = _read_troubleshooting()
    md = (
        '## ❓ 疑难解答\n'
        '\n'
        '---\n'
        '\n'
        f'```常见问题\n{content}\n```'
    )
    await event.reply(md, buttons=buttons.build_support_buttons())


# ──────── /关于 抢占(优于系统插件的同名指令) ───────────────────────────────
# 系统插件 ``plugins/system/app/basic.py::about_info`` 注册了 ``^关于$`` 默认
# priority=0,展示框架级机器人信息。本插件的引擎自带 about 回执,用户在本 bot 上发 ``/关于`` 应优先看到这个
#
# priority=50 高于系统插件 + block=True 拦截后续. 函数体里直接转发给 lgtbot_dispatch。

@handler(_P_ABOUT,
         name='LGTBot 关于',
         desc='查看 LGTBot 版本、作者、仓库链接',
         priority=50,
         block=True,
         event_types=_LGT_MSG_EVENTS)
async def lgtbot_about(event, match):
    await lgtbot_dispatch(event, match, _from_exclusive=True)


# ──────── 消息派发 ────────────────────────────────────────────────────────

@handler(r'.*',
         name='LGTBot 消息派发',
         desc='把群 @bot / 私聊 / 全量群 @bot 消息派发给 LGTBot C++ 引擎',
         priority=-100,
         event_types=_LGT_MSG_EVENTS, ignore_at_check=True)
async def lgtbot_dispatch(event, match, *, _from_exclusive=False):
    """将所有群 @ / 私聊消息派发给 LGTBot 引擎（不消费事件，其他插件仍可处理）。

    ``ignore_at_check=True`` 让框架把全量群的 non-AT 消息也派进来,但本 handler
    regex 是 ``.*`` 会吞日常对话 —— 所以本体里强制再过一道 is_at_self 闸:
    群里没 @bot 的消息直接 return,等同于 LGTBot 仍只对 @bot 触发响应。
    (私聊 / 频道私信不受影响,本身没有「@」概念。)

    ``_from_exclusive=True`` 表示由专属 handler 主动转发(目前只有
    lgtbot_about),跳过下方的专属指令排除闸 —— 那一次转发正是它的职责。
    框架调用固定传 (event, match) 两个位置参数,不会碰到这个 kwarg。
    """
    if not state.started:
        return

    # GROUP_MESSAGE_CREATE 事件本身就是 QQ 给本 bot 在该群开了「全量推送」
    # 的直接证据(QQ 后台没开根本不会投递),记下 gid 给 callbacks 端
    # is_full_volume_group 用 —— 比框架 non_at_message 配置(可能滞后/缺失)更准。
    if event.event_type == GROUP_MESSAGE_CREATE and event.group_id:
        state.full_volume_groups.add(event.group_id)

    # 全量群里的日常对话必须挡掉(避免 r'.*' + ignore_at_check 把所有群消息
    # 都派给引擎)。这道闸**只对 GROUP_MESSAGE_CREATE 应用**:
    #   · GROUP_AT_MESSAGE_CREATE 的事件类型本身就意味着用户 @了 bot,但
    #     parse_group_message 只在 payload 含 mentions 数组 + is_you=True 时
    #     才把 is_at_self 置 True;QQ 官方 bot 的 AT_CREATE payload 不一定
    #     带 mentions(GROUP_AT 的 AT 信号来自事件类型,不在 payload 里重复),
    #     硬卡这道闸会把所有老的 AT_CREATE 流量误挡 —— 用户反馈过的现象。
    #   · GROUP_MESSAGE_CREATE 是「全量群任意消息」,只有 is_at_self=True 才
    #     该交给 LGTBot 引擎,其他是日常聊天。
    if event.event_type == GROUP_MESSAGE_CREATE and not getattr(event, 'is_at_self', False):
        return

    content = (event.content or '').strip()
    uid = event.user_id or ''
    gid = event.group_id or event.channel_id or ''

    # 专属指令排除闸(见 _EXCLUSIVE_RES 注释):菜单 / 关于 / 重启 等已由各自
    # 专属 handler 处理,catch-all 直接跳过,引擎不重复收到 —— 复刻旧框架
    # first-match-wins 的行为,不依赖框架 block 语义。
    if not _from_exclusive and _is_exclusive_command(content):
        return

    # 用户缓存：昵称 + 头像 URL（事件携带 username + 用 appid 推导头像）
    # 走 userdb 落盘，5 分钟批量 flush；name / avatar 任一为空时不会覆盖 DB 旧值
    if uid:
        appid = event.appid or ''
        avatar = helpers.QQ_AVATAR_URL.format(appid=appid, openid=uid) if appid else ''
        userdb.mark_dirty(uid, name=getattr(event, 'username', '') or '', avatar=avatar)

    # 用户消息 → 用 msg_id 刷新被动引用配额（5 条新额度）
    #
    # ⚠️ 两条分支**必须互斥**:msg_id 带场景,群消息产生的 msg_id 拿去发该用户私信会触发 QQ 端 `请求参数 msg_id 无效或越权`。
    # 使用 ``elif event.is_direct``,``u:<uid>`` 只会接收真正私信场景的 msg_id。
    appid_str = event.appid or ''
    if event.message_id:
        if event.is_group and gid:
            quota.refresh_ref(helpers.target_key(gid, False), 'msg_id', event.message_id, appid_str)
        elif event.is_direct and uid:
            quota.refresh_ref(helpers.target_key(uid, True), 'msg_id', event.message_id, appid_str)

    # 空消息（仅 @bot）→ 回欢迎菜单，不进 LGTBot 引擎
    if not content:
        page_logs.log_incoming(uid, gid, '(空消息：触发欢迎菜单)')
        await _send_welcome_menu(event)
        return

    # 按钮附加完全交给 C++ 桥接层根据消息内容判断（见
    # LGTBot_ElainaBot.cc::ClassifyMatchEvent）—— 此处不再做命令模式匹配,
    # 这样 /新游戏 触发的「先解散后新建」两条消息也不会把按钮挂错位置。

    page_logs.log_incoming(uid, gid if event.is_group else '', content)

    # 派发给 C++ 引擎（独立线程，避免 C++ match-lock 与 asyncio loop 互锁）
    try:
        if event.is_group and gid:
            threading.Thread(
                target=boot.LGTBot_ElainaBot.on_public_message,
                args=(content, uid, gid),
                daemon=True,
            ).start()
        elif event.is_direct and uid:
            threading.Thread(
                target=boot.LGTBot_ElainaBot.on_private_message,
                args=(content, uid),
                daemon=True,
            ).start()
    except Exception as e:
        log.warning(f'派发消息失败: {e}')


# ──────── INTERACTION:两类 callback 按钮 ──────────────────────────────────
# QQ INTERACTION_CREATE 事件由 type=1 callback 按钮点击触发,event.content 是
# 按钮的 data 字段。本插件处理两类:
#
#   1. 「🔄 刷新会话」按钮(data == quota.RELAY_BUTTON_DATA = '__lgt_relay__')
#      —— 专门用于续被动引用配额,不走 LGTBot 引擎。lgtbot_interaction_relay
#      只 ack + 刷新 event_id 配额,客户端看一个短暂 toast,5 条被动额度立即续上。
#
#   2. 其他所有 data(欢迎菜单的「数字蜂巢/天赋云巢/...」、规则按钮、上下文按钮
#      等)—— 等同于用户主动发送 data 这段文字。lgtbot_interaction_dispatch
#      ack 后把 content 走 on_public_message / on_private_message 派进 C++ 引擎,
#      与 lgtbot_dispatch 的消息派发路径镜像,差异仅在配额用 event_id 续而非
#      msg_id(INTERACTION 没有 msg_id,但 event_id 是独立的新 5 条额度)。
#
# 两个 handler 用互斥 regex 划分职责:relay 严格匹配 RELAY_BUTTON_DATA,
# dispatch 用负向先行 (?!) 排掉这个 sentinel。

@handler(rf'^{re.escape(quota.RELAY_BUTTON_DATA)}$',
         name='LGTBot 刷新按钮回调',
         desc='「🔄 刷新会话」按钮点击 → ack + 用新 event_id 续 5 条被动回复额度',
         priority=-200,
         event_types={INTERACTION_CREATE})
async def lgtbot_interaction_relay(event, match):
    try:
        await event.ack_interaction(code=0)
    except Exception:
        pass

    if not event.event_id:
        return
    appid_str = event.appid or ''
    # event_id 同样带场景(见上方消息派发处的注释):群里点按钮产生的 event_id
    # 不能用于该用户私信,所以两条分支必须互斥。
    if event.is_group and event.group_id:
        quota.refresh_ref(helpers.target_key(event.group_id, False),
                          'event_id', event.event_id, appid_str)
    elif event.is_direct and event.user_id:
        quota.refresh_ref(helpers.target_key(event.user_id, True),
                          'event_id', event.event_id, appid_str)


@handler(rf'^(?!{re.escape(quota.RELAY_BUTTON_DATA)}$).+',
         name='LGTBot 按钮回调派发',
         desc='非刷新 callback 按钮的 data 当作用户消息派发给 LGTBot 引擎',
         priority=-100,
         event_types={INTERACTION_CREATE})
async def lgtbot_interaction_dispatch(event, match):
    """非刷新 callback:把 button data 当用户消息派发给 LGTBot 引擎。

    与 lgtbot_dispatch 的消息处理流程几乎一致(mark_dirty / 配额续 / 日志 /
    起线程派给 C++),差异仅:
      · 必须先 ack_interaction —— 否则客户端 3s 后弹"请求超时"
      · 配额刷新键用 event.event_id('event_id' 类型),而不是 msg_id
        (INTERACTION 没 msg_id,但 event_id 同样能撑起 5 条被动回复额度)
    """
    # ack 优先于一切,确保在 3s 时限内回执
    try:
        await event.ack_interaction(code=0)
    except Exception:
        pass

    if not state.started:
        return

    content = (event.content or '').strip()
    if not content:
        return

    # 专属指令排除闸:菜单 / 更新公告 等按钮的 data 与文本指令同文案,已由
    # 专属 handler(同样监听 INTERACTION_CREATE)处理,这里跳过防止引擎重复
    # 收到 —— 与 lgtbot_dispatch 的闸对称,见 _EXCLUSIVE_RES 注释。
    if _is_exclusive_command(content):
        return

    uid = event.user_id or ''
    gid = event.group_id or event.channel_id or ''

    # 按钮点击本身就是一次活跃事件 —— mark_dirty 现在不要求 name/avatar 非空,
    # 仅刷 last_seen 也会被记下(INTERACTION 事件没有 username 字段是正常的)
    if uid:
        appid = event.appid or ''
        avatar = helpers.QQ_AVATAR_URL.format(appid=appid, openid=uid) if appid else ''
        userdb.mark_dirty(uid, name=getattr(event, 'username', '') or '', avatar=avatar)

    appid_str = event.appid or ''
    # 互斥分支同 lgtbot_interaction_relay:event_id 不能跨场景使用
    if event.event_id:
        if event.is_group and gid:
            quota.refresh_ref(helpers.target_key(gid, False),
                              'event_id', event.event_id, appid_str)
        elif event.is_direct and uid:
            quota.refresh_ref(helpers.target_key(uid, True),
                              'event_id', event.event_id, appid_str)

    page_logs.log_incoming(uid, gid if event.is_group else '', content)

    try:
        if event.is_group and gid:
            threading.Thread(
                target=boot.LGTBot_ElainaBot.on_public_message,
                args=(content, uid, gid),
                daemon=True,
            ).start()
        elif event.is_direct and uid:
            threading.Thread(
                target=boot.LGTBot_ElainaBot.on_private_message,
                args=(content, uid),
                daemon=True,
            ).start()
    except Exception as e:
        log.warning(f'派发按钮回调失败: {e}')


# ──────── 重启逻辑(命令 / WebUI 共用) ─────────────────────────────────────
# 拆成两步,让命令 handler 和 WebUI 重启按钮共用同一原子语义:
#   1. check_and_prepare_restart()  同步检查 + (若可)干净释放 C++ 引擎
#   2. schedule_exec_after(delay)    异步任务:延迟后 os.execv 整个 Python 进程
# 调用方在两者之间插入「响应已发出」步骤(event.reply 或 HTTP response),保证
# 用户看到的提示先送达再换进程。
#
# 为什么必须 exec 而不是 plugin_manager.reload:CPython 扩展模块一经 import
# 就常驻 sys.modules,plugin 热重载只重跑 Python 装饰器、并不 dlclose
# LGTBot_ElainaBot.so;同样,libbot_core.so 与各 libgame.so 一经引擎 dlopen
# 也驻留进程。要让 build.sh 重编的 C++ 二进制真正生效,只能换一个全新 Python
# 进程 —— 这正是本插件 /重启 与 WebUI 重启按钮的诉求(主框架的 /框架重启 也
# 是这个套路,只是没有 LGTBot 活跃对局的预检)。

def check_and_prepare_restart() -> tuple[bool, str]:
    """同步检查并准备重启。返回 (是否可重启, 给用户的提示文案)。

    · 引擎未加载   → (False, 无需重启提示)
    · 活跃 match   → (False, 拒绝原因);引擎保持运行
    · 否则         → (True, 正在重启提示),并已干净释放 C++ 引擎、把
                     state.started / boot.is_engine_running 置 False;
                     调用方接下来必须立刻调度 exec,否则插件会处于
                     「Python 还在 / C++ 引擎已无」的半残状态。
    """
    if not boot.LGTBOT_AVAILABLE:
        return False, 'ℹ️ LGTBot 引擎未加载，无需重启。'
    if not boot.LGTBot_ElainaBot.release_bot_if_not_processing_games():
        return False, '⚠️ 当前存在进行中的游戏，请等待对局结束后再重启！'
    state.started = False
    boot.mark_engine_running(False)
    return True, '🔁 LGTBot 正在重启（重新加载全部 C++ 引擎与游戏插件）...'


def schedule_exec_after(delay: float = 0.5, on_failure=None) -> None:
    """延迟 delay 秒后 ``os.execv`` 自身。延迟用于让此前的响应送达。

    on_failure 可选,在 execv 失败(罕见,通常仅 sys.executable 丢失时)被调用,
    支持 sync 或 async。任务挂在当前 event loop 上,不依赖任何 Python 模块
    全局状态(本插件被销毁也不影响已经入队的 coroutine)。
    """
    async def _do():
        await asyncio.sleep(delay)
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            log.error(f'os.execv 重启失败,引擎已释放但进程未替换,需手动重启: {e}')
            if on_failure is not None:
                try:
                    if asyncio.iscoroutinefunction(on_failure):
                        await on_failure()
                    else:
                        on_failure()
                except Exception:
                    pass
    try:
        asyncio.get_running_loop().create_task(_do())
    except RuntimeError:
        log.error('无运行中 asyncio loop,无法调度 exec')


# ──────── 主人专属:本插件全套重启指令 ────────────────────────────────────
# 触发文本 "/重启"(框架自动剥前导 /，regex 不带 / 同样匹配 "重启")。
# `owner_only=True` 框架内置:非主人触发时直接回 owner_only 模板,不进函数体。
# WebUI 重启按钮也走同一对 helper —— 见 webui/main.py::_render_restart。

@handler(_P_RESTART,
         name='LGTBot 重启',
         desc='整进程 os.execv 重启,重新加载 C++ 引擎与全部游戏插件',
         owner_only=True,
         event_types=_LGT_MSG_EVENTS,
         priority=100,
         block=True)
async def lgtbot_restart(event, match):
    """主人发起的本插件「全套」重启 —— exec 整个 Python 进程,把 bridge .so /
    libbot_core.so / 全部 libgame.so 重新 dlopen,等价于让 build.sh 重编后的
    C++ 二进制即刻生效。
    """
    ok, msg = check_and_prepare_restart()
    await event.reply(msg)
    if not ok:
        return

    async def _on_fail():
        try:
            await event.reply('❌ 重启时发生错误，引擎已释放但进程未替换，需手动重启，详情请查看控制台日志。')
        except Exception:
            pass

    schedule_exec_after(0.5, on_failure=_on_fail)
