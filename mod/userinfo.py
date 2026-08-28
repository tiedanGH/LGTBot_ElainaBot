#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""主框架用户数据只读门面(替代旧 userdb 私有缓存)+ 昵称写回。

数据源(全部为主框架 per-bot 库,路径 ``data/log/<appid>/``):
  · ``data.db``       users(昵称) / members(好友) / groups_users(群内日活跃 JSON)
  · ``wakeup.db``     私信最后活跃日期(仅私信刷新)
  · ``statistics.db`` user_stats 终身消息统计(每日 04:00 聚合昨日,滞后一天)
  · ``message.db``    精确时间戳,仅日志留存期(默认 5 天)内可查
头像不落库 —— QQ 官方头像直链按 appid+openid 即时推导。

★ 消息数口径 ★
  框架聚合只计 ``at_bot != 0`` 的接收消息(``core/storage/statistics.py`` 的
  ``COALESCE(at_bot, 1) != 0``;写入见 ``core/bot/event.py``:
  ``at_bot = is_at_self if event_type == GROUP_MESSAGE_CREATE else True``)。
  于是私信与「@机器人」的群消息都计入,**全量群里没 @ 机器人的消息不计**。
  这是框架的既定口径(全量群闲聊与 bot 无关,计入会撑爆统计),本模块只如实透出。
  最后活跃日期不受影响 —— 用户追踪(``_enqueue_track``)不过 at_bot 闸。

★ 昵称语义 ★
  框架 ``users.name`` 首见即定(core/bot/event.py 的 upsert 带 ``WHERE name=''``
  守卫,之后不再更新;原作者确认为刻意设计)。本插件用 ``note_username`` 写回
  最新昵称:内存比对闸门 + 每用户写入冷却窗,实际落库 ≈ 改名事件频率,写走
  框架 ``db_queue`` 批量通道(不产生独立事务)。

★ 线程安全 ★
  ``get_name`` 会被 C++ 工作线程同步调用:``log_service.query*`` 使用独立只读
  连接(query_only=ON)+ 每库锁,任意线程安全;``_NAME_CACHE`` 的 dict 读写在
  GIL 下原子(C++ 线程读 / asyncio 线程写,最坏一次陈旧读,无需加锁)。

★ 无持久状态 ★
  本模块不持有任何连接(data.db/wakeup.db 连接归 log_service,statistics.db
  为 per-call 只读连接),缓存热重载后冷启动重查即可 —— 不进 boot._get_persistent()。

statistics.db 刻意**不走** ``log_service.query('statistics',…)``:那会顺带为它
建持久写连接并翻 WAL(_base.py:_get_read_conn 先调 _get_conn),侵入
StatisticsService 自管的文件;这里按 StatisticsService._open_ro 同款姿势自开
``mode=ro`` URI 连接,用完即关。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta

from core.base.logger import get_logger, PLUGIN

log = get_logger(PLUGIN, 'LGTBot')

# QQ 官方机器人头像直链(尺寸 40/100/140/640)
_AVATAR_URL_TPL = 'https://q.qlogo.cn/qqapp/{appid}/{openid}/{size}'

# 昵称缓存:仅存非空名。框架 users.name 一旦非空便不再被框架改写,
# 而"最新值"由 note_username 写回时同步刷进本缓存 —— 缓存即最新,可长期持有。
# 空名不缓存(用户首个事件可能漏 username,之后补上时必须能查到)。
_NAME_CACHE: dict[str, str] = {}
_NAME_CACHE_MAX = 4096          # 超限整体清空(重查便宜,避免复杂 LRU)

# 昵称写回的每用户冷却窗:窗内改名只更新缓存不落库(引擎/面板读缓存不受影响,仅框架库可见性延迟),给恶意刷昵称一个与消息频率无关的硬上限。
_WRITE_COOLDOWN_S = 600.0
_LAST_WRITE_TS: dict[str, float] = {}

# 写回 upsert:**无 WHERE 守卫**
# (框架自身的 guarded upsert 只在 name 为空时更新,两者任意先后顺序结果一致 —— 都以本条的最新 username 收尾)。
_NAME_UPSERT_SQL = ('INSERT INTO users (user_id, name) VALUES (?, ?) '
                    'ON CONFLICT(user_id) DO UPDATE SET name=excluded.name')


def _bound_bot():
    """绑定 bot 的 BotInstance;无可用 bot 返回 None(所有 API 兜底空值)。"""
    from . import helpers          # 函数内导入:helpers 顶层 import 本模块,避免环
    return helpers.get_bound_bot()


