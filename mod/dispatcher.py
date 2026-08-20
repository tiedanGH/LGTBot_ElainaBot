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
from datetime import date as _date

from core.plugin.decorators import handler
from core.base.logger import get_logger, PLUGIN
from core.message.event import (
    GROUP_AT_MESSAGE_CREATE, GROUP_MESSAGE_CREATE, C2C_MESSAGE_CREATE,
    AT_MESSAGE_CREATE, DIRECT_MESSAGE_CREATE,
    INTERACTION_CREATE,
)

from . import (state, quota, helpers, boot, buttons, uploader, userinfo, audit,
               metrics, stats_image, urgent)
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
        # 无主动推送资格的群才追加「全量申请」内联指令行;私信无群概念,不加
        menu_gid = event.group_id or event.channel_id or ''
        if event.is_group and menu_gid and not helpers.can_push_group(menu_gid):
            md += buttons.MENU_FULL_VOLUME_CMD_MD
        # 「紧急公告」垫在菜单最后,引用块渲染;未启用 / 文案为空时 menu_block() 返回 ''
        md += urgent.menu_block()
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
_P_PLANNED  = r'^/?计划重启(?:\s+(.+))?$'
_P_STATS    = r'^/?数据统计\s*(总|\d{4}|\d{2})?$'
_P_MATCHLIST = r'^/?赛事列表$'
_P_ADMIN_INTERRUPT = r'^%中断(?:\s+\S+)?$'
_P_SPONSOR  = r'^/?赞助支持$'

# 「赞助支持」也列入专属指令:赞助功能**关闭**时 handler 会自己转发给引擎。
_EXCLUSIVE_RES = tuple(re.compile(p, re.DOTALL) for p in (
    _P_QUERY_ID, _P_MENU, _P_MORE, _P_NOTICE, _P_TROUBLE, _P_ABOUT,
    _P_RESTART, _P_PLANNED, _P_STATS, _P_MATCHLIST, _P_ADMIN_INTERRUPT,
    _P_SPONSOR,
))


# 「计划重启」维护模式仅拦「新建房间」类指令:新游戏 + 随机游戏。
# # 与 / 前缀都认(引擎元指令上游为 #,本插件使用 /),不带前缀的裸指令也拦。
_NEW_GAME_RE = re.compile(r'^[/#]?(新游戏|随机游戏)')

# 从「/新游戏 <游戏名> [单机/配置…]」里抓游戏名(第一个 token;lgtbot 游戏名均无空格)。
# 单机局(引擎跳过 new_game 广播、game_started 无 brief)靠这个兜底拿游戏名,
# 见 state.pending_new_game_name / callbacks.cb_match_event。「随机游戏」不含名字,不匹配。
_NEW_GAME_NAME_RE = re.compile(r'^[/#]?新游戏\s+(\S+)')


def _capture_pending_game_name(content: str, event, gid: str, uid: str) -> None:
    """派发给引擎前,把「/新游戏 X …」的游戏名记到 state.pending_new_game_name[key]。

    仅在命令确实带游戏名时记(裸「/新游戏」/「随机游戏」不记)。
    多人局最终以引擎 new_game 的 brief 为准,这份只在 current_game 为空(单机局)时兜底。
    放在屏蔽 / 计划重启闸之后调用,被拦下的命令不会污染。"""
    m = _NEW_GAME_NAME_RE.match(content)
    if not m:
        return
    if event.is_group and gid:
        key = helpers.target_key(gid, False)
    elif event.is_direct and uid:
        key = helpers.target_key(uid, True)
    else:
        return
    state.pending_new_game_name[key] = m.group(1)


async def _maybe_notify_urgent(event, content: str, gid: str) -> None:
    """新群第一次建房 → 额外推一条「紧急公告」,发完记下该群,此后不再打扰。

    只在**群聊**里做(已通知群记录按群号存,私信没有群概念);判定条件全在
    ``urgent.pending_notify``(公告已启用 + 文案非空 + 该群没被通知过)。

    建房命令的识别复用「计划重启」维护闸的同一枚 ``_NEW_GAME_RE`` —— ``/新游戏`` 与 ``/随机游戏`` 都算,
    **不区分单机局**(单机没有引擎的建房广播,但命令层面一视同仁,少一处特例)。调用点与 ``_capture_pending_game_name``
    对称:消息事件与按钮 INTERACTION 两条路径都要覆盖(菜单快捷开局按钮的 data  就是 ``/新游戏 X``)。

    先发后记:发送失败(网络 / 配额)就不落记录,下次建房还能补上;反过来先记后发一旦发失败,这个群就永远收不到公告了。
    """
    if not (event.is_group and gid) or not _NEW_GAME_RE.match(content):
        return
    md = urgent.pending_notify(gid)
    if not md:
        return
    try:
        # 直接 reply 会真实吃掉一条被动引用额度,和欢迎菜单分支一样先把计数烧掉对齐。
        quota.try_consume_ref(helpers.target_key(gid, False))
        await event.reply(md)
    except Exception as e:
        log.warning(f'紧急公告通知发送失败 ({gid}): {e}')
        return
    page_logs.log_outgoing(gid, False, '[紧急公告通知]')
    if urgent.mark_notified(gid):
        log.info(f'📣 [紧急公告] 已通知新群 {gid}（累计 {urgent.notified_count()} 群）')


# 主动消息额度「即将用尽」告警阈值 —— 用量达上限的该比例即转黄色警告。
PUSH_QUOTA_WARN_RATIO = 0.85


def _push_quota_view(target_id: str, is_uid: bool) -> dict:
    """当前会话目标今日主动消息额度用量,供「数据统计」展示。

    额度是 **per 群 / per 用户** 的(QQ 官方接口限制),所以群里查的是本群、
    私信里查的是该用户自己的私信额度。返回
    ``{shown, is_group, used, limit, remaining, ratio, near_limit,
    exhausted, no_permission}``:
      · ``limit=0``        未设上限(仅展示用量,不告警)
      · ``shown=False``    无有效目标(不展示该行)
      · ``near_limit``     用量已达 ``PUSH_QUOTA_WARN_RATIO``(85%)但未用满 ——
        黄色警告,提醒即将触顶
      · ``exhausted``      已用满 —— 红色,已退回刷新按钮机制
      · ``no_permission``  **非全量群** —— 该群压根没有主动消息推送权限,额度
        数字对它没有意义,改为黄色警告提示群主发「全量申请」授权。
        私信不适用(私信能否直推由 sandbox_dm_users 决定,不是群权限)。
    """
    if not target_id:
        return {'shown': False, 'is_group': not is_uid, 'used': 0, 'limit': 0,
                'remaining': 0, 'ratio': 0.0, 'near_limit': False,
                'exhausted': False, 'no_permission': False}
    from . import callbacks as _callbacks      # 函数内导入,避免模块级互引
    limit = int(_callbacks.ACTIVE_PUSH_DAILY_LIMIT or 0)
    used = metrics.active_push_used(target_id, is_uid)
    no_perm = (not is_uid) and not helpers.can_push_group(target_id)
    ratio = (used / limit) if limit else 0.0
    exhausted = bool(limit and used >= limit)
    return {
        'shown': True,
        'is_group': not is_uid,
        'used': used,
        'limit': limit,
        'remaining': max(0, limit - used) if limit else 0,
        'ratio': ratio,
        'near_limit': bool(limit and not exhausted
                           and ratio >= PUSH_QUOTA_WARN_RATIO),
        'exhausted': exhausted,
        'no_permission': no_perm,
    }


def _should_block_new_game(content: str) -> bool:
    """「计划重启」维护闸的判定:仅**手动模式**拦新建房间。

    自动模式(is_planned_restart_auto)不拦 —— watcher 只在对局自然清空后
    静默重启,期间玩家照常开新局,新局会让静默计时清零、重启顺延。
    """
    return (state.is_planned_restart()
            and not state.is_planned_restart_auto()
            and bool(_NEW_GAME_RE.match(content)))


