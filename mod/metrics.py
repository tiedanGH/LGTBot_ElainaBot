#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""运行指标数据层 —— 持久计数器 + lgtbot.db 只读统计,供「📈 指标面板」与全员指令 /数据统计 共同消费。

计数器(跨 os.execv 重启不丢,文件即真相源):
  · 图床上传:总次数 / 失败次数(挂点 uploader._do_upload,唯一真实往返收敛点;
    dedup 缓存命中与未配置早退不计)
  · 引擎崩溃重启:累计 / 分信号 / 最近一次(挂点 callbacks.cb_lgtbot_crashed
    的 live 信号路径 + _belated_apology 的 marker 路径,与 marker 删除同生命
    周期恰一次;sig 归一化由调用侧完成)
  · 主动重启:面板按钮 / /重启 指令触发的 os.execv 重启次数 + 上次重启时间
    (挂两个重启入口的放行分支;崩溃自动重启计在崩溃项,不混入)
  · 配额压力:耗尽次数(TTL 内引用的被动条数真用完,**且无主动直推资格** —— 全量群 /
    沙箱私信可转主动消息,不计)/ 刷新等待超时次数(15s 未等到新引用强发降级)。
    挂 callbacks 发送路径而非 quota.py:quota 内分不清「无事件上下文」与「真耗尽」,
    拿不到全量群 / 沙箱判定,wait_and_consume 还会重复计数。
  · 今日主动消息:群聊 / 私信分开按日分桶(跨天自动清零),并按目标计数,
    供「平均每群 / 每用户」展示。挂 callbacks._deliver 的主动分支,只计送达的
    (无 msg_id/event_id 的推送:全量直推 / 沙箱直推 / 超时强发)。
  · 主动消息频控(40034100):退避后补发成功 / 等满上限仍被拒而丢弃的条数(挂 callbacks._deliver)。

持久化照 mod/audit.py 模式:threading.Lock + 整文件原子重写(tmp + os.replace)
+ 损坏改名 ``.corrupt_<ts>`` 留证 + **record 永不抛异常**(指标失败绝不影响
业务)。放 ``data/metrics/`` 子目录:框架配置入口非递归扫 data/ 根,不污染
配置列表;backup 打包白名单不含此目录(恢复备份不回滚指标)。