def _cache_put(openid: str, name: str) -> None:
    if len(_NAME_CACHE) >= _NAME_CACHE_MAX:
        _NAME_CACHE.clear()
    _NAME_CACHE[openid] = name


def clear_cache() -> None:
    """清空昵称缓存与写回冷却记录(测试 / 换绑 bot 时用)。"""
    _NAME_CACHE.clear()
    _LAST_WRITE_TS.clear()


# ──────── 读路径 ──────────────────────────────────────────────────────────

def get_name(openid: str) -> str:
    """查昵称:缓存 → 框架 data.db users 表。未命中 / 无 bot / 异常返回 ''。

    同步且线程安全(C++ 引擎线程直调):缓存命中零 I/O;未命中一次 PK SELECT。
    """
    if not openid:
        return ''
    cached = _NAME_CACHE.get(openid)
    if cached:
        return cached
    bot = _bound_bot()
    if bot is None:
        return ''
    try:
        rows = bot.log_service.query_data(
            'SELECT name FROM users WHERE user_id = ?', (openid,))
        name = str(rows[0].get('name') or '') if rows else ''
    except Exception as e:
        log.debug(f'userinfo.get_name 异常 ({openid}): {e}')
        return ''
    if name:
        _cache_put(openid, name)
    return name


def display_name(openid: str) -> str:
    """展示用昵称:判为违规时换成匿名。同步且线程安全,可从 C++ 引擎线程直调。"""
    name = get_name(openid)
    if not name:
        return name
    try:
        from . import nickname_review
        if nickname_review.should_mask(name):
            return nickname_review.masked_name(openid)
    except Exception as e:                  # 审核出问题就退回真名,绝不影响发消息
        log.debug(f'userinfo.display_name 审核查询失败 ({openid}): {e}')
    return name


def avatar_url(openid: str, size: int = 100) -> str:
    """按绑定 bot 的 appid 推导头像直链;无 appid / openid 返回 ''。"""
    if not openid:
        return ''
    from . import helpers
    appid = helpers.get_bound_appid()
    if not appid:
        return ''
    return _AVATAR_URL_TPL.format(appid=appid, openid=openid, size=size)


def get_group_names(gids) -> dict:
    """批量查群名:``{gid: group_name}``,只含真的查到非空名字的群。

    数据来自框架 ``groups_users.group_name``(``get_group_info`` 调 QQ 接口后
    落库)。**只读 DB 不碰接口** —— 群资料接口有频控,面板每次渲染都打会很快撞墙;
    框架自己会在入群 / 面板刷新时把名字写进来。

    一次 ``IN (...)`` 查完传入的全部群(进行中对局通常个位数),不逐个往返。
    无 bot / 异常 / 空入参一律返回 ``{}``,调用方自行降级。
    """
    gids = [str(g) for g in (gids or []) if g]
    if not gids:
        return {}
    bot = _bound_bot()
    if bot is None:
        return {}
    try:
        ph = ','.join('?' * len(gids))
        rows = bot.log_service.query_data(
            f'SELECT group_id, group_name FROM groups_users WHERE group_id IN ({ph})',
            tuple(gids))
    except Exception as e:
        log.debug(f'userinfo.get_group_names 异常: {e}')
        return {}
    out = {}
    for r in rows or []:
        gid = str(r.get('group_id') or '')
        name = str(r.get('group_name') or '').strip()
        if gid and name:
            out[gid] = name
    return out


def count_groups() -> int:
    """绑定 bot **当前所在**的群数量。

    与系统插件「用户统计」同源(``groups_users``),但多带 ``in_group`` 过滤 ——
    框架 ``_handle_group_del`` 只把该列置 0、不删行,不过滤会把早就退掉的群算进来。
    """
    bot = _bound_bot()
    if bot is None:
        return 0
    try:
        rows = bot.log_service.query_data(
            'SELECT COUNT(*) AS n FROM groups_users WHERE COALESCE(in_group, 1) = 1')
        return int(rows[0].get('n') or 0) if rows else 0
    except Exception as e:
        log.debug(f'userinfo.count_groups 异常: {e}')
        return 0


def count_friends() -> int:
    """绑定 bot 的好友数量(``members`` 表,同系统插件「用户统计」的好友总数)。

    注意这是**累计加过好友的人数**:框架 ``_handle_friend_del`` 只记生命周期事件,
    不从 members 删行 —— 口径由框架的数据模型决定,这里不自行修正,保持与「用户统计」一致。
    """
    bot = _bound_bot()
    if bot is None:
        return 0
    try:
        rows = bot.log_service.query_data('SELECT COUNT(*) AS n FROM members')
        return int(rows[0].get('n') or 0) if rows else 0
    except Exception as e:
        log.debug(f'userinfo.count_friends 异常: {e}')
        return 0


