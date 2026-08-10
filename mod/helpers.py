#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通用辅助：sender 查找 / 跨线程协程执行 / target_key / mention 美化"""

from __future__ import annotations
import re
import asyncio

from core.base.logger import get_logger, PLUGIN
from . import state, userinfo

log = get_logger(PLUGIN, 'LGTBot')

# 媒体消息（msg_type=7）的 content 字段是 QQ 协议层不解析 <@openid> 的纯文本，
# 图文同条场景下把 mention 退化为 "@昵称"（损失：无 ping 通知）
_MENTION_RE = re.compile(r'<@([^>\s]+)>')

# ──── markdown 危险字符转义(用户昵称等不可信内容 → 原生 md 消息) ──────────
# 本插件所有 QQ 消息默认按原生 markdown 发送,昵称里带 # / * / [ 等字符会被
# 当作语法解析(标题 / 加粗 / 链接),导致排版爆炸。统一加反斜杠转义 ——
# QQ 官方 markdown 已实测支持 \ 转义,渲染时只显示字符本身,昵称字形不变。
# ( ) ! . - + 等只在特定组合 / 行首才构成语法,且在昵称中太常见,不转义 ——
# [ ] 转义后 ``](`` 链接组合已不可能成立。< > 一并转义,防止昵称伪造
# <@openid> 提及或截断 C++ 侧 <昵称(uid)> 包装。
# str.translate 单趟替换:输入里的 \ 自身也被转义成 \\,新加的反斜杠不会被二次处理。
_MD_ESCAPE_TABLE = str.maketrans({c: '\\' + c for c in '#*_`~[]<>|\\'})


def sanitize_md_name(text: str) -> str:
    """给用户昵称等不可信文本中的 markdown 语法字符加反斜杠转义。

    **只在 markdown 语境使用**;注入点:
      · callbacks.cb_get_user_name   引擎播报里的昵称(引擎文本按 md 发送)
      · dispatcher.lgtbot_query_user 查询回执里的 **昵称**
    纯文本语境(媒体兜底 msg_type=7 / WebUI 消息日志)不转义;引擎文本因
    源头已转义,那些路径改用 ``strip_md_escapes`` 还原。
    """
    if not text:
        return text
    return text.translate(_MD_ESCAPE_TABLE)


# strip_md_escapes 的反向匹配:sanitize_md_name 加的 \X 序列(X 为转义集内字符)
_MD_UNESCAPE_RE = re.compile(r'\\([#*_`~\[\]<>|\\])')


def strip_md_escapes(text: str) -> str:
    """去掉 ``sanitize_md_name`` 加的反斜杠转义,还原纯文本。

    引擎文本里的昵称在源头(cb_get_user_name)已按 md 语境转义;走**非
    markdown** 出站路径时(媒体兜底 msg_type=7 的 content、WebUI 消息日志
    展示),反斜杠会原样露出 —— 调本函数把 ``\\X`` 还原为 ``X``。
    """
    if not text or '\\' not in text:
        return text
    return _MD_UNESCAPE_RE.sub(r'\1', text)


def target_key(target_id: str, is_uid: bool) -> str:
    """统一 target 标识：群消息 'g:<gid>'，私聊 'u:<uid>'"""
    return ('u:' if is_uid else 'g:') + target_id


def humanize_mentions(text: str) -> str:
    """把 <@openid> 转成 @昵称（用于图文消息 content）

    QQ msg_type=7 的 content 不解析 <@openid> 提及语法，会原样显示为字面字符串。
    本函数从 ``userinfo``(主框架 data.db users 表)取对应昵称替换,保持图文单条消息的同时让文字可读。未命中时退化为截短 uid 占位。
    """
    if not text or '<@' not in text:
        return text

    def _repl(m):
        uid = m.group(1)
        name = userinfo.get_name(uid)
        if name:
            # 输出只用于纯文本语境(媒体 caption / 日志),不做 md 转义
            return f'@{name}'
        # DB 未命中：截短 openid 占位
        return f'@{uid[:6]}…' if len(uid) > 6 else f'@{uid}'

    return _MENTION_RE.sub(_repl, text)