def _planned_restart_notice() -> str:
    """构造「计划重启」维护提示 —— 每次现拼,带上当前进行中对局数与维护原因。

    · 进行中对局数取自 ``state.active_matches``,为 0 时提示「可随时重启」,让玩家知道等待即将结束。
    · 维护原因由管理员在开启维护模式时填写(指令 ``/计划重启 <原因>`` 或面板输入框),
      未填写则不显示该段。原因是管理员可控文本,仍按 markdown 语境转义,防止奇怪字符把整条消息排版搞乱。
    """
    parts = [
        '## 🚧 维护提醒',
        '',
        '机器人**即将重启更新**，已暂停创建新游戏，进行中的对局与已创建的房间不受影响。',
    ]
    reason = state.planned_restart_reason()
    if reason:
        parts += ['', f'📌 维护原因：{helpers.sanitize_md_name(reason)}']
    remaining = len(state.active_matches)
    parts += ['', (f'🎮 剩余对局：**{remaining}** 局'
                   if remaining else '🎮 当前已无进行中的对局，**随时可能重启**')]
    parts += ['', '> 请稍后再试，感谢您的理解 🌹']
    return '\n'.join(parts)


def _is_exclusive_command(text: str) -> bool:
    """content 是否命中任一专属指令(含框架「加/去斜杠」重试的变体)。"""
    if not text:
        return False
    alt = text[1:] if text.startswith('/') else '/' + text
    return any(p.search(text) or p.search(alt) for p in _EXCLUSIVE_RES)


# ──────── 群管理员执行「非 %中断」管理指令时的明确拒绝 ─────────────────────
# 群管拥有 %中断 的代理权(见 lgtbot_admin_interrupt),但**没有**引擎的超级管理员权限。
# 若其他 % 指令仍回引擎原文案「您未持有管理员权限」,与"我明明能中断"自相矛盾。
# 这里由插件回一条措辞区分两级权限的文案(引擎那条不带「超级」二字,便于分辨到底是谁拒绝的)。
#
# 只拦**群管理员**:普通成员没有任何管理能力,引擎原文案本就准确,不加插件干预
# (行为与本特性上线前完全一致);已配置的超级管理员当然要放行给引擎真执行。
_ADMIN_CMD_SIGN = '%'
# 文案对齐引擎的群聊回执格式:PublicReplyMsgSender(bot_core.cc:139-145)先发
# ``At(uid) + "\n"`` 再接正文,渲染出来就是「<@openid>\n[错误] …」。这里照抄同款
# 结构(@ + 换行 + [错误] 开头),再补一行说明群管到底能用什么 —— 用户看到的
# 是与引擎一致的排版,但措辞点明是「超级」管理员权限缺失。
_SUPER_ADMIN_DENIED = ('[错误] 您未持有超级管理员权限\n'
                       '群主 / 群管理员仅可使用「%中断」强制中断本群卡死的对局')


def _super_admin_denied_text(uid: str) -> str:
    """拒绝文案(群聊里带 ``<@uid>`` 前缀,同引擎回执排版)。"""
    return f'<@{uid}>\n{_SUPER_ADMIN_DENIED}' if uid else _SUPER_ADMIN_DENIED
# 已授权给群管的唯一管理指令,拒绝闸需放行(与 lgtbot_admin_interrupt 同一 pattern)
_P_ADMIN_INTERRUPT_RE = re.compile(_P_ADMIN_INTERRUPT)
# 视为「拥有群管理权限」的 QQ member_role 取值(框架 parsers/base.py 从 author 解析;
# 群主 owner 与管理员 admin 都算,普通成员 member / 空串不算)。
_GROUP_ADMIN_ROLES = frozenset({'owner', 'admin'})


def _deny_super_admin_cmd(event, content: str, uid: str) -> bool:
    """是否应由插件拒绝这条 ``%`` 管理指令(群管理员 + 非超级管理员)。

    ``%中断`` 由专属 handler 抢占,正常不会走到本函数;仍显式排除以防
    handler 链语义变化时误拦这条已授权的指令。
    """
    if not content.startswith(_ADMIN_CMD_SIGN):
        return False
    if _P_ADMIN_INTERRUPT_RE.match(content):
        return False
    role = getattr(event, 'member_role', '') or ''
    if not (event.is_group and role in _GROUP_ADMIN_ROLES):
        return False
    from . import config as _config
    return uid not in _config.ADMIN_UIDS


# ──────── 屏蔽指令表(内置 + config.yaml: blocked_commands) ────────────────
# 与上面的专属指令排除表互补:排除表是**本插件自己**的指令,这里是**其他插件**的指令
# 框架把所有命中的 handler 全部执行,其他插件处理完后,消息仍会落进本插件的 catch-all 被转发给引擎。
# 命中屏蔽表的消息 catch-all 直接跳过,引擎不再二次回复。

# 部署自带插件的指令,固定屏蔽、无需配置。匹配斜杠不敏感,且允许参数不带空格
# 直接跟数字(``dau0503`` / ``全量申请123456789``)—— 主框架派发对 handler
# 正则本身就做斜杠互换匹配,且 全量申请 / dau 的参数正则是 ``\s*`` 空格可选。
BUILTIN_BLOCKED_COMMANDS: tuple[str, ...] = ('全量申请', '全量列表', '关闭欢迎', '开启欢迎', 'dau')

# 用户配置的追加项(config.yaml: blocked_commands,config.py 热重载时覆写),
# 与内置表共同组成屏蔽表;匹配语义比内置表严格(斜杠按配置原样)。
BLOCKED_COMMANDS: tuple[str, ...] = ()


def _is_blocked_command(text: str) -> bool:
    """content 是否命中屏蔽表(BUILTIN_BLOCKED_COMMANDS + BLOCKED_COMMANDS)。

    内置项:斜杠不敏感;完整匹配,或后跟空白 / 数字的前缀匹配 ——
    ``/dau``、``dau 0503``、``dau0503``、``全量申请123456789`` 全部命中。
    配置项:斜杠**严格匹配** —— 配置带 ``/`` 只挡带 ``/`` 的消息,两种写法互不通配;
    完整匹配,或「指令 + 空白 + 参数」前缀匹配 —— ``帮助`` 也挡 ``帮助 xxx``
    (配置项在 config.py 载入时仅做 strip / 去空 / 去重,``/`` 原样保留。)
    """
    if not text:
        return False
    bare = text[1:] if text.startswith('/') else text
    for cmd in BUILTIN_BLOCKED_COMMANDS:
        if bare == cmd:
            return True
        if bare.startswith(cmd) and (bare[len(cmd)] in ' \t　' or bare[len(cmd)].isdigit()):
            return True
    for cmd in BLOCKED_COMMANDS:
        if text == cmd:
            return True
        if text.startswith(cmd) and len(text) > len(cmd) and text[len(cmd)] in ' \t　':
            return True
    return False


# ──────── 用户查询(所有人可用) ────────────────────────────────────────────

@handler(_P_QUERY_ID,
         name='查询用户',
         desc='查主框架库中的昵称 / 头像 / 最后活跃',
         priority=50,
         block=True,
         event_types=_LGT_MSG_EVENTS)