def today_lifecycle_delta() -> dict:
    """今日群 / 好友的**净变化**,``{'group': int|None, 'friend': int|None}``。

    数据源是框架按日分库的 ``lifecycle.db``(``<log>/<appid>/<date>/lifecycle.db``,
    入群 / 退群 / 加好友 / 删好友事件实时落库),**不读 dau 表** —— 后者今天的
    那一行要等聚合任务跑过才有,当天取不到。

    去重直接复用框架的 ``compute_lifecycle_counts``:同一个群 / 好友只看首末事件,
    「先加后删」互相抵消不计数 —— 与主框架可视统计逐项对得上,不自己另算一套。
    查不到 / 异常返回 None(前端与图片端显示为「无对比数据」而非 0)。
    """
    none = {'group': None, 'friend': None}
    bot = _bound_bot()
    if bot is None:
        return none
    try:
        from core.storage.lifecycle_stats import compute_lifecycle_counts
        rows = bot.log_service.query(
            'lifecycle', 'SELECT type, user_id, group_id FROM log ORDER BY id',
            date=datetime.now().strftime('%Y-%m-%d'))
        if not rows:
            return {'group': 0, 'friend': 0}     # 今天还没有任何生命周期事件
        c = compute_lifecycle_counts(
            (r.get('type', ''), r.get('user_id', ''), r.get('group_id', ''))
            for r in rows)
    except Exception as e:
        log.debug(f'userinfo.today_lifecycle_delta 异常: {e}')
        return none
    return {
        'group': int(c.get('group_join_count', 0)) - int(c.get('group_leave_count', 0)),
        'friend': int(c.get('friend_add_count', 0)) - int(c.get('friend_remove_count', 0)),
    }


def count_users() -> int:
    """框架 users 表总数(所有给绑定 bot 发过消息 / 点过按钮的用户)。"""
    bot = _bound_bot()
    if bot is None:
        return 0
    try:
        rows = bot.log_service.query_data('SELECT COUNT(*) AS n FROM users')
        return int(rows[0].get('n') or 0) if rows else 0
    except Exception as e:
        log.debug(f'userinfo.count_users 异常: {e}')
        return 0


# ──────── 昵称写回 ────────────────────────────────────────────────────────

def note_username(openid: str, username: str) -> None:
    """dispatcher 每条入站事件调用:昵称有变化时写回框架 users 表。

    四层闸门(逐层截流,常态只到第 2 层):
      1. username 空 → return(INTERACTION 常无 username)
      2. 与缓存相同 → return(热路径:一次 dict 比较,零 I/O)
      3. 缓存冷 → 读框架库比对,相等只填缓存不写
      4. 真变化 → 刷缓存 + 每用户 10 分钟冷却窗内最多一次 db_queue 写回
    """
    if not openid or not username:
        return
    if _NAME_CACHE.get(openid) == username:
        return
    if openid not in _NAME_CACHE:
        # 缓存冷:先看框架库已有值(每用户每进程最多一次 PK 查询)
        if get_name(openid) == username:
            return                          # 库里已是最新,get_name 已填缓存
    _cache_put(openid, username)            # 本地立即生效(引擎 / 面板读到最新)
    # 走到这一层 = 这个昵称本进程第一次见(新用户或刚改名)
    try:
        from . import nickname_review
        nickname_review.enqueue(username)
    except Exception as e:                  # 审核绝不能影响昵称写回本身
        log.debug(f'userinfo.note_username 送审入队失败 ({openid}): {e}')
    # 冷却判定:「从未写过」必须与「时刻 0 写过」区分 —— monotonic 起点是系统启动,
    # 刚开机的主机(如 CI runner)now 本身 < 冷却窗,用 0.0 兜底会把首次写回误判为冷却中。
    now = time.monotonic()
    last = _LAST_WRITE_TS.get(openid)
    if last is not None and now - last < _WRITE_COOLDOWN_S:
        return                              # 冷却窗内:只更新缓存,不落库
    bot = _bound_bot()
    if bot is None:
        return
    try:
        if len(_LAST_WRITE_TS) >= _NAME_CACHE_MAX:
            _LAST_WRITE_TS.clear()
        _LAST_WRITE_TS[openid] = now
        bot.log_service.db_queue(_NAME_UPSERT_SQL, (openid, username))
    except Exception as e:
        log.debug(f'userinfo.note_username 写回失败 ({openid}): {e}')


