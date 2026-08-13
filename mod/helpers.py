#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通用辅助：sender 查找 / 跨线程协程执行 / target_key / mention 美化 / 群权限集合"""

from __future__ import annotations
import re
import time
import asyncio

from core.base.logger import get_logger, PLUGIN
from . import boot, state, userinfo

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
    """判断 ``gid`` 是否有「全量消息」权限 —— 只信任**运行时观测**到的事实。

    ⚠️ 这只回答「bot 收得到该群的全部消息吗」。**能不能主动发消息是另一回事**,
    由 ``can_push_group`` 按 ``allow_proactive_msg`` 判定 —— 刷新按钮、《消息
    回复限制》教学、配额耗尽后转主动消息全都走那一个,不要拿本函数当推送资格用。

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


# ──── 主动推送资格:按群号点查 + TTL 缓存 ────────────────────────────────
# 点查则只为**真正在用**的群付费:命中缓存 0.2µs,未命中一次 PK 索引查询(亚毫秒,且发消息本来就要走网络),缓存条目数取决于活跃群数(几十)而非总群数。
# 顺带解决两件事:① 不再依赖启动时机(冷启动 bot 未就绪只是一次 miss,30s 后自愈),② 群主新授予权限最迟 TTL 后生效,不必等下一轮全量刷新。
_SQL_GROUP_PUSH = ('SELECT allow_proactive_msg FROM groups_users '
                   'WHERE group_id = ? AND COALESCE(in_group, 1) = 1')
_PUSH_CACHE_KEY = 'group_push_cache'
_PUSH_TTL = 300.0        # 查到确切结果的缓存时长
_PUSH_MISS_TTL = 30.0    # bot 未就绪 / 查不到该群:短 TTL,别把「未知」当「无权限」钉死
_PUSH_CACHE_MAX = 512    # 超过则先清理过期项(活跃群数远小于此,正常不会触发)


def _push_cache() -> dict:
    """缓存挂持久字典 —— 跨热重载保留,省得每次改代码都把缓存打空。"""
    p = boot._get_persistent()
    c = p.get(_PUSH_CACHE_KEY)
    if c is None:
        c = p[_PUSH_CACHE_KEY] = {}
    return c


def invalidate_push_cache() -> None:
    """换绑 bot / 手动刷新后清空 —— 权限是 per-bot 的,换个 bot 结论全变。"""
    try:
        _push_cache().clear()
    except Exception:
        pass


# ──── 权限时效:主动向 QQ 拉一次 bot_state ────────────────────────────────
# 光靠读 DB 不够新:框架只在 **bot 入群** 与 **面板手动刷新群资料** 时才调 ``get_group_bot_state``
# 写 ``allow_proactive_msg``,群主在 QQ 后台授权后**没有任何事件**通知框架 —— DB 里那个 0 会一直躺着,
# 表现就是"授权很久了还在提醒消息回复受限"。所以这里自己去拉:框架那个方法本身就会把结果写回 DB,
# 我们只需在拉完后把该群的缓存打掉,下一次判定即读到新值。
#
# 触发点(都很便宜,且都带节流):
#   · can_push_group 得到否定结论时 —— 正是"可能已经授权但 DB 还没更新"的时刻
#   · dispatcher 收到 GROUP_MESSAGE_CREATE 时 —— 该事件本身就是全量消息权限的
#     直接证据,权限刚变动的可能性最高,借它做快速识别
_PUSH_PROBE_KEY = 'group_push_probe_at'   # gid → 上次发起探测的时刻
_PUSH_PROBE_INTERVAL = 60.0               # 每群最多每 60s 拉一次(群资料接口有频控)


def _probe_marks() -> dict:
    p = boot._get_persistent()
    m = p.get(_PUSH_PROBE_KEY)
    if m is None:
        m = p[_PUSH_PROBE_KEY] = {}
    return m


def refresh_group_push_permission(gid: str) -> None:
    """异步拉一次该群的 bot_state 刷新权限位(节流 + fire-and-forget)。

    同步可调 —— C++ 工作线程也会走到这里,协程用 run_coroutine_threadsafe 丢给
    事件循环,不阻塞调用方。拉取失败静默(下次节流窗口过后再试)。
    """
    if not gid:
        return
    try:
        marks = _probe_marks()
        now = time.time()
        last = marks.get(gid, 0.0)
        if now - last < _PUSH_PROBE_INTERVAL:
            return
        marks[gid] = now
        bot = get_bound_bot()
        loop = state.event_loop
        if bot is None or loop is None or loop.is_closed():
            return
        sender = getattr(bot, 'sender', None)
        if sender is None or not hasattr(sender, 'get_group_bot_state'):
            return
        asyncio.run_coroutine_threadsafe(_do_probe(sender, gid), loop)
    except Exception as e:
        log.debug(f'调度群 {gid} 权限刷新失败: {e}')


async def _do_probe(sender, gid: str) -> None:
    """调框架 ``get_group_bot_state``(它自己写回 DB),完成后打掉该群缓存。"""
    try:
        await sender.get_group_bot_state(gid, return_error=True)
    except Exception as e:
        log.debug(f'刷新群 {gid} bot_state 失败: {e}')
        return
    # 不看返回值:框架已把最新权限位落库,这里只负责让下次判定重新读 DB
    try:
        _push_cache().pop(gid, None)
    except Exception:
        pass


def note_group_message(gid: str) -> None:
    """收到 ``GROUP_MESSAGE_CREATE`` 时调用 —— 全量消息权限的直接证据。

    除了记进 ``full_volume_groups``,还借机探一次主动推送权限:两个权限通常
    在同一次授权流程里一起变动,这个事件是我们能拿到的**最快**的变动信号
    (否则要等 DB 被别的路径刷新)。节流由 ``refresh_group_push_permission`` 负责。
    """
    if not gid:
        return
    state.full_volume_groups.add(gid)
    # 已确知可推送的群不必再探(缓存命中且为 True)
    hit = _push_cache().get(gid)
    if hit is not None and hit[0] and time.time() < hit[1]:
        return
    refresh_group_push_permission(gid)


def can_push_group(gid: str) -> bool:
    """``gid`` 能否让 bot **不依赖刷新按钮**正常发消息(主动推送资格)。

    这是决定「挂不挂刷新按钮 / 发不发《消息回复限制》教学」的**唯一**判据。

    只认 ``allow_proactive_msg``,**不含**全量群 —— 两者是 QQ 后台分别开通的
    不同权限:全量消息只管 bot 收得到什么,能不能不引用 msg_id 主动发消息完全
    由主动推送权限决定。开了全量却没开主动推送的群,配额耗尽照样发不出去,
    该收到教学提示。

    数据来源是框架 ``groups_users.allow_proactive_msg``(``get_group_bot_state``
    查 QQ bot_state 接口后落库)。查不到 / bot 未就绪一律判 False 并短 TTL 重试,
    方向安全:宁可多挂一个按钮,也不要往没权限的群硬推(QQ 必拒且烧配额)。
    """
    if not gid:
        return False
    try:
        cache = _push_cache()
        now = time.time()
        hit = cache.get(gid)
        if hit is not None and now < hit[1]:
            return hit[0]

        bot = get_bound_bot()
        if bot is None:
            cache[gid] = (False, now + _PUSH_MISS_TTL)
            return False
        # query_data 失败时**返回 [] 而不抛**(框架 _base.query 吞掉异常),
        # 所以空结果既可能是"没有该群",也可能是"表不存在/查询出错" —— 两种
        # 都按无权限 + 短 TTL 处理,下次再试。
        rows = bot.log_service.query_data(_SQL_GROUP_PUSH, (gid,))
        if not rows:
            cache[gid] = (False, now + _PUSH_MISS_TTL)
            refresh_group_push_permission(gid)     # 该群还没被框架查过,拉一次
            return False
        ok = bool(rows[0].get('allow_proactive_msg'))
        if len(cache) >= _PUSH_CACHE_MAX:
            for k in [k for k, v in cache.items() if v[1] <= now]:
                cache.pop(k, None)
        # 否定结论只缓存 MISS_TTL 而非 TTL:DB 里的 0 可能只是"授权后还没人刷新过",
        # 同时主动拉一次 bot_state —— 真授权了的话下次判定就能翻过来
        cache[gid] = (ok, now + (_PUSH_TTL if ok else _PUSH_MISS_TTL))
        if not ok:
            refresh_group_push_permission(gid)
        return ok
    except Exception as e:
        log.warning(f'查询群 {gid} 主动推送权限失败: {e}')
        return False