# ──── bot 绑定(config.yaml: bind_bot_appid) ──────────────────────────────
# 本插件所有出站消息 / 数据库读取都固定走**绑定 bot**:配置了且在线用配置的,否则回退框架第一个 bot。
# 解析每次调用惰性完成(bot 列表在框架侧可能晚于插件加载就绪,启动时缓存会拿到空)。
# 来自其他 bot 的入站事件由 ``is_foreign_event`` 在 dispatcher 各 handler 顶部静默挡掉。

# 两个权限位是**QQ 后台分别开通**的不同东西,一个群可能只有其中之一:
#   · is_full_access      全量消息权限 —— bot 收得到群里非 @ 的普通消息
#   · allow_proactive_msg 主动推送权限 —— bot 可不依赖被动 msg_id 主动发消息
# 已退群的记录不计:框架 _handle_group_del 只置 in_group=0,权限位原样残留,不排除会把早就退掉的群算进数量里。
_SQL_GROUP_PERMS = (
    'SELECT SUM(is_full_access = 1) AS full_n, '
    '       SUM(allow_proactive_msg = 1) AS push_n '
    'FROM groups_users WHERE COALESCE(in_group, 1) = 1'
)
# 老框架兜底(插件可能先于框架升级):旧表只有全量群,主动推送列不一定存在
_SQL_GROUP_PERMS_LEGACY = 'SELECT COUNT(*) AS full_n FROM full_access_groups'


def _count_group_perms(appid: str) -> dict:
    """统计指定 bot 的两类群权限数量,``{'full': int|None, 'push': int|None}``。

    数据是框架按实际收到的事件落库的 per-bot 持久事实,不依赖运行时集合,
    故每个 bot(含未绑定的)都能各自统计。bot 未加载 / 查询失败 → 两项均
    None(前端显 —)。老框架回退旧表时主动推送数无从得知,单独留 None。
    """
    none = {'full': None, 'push': None}
    try:
        from core.bot.manager import _bot_manager_ref
        if _bot_manager_ref is None:
            return none
        bot = _bot_manager_ref._bots.get(appid)
        if bot is None:
            return none
        try:
            rows = bot.log_service.query_data(_SQL_GROUP_PERMS)
        except Exception:
            rows = bot.log_service.query_data(_SQL_GROUP_PERMS_LEGACY)
        if not rows:
            return {'full': 0, 'push': 0}
        r = rows[0]
        missing = object()

        def _n(key):
            """列缺失 → None(旧表没有主动推送这一列,是**未知**);
            列在但值为 NULL → 0(SUM 在空表上返回 NULL,是真的一个都没有)。"""
            v = r.get(key, missing)
            return None if v is missing else int(v or 0)
        return {'full': _n('full_n') or 0, 'push': _n('push_n')}
    except Exception as e:
        log.warning(f'查询 bot {appid} 群权限数失败: {e}')
        return none


def list_framework_bots() -> list[dict]:
    """枚举主框架 bot.yaml 里配置的机器人,供面板选择。

    ``[{'appid', 'qq', 'full_volume', 'proactive'}]``:后两项分别是该 bot 的
    **全量消息**群数与**主动推送**群数(见 ``_count_group_perms`` —— 两种权限
    由 QQ 后台分别开通,数量通常不等,面板要各显各的)。未加载 / 查询失败为 None。
    """
    try:
        from core.base.config import cfg as core_cfg
        out = []
        for b in core_cfg.get_bot_configs() or []:
            appid = str(b.get('appid') or '').strip()
            if appid:
                perms = _count_group_perms(appid)
                out.append({
                    'appid': appid,
                    'qq': str(b.get('robot_qq') or '').strip(),
                    'full_volume': perms['full'],
                    'proactive': perms['push'],
                })
        return out
    except Exception as e:
        log.warning(f'枚举框架 bot 配置失败: {e}')
        return []