# ──────── statistics.db(per-call 只读连接) ───────────────────────────────

def _stats_rows(bot, sql: str, params: tuple = ()) -> list[dict]:
    """statistics.db 只读查询,返回 [dict]。文件不存在 / 异常返回 []。"""
    try:
        base = getattr(bot.log_service, '_base_dir', '') or ''
        path = os.path.join(base, 'statistics.db') if base else ''
        if not path or not os.path.isfile(path):
            return []
        conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=2.0)
        try:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()
    except Exception as e:
        log.debug(f'userinfo._stats_rows 异常: {e}')
        return []


def _max_daily_key(raw) -> str:
    """user_stats.daily_messages JSON 的最大日期键('' 表示无 / 解析失败)。"""
    try:
        d = json.loads(raw) if isinstance(raw, str) else (raw or {})
        return max(d.keys(), default='') if isinstance(d, dict) else ''
    except Exception:
        return ''


# ──────── 活跃度合并(列表 / 单用户共用口径) ──────────────────────────────
# 最后活跃 = 三个日粒度来源取 max(均为 'YYYY-MM-DD',字典序即时间序):
#   wakeup.last_msg_date(私信) / groups_users[].last_active(群内) /
#   user_stats.daily_messages 最大键(统计,滞后一天)。
# 精确时间戳仅 message.db 留存期内可得,由 last_active_exact 单用户查询。

def _group_activity(bot) -> dict[str, str]:
    """扫 groups_users 全部 JSON,反查 uid → 群内最大 last_active 日期。"""
    out: dict[str, str] = {}
    try:
        rows = bot.log_service.query_data(
            'SELECT users FROM groups_users WHERE in_group = 1')
    except Exception as e:
        log.debug(f'userinfo._group_activity 查询异常: {e}')
        return out
    for r in rows or []:
        try:
            entries = json.loads(r.get('users') or '[]')
        except Exception:
            continue                        # 单群 JSON 损坏跳过,不拖垮整体
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            uid = str(e.get('userid') or '')
            day = str(e.get('last_active') or '')
            if uid and day > out.get(uid, ''):
                out[uid] = day
    return out


def list_users(limit: int | None = None, offset: int = 0) -> list[dict]:
    """用户列表:以框架 ``users`` 表为**基准集**,按最后活跃日期倒序。

    基准集 = 与机器人交互过的用户(与 ``count_users`` 同口径,总数与行数一致,
    面板分页因此永远能翻到最后一名)。wakeup / 群名单 / 统计三源仅**补充**活跃
    日期与消息数,不新增行 —— ``groups_users`` 名单含仅入群未互动的成员
    (GROUP_MEMBER_ADD 进群事件直接入名单,不经过 users 表的消息/按钮追踪),
    若为其建行会让列表大于「总用户」。

    ``limit``/``offset`` 供面板分块拉取(每次 1000 条):合并与排序键跨三源,
    无法下推 SQL,**每次调用仍全量合并排序**后切片 —— 分块的收益在 payload
    体积与前端解析,不在服务端查询量。切片后才补 avatar(只为返回行拼 URL)。

    返回元素:``{'openid','name','avatar','total_messages','private_messages',
    'last_active_date'}``;total/private 无统计行时为 None(前端显 —)。
    无 bot 时返回 []。
    """
    bot = _bound_bot()
    if bot is None:
        return []
    recs: dict[str, dict] = {}
    try:
        for r in bot.log_service.query_data('SELECT user_id, name FROM users') or []:
            uid = str(r.get('user_id') or '')
            if uid:
                recs[uid] = {
                    'openid': uid, 'name': str(r.get('name') or ''),
                    'total_messages': None, 'private_messages': None,
                    'last_active_date': '',
                }
    except Exception as e:
        log.debug(f'userinfo.list_users users 查询异常: {e}')
    try:
        for r in bot.log_service.query(
                'wakeup', 'SELECT openid, last_msg_date FROM log') or []:
            rec = recs.get(str(r.get('openid') or ''))
            day = str(r.get('last_msg_date') or '')
            if rec is not None and day > rec['last_active_date']:
                rec['last_active_date'] = day
    except Exception as e:
        log.debug(f'userinfo.list_users wakeup 查询异常: {e}')
    for uid, day in _group_activity(bot).items():
        rec = recs.get(uid)
        if rec is not None and day > rec['last_active_date']:
            rec['last_active_date'] = day
    for r in _stats_rows(bot, 'SELECT userid, total_messages, private_messages, '
                              'daily_messages FROM user_stats'):
        rec = recs.get(str(r.get('userid') or ''))
        if rec is None:
            continue
        rec['total_messages'] = int(r.get('total_messages') or 0)
        rec['private_messages'] = int(r.get('private_messages') or 0)
        day = _max_daily_key(r.get('daily_messages'))
        if day > rec['last_active_date']:
            rec['last_active_date'] = day

    out = sorted(recs.values(),
                 key=lambda r: (r['last_active_date'], r['total_messages'] or 0),
                 reverse=True)
    if offset > 0:
        out = out[offset:]
    if limit is not None:
        out = out[:limit]
    for r in out:
        r['avatar'] = avatar_url(r['openid'])
        if r['name']:
            try:
                from . import nickname_review
                if nickname_review.should_mask(r['name']):
                    r['name'] = nickname_review.masked_name(r['openid'])
            except Exception:
                pass
    return out