async def lgtbot_query_user(event, match):
    """以输入的 openid 查主框架用户数据(userinfo 门面),markdown 输出。

    - openid 长度必须严格 32 位,否则只回报错不查库
    - 四源(users / wakeup / 群活跃 / 统计)均查无 → 「查询失败:该 ID 不存在」
    - 活跃时间精度:日志留存期(默认 5 天)内精确到秒,更早降为日粒度(按日),全无记录显示「从未活跃」
    """
    # 非绑定 bot 的事件静默忽略
    # (多 bot 部署下本插件只服务绑定 bot,不回复不打日志;下同,所有 handler 的第一道闸)
    if helpers.is_foreign_event(event):
        return
    target = match.group(1).strip()
    if len(target) != 32:
        await event.reply(f'❌ ID 长度不正确（需 32 位，当前 {len(target)} 位）')
        return

    # get_user 含多次同步 SQLite 查询(ms 级),丢 executor 不占用事件循环
    user = await asyncio.get_running_loop().run_in_executor(
        None, userinfo.get_user, target)
    if user is None:
        await event.reply('❌ 查询失败：该 ID 不存在')
        return

    # 昵称是用户可控文本,下面要嵌进 **{name}** markdown,先消毒语法字符
    raw_name = user.get('name') or ''
    name = helpers.sanitize_md_name(raw_name) if raw_name else '[未知昵称]'
    avatar = user.get('avatar') or ''
    exact = user.get('last_active_exact') or ''
    day = user.get('last_active_date') or ''
    if exact:
        time_str = exact
    elif day:
        time_str = f'{day}（长期未活跃）'
    else:
        time_str = '从未活跃'
    # 内嵌头像 —— QQ markdown 用 `#WIDTHpx #HEIGHTpx` alt-text 控制尺寸,
    # 40px 是「小头像」典型尺寸,行内 + 昵称 一目了然
    avatar_md = (f'![头像 #40px #40px]({avatar})' if avatar else '[未知]')
    total = user.get('total_messages')
    stats_line = (f'累计消息 {total} 条（统计截至昨日）\n'
                  if total is not None else '')
    md = (
        '## ✅ 查询成功\n'
        '\n'
        f'{avatar_md} | **{name}**\n'
        '\n'
        f'```最后活跃\n'
        f'{time_str}\n'
        f'{stats_line}'
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

# 赞助鸣谢名单 —— 与其他 txt 同一套「实时读盘 + 面板可编辑」机制。
# 只在 buttons.SPONSOR_ENABLED 打开时才会被读到。
_SPONSORS_PATH = os.path.join(boot.DATA_DIR, 'sponsors.txt')
_DEFAULT_SPONSORS = '暂无赞助记录，你可以成为第一位 ❤️'

# 首次访问时写入的默认 Q&A
_DEFAULT_TROUBLESHOOTING = (
    'Q：为什么 bot 只回了几条就停了？\n'
    'A：QQ 协议限制 —— 普通群里 bot 对同一条消息最多回 5 条，仅 5 分钟内有效；私信为 4 条 / 60 分钟。\n'
    '   ▸ 群主可@bot发送「全量申请」，并根据提示开通全量权限，bot 在全量群可推送主动消息，不受限制\n'
    '   ▸ 私信场景请添加机器人为好友，并保持机器人「权限设置」中的主动消息权限开启，即可正常接收主动私信\n'
    '\n'
    'Q：为什么有些游戏私信 bot 后一直显示省略号？\n'
    'A：部分游戏没有写预设的指令回复，只要没有反馈 "[错误]" 字样就是提交成功了。\n'
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


# 「按需存在」的可选 txt 读取 / markdown 引用块包装
# 实现在 helpers,紧急公告(urgent.py)与这里共用同一份,避免两处各写一遍再漂移(引用块里空行必须带裸 ``>`` 这种细节最容易只改一边)。
_read_optional_txt = helpers.read_optional_txt
_as_quote = helpers.as_quote


def _read_important_update() -> str:
    """「重要更新」置顶区 —— 空则 ``lgtbot_update_notice`` 跳过该代码块。"""
    return _read_optional_txt(_IMPORTANT_UPDATE_PATH, 'important_update.txt')


def _read_troubleshooting() -> str:
    return _read_txt_with_default(
        _TROUBLESHOOTING_PATH, _DEFAULT_TROUBLESHOOTING, 'troubleshooting.txt')


def _read_sponsors() -> str:
    return _read_txt_with_default(
        _SPONSORS_PATH, _DEFAULT_SPONSORS, 'sponsors.txt')


@handler(_P_MENU,
         name='欢迎菜单',
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
    if helpers.is_foreign_event(event):
        return
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
         name='更多功能',
         desc='展示「更多功能」子菜单',
         priority=50,
         block=True,
         event_types=_LGT_MSG_EVENTS | {INTERACTION_CREATE})
async def lgtbot_more_features(event, match):
    """收到「更多功能」(文本或按钮)→ 回一条带子菜单按钮的 markdown。

    INTERACTION 路径要先 ack 抑制客户端 3s「请求超时」toast。
    """
    if helpers.is_foreign_event(event):
        return
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
         name='更新公告',
         desc='读取 data/update_notice.txt 实时返回公告内容',
         priority=50,
         block=True,
         event_types=_LGT_MSG_EVENTS | {INTERACTION_CREATE})
async def lgtbot_update_notice(event, match):
    """收到「更新公告」(文本或按钮)→ 把 txt 文件内容包在代码块里 reply。

    若 ``important_update.txt`` 非空,在常规「公告详情」代码块上方再渲染一个
    ``重要更新`` 代码块作为置顶提示;为空时该区块不出现,UI 与旧版完全一致。
    """
    if helpers.is_foreign_event(event):
        return
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
    # 赞助功能开启时在公告下方挂一行「赞助支持」。
    entry = buttons.build_sponsor_entry_buttons()
    if entry:
        await event.reply(md, buttons=entry)
    else:
        await event.reply(md)


# ──────── 「数据统计」的四个窗口视图(按日 / 按月 / 按年 / 累计总计) ─────────
# 它们与今日视图的差异完全一致:无涨跌标识、无主动消息行、无趋势图,第 4 卡为该期**对局人次**,双榜 TOP10(今日视图 5)。
# 四者之间只差
#   period  卡片 / 榜单文案里的期间词(当日 / 当月 / 当年 / 累计)
#   who     前两卡的前缀(总计视图是"累计玩家 / 累计群聊",其余是"活跃…")
#   empty   该期没有已完成对局时的提示措辞
#   flags   叠给 stats_image 的子模式开关(date_mode 恒真,是这批视图的总闸)
# 所以实现只有 _reply_period_stats 一份 —— 之前按日 / 按月各一份复制,再加年 / 总就是四份几乎相同的代码。

# 「数据统计YYYY」可查的最早年份
_MIN_STATS_YEAR = 2026

_SPAN_VIEWS = {
    'date':  {'period': '当日', 'who': '活跃', 'flags': {}, 'empty': '该日期没有已完成的对局'},
    'month': {'period': '当月', 'who': '活跃', 'flags': {'month_mode': True}, 'empty': '该月份没有已完成的对局'},
    'year':  {'period': '当年', 'who': '活跃', 'flags': {'year_mode': True}, 'empty': '该年份没有已完成的对局'},
    'total': {'period': '累计', 'who': '累计', 'flags': {'total_mode': True}, 'empty': '数据库中还没有已完成的对局'},
}


async def _reply_period_stats(event, view: str, stats: dict, prefix: str,
                              label: str, sub_title: str) -> None:
    """窗口视图的统一实现:``stats`` 是 metrics 的窗口查询结果,``prefix`` 是它的键前缀。

    ``label`` 进文本首行与报错提示(如 ``2026-08-02`` / ``2026`` / ``全部历史``),
    ``sub_title`` 是图片顶栏标题右侧的小标签。图片优先(线程池渲染 → 图床 →
    markdown 内嵌),渲染 / 上传失败回退纯文本(榜单文本仍 TOP3 控制消息长度)。
    """
    if not stats.get('available'):
        await event.reply(f'<@{event.user_id}>\n❌ 数据统计暂不可用，请稍后再试')
        return
    cfg = _SPAN_VIEWS[view]
    matches = stats.get(f'{prefix}_matches')
    if not matches:
        await event.reply(f'<@{event.user_id}>\n❌ {label} 无统计数据（{cfg["empty"]}）')
        return
    players = stats.get(f'{prefix}_players')
    groups = stats.get(f'{prefix}_groups')
    attendances = stats.get(f'{prefix}_attendances')
    top_games = stats.get(f'top_games_{prefix}') or []
    top_players = stats.get(f'top_players_{prefix}') or []

    # 映射成 stats_image 的通用形状:date_mode 下不含 push_quota / trend / yesterday_*
    g = {
        'available': True,
        'date_mode': True,
        'rank_limit': 10,
        'today_matches': matches,
        'today_players': players,
        'today_groups': groups,
        'attendances': attendances,
        'top_games_today': top_games,
        'top_players_today': top_players,
        'trend_10d': [],
    }
    g.update(cfg['flags'])
    # 总计视图带 bot 规模行:群组 / 好友总数本身就是累计值,与这一屏口径一致。
    # **不给增减角标** —— 角标是「今日净变化」,放在累计视图里没有意义(需求亦如此)。
    if view == 'total':
        g['bot_groups'] = userinfo.count_groups()
        g['bot_friends'] = userinfo.count_friends()

    uid = event.user_id or ''
    gid = event.group_id or event.channel_id or ''
    is_group = bool(event.is_group and gid)
    if uploader.SELECTED_BACKEND:
        loop = asyncio.get_running_loop()
        img = await loop.run_in_executor(
            None, stats_image.render_stats_image, g, sub_title)
        if img:
            url = await uploader.upload_image(
                img, 'lgtbot_stats.png',
                target_id=(gid if is_group else uid),
                target_is_uid=not is_group)
            if url:
                w, h = uploader.get_image_size(img)
                await event.reply(f'<@{uid}>![数据统计 #{w}px #{h}px]({url})')
                return

    def _n(v):
        return '—' if v is None else v

    period, who = cfg['period'], cfg['who']
    lines = [
        f'<@{uid}>',
        f'📈 LGT-Bot 数据统计 ({label})',
        f'🎮 {period}对局: {_n(matches)} 局',
        f'👤 {who}玩家: {_n(players)} 人',
        f'👥 {who}群聊: {_n(groups)} 个',
        f'🎫 {period}对局人次: {_n(attendances)} 人次',
    ]
    if view == 'total':
        # 与图片的 bot 规模行对齐
        lines += [f'👥 群组总数: {_n(g["bot_groups"])} 个',
                  f'👤 好友总数: {_n(g["bot_friends"])} 人']
    if top_games[:3]:
        lines.append(f'🔥 {period}游戏榜:')
        lines += [f'  {i}、{t["game_name"]} ({t["count"]}局)'
                  for i, t in enumerate(top_games[:3], 1)]
    if top_players[:3]:
        lines.append('👑 玩家参与榜:')
        lines += [f'  {i}、{p["display"]} ({p["count"]}局)'
                  for i, p in enumerate(top_players[:3], 1)]
    await event.reply('\n'.join(lines))


async def _reply_date_stats(event, target_day) -> None:
    """「数据统计MMDD」历史日期视图。"""
    ds = target_day.strftime('%Y-%m-%d')
    await _reply_period_stats(event, 'date', metrics.query_game_stats_for_date(ds),
                              'day', ds, ds)


async def _reply_month_stats(event, year: int, month: int) -> None:
    """「数据统计MM」按月视图(默认今年)。"""
    ym = f'{year:04d}-{month:02d}'
    await _reply_period_stats(event, 'month',
                              metrics.query_game_stats_for_month(year, month),
                              'month', ym, f'{ym} 月度统计')


async def _reply_year_stats(event, year: int) -> None:
    """「数据统计YYYY」按年视图。"""
    await _reply_period_stats(event, 'year', metrics.query_game_stats_for_year(year),
                              'year', f'{year:04d}', f'{year:04d} 年度统计')


async def _reply_total_stats(event) -> None:
    """「数据统计总」全部历史累计视图 —— 唯一额外带 bot 规模行的窗口视图。"""
    await _reply_period_stats(event, 'total', metrics.query_game_stats_total(),
                              'total', '全部历史',
                              f'累计总统计 · 截至{time.strftime("%Y-%m-%d")}')


@handler(_P_STATS,
         name='数据统计',
         desc='数据统计 / 游戏榜 / 玩家参与榜 / 近期趋势，可按日(MMDD)/按月(MM)/按年(YYYY)/累计(总)查询',
         priority=50,
         block=True,
         event_types=_LGT_MSG_EVENTS | {INTERACTION_CREATE})
async def lgtbot_data_stats(event, match):
    """收到「数据统计」(文本或按钮)→ 输出 lgtbot.db 游戏数据摘要(dau 风格)。

    四个窗口视图(见 _SPAN_VIEWS / _reply_period_stats):
      · 「数据统计MMDD」(如 数据统计0802,默认今年)  某个历史日期
      · 「数据统计MM」  (如 数据统计08)              当年某自然月
      · 「数据统计YYYY」(如 数据统计2026)            某个自然年
      · 「数据统计总」                               全部历史累计
    共同口径:无涨跌标识、无主动消息、无趋势图,第 4 卡为该期对局人次
    (不去重),双榜 TOP10;无对局直接报错。「总」额外带群组 / 好友总数一行
    (无增减角标)。输入今天的日期等价于无参数(仍带涨跌)。

    配置了图床(image_hosting 非空)时优先走**图片通道**(参照主框架 dau 指令):
    stats_image 渲染统计卡片(线程池,不阻塞事件循环)→ uploader 上传 →
    markdown 内嵌图回复;渲染失败(无 PIL / 无中文字体)或上传失败时回退下方
    纯文本 —— 图片是增强,文本是保底。

    文本口径:仅游戏数据 —— 两个榜单均为今日口径(00:00 起)且 TOP3 截断控制
    消息长度(面板另有总榜与本周榜);玩家无缓存昵称时以脱敏 ID(前3****后3)
    展示。序号用「1、」而非「1.」—— QQ 客户端会把「1.」解析成 markdown 有序
    列表并自行重排编号,导致显示错乱。
    """
    if helpers.is_foreign_event(event):
        return
    if event.is_interaction:
        try:
            await event.ack_interaction(code=0)
        except Exception:
            pass

    # ── 窗口查询分支(总 / 按年 YYYY / 按日 MMDD / 按月 MM)──────────────────
    # 4 位数字的**年份与 MMDD 不会撞车**:MMDD 的前两位必须是合法月份 01-12,
    # 而 2000-2099 的年份前两位恒为 20 —— 两个取值域天然不相交。
    # 所以路由规则就一句:前两位是合法月份 → 按 MMDD,否则按年份解析。
    arg = ''
    try:
        arg = (match.group(1) or '') if match else ''
    except (AttributeError, IndexError):
        arg = ''
    if arg == '总':                                      # 全部历史累计
        await _reply_total_stats(event)
        return
    if len(arg) == 2:                                    # 两位 = 月份(默认今年)
        month = int(arg)
        if not 1 <= month <= 12:
            await event.reply(f'<@{event.user_id}>\n❌ 月份无效：{arg}（格式 MM，如 数据统计08 查看 8月）')
            return
        await _reply_month_stats(event, _date.today().year, month)
        return
    if len(arg) == 4 and not 1 <= int(arg[:2]) <= 12:     # 前两位非月份 → 按年份
        year, this_year = int(arg), _date.today().year
        if not _MIN_STATS_YEAR <= year <= this_year:
            await event.reply(
                f'<@{event.user_id}>\n❌ 参数无效：{arg}'
                f'（按年 YYYY 如 数据统计{this_year}，可查 {_MIN_STATS_YEAR}-{this_year}；'
                f'按日 MMDD 如 数据统计0802）')
            return
        await _reply_year_stats(event, year)
        return
    if arg:
        try:
            target_day = _date(_date.today().year, int(arg[:2]), int(arg[2:]))
        except ValueError:
            await event.reply(f'<@{event.user_id}>\n❌ 日期无效：{arg}（格式 MMDD，如 数据统计0802 查看 8月2日）')
            return
        if target_day != _date.today():                  # 输今天 → 走默认今日视图
            await _reply_date_stats(event, target_day)
            return

    g = metrics.query_game_stats()
    if not g.get('available'):
        await event.reply(f'<@{event.user_id}>\n❌ 数据统计暂不可用，请稍后再试')
        return

    # bot 规模:绑定 bot 当前所在群数 + 好友数,带今日净变化角标。只在今日视图注入。
    _delta = userinfo.today_lifecycle_delta()
    g['bot_groups'] = userinfo.count_groups()
    g['bot_friends'] = userinfo.count_friends()
    g['bot_groups_delta'] = _delta.get('group')
    g['bot_friends_delta'] = _delta.get('friend')

    # 本会话(群 / 私信)的今日主动消息额度用量 —— 群里看本群、私信看本人。
    # 额度是 per-target 的,所以按当前会话目标取,不是全局汇总。
    _uid = event.user_id or ''
    _gid = event.group_id or event.channel_id or ''
    _is_group = bool(event.is_group and _gid)
    g['push_quota'] = _push_quota_view(_gid if _is_group else _uid, not _is_group)

    # ── 图片通道 ──────────────────────────────────────────────────────────
    if uploader.SELECTED_BACKEND:
        sub = f'截至 {time.strftime("%H:%M")}'
        loop = asyncio.get_running_loop()
        img = await loop.run_in_executor(
            None, stats_image.render_stats_image, g, sub)
        if img:
            uid = event.user_id or ''
            gid = event.group_id or event.channel_id or ''
            is_group = bool(event.is_group and gid)
            url = await uploader.upload_image(
                img, 'lgtbot_stats.png',
                target_id=(gid if is_group else uid),
                target_is_uid=not is_group)
            if url:
                w, h = uploader.get_image_size(img)
                await event.reply(f'<@{uid}>![数据统计 #{w}px #{h}px]({url})')
                return
        # 渲染 / 上传失败 → 落到下方文本保底

    def _n(v):
        return '—' if v is None else v

    def _delta(cur, yday):
        """较昨日同时段的增减后缀(同图片卡的涨跌胶囊;任一缺数据不显示)。"""
        if cur is None or yday is None:
            return ''
        diff = int(cur) - int(yday)
        if diff > 0:
            return f'（↑{diff}）'
        if diff < 0:
            return f'（↓{abs(diff)}）'
        return '（持平）'

    lines = [
        f'<@{event.user_id}>',
        f'📈 LGT-Bot 数据统计 (截至{time.strftime("%H:%M")})',
        f'🎮 今日对局: {_n(g.get("today_matches"))} 局'
        f'{_delta(g.get("today_matches"), g.get("yesterday_matches_same_span"))}',
        f'👤 活跃玩家: {_n(g.get("today_players"))} 人'
        f'{_delta(g.get("today_players"), g.get("yesterday_players_same_span"))}',
        f'👥 活跃群聊: {_n(g.get("today_groups"))} 个'
        f'{_delta(g.get("today_groups"), g.get("yesterday_groups_same_span"))}',
    ]
    # bot 规模 —— 括号里是今日净变化(不是与昨日对比),0 显示「持平」
    def _net(v):
        if v is None:
            return ''
        v = int(v)
        return f'（↑{v}）' if v > 0 else (f'（↓{abs(v)}）' if v < 0 else '（持平）')

    if g.get('bot_groups') is not None:
        lines.append(f'🏠 群组总数: {_n(g.get("bot_groups"))} 个'
                     f'{_net(g.get("bot_groups_delta"))}')
    if g.get('bot_friends') is not None:
        lines.append(f'🧑‍🤝‍🧑 好友总数: {_n(g.get("bot_friends"))} 人'
                     f'{_net(g.get("bot_friends_delta"))}')
    top_today = (g.get('top_games_today') or [])[:3]
    if top_today:
        lines.append('🔥 今日游戏榜:')
        lines += [f'  {i}、{t["game_name"]} ({t["count"]}局)'
                  for i, t in enumerate(top_today, 1)]
    top_players = (g.get('top_players_today') or [])[:3]
    if top_players:
        lines.append('👑 玩家参与榜:')
        lines += [f'  {i}、{p["display"]} ({p["count"]}局)'
                  for i, p in enumerate(top_players, 1)]
    trend = g.get('trend_10d') or []
    if trend:
        # 近10日的涨跌对比「上一个 10 日」整期(prev10_matches,不做时段对齐)
        total10 = sum(t['count'] for t in trend)
        lines.append(f'📅 近10日对局: {total10} 局'
                     f'{_delta(total10, g.get("prev10_matches"))}')
    pq = g.get('push_quota') or {}
    if pq.get('shown'):
        if pq.get('no_permission'):
            # 非全量群:额度数字无意义,给黄色警告 + 授权指引
            lines.append('⚠️ 本群未开启全量消息权限，无法推送主动消息')
            lines.append('   请 @机器人 发送「全量申请」完成授权')
        else:
            scope = '本群' if pq['is_group'] else '你的私信'
            if pq['limit']:
                if pq['exhausted']:
                    icon, tail = '⚠️', '（已用满，改用刷新按钮，次日 0 点恢复）'
                elif pq.get('near_limit'):
                    icon = '⚠️'
                    tail = f'（即将用尽，剩余 {pq["remaining"]} 条）'
                else:
                    icon, tail = '📮', ''
                lines.append(f'{icon} {scope}今日主动消息: '
                             f'{pq["used"]}/{pq["limit"]} 条{tail}')
            else:
                lines.append(f'📮 {scope}今日主动消息: {pq["used"]} 条')
    await event.reply('\n'.join(lines))


@handler(_P_TROUBLE,
         name='疑难解答',
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
    if helpers.is_foreign_event(event):
        return
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


# ──────── 赞助支持(默认关闭) ────────────────────────

_SPONSOR_NOTE = (
    '如果您有游戏投稿、更新建议，欢迎联系铁蛋 🌹'
)


@handler(_P_SPONSOR,
         name='赞助支持',
         desc='读取 data/sponsors.txt 实时返回赞助鸣谢名单（需开启 sponsor_enabled）',
         priority=50,
         block=True,
         event_types=_LGT_MSG_EVENTS | {INTERACTION_CREATE})
async def lgtbot_sponsor(event, match):
    """收到「赞助支持」(文本或按钮)→ 回鸣谢名单 + 简短说明 + 两个链接按钮。

    名单用**引用块**而非代码块渲染:引用保留 markdown 行内语法,管理员想给某个
    名字加粗或加 emoji,直接在 ``sponsors.txt`` 里写就行。

    ``buttons.SPONSOR_ENABLED`` 关闭时**完全隐藏本功能** —— 消息事件按
    ``lgtbot_about`` 的套路原样转发给引擎(``_from_exclusive=True`` 跳过专属指令
    闸),第三方部署方的用户只会看到引擎自己的响应,察觉不到插件里有赞助菜单。
    """
    if helpers.is_foreign_event(event):
        return
    if not buttons.SPONSOR_ENABLED:
        # INTERACTION 只可能来自历史消息里的旧按钮(开关关闭后不再生成按钮),ack 掉即可 —— 引擎没有对应的元指令可转发。
        if event.is_interaction:
            try:
                await event.ack_interaction(code=0)
            except Exception:
                pass
            return
        await lgtbot_dispatch(event, match, _from_exclusive=True)
        return
    if event.is_interaction:
        try:
            await event.ack_interaction(code=0)
        except Exception:
            pass
    md = (
        '## ❤️ 赞助鸣谢\n'
        '\n'
        f'{_as_quote(_read_sponsors())}\n'
        '\n'
        f'{_SPONSOR_NOTE}'
    )
    await event.reply(md, buttons=buttons.build_sponsor_buttons())


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
    if helpers.is_foreign_event(event):
        return
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
    if helpers.is_foreign_event(event):
        return
    if not state.started:
        return

    # GROUP_MESSAGE_CREATE 事件本身就是 QQ 给本 bot 在该群开了「全量消息」权限的直接证据(QQ 后台没开根本不会投递)
    # 比框架 non_at_message 配置(可能滞后 / 缺失)更准。note_group_message 除了记进 full_volume_groups,
    # 还借这个最快的变动信号顺带探一次主动推送权限(节流,详见 helpers)。
    if event.event_type == GROUP_MESSAGE_CREATE and event.group_id:
        helpers.note_group_message(event.group_id)

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

    # 屏蔽指令闸(内置 BUILTIN_BLOCKED_COMMANDS + config.yaml blocked_commands):其他插件的指令在此拦下,不再转发给引擎。
    if _is_blocked_command(content):
        log.info(f'🚫 [屏蔽指令] 命中屏蔽指令表，跳过引擎派发: {content[:30]!r}')
        return

    # 超级管理员权限闸:群管理员执行**非 %中断** 的管理指令 → 插件明确拒绝,
    # 不派发给引擎(见 _deny_super_admin_cmd 的取舍说明)。
    if _deny_super_admin_cmd(event, content, uid):
        log.info(f'🔒 [超管拒绝] 群管 {uid} 无超级管理员权限: {content[:30]!r}')
        denied = _super_admin_denied_text(uid)
        page_logs.log_incoming(uid, gid if event.is_group else '', content)
        page_logs.log_outgoing(gid or uid, not (event.is_group and gid), denied)
        await event.reply(denied)
        return

    # 「计划重启」维护闸:仅拦新建房间,回维护提示,不派发给引擎。
    # 放在 refresh_ref 之前 —— 该消息不进配额表,提示走消息自己的被动额度。
    # 底部挂官方群 / 问题反馈 link 按钮 —— 即将 execv 重启的场景下 link 按钮。
    if _should_block_new_game(content):
        page_logs.log_incoming(uid, gid if event.is_group else '', content)
        page_logs.log_outgoing(gid or uid, not (event.is_group and gid), '[计划重启维护提示]')
        await event.reply(_planned_restart_notice(), buttons=buttons.build_support_buttons())
        return

    # 昵称写回:框架自身按「首见即定」记录 users.name,这里补"最新化" ——
    # 与内存缓存比对,真变化才经 db_queue 落框架库(活跃跟踪已由框架完成)。
    if uid:
        userinfo.note_username(uid, getattr(event, 'username', '') or '')

    # 用户消息 → 用 msg_id 刷新被动引用配额（新一轮额度,群 5 条 / 私信 4 条）
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
        # 欢迎菜单走 event.reply,同样真实消耗上面刚 refresh 的 msg_id 一条引用额度(QQ 按 msg_id 计总数,不区分发送入口)。
        # 这里先把配额计数烧掉 1 条对齐,分支条件与上方 refresh_ref 完全镜像 —— refresh 没发生就不烧。
        if event.message_id:
            if event.is_group and gid:
                quota.try_consume_ref(helpers.target_key(gid, False))
            elif event.is_direct and uid:
                quota.try_consume_ref(helpers.target_key(uid, True))
        await _send_welcome_menu(event)
        return

    page_logs.log_incoming(uid, gid if event.is_group else '', content)

    # 单机局游戏名兜底:派发前从「/新游戏 X」命令抓游戏名(见 _capture_pending_game_name)
    _capture_pending_game_name(content, event, gid, uid)

    # 新群首次建房 → 额外推一条紧急公告(公告已启用且该群没通知过时才发)
    await _maybe_notify_urgent(event, content, gid)

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
#      只 ack + 刷新 event_id 配额,客户端看一个短暂 toast,新一轮被动额度立即续上。
#
#   2. 其他所有 data(欢迎菜单的「数字蜂巢/天赋云巢/...」、规则按钮、上下文按钮
#      等)—— 等同于用户主动发送 data 这段文字。lgtbot_interaction_dispatch
#      ack 后把 content 走 on_public_message / on_private_message 派进 C++ 引擎,
#      与 lgtbot_dispatch 的消息派发路径镜像,差异仅在配额用 event_id 续而非
#      msg_id(INTERACTION 没有 msg_id,但 event_id 是独立的一轮新额度)。
#
# 两个 handler 用互斥 regex 划分职责:relay 严格匹配 RELAY_BUTTON_DATA,
# dispatch 用负向先行 (?!) 排掉这个 sentinel。

@handler(rf'^{re.escape(quota.RELAY_BUTTON_DATA)}$',
         name='LGTBot 刷新按钮回调',
         desc='「🔄 刷新会话」按钮点击 → ack + 用新 event_id 续一轮被动回复额度',
         priority=-200,
         event_types={INTERACTION_CREATE})
async def lgtbot_interaction_relay(event, match):
    if helpers.is_foreign_event(event):
        return
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
        (INTERACTION 没 msg_id,但 event_id 同样能撑起一轮被动回复额度)
    """
    if helpers.is_foreign_event(event):
        return
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

    # 屏蔽指令闸(内置 + 配置)—— 其他插件的 callback 按钮 data 也会落进本 catch-all,与 lgtbot_dispatch 的闸对称。
    if _is_blocked_command(content):
        log.info(f'🚫 [屏蔽指令] 按钮回调命中屏蔽指令表，跳过引擎派发: {content[:30]!r}')
        return

    uid = event.user_id or ''
    gid = event.group_id or event.channel_id or ''

    # 超级管理员权限闸(与 lgtbot_dispatch 的闸对称)—— 按钮 data 也可能是 % 指令。
    if _deny_super_admin_cmd(event, content, uid):
        log.info(f'🔒 [超管拒绝] 群管 {uid} 无超级管理员权限(按钮): {content[:30]!r}')
        denied = _super_admin_denied_text(uid)
        page_logs.log_incoming(uid, gid if event.is_group else '', content)
        page_logs.log_outgoing(gid or uid, not (event.is_group and gid), denied)
        await event.reply(denied)
        return

    # 「计划重启」维护闸 —— 欢迎菜单的游戏快捷按钮(data 为 /新游戏 X)也从这里进引擎,与 lgtbot_dispatch 的闸对称。
    if _should_block_new_game(content):
        page_logs.log_incoming(uid, gid if event.is_group else '', content)
        page_logs.log_outgoing(gid or uid, not (event.is_group and gid), '[计划重启维护提示]')
        await event.reply(_planned_restart_notice(), buttons=buttons.build_support_buttons())
        return

    # 昵称写回(与 lgtbot_dispatch 对称)。按钮活跃本身由框架记录 ——
    # INTERACTION 也走 core/bot/event.py 的用户追踪;此处 username 常为空,
    # note_username 第一层闸直接返回,近零开销。
    if uid:
        userinfo.note_username(uid, getattr(event, 'username', '') or '')

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

    # 单机局游戏名兜底:与 lgtbot_dispatch 对称,按钮 data 若为「/新游戏 X」也抓一手
    _capture_pending_game_name(content, event, gid, uid)

    # 紧急公告通知:菜单「游戏快捷开局」按钮的 data 也是建房命令,与消息路径对称
    await _maybe_notify_urgent(event, content, gid)

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

    · 引擎未加载   → (True, 正在重启提示);没有 C++ 引擎要释放,直接换进程即可 ——
                     无编译产物 / 刚下载完预编译包待切换时,用户正是要靠重启让 boot
                     按最新 marker 重新加载产物,这里若拒绝重启用户就永远起不来。
    · 活跃 match   → (False, 拒绝原因);引擎保持运行
    · 否则         → (True, 正在重启提示),并已干净释放 C++ 引擎、把
                     state.started / boot.is_engine_running 置 False;
                     调用方接下来必须立刻调度 exec,否则插件会处于
                     「Python 还在 / C++ 引擎已无」的半残状态。
    """
    if not boot.LGTBOT_AVAILABLE:
        state.started = False
        return True, '🔁 LGTBot 正在重启（当前引擎未加载，将按最新构建来源重新加载）...'
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

def set_planned_mode(enable: bool, reason: str = '', auto: bool = False) -> tuple[bool, str]:
    """显式开 / 关「计划重启」维护模式,返回 (新状态, 提示文案)。

    ``reason`` 为维护原因,仅在开启时记录并展示给玩家(见
    ``_planned_restart_notice``);``auto=True`` 同时启用**自动重启**:watcher
    轮询进行中对局,全部结束并静默 30s 后自动执行重启(默认手动 ——
    管理员自己点重启)。两种模式对玩家的差别:手动模式拦下新建房间回维护
    提示;**自动模式不限制新游戏创建**(新局只是让自动重启顺延)。
    关闭时 reason / auto 一并清掉。回执带剩余进行中对局数。
    """
    reason = (reason or '').strip()
    auto = bool(auto and enable)
    state.set_planned_restart(enable, reason, auto)
    if enable:
        remaining = len(state.active_matches)
        log.warning(f'🚧 [计划重启] 维护模式已启用'
                    f'（剩余对局 {remaining}，自动重启 {"开" if auto else "关"}，'
                    f'{"不限制" if auto else "禁用"}新游戏创建）'
                    + (f'，原因：{reason}' if reason else ''))
        if auto:
            msg = (f'🚧 计划重启已启用（自动模式）：不限制新游戏创建'
                   f'（剩余进行中对局 {remaining} 局）。')
        else:
            msg = f'🚧 计划重启已启用：已禁用新游戏创建（剩余进行中对局 {remaining} 局）。'
        if reason:
            msg += f'\n📌 维护原因：{reason}'
        if auto:
            msg += '\n🤖 自动重启：在无人进行对局时自动执行重启。'
            _ensure_auto_restart_watcher()
        return True, msg
    log.warning('✅ [计划重启] 维护模式已取消：恢复新游戏创建')
    return False, '✅ 计划重启已取消：已恢复新游戏创建。'


def toggle_planned_restart(reason: str = '') -> tuple[bool, str]:
    """翻转「计划重启」维护模式(命令 / WebUI 入口,恒为手动重启模式)。"""
    return set_planned_mode(not state.is_planned_restart(), reason)


# ──────── 计划重启·自动重启 watcher ──────────────────────────────────────
# 轮询而非挂 cb_match_event 的对局结束分支 —— 引擎复用时 C++ 持有的是**旧 callbacks 模块**的回调
# (CLAUDE.md §5,发送失败统计踩过的同一课),挂在新模块的事件钩子不在旧回调执行路径上;
# 轮询读的 state.active_matches 是跨模块共享的持久 dict,对新旧代码一律成立。
#
# task 引用挂持久 dict:热重载重复调用 _ensure 不会二次起 task(旧 task 读同一份持久 flag,行为一致);模式关闭 / execv 后 task 自然终结。
_AUTO_WATCH_KEY = 'planned_auto_watcher'
_AUTO_WATCH_INTERVAL = 20.0
# 对局清空后的静默期:结算消息(终局赛况文本 / 图片)由 cb_send_* fire-and-forget 投递,还要排 per-target 发送队列 + 图床上传,
# 对局从 active_matches 移除的那一刻它们往往尚未送达 QQ —— 立即 execv 会把这些 pending 发送连同终局赛况一起丢掉。
# 持续空满该秒数才真正重启;期间已建房间开出新局则计时清零重来。
_AUTO_RESTART_GRACE = 30.0


def ensure_auto_restart_watcher_on_load() -> None:
    """@on_load 恢复入口:热重载后 planned + auto 仍开启且 watcher 已死时补拉起。

    正常情况下旧 watcher task(引用旧模块代码,读同一份持久 state)跨热重载
    继续服务,_ensure 幂等直接返回;仅在旧 task 因异常终止时真正重建。
    """
    if state.is_planned_restart() and state.is_planned_restart_auto():
        _ensure_auto_restart_watcher()


def _ensure_auto_restart_watcher() -> None:
    """幂等启动自动重启 watcher(无运行中 loop 时静默跳过,由下次开启补起)。"""
    p = boot._get_persistent()
    t = p.get(_AUTO_WATCH_KEY)
    if t is not None and not t.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning('[计划重启] 无运行中 asyncio loop,自动重启 watcher 未启动')
        return
    p[_AUTO_WATCH_KEY] = loop.create_task(_auto_restart_watcher())


async def _auto_restart_watcher() -> None:
    """「自动重启」轮询:planned + auto 开启期间每 20s 检查进行中对局。

    对局清空后**不立即**重启,先进入 ``_AUTO_RESTART_GRACE`` 秒静默期 ——
    终局赛况等结算消息此刻多半还在异步发送链路上(见常量注释),等它们送达后才 execv;静默期内出现新对局(已建房间开局)则计时清零。
    触发时走与手动重启完全相同的 ``check_and_prepare_restart`` 原子预检
    (release 内部还会二次确认引擎侧无对局),成功后计审计(SRC_AUTO)+重启指标,再调度 os.execv;预检被拒(race)回到等待重来。
    """
    empty_since = None
    while state.is_planned_restart() and state.is_planned_restart_auto():
        if state.active_matches:
            empty_since = None
        else:
            now = time.monotonic()
            if empty_since is None:
                empty_since = now
                log.info(f'🤖 [自动重启] 对局已全部结束,进入 '
                         f'{_AUTO_RESTART_GRACE:.0f}s 静默期(等待结算消息送达)')
            elif now - empty_since >= _AUTO_RESTART_GRACE:
                ok, _msg = check_and_prepare_restart()
                if ok:
                    reason = state.planned_restart_reason()
                    audit.record('restart', '自动重启',
                                 f'对局已全部结束，静默 {_AUTO_RESTART_GRACE:.0f}s 后自动执行计划重启'
                                 + (f'（原因：{reason}）' if reason else ''),
                                 src=audit.SRC_AUTO)
                    metrics.record_restart()
                    log.warning('🤖 [自动重启] 静默期结束，执行计划重启')
                    # 先把通知推到严重问题通知群(带维护原因;仅自动重启),await 保证 HTTP 往返在换进程之前完成。
                    await _notify_auto_restart(reason)
                    schedule_exec_after(0.5)
                    return
                empty_since = None          # 预检被拒(race 新开局)→ 重新计时
        await asyncio.sleep(_AUTO_WATCH_INTERVAL)


async def _notify_auto_restart(reason: str) -> None:
    """自动重启触发时向严重问题通知群推送一条说明(手动重启不推)。

    复用崩溃通知的通道语义:``CRASH_NOTIFY_GROUP`` 配置的群 + 主动消息
    (该群需全量权限,没权限会被 QQ 拒,仅 warning 不阻断重启)。
    整体限时 5s —— 通知只是锦上添花,不能卡住已经释放引擎的重启流程。
    """
    from . import callbacks as _callbacks   # 延迟取,拿 config 覆盖后的最新值
    notify_group = getattr(_callbacks, 'CRASH_NOTIFY_GROUP', '') or ''
    if not notify_group:
        return
    sender = helpers.get_sender('')
    if sender is None:
        return
    md = (
        '## 🔁 LGT-Bot 自动重启\n'
        '\n'
        '> 当前无人正在进行游戏\n'
        '> 计划重启（自动模式）已触发\n'
        '\n'
        + (f'📌 更新内容：\n- {reason}\n\n' if reason else '') +
        '⏳ 主进程即将重启\n'
        '预计 10 秒内自动恢复服务...'
    )
    try:
        from .webui import page_logs
        from . import log_attribution
        page_logs.log_outgoing(notify_group, False, md)
        with log_attribution.mark_outbound():
            await asyncio.wait_for(sender.send_to_group(notify_group, md), timeout=5.0)
    except Exception as e:
        log.warning(f'自动重启通知推送失败 ({notify_group}): {e}')


@handler(_P_MATCHLIST,
         name='LGTBot 赛事列表',
         desc='列出全部公开 / 私密赛事 (引擎元指令，限制为主人可用)',
         owner_only=True,
         event_types=_LGT_MSG_EVENTS | {INTERACTION_CREATE},
         priority=100,
         block=True)
async def lgtbot_match_list(event, match):
    """抢占引擎的「赛事列表」元指令并加主人权限,主人触发时原样透传给引擎。

    引擎的 show_matches(message_handlers.cc)会把**全部**公开 + 私密赛事列成表格,普通玩家不该看到别人的私密房间;
    框架的 ``owner_only=True`` 在非主人触发时直接回模板、不进函数体,主人触发才走到这里由引擎生成真正的列表。

    ``priority=100`` + ``block=True`` 抢在 catch-all(-100)之前;指令同时登记进
    ``_EXCLUSIVE_RES``,即便框架链语义变化,catch-all 也不会重复派发给引擎。
    """
    if helpers.is_foreign_event(event):
        return
    if event.is_interaction:
        try:
            await event.ack_interaction(code=0)
        except Exception:
            pass
    if not state.started:
        await event.reply('⏳ LGTBot 引擎尚未就绪，请稍后再试')
        return
    uid = event.user_id or ''
    gid = event.group_id or event.channel_id or ''
    # 本 handler block=True 抢在 catch-all 之前,catch-all 里的 refresh_ref 不会执行,
    # 必须自己登记本次事件的引用 —— 否则引擎生成的列表走插件配额通道时没有可用
    appid_str = event.appid or ''
    ref_type, ref_value = ('event_id', event.event_id) if event.is_interaction \
        else ('msg_id', event.message_id)
    if ref_value:
        if event.is_group and gid:
            quota.refresh_ref(helpers.target_key(gid, False), ref_type, ref_value, appid_str)
        elif event.is_direct and uid:
            quota.refresh_ref(helpers.target_key(uid, True), ref_type, ref_value, appid_str)
    # 引擎认的是带前导斜杠的元指令(META_COMMAND_SIGN),框架可能已把 / 剥掉,这里统一补齐再透传。
    cmd = '/赛事列表'
    page_logs.log_incoming(uid, gid if event.is_group else '', cmd)
    try:
        if event.is_group and gid:
            threading.Thread(
                target=boot.LGTBot_ElainaBot.on_public_message,
                args=(cmd, uid, gid),
                daemon=True,
            ).start()
        elif event.is_direct and uid:
            threading.Thread(
                target=boot.LGTBot_ElainaBot.on_private_message,
                args=(cmd, uid),
                daemon=True,
            ).start()
    except Exception as e:
        log.warning(f'派发赛事列表失败: {e}')


@handler(_P_ADMIN_INTERRUPT,
         name='LGTBot 管理中断',
         desc='群管理员可强制中断本群卡死的对局 (仅中断，不含其他管理权限)',
         event_types=_LGT_MSG_EVENTS | {INTERACTION_CREATE},
         priority=100,
         block=True)
async def lgtbot_admin_interrupt(event, match):
    """抢占引擎管理指令「%中断」,给**群管理员**开放且仅开放这一条管理能力。

    背景:引擎权限是单极的 —— ``bot_core.cc::HandleRequest`` 见 '%' 开头就查 `HasAdmin(uid)``,
    过了便放行整个 ``admin_cmds``(含 %清除战绩 / %荣誉 等破坏性指令)。
    把群管写进引擎 admins 会连带交出这些权限,故改在插件层做**受限代理**:

      · 群聊 + 请求者是群主 / 群管理(``event.member_role``)→ 把发给引擎的
        uid 换成**已配置的引擎管理员**(``config.ADMIN_UIDS[0]``),引擎因此
        放行 %中断;审计记录真实操作者。
      · 其他情况(普通群员 / 私信)→ **原样用请求者自己的 uid** 派发,由引擎
        自行裁决:本身在 admins 里(主人)照常执行,否则引擎回「未持有管理员权限」。
        插件不自造拒绝文案,语义与引擎保持一致。

    其他管理指令(%清除战绩 等)没有专属 handler,会走 catch-all 用请求者本人
    uid 进引擎 → 群管无权、被引擎拒绝,这正是期望行为。

    ``priority=100`` + ``block=True`` 抢在 catch-all(-100)之前;pattern 也登记
    进 ``_EXCLUSIVE_RES``,即便框架链语义变化 catch-all 也不会重复派发。
    """
    if helpers.is_foreign_event(event):
        return
    if event.is_interaction:
        try:
            await event.ack_interaction(code=0)
        except Exception:
            pass
    if not state.started:
        await event.reply('⏳ LGTBot 引擎尚未就绪，请稍后再试')
        return

    uid = event.user_id or ''
    gid = event.group_id or event.channel_id or ''
    role = getattr(event, 'member_role', '') or ''
    is_group_admin = bool(event.is_group and gid and role in _GROUP_ADMIN_ROLES)

    # 代理身份:群管理 → 用已配置的引擎管理员 uid 发给引擎;否则用本人 uid
    send_uid = uid
    proxied = False
    if is_group_admin:
        from . import config as _config
        engine_admins = _config.ADMIN_UIDS
        if engine_admins and uid not in engine_admins:
            send_uid = engine_admins[0]
            proxied = True
        elif not engine_admins:
            # 群管有资格,但没有可借用的引擎管理员身份 —— 属配置缺失,明确告知
            log.warning('%中断:群管理请求代理中断,但 config.yaml 的 admin_uids 为空')
            await event.reply('⚠️ 未配置 LGTBot 管理员，无法代为中断')
            return

    # 本 handler block=True 抢在 catch-all 之前,catch-all 的 refresh_ref 不会执行,
    # 必须自己登记本次事件的引用 —— 否则引擎的回执没有可用的被动配额(同赛事列表)。
    appid_str = event.appid or ''
    ref_type, ref_value = ('event_id', event.event_id) if event.is_interaction \
        else ('msg_id', event.message_id)
    if ref_value:
        if event.is_group and gid:
            quota.refresh_ref(helpers.target_key(gid, False), ref_type, ref_value, appid_str)
        elif event.is_direct and uid:
            quota.refresh_ref(helpers.target_key(uid, True), ref_type, ref_value, appid_str)

    cmd = (event.content or '').strip() or '%中断'
    page_logs.log_incoming(uid, gid if event.is_group else '', cmd)
    if proxied:
        # 审计里的游戏名分三种情形,别把「没有对局」写成「未知游戏」——
        # 事后追溯时要能分辨这次代理中断到底作用在哪局上:
        #   · 有名字(等待房间 / 已开局)      → 游戏名
        #   · 有对局但名字未知(单机局兜底失效)→ 未知游戏
        #   · 群里根本没有对局(引擎会回「该房间未进行游戏」)→ 无游戏
        key = helpers.target_key(gid, False)
        rec = state.active_matches.get(key) or {}
        game = state.current_game.get(key, '') or rec.get('game', '')
        if not game:
            game = '未知游戏' if rec else '无游戏'
        audit.record('match', '群管中断对局',
                     f'群 {gid} / 操作者 {uid} / 游戏 {game}', src=audit.SRC_CMD)
        log.info(f'🎮 [管理中断] 群管 {uid} 代理中断 群 {gid} 的对局({game})')
        # 登记一次性 @ 改写,把下一条回执的 mention 换回真正执行指令的群管
        # (见 callbacks.register_mention_rewrite:限本群 + 5s + 用完即弃)。
        from . import callbacks as _callbacks      # 函数内导入,避免模块级互引
        _callbacks.register_mention_rewrite(key, send_uid, uid)
    try:
        if event.is_group and gid:
            threading.Thread(
                target=boot.LGTBot_ElainaBot.on_public_message,
                args=(cmd, send_uid, gid),
                daemon=True,
            ).start()
        elif event.is_direct and uid:
            threading.Thread(
                target=boot.LGTBot_ElainaBot.on_private_message,
                args=(cmd, send_uid),
                daemon=True,
            ).start()
    except Exception as e:
        log.warning(f'派发管理中断失败: {e}')


@handler(_P_PLANNED,
         name='计划重启',
         desc='切换维护模式:暂停创建新游戏 (可带维护原因:计划重启 <原因>)',
         owner_only=True,
         event_types=_LGT_MSG_EVENTS,
         priority=100,
         block=True)
async def lgtbot_planned_restart(event, match):
    """主人切换「计划重启」维护模式 —— 重启前逐渐清空对局用。

    与真「重启」互补:先启用本模式挡住新房间,等进行中的对局自然结束,
    再发「重启」平滑换进程(重启后本模式自动恢复关闭)。

    「计划重启 <原因>」可带维护原因,原因会展示在玩家创建新游戏时收到的
    维护提示里(关闭维护模式时自动清空,故关闭指令不必带原因)。
    「计划重启 自动 <原因>」首词恰为「自动」时启用**自动重启**:不限制新
    游戏创建,全部对局结束并静默 30s 后自动执行重启(默认手动 —— 拦新建
    房间;原因含"自动"字样但不独立成词不受影响)。
    """
    if helpers.is_foreign_event(event):
        return
    raw = (match.group(1) or '').strip() if match and match.lastindex else ''
    auto = False
    reason = raw
    parts = raw.split(None, 1)
    if parts and parts[0] == '自动':
        auto = True
        reason = parts[1].strip() if len(parts) > 1 else ''
    _on, msg = set_planned_mode(not state.is_planned_restart(), reason, auto)
    audit.record('restart', '计划重启模式',
                 ('已开启维护模式' + (f'（原因：{reason}）' if reason else '')
                  + (' + 自动重启' if auto and _on else ''))
                 if _on else '已取消维护模式', src=audit.SRC_CMD)
    await event.reply(msg)


@handler(_P_RESTART,
         name='框架重启',
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
    if helpers.is_foreign_event(event):
        return
    ok, msg = check_and_prepare_restart()
    # record 同步写盘,任何 await 前已持久化 —— 重启换进程也不丢这些记录
    audit.record('restart', '重启 LGTBot', '' if ok else msg,
                 ok=ok, src=audit.SRC_CMD)
    if ok:
        metrics.record_restart()
    await event.reply(msg)
    if not ok:
        return

    async def _on_fail():
        try:
            await event.reply('❌ 重启时发生错误，引擎已释放但进程未替换，需手动重启，详情请查看控制台日志。')
        except Exception:
            pass

    schedule_exec_after(0.5, on_failure=_on_fail)