lgtbot.db 统计严格只读(``file:...?mode=ro`` URI,不产生 -wal/-shm 旁路文件),
每条 SQL 独立 try/except —— 单表缺失不拖垮整包。时间过滤沿用引擎自身惯用法:
``finish_time`` 由引擎以 ``datetime(CURRENT_TIMESTAMP,'localtime')`` 写入本地
时间字符串,直接与 ``datetime('now','localtime','start of day')`` 字符串比较。
"""

from __future__ import annotations

import difflib
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from core.base.logger import get_logger, PLUGIN

from . import boot, userinfo

log = get_logger(PLUGIN, 'LGTBot')

METRICS_DIR = os.path.join(boot.DATA_DIR, 'metrics')
METRICS_PATH = os.path.join(METRICS_DIR, 'metrics.json')

# snapshot() 的零值兜底 —— 文件缺失 / 缺 key / 损坏时对外形状恒定
_DEFAULTS = {
    'upload_total': 0,
    'upload_fail': 0,
    'crash_total': 0,
    'crash_by_sig': {},
    'last_crash_ts': 0,
    'last_crash_sig': '',
    'restart_total': 0,
    'last_restart_ts': 0,
    'quota_exhausted': 0,
    'quota_wait_timeout': 0,
    'send_fail_total': 0,
    'send_fail_all': 0,
    'send_fail_by_code': {},
    'rate_limit_recovered': 0,
    'rate_limit_dropped': 0,
}

_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────
# 持久计数器(照 audit.py:文件即真相源,原子重写,永不抛)
# ─────────────────────────────────────────────────────────────────────────

def _load_raw() -> dict:
    """读 metrics.json 为 dict。必须在持有 _lock 时调用。

    不存在 → {};损坏(解析失败 / 根不是 dict)→ 改名留证 + 返回 {}。
    """
    try:
        with open(METRICS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        log.warning('[metrics] metrics.json 根节点不是 dict,按损坏处理')
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning(f'[metrics] metrics.json 解析失败,按损坏处理: {e}')
    try:
        os.replace(METRICS_PATH, f'{METRICS_PATH}.corrupt_{int(time.time())}')
    except OSError:
        pass
    return {}


def _atomic_write(d: dict) -> None:
    """临时文件 + os.replace 原子落盘(同 audit._atomic_write)。"""
    os.makedirs(METRICS_DIR, exist_ok=True)
    tmp = METRICS_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, METRICS_PATH)


def _bump(mutator) -> None:
    """load → mutator(d) 原地修改 → 原子写。任何异常吞掉仅 log.warning。"""
    try:
        with _lock:
            d = _load_raw()
            mutator(d)
            _atomic_write(d)
    except Exception as e:
        log.warning(f'[metrics] 记录失败(不影响业务): {e}')


def record_upload(ok: bool) -> None:
    """一次真实图床往返(缓存命中不算)。"""
    def _m(d: dict) -> None:
        d['upload_total'] = int(d.get('upload_total') or 0) + 1
        if not ok:
            d['upload_fail'] = int(d.get('upload_fail') or 0) + 1
    _bump(_m)


def record_crash(sig_name: str, ts: int | None = None) -> None:
    """一次引擎崩溃重启。sig_name 为调用侧已归一化的可读名;
    ts 缺省当前时刻(belated 路径传 marker 自带的崩溃时刻)。"""
    def _m(d: dict) -> None:
        d['crash_total'] = int(d.get('crash_total') or 0) + 1
        by_sig = d.get('crash_by_sig')
        if not isinstance(by_sig, dict):
            by_sig = {}
        key = str(sig_name)
        by_sig[key] = int(by_sig.get(key) or 0) + 1
        d['crash_by_sig'] = by_sig
        d['last_crash_ts'] = int(ts or time.time())
        d['last_crash_sig'] = key
    _bump(_m)


def record_restart() -> None:
    """一次主动重启(面板按钮 / /重启 指令放行后的 os.execv 换进程)。

    record 同步写盘,返回即已持久化 —— 在调度 execv 之前调用即可保证
    重启后计数仍在。崩溃触发的自动重启由 record_crash 单独统计,不混入。
    """
    def _m(d: dict) -> None:
        d['restart_total'] = int(d.get('restart_total') or 0) + 1
        d['last_restart_ts'] = int(time.time())
    _bump(_m)


def record_quota_exhausted() -> None:
    """被动配额真耗尽且有实际影响:TTL 内引用的被动回复真用完(无上下文的推送不算),
    且目标无主动直推资格(全量群 / 沙箱私信可转主动消息,由调用方过滤不计)。"""
    def _m(d: dict) -> None:
        d['quota_exhausted'] = int(d.get('quota_exhausted') or 0) + 1
    _bump(_m)


def _empty_active_push(date: str) -> dict:
    return {'date': date, 'group_total': 0, 'dm_total': 0,
            'group_targets': {}, 'dm_targets': {}}


def record_active_push(target_id: str, is_uid: bool) -> None:
    """一次主动消息(无 msg_id/event_id 的推送)。

    按日分桶:桶内 date 不是今天时整桶重建(跨天自动清零);按目标累计
    条数,len(targets) 即今日去重目标数,供「平均每群 / 每用户」计算。
    """
    today = datetime.now().strftime('%Y-%m-%d')

    def _m(d: dict) -> None:
        ap = d.get('active_push')
        if not isinstance(ap, dict) or ap.get('date') != today:
            ap = _empty_active_push(today)
        kind = 'dm' if is_uid else 'group'
        ap[f'{kind}_total'] = int(ap.get(f'{kind}_total') or 0) + 1
        targets = ap.get(f'{kind}_targets')
        if not isinstance(targets, dict):
            targets = {}
        tid = str(target_id)
        targets[tid] = int(targets.get(tid) or 0) + 1
        ap[f'{kind}_targets'] = targets
        d['active_push'] = ap
    _bump(_m)


def active_push_today() -> dict:
    """今日主动消息概况(桶过期 / 缺失 / 异常一律返回零值):

    ``{group_total, group_targets_n, dm_total, dm_targets_n}``
    平均值(总数 ÷ 去重目标数)由展示层计算。
    """
    today = datetime.now().strftime('%Y-%m-%d')
    try:
        with _lock:
            ap = _load_raw().get('active_push')
        if not isinstance(ap, dict) or ap.get('date') != today:
            ap = _empty_active_push(today)
        group_targets = ap.get('group_targets')
        dm_targets = ap.get('dm_targets')
        return {
            'group_total': int(ap.get('group_total') or 0),
            'group_targets_n': len(group_targets) if isinstance(group_targets, dict) else 0,
            'dm_total': int(ap.get('dm_total') or 0),
            'dm_targets_n': len(dm_targets) if isinstance(dm_targets, dict) else 0,
        }
    except Exception as e:
        log.warning(f'[metrics] 主动消息概况读取失败: {e}')
        return {'group_total': 0, 'group_targets_n': 0, 'dm_total': 0, 'dm_targets_n': 0}


def active_push_used(target_id: str, is_uid: bool) -> int:
    """某个群 / 用户**今日**已用的主动消息条数(桶过期 / 缺失 / 异常一律 0)。

    与 ``record_active_push`` 同一份日分桶数据,桶内 date 不是今天即视为 0 ——
    跨天自动重置,无需定时任务。发送路径的限额判定(callbacks 的
    ``_active_push_allowed``)与「数据统计」的额度展示都读这里。
    """
    if not target_id:
        return 0
    today = datetime.now().strftime('%Y-%m-%d')
    try:
        with _lock:
            ap = _load_raw().get('active_push')
        if not isinstance(ap, dict) or ap.get('date') != today:
            return 0
        targets = ap.get('dm_targets' if is_uid else 'group_targets')
        if not isinstance(targets, dict):
            return 0
        return int(targets.get(str(target_id)) or 0)
    except Exception as e:
        log.warning(f'[metrics] 主动消息用量读取失败: {e}')
        return 0


def record_quota_wait_timeout() -> None:
    """刷新等待超时:等新引用 15s 未果,走强发 / 降级路径。"""
    def _m(d: dict) -> None:
        d['quota_wait_timeout'] = int(d.get('quota_wait_timeout') or 0) + 1
    _bump(_m)


# 不计入主数值的返回码(仍计入 ``send_fail_all`` 与 by_code 分布留证):
#   · 40034105 = 配额超时强发撞上「无主动消息权限」—— **预期**失败,已计入配额压力
#   · 40034100 = 主动消息频控 —— 出口会退避重发,真丢掉的由 record_rate_limit 计入主数值
SEND_FAIL_IGNORED_CODES = frozenset({40034105, 40034100})


def record_send_failure(code) -> None:
    """一次出站消息被 QQ 接口拒绝(``send_to_*`` 返回 ``ok=False``)。

    挂 callbacks 各出站调用点(``_note_send_result``):被动引用回复与主动
    直推同一条 ``_send_push`` 链路,两类失败都计。双口径:

      · ``send_fail_total``   非预期失败(面板大数字;排除 IGNORED_CODES,另加频控重发后仍丢弃的条数)
      · ``send_fail_all``     全部失败,含预期拒绝(面板小字)
      · ``send_fail_by_code`` 全部失败按返回码分布(文件留证,面板不展开)

    完整错误 message 在框架错误中心(``report_error_raw``)可查,这里不重复存。
    """
    ignored = False
    try:
        ignored = code is not None and int(code) in SEND_FAIL_IGNORED_CODES
    except (TypeError, ValueError):
        pass

    def _m(d: dict) -> None:
        d['send_fail_all'] = int(d.get('send_fail_all') or 0) + 1
        if not ignored:
            d['send_fail_total'] = int(d.get('send_fail_total') or 0) + 1
        by = d.get('send_fail_by_code')
        if not isinstance(by, dict):
            by = {}
        key = str(code) if code is not None else 'unknown'
        by[key] = int(by.get(key) or 0) + 1
        d['send_fail_by_code'] = by
    _bump(_m)


def record_rate_limit(dropped: bool) -> None:
    """一条主动消息撞上 40034100 之后的结局:退避后补发成功,或等满上限仍被拒而丢弃。

    丢弃同时计入 ``send_fail_total`` —— 每次被拒本身已被 IGNORED_CODES 排除,面板大数字只认真丢掉的。
    """
    def _m(d: dict) -> None:
        k = 'rate_limit_dropped' if dropped else 'rate_limit_recovered'
        d[k] = int(d.get(k) or 0) + 1
        if dropped:
            d['send_fail_total'] = int(d.get('send_fail_total') or 0) + 1
    _bump(_m)


def snapshot() -> dict:
    """全部计数(缺 key 按零值兜底)。异常时返回全零 dict。"""
    try:
        with _lock:
            raw = _load_raw()
        out = dict(_DEFAULTS)
        # 可变默认值(dict)换成新实例,避免把 _DEFAULTS 里的共享对象漏出去
        out['crash_by_sig'] = {}
        out['send_fail_by_code'] = {}
        for k, default in _DEFAULTS.items():
            v = raw.get(k, default)
            out[k] = v if isinstance(v, type(default)) else default
        return out
    except Exception as e:
        log.warning(f'[metrics] 快照读取失败: {e}')
        return dict(_DEFAULTS)


def mask_id(s: str, n: int = 3) -> str:
    """openid 脱敏:前 n 位 + **** + 后 n 位;过短原样返回。"""
    s = str(s or '')
    return s if len(s) <= n * 2 else f'{s[:n]}****{s[-n:]}'


# ─────────────────────────────────────────────────────────────────────────
# 不计分对局的临时账本(data/metrics/unranked.json)
# ─────────────────────────────────────────────────────────────────────────
#
# 引擎只在「正式局 + 倍率非 0 + 玩家 ≥2 + 已连库」时才 RecordMatch
# (match.cc::ApplyChildGameOverFromScores),不计分局压根不在 lgtbot.db 里。
# callbacks 在结算广播到达时补记到这里,query_game_stats 再合并进今日口径。
#
# 只留两天:今天供今日各卡与双榜,昨天**仅**供「昨日同时段」的涨跌对比;
# 历史窗口查询(数据统计MMDD / MM / YYYY / 总)仍然只读数据库,不掺账本。

UNRANKED_PATH = os.path.join(METRICS_DIR, 'unranked.json')

_UNRANKED_KEEP_DAYS = 2
# 私聊对局的广播按参与者逐个私发(match.h::MsgSenderBatchHandler),同一局的结算文本会到达多次。
# 同游戏 + 同参与者 + 同群且相隔这么近的两条不可能是两局。
_UNRANKED_DEDUP_S = 10


def _unranked_load() -> dict:
    """读 unranked.json 为 ``{日期: [条目]}``。必须在持有 _lock 时调用。"""
    try:
        with open(UNRANKED_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        log.warning('[metrics] unranked.json 根节点不是 dict,按损坏处理')
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning(f'[metrics] unranked.json 解析失败,按损坏处理: {e}')
    try:
        os.replace(UNRANKED_PATH, f'{UNRANKED_PATH}.corrupt_{int(time.time())}')
    except OSError:
        pass
    return {}


def _unranked_write(d: dict) -> None:
    """原子落盘(同 _atomic_write,只是换一个文件)。"""
    os.makedirs(METRICS_DIR, exist_ok=True)
    tmp = UNRANKED_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, UNRANKED_PATH)


def _unranked_prune(d: dict) -> dict:
    """只留今天与昨天 —— 更早的账本没有任何消费方。

    保留窗口按**墙上时钟**算,不按被写入那条的时间戳:补一条旧记录不该把今天的账本清掉。
    """
    today = datetime.now()
    keep = {(today - timedelta(days=i)).strftime('%Y-%m-%d')
            for i in range(_UNRANKED_KEEP_DAYS)}
    return {k: v for k, v in d.items() if k in keep}


def _same_match(row: dict, game: str, ids: list, gid: str) -> bool:
    return (str(row.get('game') or '') == game
            and sorted(str(p) for p in (row.get('players') or [])) == sorted(ids)
            and str(row.get('gid') or '') == gid)


def record_unranked_match(game: str, players, gid: str = '', ts: float | None = None) -> None:
    """记一局不计分对局。永不抛 —— 统计失败绝不影响发消息。

    ``players`` 是参与者 openid;``gid`` 私聊局为空串,与库里 group_id 为 NULL 同义。
    """
    try:
        now = float(time.time() if ts is None else ts)
        day = datetime.fromtimestamp(now).strftime('%Y-%m-%d')
        ids = [str(p) for p in (players or []) if p]
        game = str(game or '')
        gid = str(gid or '')
        with _lock:
            d = _unranked_load()
            rows = d.get(day)
            if not isinstance(rows, list):
                rows = []
            for row in reversed(rows):          # 新 → 旧,出窗即停
                try:
                    if float(row.get('ts') or 0) < now - _UNRANKED_DEDUP_S:
                        break
                except (TypeError, ValueError):
                    continue
                if _same_match(row, game, ids, gid):
                    return                      # 私发扇出的同一局,不是第二局
            rows.append({'ts': int(now), 'game': game, 'players': ids, 'gid': gid})
            d[day] = rows
            _unranked_write(_unranked_prune(d))
    except Exception as e:
        log.warning(f'[metrics] 不计分对局记录失败(不影响业务): {e}')


def unranked_window(start: datetime, end: datetime | None = None) -> dict:
    """``[start, end)`` 内的不计分对局汇总(``end`` 为 None = 到现在为止)。

    返回 ``{'matches', 'players', 'groups', 'games', 'player_counts'}``,
    其中 players / groups 是集合(要和库里的去重集合取并集,只给个数就没法去重)。
    """
    out = {'matches': 0, 'players': set(), 'groups': set(),
           'games': {}, 'player_counts': {}}
    lo = start.timestamp()
    hi = end.timestamp() if end is not None else None
    try:
        with _lock:
            d = _unranked_load()
    except Exception as e:
        log.warning(f'[metrics] 不计分账本读取失败: {e}')
        return out
    # 账本至多两天,直接扫全量按 ts 过滤,不必按日期键挑
    for rows in d.values():
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            try:
                ts = float(row.get('ts') or 0)
            except (TypeError, ValueError):
                continue
            if ts < lo or (hi is not None and ts >= hi):
                continue
            out['matches'] += 1
            gid = str(row.get('gid') or '')
            if gid:
                out['groups'].add(gid)
            name = str(row.get('game') or '')
            if name:
                out['games'][name] = out['games'].get(name, 0) + 1
            for p in row.get('players') or []:
                p = str(p)
                out['players'].add(p)
                out['player_counts'][p] = out['player_counts'].get(p, 0) + 1
    return out


# ─────────────────────────────────────────────────────────────────────────
# lgtbot.db 只读统计
# ─────────────────────────────────────────────────────────────────────────

_TODAY = "datetime('now','localtime','start of day')"

# 「昨日同时段」窗口:昨日 00:00 → 恰好 24 小时前,与「今日 00:00 → 现在」严格等长,供**增减标识**对比;
# 跟昨日全天比的话,没过完的今天永远显示假跌。
_YDAY_START = "datetime('now','localtime','start of day','-1 day')"
_YDAY_SAME = "datetime('now','localtime','-1 day')"

# 标量查询:key → SQL(同一只读连接一次查完)
_SCALAR_SQL = {
    'lgtbot_users':             'SELECT COUNT(*) FROM user',
    'lgtbot_matches':           'SELECT COUNT(*) FROM match',
    'lgtbot_match_attendances': 'SELECT COUNT(*) FROM user_with_match',
    'lgtbot_achievements':      'SELECT COUNT(*) FROM user_with_achievement',
    'today_matches':            f'SELECT COUNT(*) FROM match WHERE finish_time >= {_TODAY}',
    'yesterday_matches_same_span':  ('SELECT COUNT(*) FROM match '
                                     f'WHERE finish_time >= {_YDAY_START} '
                                     f'AND finish_time < {_YDAY_SAME}'),
    # 上一个 10 日([今天-19 天, 今天-9 天) 整天窗口)——「近10日对局」的涨跌对比基准。
    # 不做时段对齐:语义是"这一轮 10 天目前跑到哪了",一天内对比结果单调爬升。
    'prev10_matches':               ('SELECT COUNT(*) FROM match '
                                     "WHERE finish_time >= datetime('now','localtime','start of day','-19 days') "
                                     "AND finish_time < datetime('now','localtime','start of day','-9 days')"),
}

# 取去重集合而不是 COUNT:要和不计分账本取并集才能去重;窗口是一天,行数与日活同量级。
_SET_SQL = {
    'today_players':                ('SELECT DISTINCT uwm.user_id FROM user_with_match uwm '
                                     'JOIN match m ON m.match_id = uwm.match_id '
                                     f'WHERE m.finish_time >= {_TODAY}'),
    'today_groups':                 ('SELECT DISTINCT group_id FROM match '
                                     f"WHERE finish_time >= {_TODAY} "
                                     "AND group_id IS NOT NULL AND group_id != ''"),
    'yesterday_players_same_span':  ('SELECT DISTINCT uwm.user_id FROM user_with_match uwm '
                                     'JOIN match m ON m.match_id = uwm.match_id '
                                     f'WHERE m.finish_time >= {_YDAY_START} '
                                     f'AND m.finish_time < {_YDAY_SAME}'),
    'yesterday_groups_same_span':   ('SELECT DISTINCT group_id FROM match '
                                     f'WHERE finish_time >= {_YDAY_START} '
                                     f'AND finish_time < {_YDAY_SAME} '
                                     "AND group_id IS NOT NULL AND group_id != ''"),
}

# 「本周」= 近 7 天(含今天),与今日口径同为本地 00:00 边界
_WEEK = "datetime('now','localtime','start of day','-6 days')"

# 游戏局数总榜(全量)
_TOP_GAMES_ALL_SQL = ('SELECT game_name, COUNT(*) c FROM match '
                      'GROUP BY game_name ORDER BY c DESC LIMIT 10')
# 本周游戏榜(面板)/ 今日游戏榜(/数据统计 指令)
_TOP_GAMES_WEEK_SQL = ('SELECT game_name, COUNT(*) c FROM match '
                       f'WHERE finish_time >= {_WEEK} '
                       'GROUP BY game_name ORDER BY c DESC LIMIT 10')
# 今日双榜不带 LIMIT:要先和不计分账本合并再排序。今日的分组基数很小。
_TOP_GAMES_TODAY_SQL = ('SELECT game_name, COUNT(*) c FROM match '
                        f'WHERE finish_time >= {_TODAY} '
                        'GROUP BY game_name')
# 本周玩家参与榜(面板)/ 今日玩家参与榜(/数据统计 指令)
_TOP_PLAYERS_WEEK_SQL = ('SELECT uwm.user_id, COUNT(*) c FROM user_with_match uwm '
                         'JOIN match m ON m.match_id = uwm.match_id '
                         f'WHERE m.finish_time >= {_WEEK} '
                         'GROUP BY uwm.user_id ORDER BY c DESC LIMIT 10')
_TOP_PLAYERS_TODAY_SQL = ('SELECT uwm.user_id, COUNT(*) c FROM user_with_match uwm '
                          'JOIN match m ON m.match_id = uwm.match_id '
                          f'WHERE m.finish_time >= {_TODAY} '
                          'GROUP BY uwm.user_id')

# 对局趋势窗口:10 天(含今天,对齐排行榜 TOP10);每日对局数与每日活跃玩家同窗口,前端并排成一张表。
_TREND_WINDOW_DAYS = 10
_TREND_SINCE = f"datetime('now','localtime','start of day','-{_TREND_WINDOW_DAYS - 1} days')"
_TREND_MATCHES_SQL = ('SELECT date(finish_time) d, COUNT(*) c FROM match '
                      f'WHERE finish_time >= {_TREND_SINCE} '
                      'GROUP BY d ORDER BY d')
_TREND_PLAYERS_SQL = ('SELECT date(m.finish_time) d, COUNT(DISTINCT uwm.user_id) c '
                      'FROM user_with_match uwm '
                      'JOIN match m ON m.match_id = uwm.match_id '
                      f'WHERE m.finish_time >= {_TREND_SINCE} '
                      'GROUP BY d ORDER BY d')

# 面板「游戏数据」的近 10 日小字行 + 对局人次卡,只有 ``ten_day=True`` 才查:/数据统计 指令在事件循环里同步查库,用不上的不跑。
# 窗口同趋势图 / prev10_matches,口径同 _span_stats。user_with_match 没有 match_id 索引,每条 JOIN 都要扫整表,所以合成一条一次扫完。
_PREV10_SINCE = "datetime('now','localtime','start of day','-19 days')"
_TEN_DAY_SQL = {
    ('recent10_matches', 'recent10_groups', 'prev10_groups'): (
        f'SELECT SUM(finish_time >= {_TREND_SINCE}), '
        f"COUNT(DISTINCT CASE WHEN finish_time >= {_TREND_SINCE} THEN NULLIF(group_id, '') END), "
        f"COUNT(DISTINCT CASE WHEN finish_time < {_TREND_SINCE} THEN NULLIF(group_id, '') END) "
        f'FROM match WHERE finish_time >= {_PREV10_SINCE}'),
    ('recent10_players', 'prev10_players', 'recent10_attendances', 'prev10_attendances',
     'today_attendances', 'yesterday_attendances_same_span'): (
        f'SELECT COUNT(DISTINCT CASE WHEN m.finish_time >= {_TREND_SINCE} THEN uwm.user_id END), '
        f'COUNT(DISTINCT CASE WHEN m.finish_time < {_TREND_SINCE} THEN uwm.user_id END), '
        f'SUM(m.finish_time >= {_TREND_SINCE}), SUM(m.finish_time < {_TREND_SINCE}), '
        f'SUM(m.finish_time >= {_TODAY}), '
        f'SUM(m.finish_time >= {_YDAY_START} AND m.finish_time < {_YDAY_SAME}) '
        'FROM user_with_match uwm JOIN match m ON m.match_id = uwm.match_id '
        f'WHERE m.finish_time >= {_PREV10_SINCE}'),
}


def query_game_stats(ten_day: bool = False) -> dict:
    """lgtbot.db 游戏统计快照(只读)。任何失败不抛 —— 单项置 None/空 + errors。

    参与榜在此完成昵称解析(userinfo.display_name,主框架 users 表)与脱敏兜底(mask_id),
    原始 openid 不出本模块。榜单双口径:本周(近 7 天,面板展示)与今日
    (/数据统计 指令用)。trend_10d 恒 10 项(缺失日补 0,含今天,新→旧),
    每项含当日对局数与当日活跃玩家数。

    **今日与昨日同时段的各项 + 今日双榜合并了不计分对局**(见 unranked_window):
    这些局不在 lgtbot.db 里,不合并的话今日统计只报计分局。今日游戏榜的条目带 ``unranked`` 标志,
    真时表示该游戏今天的对局全是不计分的。累计项、本周 / 总榜、近 10 日与趋势图都只读数据库。

    ``ten_day`` 真时额外给出 _TEN_DAY_SQL 那几项(面板用),否则留 None。
    """
    out: dict = {
        'available': False,
        'errors': [],
        **{k: None for k in _SCALAR_SQL},
        **{k: None for k in _SET_SQL},
        **{k: None for keys in _TEN_DAY_SQL for k in keys},
        'top_games_all': [],
        'top_games_week': [],
        'top_games_today': [],
        'top_players_week': [],
        'top_players_today': [],
        'trend_10d': [],
    }
    if not os.path.isfile(boot.DB_PATH):
        out['errors'].append(f'lgtbot.db 不存在:{boot.DB_PATH}(引擎启动时自动创建)')
        return out
    conn = None
    try:
        conn = sqlite3.connect(f'file:{boot.DB_PATH}?mode=ro', uri=True, timeout=2.0)
        out['available'] = True

        failed: set = set()

        def _rows(sql: str, tag: str) -> list:
            try:
                return conn.execute(sql).fetchall()
            except sqlite3.OperationalError as e:
                out['errors'].append(f'{tag}:{e}')
                failed.add(tag)
                return []

        for key, sql in _SCALAR_SQL.items():
            rows = _rows(sql, key)
            out[key] = int(rows[0][0]) if rows else None

        # 去重集合先留着,下面和不计分账本取并集后才定数
        db_sets = {key: {str(r[0]) for r in _rows(sql, key)}
                   for key, sql in _SET_SQL.items()}

        def _games(sql: str, tag: str) -> list:
            return [{'game_name': str(g), 'count': int(c)} for g, c in _rows(sql, tag)]

        def _players(sql: str, tag: str) -> list:
            return [{'display': userinfo.display_name(str(uid)) or mask_id(str(uid)),
                     'count': int(c)} for uid, c in _rows(sql, tag)]

        out['top_games_all'] = _games(_TOP_GAMES_ALL_SQL, 'top_games_all')
        out['top_games_week'] = _games(_TOP_GAMES_WEEK_SQL, 'top_games_week')
        out['top_players_week'] = _players(_TOP_PLAYERS_WEEK_SQL, 'top_players_week')

        # ── 合并不计分对局(仅今日与昨日同时段两个窗口)──────────────────
        # 「近 10 日对局」与趋势图不掺:账本只有 2 天,混进 10 天的序列会让曲线与 prev10 的对比同时失真。
        now = datetime.now()
        today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        u_today = unranked_window(today0)
        u_yday = unranked_window(today0 - timedelta(days=1), now - timedelta(days=1))

        def _plus(key: str, extra: int) -> None:
            """库里查失败(None)时不补 —— 只有不计分的那半边数字会误导。"""
            if out[key] is not None:
                out[key] += extra

        _plus('today_matches', u_today['matches'])
        _plus('yesterday_matches_same_span', u_yday['matches'])
        for key, extra in (('today_players', u_today['players']),
                           ('today_groups', u_today['groups']),
                           ('yesterday_players_same_span', u_yday['players']),
                           ('yesterday_groups_same_span', u_yday['groups'])):
            out[key] = None if key in failed else len(db_sets[key] | extra)

        if ten_day:
            for keys, sql in _TEN_DAY_SQL.items():
                rows = _rows(sql, keys[0])
                if rows:
                    # SUM 在窗口内没有行时是 NULL
                    out.update({k: int(v or 0) for k, v in zip(keys, rows[0])})
            # 人次不去重,不计分那半边直接按参与者个数相加
            _plus('today_attendances', sum(u_today['player_counts'].values()))
            _plus('yesterday_attendances_same_span', sum(u_yday['player_counts'].values()))

        db_games = {str(g): int(c) for g, c in _rows(_TOP_GAMES_TODAY_SQL, 'top_games_today')}
        games = dict(db_games)
        for name, c in u_today['games'].items():
            games[name] = games.get(name, 0) + c
        # 全部对局都不计分的游戏才打标;既有计分又有不计分的只汇总,不打标
        out['top_games_today'] = [
            {'game_name': n, 'count': c, 'unranked': n not in db_games}
            for n, c in sorted(games.items(), key=lambda kv: (-kv[1], kv[0]))[:10]]

        players = {str(u): int(c)
                   for u, c in _rows(_TOP_PLAYERS_TODAY_SQL, 'top_players_today')}
        for uid, c in u_today['player_counts'].items():
            players[uid] = players.get(uid, 0) + c
        out['top_players_today'] = [
            {'display': userinfo.display_name(uid) or mask_id(uid), 'count': c}
            for uid, c in sorted(players.items(), key=lambda kv: (-kv[1], kv[0]))[:10]]

        # 10 日趋势:SQL 只返回有对局的日期,Python 端补零成恒 10 项,新→旧。
        matches_by_date = {str(d): int(c)
                           for d, c in _rows(_TREND_MATCHES_SQL, 'trend_10d')}
        players_by_date = {str(d): int(c)
                           for d, c in _rows(_TREND_PLAYERS_SQL, 'trend_players')}
        today = datetime.now().date()
        trend = []
        for i in range(_TREND_WINDOW_DAYS):
            ds = (today - timedelta(days=i)).strftime('%Y-%m-%d')
            trend.append({'date': ds,
                          'count': matches_by_date.get(ds, 0),
                          'players': players_by_date.get(ds, 0)})
        out['trend_10d'] = trend
    except Exception as e:
        out['errors'].append(f'打开 lgtbot.db 失败:{e}')
        out['available'] = False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


# ── 窗口统计:按日 / 按月 / 按年 / 累计总计共用一份 SQL ────────────────────
# 四个视图的**口径完全一致**,只有时间窗口不同(总计连窗口都没有):
#   · matches      窗口内已完成对局数
#   · players      窗口内去重玩家
#   · groups       窗口内活跃群聊(私聊局 group_id 为 NULL / 空,不计)
#   · attendances  窗口内**对局人次**(user_with_match 行数,不去重;这四个视图的第 4 张卡用它替代「近10日对局」)
#   · top_games / top_players  窗口内双榜,LIMIT 10
# SQL 只写一遍,复制四份必然漂移;各视图的公开函数只做键名改写(见 _prefixed)。

_SPAN_KEYS = ('matches', 'players', 'groups', 'attendances')


def _blank_span(*errors: str) -> dict:
    """空窗口结果(available=False),供库缺失 / 参数非法时早退。"""
    out: dict = {'available': False, 'errors': list(errors),
                 'top_games': [], 'top_players': []}
    out.update({k: None for k in _SPAN_KEYS})
    return out


def _span_stats(start: str | None, end: str | None) -> dict:
    """统计 ``[start, end)`` 窗口内的游戏数据;``start``/``end`` 为 None = 全量总计。

    失败语义同 ``query_game_stats``:available=False,或单项 None + errors 累加。
    """
    if not os.path.isfile(boot.DB_PATH):
        return _blank_span(f'lgtbot.db 不存在:{boot.DB_PATH}')
    out = _blank_span()
    if start is None or end is None:
        w_m = w_j = ''
        args: tuple = ()
    else:
        w_m = ' WHERE finish_time >= ? AND finish_time < ?'
        w_j = ' WHERE m.finish_time >= ? AND m.finish_time < ?'
        args = (start, end)
    join = 'FROM user_with_match uwm JOIN match m ON m.match_id = uwm.match_id'
    _gc = "group_id IS NOT NULL AND group_id != ''"
    w_g = f'{w_m} AND {_gc}' if w_m else f' WHERE {_gc}'

    conn = None
    try:
        conn = sqlite3.connect(f'file:{boot.DB_PATH}?mode=ro', uri=True, timeout=2.0)
        out['available'] = True

        def _rows(sql: str, tag: str) -> list:
            try:
                return conn.execute(sql, args).fetchall()
            except sqlite3.OperationalError as e:
                out['errors'].append(f'{tag}:{e}')
                return []

        def _scalar(sql: str, tag: str):
            rows = _rows(sql, tag)
            return int(rows[0][0]) if rows else None

        out['matches'] = _scalar(f'SELECT COUNT(*) FROM match{w_m}', 'matches')
        out['players'] = _scalar(
            f'SELECT COUNT(DISTINCT uwm.user_id) {join}{w_j}', 'players')
        out['groups'] = _scalar(
            f'SELECT COUNT(DISTINCT group_id) FROM match{w_g}', 'groups')
        out['attendances'] = _scalar(f'SELECT COUNT(*) {join}{w_j}', 'attendances')
        out['top_games'] = [
            {'game_name': str(gname), 'count': int(c)} for gname, c in _rows(
                f'SELECT game_name, COUNT(*) c FROM match{w_m} '
                'GROUP BY game_name ORDER BY c DESC LIMIT 10', 'top_games')]
        out['top_players'] = [
            {'display': userinfo.display_name(str(uid)) or mask_id(str(uid)),
             'count': int(c)} for uid, c in _rows(
                f'SELECT uwm.user_id, COUNT(*) c {join}{w_j} '
                'GROUP BY uwm.user_id ORDER BY c DESC LIMIT 10', 'top_players')]
    except Exception as e:
        out['errors'].append(f'打开 lgtbot.db 失败:{e}')
        out['available'] = False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


def _prefixed(span: dict, prefix: str) -> dict:
    """通用键 → ``<prefix>_matches`` / ``top_games_<prefix>`` 等各视图命名。"""
    out = {'available': span['available'], 'errors': span['errors']}
    for k in _SPAN_KEYS:
        out[f'{prefix}_{k}'] = span[k]
    out[f'top_games_{prefix}'] = span['top_games']
    out[f'top_players_{prefix}'] = span['top_players']
    return out


def query_game_stats_for_month(year: int, month: int) -> dict:
    """某个**自然月**的游戏统计(只读,供「数据统计MM」指令)。

    窗口 [当月 1 日 00:00, 次月 1 日 00:00) 全整天,键前缀 ``month_``。不含涨跌对比。
    """
    start = f'{year:04d}-{month:02d}-01 00:00:00'
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    end = f'{ny:04d}-{nm:02d}-01 00:00:00'
    out = _prefixed(_span_stats(start, end), 'month')
    out['month'] = f'{year:04d}-{month:02d}'
    return out


def query_game_stats_for_year(year: int) -> dict:
    """某个**自然年**的游戏统计(只读,供「数据统计YYYY」指令)。

    窗口 [当年 1 月 1 日 00:00, 次年 1 月 1 日 00:00),键前缀 ``year_``。
    """
    out = _prefixed(_span_stats(f'{year:04d}-01-01 00:00:00',
                                f'{year + 1:04d}-01-01 00:00:00'), 'year')
    out['year'] = f'{year:04d}'
    return out


def query_game_stats_total() -> dict:
    """**全部历史**累计的游戏统计(只读,供「数据统计总」指令),键前缀 ``total_``。

    不加任何时间条件 —— 于是「累计 = 各月之和」在口径上天然成立(同一套 SQL)。
    """
    return _prefixed(_span_stats(None, None), 'total')


def query_game_stats_for_date(date_str: str) -> dict:
    """某个**历史日期**的游戏统计(只读,供「数据统计MMDD」指令)。

    ``date_str`` 形如 ``'2026-08-02'``;窗口 [该日 00:00, 次日 00:00) 全整天,
    键前缀 ``day_``。双榜 LIMIT 10(今日视图是 5)。
    日期格式非法 → available=False + errors,不查库。
    """
    try:
        day = datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        out = _prefixed(_blank_span(f'日期格式非法:{date_str!r}'), 'day')
        out['date'] = date_str
        return out
    fmt = '%Y-%m-%d %H:%M:%S'
    out = _prefixed(_span_stats(day.strftime(fmt),
                                (day + timedelta(days=1)).strftime(fmt)), 'day')
    out['date'] = date_str
    return out


# ── 单游戏统计:「数据统计<游戏名>」 ──────────────────────────────────────────
# 同窗口视图只读数据库、不掺不计分账本。可查的游戏名就是 match 表里出现过的那些:
# 「近 7 日」同本周榜(含今天),上一个 7 日是紧挨着的前 7 天;趋势按 7 天一桶往前滚,最新一桶就是「近 7 日」。

GAME_TREND_WEEKS = 12
GAME_RANK_LIMIT = 10
# 实力排行的上榜门槛(局数):从高往低试,够格的凑满 GAME_RANK_LIMIT 人就用这一档,都凑不满用最后一档
POWER_MIN_TIERS = (10, 5, 3)
# 与候选按钮的容量一致:按钮最多 5 排,末排是「游戏列表」,候选最多 4 排 × 3 个
_GAME_SUGGEST_LIMIT = 12
# 输入超过这个长度就不可能是游戏名,只截前段去比,免得超长文本拖慢 difflib
_GAME_QUERY_MAX = 30

# 比对时忽略空白与书名号 / 引号:按钮文案里的游戏名带《》,用户常照抄过来
_GAME_NAME_NOISE = str.maketrans('', '', ' \t　 《》〈〉「」『』“”"\'‘’')

# 游戏名表(按累计局数降序)+ 每款游戏的近 7 日局数,同时供名字匹配与热度排名
_GAME_LIST_SQL = ('SELECT game_name, COUNT(*) c, SUM(finish_time >= ?) FROM match '
                  'GROUP BY game_name ORDER BY c DESC, game_name')
_GAME_MATCH_SQL = ('SELECT COUNT(*), SUM(user_count), MIN(user_count), MAX(user_count), '
                   "COUNT(DISTINCT NULLIF(group_id, '')), MAX(finish_time), "
                   'SUM(finish_time >= ? AND finish_time < ?), SUM(group_id = ?) '
                   'FROM match WHERE game_name = ?')
# 距今第几个 7 天桶(0 = 近 7 日);参数是今天的日期串
_WEEK_IDX = 'CAST((julianday(?) - julianday(date({col}))) / 7 AS INTEGER)'
_GAME_WEEKS_SQL = (f'SELECT {_WEEK_IDX.format(col="finish_time")} wk, COUNT(*) FROM match '
                   'WHERE game_name = ? AND finish_time >= ? GROUP BY wk')
# 玩家侧各项(人数 / 本群人数 / 每周活跃 / 两张榜 / 我的名次)都从这一条出:
# user_with_match 没有 match_id 索引,每条 JOIN 都要扫整表。趋势窗口外的对局归到 -1 桶,只计入累计。
# 第 4 列是各局「击败对手比例」之和:引擎的 rank_score 按得分升序给每组同分玩家记「之前的人数 × 2 + 本组人数」,
# n 人局从末名 1 到头名 2n-1,(rank_score - 1) / (2n - 2) 正好是击败的对手占比,同分的对手各算一半(score_calculation.cc::CalLevelScoreRank)。
_GAME_PLAYERS_SQL = (
    'SELECT uwm.user_id, CASE WHEN m.finish_time >= ? '
    f'THEN {_WEEK_IDX.format(col="m.finish_time")} ELSE -1 END wk, '
    'COUNT(*), SUM((uwm.rank_score - 1) * 1.0 / MAX(1, 2 * m.user_count - 2)), MAX(m.group_id = ?) '
    'FROM user_with_match uwm JOIN match m ON m.match_id = uwm.match_id '
    'WHERE m.game_name = ? GROUP BY uwm.user_id, wk')


def _norm_game_name(s: str) -> str:
    return str(s or '').translate(_GAME_NAME_NOISE).casefold()


def resolve_game_name(query: str, names) -> tuple:
    """把用户输入对到库里的游戏名,返回 ``(游戏名, 候选列表)``;没对上时游戏名为 ``''``。

    ``names`` 按热度降序。先原样比,再忽略大小写 / 空白 / 书名号比;都不中时给最多 12 个候选:
    名字互相包含的按热度排在前,再补字面相近的(错一两个字这类,difflib)。
    """
    names = [str(n) for n in names or () if n]
    q = str(query or '').strip()[:_GAME_QUERY_MAX]
    if q in names:
        return q, []
    nq = _norm_game_name(q)
    if not nq:
        return '', []
    by_norm: dict = {}
    for n in names:
        by_norm.setdefault(_norm_game_name(n), n)
    if nq in by_norm:
        return by_norm[nq], []
    out = [n for n in names if nq in _norm_game_name(n) or _norm_game_name(n) in nq]
    for k in difflib.get_close_matches(nq, list(by_norm), n=_GAME_SUGGEST_LIMIT, cutoff=0.5):
        if by_norm[k] not in out:
            out.append(by_norm[k])
    return '', out[:_GAME_SUGGEST_LIMIT]


def _blank_game_detail(*errors: str) -> dict:
    """单游戏统计的完整键集(查不到 / 查失败的项为 None 或空)。"""
    return {
        'available': False, 'errors': list(errors), 'found': False, 'suggestions': [],
        'game_name': '', 'game_count': None, 'rank': None, 'week_rank': None,
        'matches': None, 'attendances': None, 'avg_players': None,
        'min_players': None, 'max_players': None,
        'groups': None, 'group_matches': None, 'last_time': '',
        'week_matches': None, 'prev_week_matches': None,
        'players': None, 'group_players': None, 'week_players': None, 'prev_week_players': None,
        'trend_weeks': [], 'top_players': [], 'top_power': [], 'power_min': None, 'me': None,
    }


def _power_min(counts) -> int:
    """实力排行这次用哪一档门槛(见 POWER_MIN_TIERS)。"""
    counts = list(counts)
    for n in POWER_MIN_TIERS:
        if sum(1 for c in counts if c >= n) >= GAME_RANK_LIMIT:
            return n
    return POWER_MIN_TIERS[-1]


def _power_order(per_user: dict, need: int) -> tuple:
    """实力排行:``per_user`` 是 ``uid → [局数, 击败比例之和]``,返回 ``(uid → 比例, 排好序的 uid)``。

    只算局数满 ``need`` 的;比例按显示用的 2 位小数比较,显示相同的局数多的在前。
    不能按看不见的尾数排,会出现两行比例一样、局数少的反而在上面。
    """
    rate = {u: round(b / n * 100, 2) for u, (n, b) in per_user.items() if n >= need}
    return rate, sorted(rate, key=lambda u: (-rate[u], -per_user[u][0], u))


def query_game_detail(query: str, uid: str = '', gid: str = '',
                      now: datetime | None = None) -> dict:
    """「数据统计<游戏名>」的单游戏统计(只读)。失败语义同 ``_span_stats``。

    游戏名没对上 → ``found=False`` + ``suggestions``;对上了 → ``found=True``,``game_name`` 是库里的写法。
    ``uid`` / ``gid`` 是查询者与所在群(私信传空):``me``(查询者在两张榜上的名次,没玩过为 None)
    与 ``group_matches`` / ``group_players``(本群局数 / 人数)只在给了时才有;两张榜的条目带 ``me`` 标志。
    实力排行按场均击败对手比例(``rate``,百分数,2 位小数)排,局数满 ``power_min`` 才上榜,同比例局数多的在前
    (见 _power_order);查询者没满门槛时 ``me['power_rank']`` 为 None。
    ``trend_weeks`` 恒 ``GAME_TREND_WEEKS`` 项,新→旧,``start`` 是该桶第一天。
    """
    if not os.path.isfile(boot.DB_PATH):
        return _blank_game_detail(f'lgtbot.db 不存在:{boot.DB_PATH}')
    now = now or datetime.now()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    fmt = '%Y-%m-%d %H:%M:%S'
    week0 = (today0 - timedelta(days=6)).strftime(fmt)
    prev0 = (today0 - timedelta(days=13)).strftime(fmt)
    since = (today0 - timedelta(days=GAME_TREND_WEEKS * 7 - 1)).strftime(fmt)
    today = today0.strftime('%Y-%m-%d')
    out = _blank_game_detail()
    conn = None
    try:
        conn = sqlite3.connect(f'file:{boot.DB_PATH}?mode=ro', uri=True, timeout=2.0)
        # 游戏名表都查不出来就没法判断游戏在不在,整体按不可用处理
        games = conn.execute(_GAME_LIST_SQL, (week0,)).fetchall()
        out['available'] = True
        name, out['suggestions'] = resolve_game_name(query, [g for g, _c, _w in games])
        if not name:
            return out
        out['found'], out['game_name'] = True, name

        counts = {str(g): (int(c), int(w or 0)) for g, c, w in games}
        mine_c, mine_w = counts[name]
        out['game_count'] = len(counts)
        out['rank'] = 1 + sum(1 for c, _w in counts.values() if c > mine_c)
        out['week_rank'] = (1 + sum(1 for _c, w in counts.values() if w > mine_w)) if mine_w else None
        out['week_matches'] = mine_w

        def _rows(sql: str, args: tuple, tag: str):
            """失败返回 None(区别于没有行的 []),对应各项留 None。"""
            try:
                return conn.execute(sql, args).fetchall()
            except sqlite3.OperationalError as e:
                out['errors'].append(f'{tag}:{e}')
                return None

        row = _rows(_GAME_MATCH_SQL, (prev0, week0, gid or None, name), 'game_matches')
        if row:
            m, att, lo, hi, groups, last, prev, here = row[0]
            out.update(matches=int(m), attendances=int(att or 0),
                       min_players=lo, max_players=hi, groups=int(groups or 0),
                       last_time=str(last or ''), prev_week_matches=int(prev or 0),
                       group_matches=int(here or 0) if gid else None)
            out['avg_players'] = round(out['attendances'] / out['matches'], 2) if m else None

        wk_rows = _rows(_GAME_WEEKS_SQL, (today, name, since), 'game_trend')
        p_rows = _rows(_GAME_PLAYERS_SQL, (since, today, gid or None, name), 'game_players')
        per_user: dict = {}                 # uid → [局数, 击败比例之和]
        wk_players: dict = {}               # 桶号 → 去重玩家
        here_players: set = set()           # 在本群玩过的
        for u, k, n, beat, here in p_rows or []:
            u = str(u)
            acc = per_user.setdefault(u, [0, 0.0])
            acc[0] += int(n)
            acc[1] += float(beat or 0)
            if int(k) >= 0:
                wk_players.setdefault(int(k), set()).add(u)
            if here:
                here_players.add(u)

        if wk_rows is not None:
            wk_matches = {int(k): int(c) for k, c in wk_rows}
            out['trend_weeks'] = [
                {'start': (today0 - timedelta(days=7 * i + 6)).strftime('%Y-%m-%d'),
                 'matches': wk_matches.get(i, 0),
                 'players': None if p_rows is None else len(wk_players.get(i, ()))}
                for i in range(GAME_TREND_WEEKS)]
        if p_rows is None:
            return out

        out['players'] = len(per_user)
        out['group_players'] = len(here_players) if gid else None
        out['week_players'] = len(wk_players.get(0, ()))
        out['prev_week_players'] = len(wk_players.get(1, ()))

        def _who(u: str) -> str:
            return userinfo.display_name(u) or mask_id(u)

        by_count = sorted(per_user.items(), key=lambda kv: (-kv[1][0], kv[0]))
        out['top_players'] = [{'display': _who(u), 'count': n, 'me': u == uid}
                              for u, (n, _b) in by_count[:GAME_RANK_LIMIT]]
        need = out['power_min'] = _power_min(n for n, _b in per_user.values())
        rate, by_rate = _power_order(per_user, need)
        out['top_power'] = [{'display': _who(u), 'rate': rate[u], 'count': per_user[u][0], 'me': u == uid}
                            for u in by_rate[:GAME_RANK_LIMIT]]
        if uid and uid in per_user:
            n, b = per_user[uid]
            out['me'] = {
                'display': _who(uid), 'matches': n, 'rate': round(b / n * 100, 2),
                # 局数排行并列同名次:名次 = 比自己多的人数 + 1;实力排行就是榜上的先后
                'rank': 1 + sum(1 for c, _b in per_user.values() if c > n),
                'power_rank': by_rate.index(uid) + 1 if uid in rate else None,
            }
    except Exception as e:
        out['errors'].append(f'打开 lgtbot.db 失败:{e}')
        out['available'] = False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out