def last_active_exact(openid: str) -> str:
    """留存期内的精确最后活跃时间('YYYY-MM-DD HH:MM:SS');查无返回 ''。

    从今天起倒扫 ``logging.retention_days``(默认 5)个日库,命中即停 ——
    idx_msg_user_agg(user_id,id,timestamp) 覆盖,单日探测 µs 级。
    """
    if not openid:
        return ''
    bot = _bound_bot()
    if bot is None:
        return ''
    try:
        from core.base.config import cfg
        retention = int(cfg.get('settings', 'logging.retention_days', 5) or 5)
    except Exception:
        retention = 5
    today = datetime.now().date()
    for i in range(max(1, retention)):
        d = (today - timedelta(days=i)).strftime('%Y-%m-%d')
        try:
            rows = bot.log_service.query(
                'message',
                "SELECT timestamp FROM log WHERE user_id = ? AND "
                "direction = 'receive' ORDER BY id DESC LIMIT 1",
                (openid,), date=d)
        except Exception as e:
            log.debug(f'userinfo.last_active_exact 查询异常 ({d}): {e}')
            continue
        if rows:
            return str(rows[0].get('timestamp') or '')
    return ''


def get_user(openid: str) -> dict | None:
    """单用户信息(查询id 指令用)。四源均查无返回 None。

    返回 ``{'openid','name','avatar','last_active_date','last_active_exact',
    'total_messages','private_messages'}``。
    """
    if not openid:
        return None
    bot = _bound_bot()
    if bot is None:
        return None
    found = False
    name = ''
    try:
        rows = bot.log_service.query_data(
            'SELECT name FROM users WHERE user_id = ?', (openid,))
        if rows:
            found = True
            name = str(rows[0].get('name') or '')
    except Exception as e:
        log.debug(f'userinfo.get_user users 查询异常: {e}')
    last_day = ''
    try:
        rows = bot.log_service.query(
            'wakeup', 'SELECT last_msg_date FROM log WHERE openid = ?', (openid,))
        if rows:
            found = True
            last_day = str(rows[0].get('last_msg_date') or '')
    except Exception as e:
        log.debug(f'userinfo.get_user wakeup 查询异常: {e}')
    gday = _group_activity(bot).get(openid, '')
    if gday:
        found = True
        if gday > last_day:
            last_day = gday
    total = private = None
    srows = _stats_rows(bot, 'SELECT total_messages, private_messages, '
                             'daily_messages FROM user_stats WHERE userid = ?',
                        (openid,))
    if srows:
        found = True
        total = int(srows[0].get('total_messages') or 0)
        private = int(srows[0].get('private_messages') or 0)
        sday = _max_daily_key(srows[0].get('daily_messages'))
        if sday > last_day:
            last_day = sday
    if not found:
        return None
    return {
        'openid': openid,
        'name': name,
        'avatar': avatar_url(openid),
        'last_active_date': last_day,
        'last_active_exact': last_active_exact(openid),
        'total_messages': total,
        'private_messages': private,
    }


def dm_active_count(days: int = 7) -> int:
    """近 ``days`` 日(含今天)私信过机器人的用户数(wakeup.db,日粒度)。"""
    bot = _bound_bot()
    if bot is None:
        return 0
    cutoff = (datetime.now().date() - timedelta(days=max(1, days) - 1)
              ).strftime('%Y-%m-%d')
    try:
        rows = bot.log_service.query(
            'wakeup', 'SELECT COUNT(*) AS n FROM log WHERE last_msg_date >= ?',
            (cutoff,))
        return int(rows[0].get('n') or 0) if rows else 0
    except Exception as e:
        log.debug(f'userinfo.dm_active_count 异常: {e}')
        return 0