def get_bound_appid() -> str:
    """解析当前绑定 bot 的 appid。

    优先级:``state.bind_bot_appid`` 配置了且该 bot 已加载 → 用配置;
    否则框架第一个已加载 bot;无 bot 返回 ``''``。
    """
    try:
        from core.bot.manager import _bot_manager_ref
        if _bot_manager_ref is None or not _bot_manager_ref._bots:
            return ''
        bots = _bot_manager_ref._bots
        cfgd = (state.bind_bot_appid or '').strip()
        if cfgd and cfgd in bots:
            return cfgd
        return next(iter(bots.keys()))
    except Exception:
        return ''


def get_bound_bot():
    """返回绑定 bot 的 BotInstance;无可用 bot 返回 None。"""
    try:
        from core.bot.manager import _bot_manager_ref
        appid = get_bound_appid()
        if not appid or _bot_manager_ref is None:
            return None
        return _bot_manager_ref._bots.get(appid)
    except Exception:
        return None


def is_foreign_event(event) -> bool:
    """事件是否来自**非绑定** bot —— True 时调用方应静默 return(不打日志)。

    多 bot 部署下,其他 bot 收到的消息也会进到本插件的 handler;绑定后本插件
    只服务一个 bot,其余事件完全忽略(不回复、不计日志、不刷配额)。
    """
    bound = get_bound_appid()
    if not bound:
        return False   # 无 bot 可解析时不拦(单以防误伤;此时本来也无事件)
    return (event.appid or '') != bound


# 全量群 id 列表 —— 新框架的 groups_users(见 _SQL_GROUP_PERMS 注释),
# 与旧框架的 full_access_groups 兜底
_SQL_FULL_GROUPS = ('SELECT group_id FROM groups_users '
                    'WHERE is_full_access = 1 AND COALESCE(in_group, 1) = 1')
_SQL_FULL_GROUPS_LEGACY = 'SELECT group_id FROM full_access_groups'


def seed_full_volume_groups_from_db() -> int:
    """从绑定 bot 的 data.db 读全量群记录,整体替换运行时全量群集合。

    框架 ``core/bot/event.py::_record_full_access_group`` 按实际收到
    GROUP_MESSAGE_CREATE 落库(per-bot data.db),是跨进程重启的持久事实来源;
    这里在绑定生效时(启动 / 热重载 / 面板换绑)一次性载入,弥补
    ``state.full_volume_groups`` 进程重启即丢的缺口。

    只取**全量消息**权限(``is_full_access``),不含主动推送权限 —— 本集合的唯一
    用途是 ``is_full_volume_group``,决定要不要挂刷新按钮,那是被动消息的事。

    注意 ``state.full_volume_groups`` 是跨热重载持久 set(挂在 C++ 扩展上),
    必须**原地** clear+update,不能重新赋值。查询失败保留现状不清空。
    返回载入的群数量;bot 未就绪 / 查询失败返回 -1。
    """
    bot = get_bound_bot()
    if bot is None:
        return -1
    try:
        try:
            rows = bot.log_service.query_data(_SQL_FULL_GROUPS)
        except Exception:
            rows = bot.log_service.query_data(_SQL_FULL_GROUPS_LEGACY)
        gids = {str(r['group_id']) for r in rows if r.get('group_id')}
    except Exception as e:
        log.warning(f'读取绑定 bot 全量群失败: {e}')
        return -1
    state.full_volume_groups.clear()
    state.full_volume_groups.update(gids)
    return len(gids)


