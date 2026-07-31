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
  · 配额压力:耗尽次数(TTL 内引用 5 条真用完,**且无主动直推资格** —— 全量群 /
    沙箱私信配额满后可无缝转主动消息,无实际影响,不计)/ 刷新等待超时次数(15s
    未等到新引用强发降级)。挂 callbacks 发送路径 —— quota 模块内部无法区分
    「无事件上下文」与「真耗尽」,也拿不到全量群 / 沙箱判定,且 wait_and_consume
    内部重复调 try_consume 会重计,故不挂 quota.py。
  · 今日主动消息:群聊 / 私信分开按日分桶(跨天自动清零),并按目标计数,
    供「平均每群 / 每用户」展示。挂 callbacks 两条发送路径的主动分支
    (无 msg_id/event_id 的推送:全量直推 / 沙箱直推 / 超时强发)。

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
    """被动配额真耗尽且有实际影响:TTL 内引用的 5 条回复真用完(无上下文的推送不算),
    且目标无主动直推资格(全量群 / 沙箱私信可转主动消息、无影响,由调用方过滤不计)。"""
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


def snapshot() -> dict:
    """全部计数(缺 key 按零值兜底)。异常时返回全零 dict。"""
    try:
        with _lock:
            raw = _load_raw()
        out = dict(_DEFAULTS)
        out['crash_by_sig'] = {}
        for k, default in _DEFAULTS.items():
            v = raw.get(k, default)
            out[k] = v if isinstance(v, type(default)) else default
        return out
    except Exception as e:
        log.warning(f'[metrics] 快照读取失败: {e}')
        return dict(_DEFAULTS)


def mask_id(s: str, n: int = 3) -> str:
    """openid 脱敏(同主框架 dau):前 n 位 + **** + 后 n 位;过短原样返回。"""
    s = str(s or '')
    return s if len(s) <= n * 2 else f'{s[:n]}****{s[-n:]}'


# ─────────────────────────────────────────────────────────────────────────
# lgtbot.db 只读统计
# ─────────────────────────────────────────────────────────────────────────

_TODAY = "datetime('now','localtime','start of day')"

# 标量查询:key → SQL(前 4 个是原仪表盘「数据统计」区的基础 COUNT,随
# 指标面板特性一并搬到这里,同一只读连接一次查完)
_SCALAR_SQL = {
    'lgtbot_users':             'SELECT COUNT(*) FROM user',
    'lgtbot_matches':           'SELECT COUNT(*) FROM match',
    'lgtbot_match_attendances': 'SELECT COUNT(*) FROM user_with_match',
    'lgtbot_achievements':      'SELECT COUNT(*) FROM user_with_achievement',
    'today_matches':            f'SELECT COUNT(*) FROM match WHERE finish_time >= {_TODAY}',
    'today_players':            ('SELECT COUNT(DISTINCT uwm.user_id) FROM user_with_match uwm '
                                 'JOIN match m ON m.match_id = uwm.match_id '
                                 f'WHERE m.finish_time >= {_TODAY}'),
    # 私聊局 group_id 为 NULL,天然排除
    'today_groups':             ('SELECT COUNT(DISTINCT group_id) FROM match '
                                 f"WHERE finish_time >= {_TODAY} "
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
_TOP_GAMES_TODAY_SQL = ('SELECT game_name, COUNT(*) c FROM match '
                        f'WHERE finish_time >= {_TODAY} '
                        'GROUP BY game_name ORDER BY c DESC LIMIT 10')
# 本周玩家参与榜(面板)/ 今日玩家参与榜(/数据统计 指令)
_TOP_PLAYERS_WEEK_SQL = ('SELECT uwm.user_id, COUNT(*) c FROM user_with_match uwm '
                         'JOIN match m ON m.match_id = uwm.match_id '
                         f'WHERE m.finish_time >= {_WEEK} '
                         'GROUP BY uwm.user_id ORDER BY c DESC LIMIT 10')
_TOP_PLAYERS_TODAY_SQL = ('SELECT uwm.user_id, COUNT(*) c FROM user_with_match uwm '
                          'JOIN match m ON m.match_id = uwm.match_id '
                          f'WHERE m.finish_time >= {_TODAY} '
                          'GROUP BY uwm.user_id ORDER BY c DESC LIMIT 10')

# 对局趋势窗口:10 天(含今天,对齐排行榜 TOP10)。除每日对局数外,同窗口再查
# 每日活跃玩家(去重),前端并排成一张表。
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


def query_game_stats() -> dict:
    """lgtbot.db 游戏统计快照(只读)。任何失败不抛 —— 单项置 None/空 + errors。

    参与榜在此完成昵称解析(userinfo.get_name,主框架 users 表)与脱敏兜底(mask_id),
    原始 openid 不出本模块。榜单双口径:本周(近 7 天,面板展示)与今日
    (/数据统计 指令用)。trend_10d 恒 10 项(缺失日补 0,含今天,新→旧),
    每项含当日对局数与当日活跃玩家数。
    """
    out: dict = {
        'available': False,
        'errors': [],
        **{k: None for k in _SCALAR_SQL},
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

        def _rows(sql: str, tag: str) -> list:
            try:
                return conn.execute(sql).fetchall()
            except sqlite3.OperationalError as e:
                out['errors'].append(f'{tag}:{e}')
                return []

        for key, sql in _SCALAR_SQL.items():
            rows = _rows(sql, key)
            out[key] = int(rows[0][0]) if rows else None

        def _games(sql: str, tag: str) -> list:
            return [{'game_name': str(g), 'count': int(c)} for g, c in _rows(sql, tag)]

        def _players(sql: str, tag: str) -> list:
            return [{'display': userinfo.get_name(str(uid)) or mask_id(str(uid)),
                     'count': int(c)} for uid, c in _rows(sql, tag)]

        out['top_games_all'] = _games(_TOP_GAMES_ALL_SQL, 'top_games_all')
        out['top_games_week'] = _games(_TOP_GAMES_WEEK_SQL, 'top_games_week')
        out['top_games_today'] = _games(_TOP_GAMES_TODAY_SQL, 'top_games_today')
        out['top_players_week'] = _players(_TOP_PLAYERS_WEEK_SQL, 'top_players_week')
        out['top_players_today'] = _players(_TOP_PLAYERS_TODAY_SQL, 'top_players_today')

        # 10 日趋势:查询按存在的日期聚合,Python 端补零成恒 10 项。
        # 新→旧排列(今天在最前,越靠近的日期越靠前)。
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