def get_bot_uin(appid: str = '') -> str:
    """从主框架 BotManager 拿 bot 的 QQ uin (``BotInstance.robot_qq``)。

    由 ``bot.yaml`` 每个 bot 节下的 ``robot_qq`` 字段配置(框架在
    ``core/bot/instance.py::__init__`` 里读出来挂到 BotInstance)。

    Args:
        appid: bot 的 appid;空 / 未加载时回退**绑定 bot**。

    Returns:
        uin 字符串。bot 未加载 / 字段未配置时返回 ``''``,调用方应能优雅降级
        (比如「邀我进群」按钮的链接里 ``robot_uin=`` 留空仍可生成 URL,
        QQ 点击后会提示无效 robot,用户回头查 bot.yaml 即可修复)。
    """
    try:
        from core.bot.manager import _bot_manager_ref
        if _bot_manager_ref is None or not _bot_manager_ref._bots:
            return ''
        bots = _bot_manager_ref._bots
        if appid and appid in bots:
            return getattr(bots[appid], 'robot_qq', '') or ''
        bot = get_bound_bot()
        if bot is not None:
            return getattr(bot, 'robot_qq', '') or ''
    except Exception as e:
        log.warning(f'获取 bot uin 失败: {e}')
    return ''


def get_sender(appid: str = ''):
    """从 BotManager 全局引用获取 MessageSender。

    appid 为空 / 未加载时返回**绑定 bot** 的 sender ——
    崩溃通知、补发道歉等无事件上下文的出站路径都固定从绑定 bot 发出。
    """
    try:
        from core.bot.manager import _bot_manager_ref
        if _bot_manager_ref is None or not _bot_manager_ref._bots:
            return None
        if appid and appid in _bot_manager_ref._bots:
            return _bot_manager_ref._bots[appid].sender
        bot = get_bound_bot()
        return bot.sender if bot is not None else None
    except Exception as e:
        log.warning(f'获取 sender 失败: {e}')
        return None


def run_coro_blocking(coro, timeout: float = 15.0):
    """C++ 工作线程 → asyncio 事件循环 的安全桥接（阻塞等待结果）"""
    loop = state.event_loop
    if loop is None or loop.is_closed():
        log.warning('事件循环不可用，丢弃协程')
        return None
    try:
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=timeout)
    except Exception as e:
        log.warning(f'协程执行异常: {e}')
        return None


def is_full_volume_group(gid: str) -> bool:
    """判断 ``gid`` 是否是「全量推送」群 —— 只信任**运行时观测**到的事实。

    判定唯一依据:``state.full_volume_groups`` 集合,由 dispatcher 在见到
    ``GROUP_MESSAGE_CREATE`` 事件时填入。

    为什么不再退回框架 ``non_at_message.{enabled,group_whitelist}`` 配置:

      · QQ 的全量推送权限是在 **QQ 官方 bot 管理后台**给单个 (bot, 群) 维度开
        的;开了之后 QQ 才会向 bot 投递 ``GROUP_MESSAGE_CREATE`` 事件。
      · 框架 ``non_at_message.*`` 配置只是「框架收到 non-AT 后,要不要派给
        非 ``ignore_at_check`` 插件」的二级开关 —— 它和 QQ 后台权限**不同步**,
        可以一边开一边关。
      · 当用户在 ``bot.yaml`` 里写了 ``group_whitelist``、但 QQ 后台并没真给
        权限时,该群永远不会有 ``GROUP_MESSAGE_CREATE`` 投来。这时 helper 若
        信任配置就会把非全量群误判为全量,引发**非全量群里漏挂刷新按钮、
        被动配额耗尽后乱走主动消息**(用户反馈的现象)。
      · 框架自身在 ``core/bot/event.py::_record_full_access_group`` 也是按
        实际收到 ``GROUP_MESSAGE_CREATE`` 来记录全量群的(内存 cache + SQLite
        ``groups_users.is_full_access``),并不查 ``non_at_message.*`` ——
        这进一步说明运行时观测才是 ground truth。

    取舍:进程首次启动后,第一次在某全量群收到 non-AT 消息前,helper 会暂时
    返回 False(空集合);该窗口里第一条引擎回复会按非全量逻辑挂刷新按钮 —— 视觉上多一个按钮,无功能损失。一旦任何 non-AT 消息到达,集合即标记,
    后续行为正确。
    """
    if not gid:
        return False
    try:
        return gid in state.full_volume_groups
    except Exception:
        return False
