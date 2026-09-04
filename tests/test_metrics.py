#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""metrics 模块测试 —— 持久计数器(落盘/损坏容错/静默失败/并发)+
lgtbot.db 只读统计(今日过滤/排行/参与榜脱敏/7日趋势补零)。

游戏查询组在 conftest 假 boot 的 tmp ``DB_PATH`` 上建 db_manager.cc 同款
schema;``finish_time`` 用**本地时间字符串**构造今/昨/8 天前数据(与引擎
``datetime(CURRENT_TIMESTAMP,'localtime')`` 写入格式一致)。
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import pytest

# conftest.py 已 inject 假 boot,这里安全 import
from plugins.LGTBot_ElainaBot.mod import boot, metrics


@pytest.fixture(autouse=True)
def _clean_metrics():
    """每个测试前后清空指标目录与 tmp 引擎 db。"""
    def _wipe():
        shutil.rmtree(metrics.METRICS_DIR, ignore_errors=True)
        try:
            os.remove(boot.DB_PATH)
        except OSError:
            pass
    _wipe()
    os.makedirs(os.path.dirname(boot.DB_PATH), exist_ok=True)
    yield
    _wipe()


def _read_file() -> dict:
    with open(metrics.METRICS_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────────────────────
# 计数器组
# ─────────────────────────────────────────────────────────────────────────


def test_record_upload_counts_total_and_fail():
    metrics.record_upload(True)
    metrics.record_upload(False)
    metrics.record_upload(True)
    d = _read_file()
    assert d['upload_total'] == 3 and d['upload_fail'] == 1
    assert not os.path.exists(metrics.METRICS_PATH + '.tmp')   # 无残留
    snap = metrics.snapshot()
    assert snap['upload_total'] == 3 and snap['upload_fail'] == 1


def test_record_crash_by_sig_and_last():
    metrics.record_crash('SIGSEGV')
    metrics.record_crash('SIGSEGV')
    metrics.record_crash('sigabrt', ts=1700000000)
    snap = metrics.snapshot()
    assert snap['crash_total'] == 3
    assert snap['crash_by_sig'] == {'SIGSEGV': 2, 'sigabrt': 1}
    assert snap['last_crash_ts'] == 1700000000       # 显式 ts 生效
    assert snap['last_crash_sig'] == 'sigabrt'


def test_quota_counters():
    metrics.record_quota_exhausted()
    metrics.record_quota_exhausted()
    metrics.record_quota_wait_timeout()
    snap = metrics.snapshot()
    assert snap['quota_exhausted'] == 2
    assert snap['quota_wait_timeout'] == 1


def test_active_push_today_by_kind_and_targets():
    metrics.record_active_push('G1', is_uid=False)
    metrics.record_active_push('G1', is_uid=False)
    metrics.record_active_push('G2', is_uid=False)
    metrics.record_active_push('U1', is_uid=True)
    ap = metrics.active_push_today()
    assert ap['group_total'] == 3 and ap['group_targets_n'] == 2   # G1×2+G2
    assert ap['dm_total'] == 1 and ap['dm_targets_n'] == 1


def test_active_push_used_per_target():
    """per 群 / per 用户 用量:群与私信各自独立,未记录的目标为 0。"""
    metrics.record_active_push('G1', is_uid=False)
    metrics.record_active_push('G1', is_uid=False)
    metrics.record_active_push('G2', is_uid=False)
    metrics.record_active_push('G1', is_uid=True)     # 同名但私信侧,独立计数
    assert metrics.active_push_used('G1', False) == 2
    assert metrics.active_push_used('G2', False) == 1
    assert metrics.active_push_used('G1', True) == 1
    assert metrics.active_push_used('NOPE', False) == 0
    assert metrics.active_push_used('', False) == 0


def test_active_push_used_resets_across_day():
    """跨天:桶 date 不是今天 → 用量归 0(限额因此次日 0 点自动恢复,
    进行中的对局跨天也无需特殊处理 —— 每条消息独立判定,不缓存资格)。"""
    import json as _json
    metrics.record_active_push('GDAY', is_uid=False)
    assert metrics.active_push_used('GDAY', False) == 1
    with open(metrics.METRICS_PATH, encoding='utf-8') as f:
        d = _json.load(f)
    d['active_push']['date'] = '2000-01-01'          # 模拟跨天
    with open(metrics.METRICS_PATH, 'w', encoding='utf-8') as f:
        _json.dump(d, f)
    assert metrics.active_push_used('GDAY', False) == 0


def test_active_push_resets_across_day(monkeypatch):
    """桶 date 不是今天时整桶重建(跨天自动清零)。"""
    import json as _json
    metrics.record_active_push('G1', is_uid=False)
    # 手动把桶日期改成昨天,模拟跨天
    with open(metrics.METRICS_PATH, encoding='utf-8') as f:
        d = _json.load(f)
    d['active_push']['date'] = '2000-01-01'
    with open(metrics.METRICS_PATH, 'w', encoding='utf-8') as f:
        _json.dump(d, f)
    assert metrics.active_push_today() == {
        'group_total': 0, 'group_targets_n': 0, 'dm_total': 0, 'dm_targets_n': 0}
    # 过期桶不影响新计数
    metrics.record_active_push('U9', is_uid=True)
    ap = metrics.active_push_today()
    assert ap['dm_total'] == 1 and ap['group_total'] == 0


def test_active_push_without_file_is_zero():
    assert metrics.active_push_today() == {
        'group_total': 0, 'group_targets_n': 0, 'dm_total': 0, 'dm_targets_n': 0}


def test_record_restart_counts_and_last_ts():
    before = int(__import__('time').time())
    metrics.record_restart()
    metrics.record_restart()
    snap = metrics.snapshot()
    assert snap['restart_total'] == 2
    assert snap['last_restart_ts'] >= before


def test_snapshot_without_file_returns_all_zero():
    snap = metrics.snapshot()
    assert snap == {'upload_total': 0, 'upload_fail': 0, 'crash_total': 0,
                    'crash_by_sig': {}, 'last_crash_ts': 0, 'last_crash_sig': '',
                    'restart_total': 0, 'last_restart_ts': 0,
                    'quota_exhausted': 0, 'quota_wait_timeout': 0,
                    'send_fail_total': 0, 'send_fail_all': 0,
                    'send_fail_by_code': {}}


def test_corrupt_file_renamed_and_recovers():
    os.makedirs(metrics.METRICS_DIR, exist_ok=True)
    with open(metrics.METRICS_PATH, 'w', encoding='utf-8') as f:
        f.write('{ not valid json !!!')

    assert metrics.snapshot()['upload_total'] == 0            # 不抛
    assert glob.glob(metrics.METRICS_PATH + '.corrupt_*')     # 留证

    metrics.record_upload(True)                               # 从空续记
    assert _read_file()['upload_total'] == 1


def test_list_root_treated_as_corrupt():
    os.makedirs(metrics.METRICS_DIR, exist_ok=True)
    with open(metrics.METRICS_PATH, 'w', encoding='utf-8') as f:
        json.dump([1, 2, 3], f)
    assert metrics.snapshot()['crash_total'] == 0
    assert glob.glob(metrics.METRICS_PATH + '.corrupt_*')


def test_record_swallows_write_failure(monkeypatch):
    def _boom(d):
        raise OSError('disk full')
    monkeypatch.setattr(metrics, '_atomic_write', _boom)
    metrics.record_upload(True)          # 不应抛出
    monkeypatch.undo()
    metrics.record_upload(True)
    assert _read_file()['upload_total'] == 1


def test_concurrent_records_no_loss():
    threads = [threading.Thread(target=lambda: [metrics.record_upload(True)
                                                for _ in range(25)])
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()
    assert _read_file()['upload_total'] == 100


# ─────────────────────────────────────────────────────────────────────────
# 游戏查询组 —— db_manager.cc 同款 schema
# ─────────────────────────────────────────────────────────────────────────

_SCHEMA = [
    '''CREATE TABLE match(
        match_id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_name VARCHAR(100) NOT NULL, finish_time DATETIME,
        group_id VARCHAR(100), host_user_id VARCHAR(100) NOT NULL,
        user_count BIGINT UNSIGNED NOT NULL, multiple INT UNSIGNED NOT NULL)''',
    '''CREATE TABLE user_with_match(
        user_id VARCHAR(100) NOT NULL, birth_count INT UNSIGNED NOT NULL,
        match_id BIGINT UNSIGNED NOT NULL, game_score BIGINT NOT NULL,
        zero_sum_score BIGINT NOT NULL, top_score BIGINT NOT NULL,
        level_score DOUBLE NOT NULL, rank_score BIGINT NOT NULL,
        PRIMARY KEY (user_id, match_id))''',
    '''CREATE TABLE user(
        user_id VARCHAR(100) PRIMARY KEY, birth_time DATETIME,
        birth_count INT UNSIGNED DEFAULT 0, passwd VARCHAR(100))''',
    '''CREATE TABLE user_with_achievement(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id VARCHAR(100) NOT NULL,
        birth_count INT UNSIGNED NOT NULL, match_id BIGINT UNSIGNED NOT NULL,
        achievement_name VARCHAR(100) NOT NULL)''',
]


def _ts(days_ago: int = 0) -> str:
    """引擎同款本地时间字符串(取当天中午,避免跨午夜抖动)。"""
    d = datetime.now() - timedelta(days=days_ago)
    return d.strftime('%Y-%m-%d 12:00:00')


def _make_db(matches: list) -> None:
    """matches: [(game, days_ago | 'YYYY-MM-DD HH:MM:SS', group_id, players[])]
    → 建库插数。第二项传 int 走 _ts(当天中午);传 str 作为 finish_time 原样落库
    (「昨日同时段」窗口测试要用精确到时分秒的时间戳)。"""
    conn = sqlite3.connect(boot.DB_PATH)
    try:
        for sql in _SCHEMA:
            conn.execute(sql)
        for i, (game, when, group_id, players) in enumerate(matches, start=1):
            ts = when if isinstance(when, str) else _ts(when)
            conn.execute(
                'INSERT INTO match(match_id, game_name, finish_time, group_id,'
                ' host_user_id, user_count, multiple) VALUES (?,?,?,?,?,?,1)',
                (i, game, ts, group_id, players[0] if players else 'U0',
                 len(players)))
            for p in players:
                conn.execute(
                    'INSERT INTO user_with_match VALUES (?,0,?,0,0,0,0.0,0)',
                    (p, i))
        conn.commit()
    finally:
        conn.close()


def test_game_stats_today_filter_and_rankings():
    _make_db([
        ('五子棋', 0, 'G1', ['U1', 'U2']),      # 今日
        ('五子棋', 0, 'G2', ['U1', 'U3']),      # 今日
        ('大富翁', 0, None, ['U1']),            # 今日私聊局(group_id NULL)
        ('五子棋', 1, 'G1', ['U4', 'U5']),      # 昨日
        ('大富翁', 12, 'G3', ['U6']),           # 12 天前(趋势窗口外)
    ])
    g = metrics.query_game_stats()
    assert g['available'] is True
    assert g['lgtbot_matches'] == 5
    assert g['lgtbot_match_attendances'] == 8
    assert g['today_matches'] == 3
    assert g['today_players'] == 3               # U1/U2/U3 去重
    assert g['today_groups'] == 2                # G1/G2,NULL 不计
    # 局数总榜:五子棋 3 > 大富翁 2
    assert [(t['game_name'], t['count']) for t in g['top_games_all']] == \
           [('五子棋', 3), ('大富翁', 2)]
    # 本周榜(近 7 天,含昨日):五子棋 3 > 大富翁 1;12 天前不入
    assert [(t['game_name'], t['count']) for t in g['top_games_week']] == \
           [('五子棋', 3), ('大富翁', 1)]
    # 今日榜:五子棋 2 > 大富翁 1
    assert [(t['game_name'], t['count']) for t in g['top_games_today']] == \
           [('五子棋', 2), ('大富翁', 1)]
    # 今日玩家参与榜:U1 3 局居首
    assert g['top_players_today'][0]['count'] == 3


def test_record_send_failure_counts_and_ignores_expected_code():
    """发送失败双口径:total 排除 40034105(配额超时预期失败),all 全部计入;
    by_code 记全部分布,code 缺失归入 unknown。"""
    metrics.record_send_failure(40034102)
    metrics.record_send_failure(40034102)
    metrics.record_send_failure(22009)
    metrics.record_send_failure(None)
    metrics.record_send_failure(40034105)        # 预期失败 → 不进 total
    metrics.record_send_failure('40034105')      # 字符串形态同样识别
    snap = metrics.snapshot()
    assert snap['send_fail_total'] == 4
    assert snap['send_fail_all'] == 6
    assert snap['send_fail_by_code'] == {'40034102': 2, '22009': 1,
                                         'unknown': 1, '40034105': 2}


def test_yesterday_same_span_counts_only_matching_window():
    """「昨日同时段」= 昨日 00:00 → 恰 24h 前:窗口内计入,昨日**晚于**同时段
    的对局不计(否则白天时段跟昨日全天比永远假跌)。玩家数按窗口内去重。"""
    now = datetime.now()
    yday_same = now - timedelta(days=1)                       # 昨日同一时刻
    yday_start = yday_same.replace(hour=0, minute=0, second=0, microsecond=0)
    if (yday_same - yday_start).total_seconds() < 2:
        pytest.skip('恰在午夜,同时段窗口为空')
    fmt = '%Y-%m-%d %H:%M:%S'
    inside = (yday_start + (yday_same - yday_start) / 2).strftime(fmt)
    today_start = yday_start + timedelta(days=1)
    after_span = (yday_same + (today_start - yday_same) / 2).strftime(fmt)

    _make_db([
        ('五子棋', 0, 'G1', ['U1']),                 # 今天(不进昨日窗口)
        ('五子棋', inside, 'G1', ['U1', 'U2']),      # 昨日·同时段内 → 计入
        ('大富翁', inside, 'G1', ['U2']),            # 同上(U2 去重)
        ('五子棋', after_span, 'G1', ['U3']),        # 昨日·晚于同时段 → 不计
    ])
    g = metrics.query_game_stats()
    assert g['yesterday_matches_same_span'] == 2
    assert g['yesterday_players_same_span'] == 2      # U1 / U2
    assert g['yesterday_groups_same_span'] == 1       # 都在 G1
    assert g['today_matches'] == 1                    # 今日口径不受影响


def test_prev10_matches_counts_previous_block_only():
    """prev10_matches = [今天-19 天, 今天-9 天) 整天窗口:近 10 日(0-9 天前)
    不计,20 天前也不计。"""
    _make_db([
        ('五子棋', 0, 'G1', ['U1']),        # 近10日内
        ('五子棋', 9, 'G1', ['U1']),        # 近10日内(边界:9 天前属于本期)
        ('五子棋', 10, 'G1', ['U2']),       # 上一个10日(边界:10 天前属上期)
        ('五子棋', 19, 'G1', ['U3']),       # 上一个10日(最远边界)
        ('五子棋', 20, 'G1', ['U4']),       # 更早,不计
    ])
    g = metrics.query_game_stats()
    assert g['prev10_matches'] == 2
    assert sum(t['count'] for t in g['trend_10d']) == 2      # 本期(0/9 天前)


def test_query_game_stats_for_date_day_and_attendances():
    """历史日期查询:当日三项口径 + 当日对局人次(不去重)+ 双榜 LIMIT 10。"""
    d5 = (datetime.now() - timedelta(days=5)).strftime('%Y-%m-%d')
    mk = lambda days, hh: (datetime.now() - timedelta(days=days)).strftime(f'%Y-%m-%d {hh}:00:00')
    _make_db([
        ('五子棋', mk(5, '10'), 'G1', ['U1', 'U2']),   # 目标日
        ('大富翁', mk(5, '20'), 'G2', ['U1']),          # 目标日(晚间也算全天)
        ('五子棋', mk(6, '12'), 'G1', ['U3']),          # 前一天:任何当日口径不含
        ('五子棋', 0, 'G9', ['U9']),                    # 今天:任何口径都不含
    ])
    day = metrics.query_game_stats_for_date(d5)
    assert day['available'] is True
    assert day['day_matches'] == 2
    assert day['day_players'] == 2                      # U1/U2 去重
    assert day['day_groups'] == 2                       # G1/G2
    assert day['day_attendances'] == 3                  # U1×2 + U2,不去重
    # 目标日两款游戏各 1 局,并列时 SQLite 排序不稳定 → 集合断言
    assert {t['game_name'] for t in day['top_games_day']} == {'五子棋', '大富翁'}
    assert day['top_players_day'][0]['count'] == 2      # U1 两局居首


def test_query_game_stats_for_date_bad_input_and_missing_db():
    _make_db([('五子棋', 0, 'G1', ['U1'])])
    bad = metrics.query_game_stats_for_date('not-a-date')
    assert bad['available'] is False and bad['errors']


def test_query_game_stats_for_month_window_and_attendances():
    """月度查询:[当月1日, 次月1日) 全整天窗口;人次 = user_with_match 行数
    **不去重**(U1 两局计 2),玩家数照旧去重;群聊 NULL 不计;双榜 LIMIT 10。"""
    _make_db([
        ('五子棋', '2026-03-05 12:00:00', 'G1', ['U1', 'U2']),   # 月内
        ('大富翁', '2026-03-31 23:59:59', 'G2', ['U1']),          # 月内(末日边界)
        ('五子棋', '2026-03-01 00:00:00', None, ['U3']),          # 月内首刻,私聊局
        ('五子棋', '2026-02-28 23:59:59', 'G1', ['U4']),          # 上月 → 不计
        ('五子棋', '2026-04-01 00:00:00', 'G1', ['U5']),          # 次月首刻 → 不计
    ])
    mon = metrics.query_game_stats_for_month(2026, 3)
    assert mon['available'] is True
    assert mon['month_matches'] == 3
    assert mon['month_players'] == 3                  # U1/U2/U3 去重
    assert mon['month_attendances'] == 4              # U1×2 + U2 + U3,不去重
    assert mon['month_groups'] == 2                   # G1/G2,NULL 不计
    assert [(t['game_name'], t['count']) for t in mon['top_games_month']] == \
           [('五子棋', 2), ('大富翁', 1)]
    assert mon['top_players_month'][0]['count'] == 2  # U1 两局居首


def test_query_game_stats_for_year_window():
    """年度查询:[当年 1 月 1 日, 次年 1 月 1 日) —— 跨年边界两侧都不算进来。"""
    _make_db([
        ('五子棋', '2025-01-01 00:00:00', 'G1', ['U1']),          # 年内首刻
        ('大富翁', '2025-12-31 23:59:59', 'G2', ['U1', 'U2']),    # 年内末刻
        ('五子棋', '2024-12-31 23:59:59', 'G1', ['U3']),          # 上一年 → 不计
        ('五子棋', '2026-01-01 00:00:00', 'G1', ['U4']),          # 次年首刻 → 不计
        ('五子棋', '2025-06-01 12:00:00', None, ['U5']),          # 年内私聊局(NULL)
        # 空串 group_id:COUNT(DISTINCT) 只天然忽略 NULL,'' 会被计成一个群
        ('狼人杀', '2025-06-02 12:00:00', '', ['U6']),
    ])
    yr = metrics.query_game_stats_for_year(2025)
    assert yr['available'] is True and yr['year'] == '2025'
    assert yr['year_matches'] == 4
    assert yr['year_players'] == 4                    # U1/U2/U5/U6 去重
    assert yr['year_attendances'] == 5                # U1×2 + U2 + U5 + U6
    assert yr['year_groups'] == 2                     # G1/G2;NULL 与 '' 都不计
    # 榜首确定(2 局),1 局的三款并列 → SQLite 排序不稳定,用集合断言
    top = yr['top_games_year']
    assert (top[0]['game_name'], top[0]['count']) == ('五子棋', 2)
    assert {t['game_name'] for t in top} == {'五子棋', '大富翁', '狼人杀'}
    assert yr['top_players_year'][0]['count'] == 2    # U1 两局居首


def test_query_game_stats_total_covers_everything():
    """累计总计:不加任何时间条件 —— 多久以前的对局都计入。"""
    _make_db([
        ('五子棋', '2021-03-01 12:00:00', 'G1', ['U1']),
        ('五子棋', '2025-06-01 12:00:00', 'G2', ['U1', 'U2']),
        ('大富翁', 0, None, ['U3']),                   # 今天 + 私聊局(NULL)
        # 空串 group_id:``COUNT(DISTINCT)`` 会**照常把 '' 计成一个值**
        # (只有 NULL 被它天然忽略),所以 group_id != '' 这条过滤缺一不可
        ('狼人杀', 0, '', ['U4']),
    ])
    tot = metrics.query_game_stats_total()
    assert tot['available'] is True
    assert tot['total_matches'] == 4
    assert tot['total_players'] == 4                  # U1 去重
    assert tot['total_attendances'] == 5              # U1×2 + U2 + U3 + U4
    assert tot['total_groups'] == 2                   # G1/G2;NULL 与 '' 都不计
    assert {t['game_name'] for t in tot['top_games_total']} == \
        {'五子棋', '大富翁', '狼人杀'}


def test_total_equals_sum_of_year_windows():
    """★ 四个视图共用一份 SQL 的意义:累计 == 各年之和,口径不会各自漂移。

    以前按日 / 按月各自复制过一份 SQL,这条不变式就是那种复制的防线 ——
    任何一处窗口条件被改歪(比如群聊那条漏掉私聊局排除)都会让等式失衡。
    """
    _make_db([
        ('五子棋', '2024-05-01 10:00:00', 'G1', ['U1', 'U2']),
        ('大富翁', '2025-05-01 10:00:00', 'G1', ['U1']),
        ('狼人杀', '2025-07-01 10:00:00', None, ['U3']),
    ])
    tot = metrics.query_game_stats_total()
    y24 = metrics.query_game_stats_for_year(2024)
    y25 = metrics.query_game_stats_for_year(2025)
    assert tot['total_matches'] == y24['year_matches'] + y25['year_matches']
    assert tot['total_attendances'] == \
        y24['year_attendances'] + y25['year_attendances']


def test_query_game_stats_for_month_december_wraps_year():
    """12 月窗口上界跨年 → 次年 1 月 1 日 00:00(不含)。"""
    _make_db([
        ('五子棋', '2025-12-15 12:00:00', 'G1', ['U1']),
        ('五子棋', '2026-01-01 00:00:00', 'G1', ['U2']),   # 次年首刻,不计
    ])
    mon = metrics.query_game_stats_for_month(2025, 12)
    assert mon['month_matches'] == 1 and mon['month_players'] == 1


def test_top_players_nickname_and_mask(monkeypatch):
    _make_db([('五子棋', 0, 'G1', ['user_openid_abcdefgh', 'U2']),
              ('五子棋', 0, 'G1', ['user_openid_abcdefgh']),
              ('五子棋', 3, 'G1', ['user_openid_abcdefgh'])])    # 本周内、非今日
    monkeypatch.setattr(metrics.userinfo, 'get_name',
                        lambda uid: '铁蛋' if uid == 'user_openid_abcdefgh' else '')
    g = metrics.query_game_stats()
    # 本周口径含 3 天前那局;今日口径只含 2 局
    assert g['top_players_week'][0] == {'display': '铁蛋', 'count': 3}
    assert g['top_players_week'][1] == {'display': 'U2', 'count': 1}   # ≤6 字符原样
    assert g['top_players_today'][0] == {'display': '铁蛋', 'count': 2}

    monkeypatch.setattr(metrics.userinfo, 'get_name', lambda uid: '')
    g2 = metrics.query_game_stats()
    assert g2['top_players_week'][0]['display'] == 'use****fgh'        # 脱敏兜底


def test_trend_10d_always_ten_zero_filled_with_players():
    _make_db([('五子棋', 0, 'G1', ['U1']),
              ('五子棋', 2, 'G1', ['U1', 'U2']),
              ('五子棋', 2, 'G1', ['U1']),
              ('大富翁', 12, 'G1', ['U9'])])    # 窗口外
    g = metrics.query_game_stats()
    trend = g['trend_10d']
    assert len(trend) == 10
    today = datetime.now().date()
    # 新→旧:今天在最前(index 0),9 天前在最后
    assert trend[0]['date'] == today.strftime('%Y-%m-%d')
    assert trend[-1]['date'] == (today - timedelta(days=9)).strftime('%Y-%m-%d')
    assert trend[0]['count'] == 1 and trend[0]['players'] == 1     # 今天
    assert trend[2]['count'] == 2                # 2 天前:2 局
    assert trend[2]['players'] == 2              # 2 天前:U1/U2 去重
    assert sum(t['count'] for t in trend) == 3   # 12 天前不入窗
    assert trend[1]['count'] == 0 and trend[1]['players'] == 0     # 1 天前补零日


def test_game_stats_missing_db_not_raise():
    g = metrics.query_game_stats()
    assert g['available'] is False
    assert g['errors']
    assert g['today_matches'] is None
    assert g['trend_10d'] == []


def test_game_stats_missing_table_partial():
    """只建 match 表 —— user 相关查询报错进 errors,match 查询照常。"""
    conn = sqlite3.connect(boot.DB_PATH)
    conn.execute(_SCHEMA[0])
    conn.execute("INSERT INTO match(game_name, finish_time, group_id, host_user_id,"
                 " user_count, multiple) VALUES ('五子棋', ?, 'G1', 'U1', 1, 1)",
                 (_ts(0),))
    conn.commit()
    conn.close()
    g = metrics.query_game_stats()
    assert g['available'] is True
    assert g['today_matches'] == 1
    assert g['lgtbot_users'] is None
    assert any('lgtbot_users' in e for e in g['errors'])

# ─────────────────────────────────────────────────────────────────────────
# 不计分对局账本 —— 引擎不写库,合并进今日口径
# ─────────────────────────────────────────────────────────────────────────


def _unranked_file() -> dict:
    with open(metrics.UNRANKED_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def _day(days_ago: int = 0) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime('%Y-%m-%d')


def _mid_today() -> float:
    """今天 00:00 之后、现在之前的一个时刻(午夜刚过时退回 00:00 本身)。"""
    now = datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (start + (now - start) / 2).timestamp()


def test_unranked_ledger_keeps_only_two_days():
    """★ 只留今天与昨天 —— 昨天仅为「昨日同时段」对比而存在,更早的没有消费方。

    保留窗口按墙上时钟算:补一条旧记录不能把今天的账本一起清掉,所以这里故意把最旧的一条放在最后写。
    """
    now = time.time()
    for days in (0, 1, 2, 5):
        metrics.record_unranked_match('五子棋', ['U1', 'U2'], 'G1',
                                      ts=now - days * 86400)
    assert sorted(_unranked_file()) == sorted([_day(0), _day(1)])
    assert not os.path.exists(metrics.UNRANKED_PATH + '.tmp')


def test_unranked_entry_keeps_name_players_group_and_time():
    ts = _mid_today()
    metrics.record_unranked_match('大富翁', ['U1', 'U2'], 'G7', ts=ts)
    row = _unranked_file()[_day(0)][0]
    assert row == {'ts': int(ts), 'game': '大富翁',
                   'players': ['U1', 'U2'], 'gid': 'G7'}


def test_unranked_dedups_the_private_broadcast_fanout():
    """★ 私聊对局的结算广播按参与者逐个私发,同一局会到达多次 —— 不去重的话一局记成 N 局。"""
    ts = _mid_today()
    for _ in range(3):
        metrics.record_unranked_match('大富翁', ['U1', 'U2'], '', ts=ts)
    assert len(_unranked_file()[_day(0)]) == 1
    # 间隔够远 = 真的第二局
    metrics.record_unranked_match('大富翁', ['U1', 'U2'], '',
                                  ts=ts + metrics._UNRANKED_DEDUP_S + 1)
    assert len(_unranked_file()[_day(0)]) == 2


def test_unranked_dedup_does_not_swallow_a_different_match():
    ts = _mid_today()
    metrics.record_unranked_match('大富翁', ['U1', 'U2'], 'G1', ts=ts)
    metrics.record_unranked_match('大富翁', ['U1', 'U3'], 'G1', ts=ts)   # 换人
    metrics.record_unranked_match('五子棋', ['U1', 'U2'], 'G1', ts=ts)   # 换游戏
    metrics.record_unranked_match('大富翁', ['U1', 'U2'], 'G2', ts=ts)   # 换群
    assert len(_unranked_file()[_day(0)]) == 4


def test_unranked_record_swallows_write_failure(monkeypatch):
    """统计失败绝不影响发消息 —— 这条挂在引擎线程的发送路径上。"""
    monkeypatch.setattr(metrics, '_unranked_write',
                        lambda d: (_ for _ in ()).throw(OSError('disk full')))
    metrics.record_unranked_match('五子棋', ['U1'], 'G1')       # 不抛即通过


def test_unranked_folds_into_today_counts_with_dedup():
    """★ 玩家 / 群聊要和库里的集合**取并集**:同一个人今天既打了计分局也打了不计分局,只能算一个人。"""
    _make_db([('五子棋', 0, 'G1', ['U1', 'U2'])])
    ts = _mid_today()
    metrics.record_unranked_match('大富翁', ['U2', 'U3'], 'G1', ts=ts)   # U2/G1 重合
    metrics.record_unranked_match('大富翁', ['U4'], 'G9', ts=ts)

    g = metrics.query_game_stats()
    assert g['today_matches'] == 3                  # 1 计分 + 2 不计分
    assert g['today_players'] == 4                  # U1/U2/U3/U4
    assert g['today_groups'] == 2                   # G1/G9


def test_unranked_private_match_adds_no_group():
    _make_db([('五子棋', 0, 'G1', ['U1'])])
    metrics.record_unranked_match('大富翁', ['U2'], '', ts=_mid_today())
    g = metrics.query_game_stats()
    assert g['today_matches'] == 2
    assert g['today_groups'] == 1                   # 私聊局无群,同库里 NULL 口径


def test_unranked_folds_into_yesterday_same_span():
    """昨日账本只服务「昨日同时段」的涨跌对比:窗口外的不计入。"""
    now = datetime.now()
    yday_same = now - timedelta(days=1)
    yday_start = yday_same.replace(hour=0, minute=0, second=0, microsecond=0)
    span = (yday_same - yday_start).total_seconds()
    # 窗口上界是 query_game_stats 自己取的 now,两端各留 60s 余量,建库耗时才不会把「窗口外」那条挤进窗口
    if not 60 < span < 86400 - 60:
        pytest.skip('恰在午夜前后,同时段窗口两端留不出余量')
    _make_db([('五子棋', 0, 'G1', ['U1'])])
    inside = (yday_start + timedelta(seconds=span / 2)).timestamp()
    metrics.record_unranked_match('大富翁', ['U8'], 'G8', ts=inside)
    metrics.record_unranked_match('大富翁', ['U9'], 'G9',
                                  ts=yday_same.timestamp() + 60)     # 晚于同时段

    g = metrics.query_game_stats()
    assert g['yesterday_matches_same_span'] == 1
    assert g['yesterday_players_same_span'] == 1
    assert g['yesterday_groups_same_span'] == 1
    # 窗口下界:昨天的账本不能漏进今日口径
    assert g['today_matches'] == 1
    assert g['today_players'] == 1
    assert g['today_groups'] == 1


def test_unranked_tag_only_when_every_match_is_unranked():
    """★ 标签的判定口径:该游戏今天**一局计分的都没有**才打标;既有计分又有不计分的只汇总,不打标。"""
    _make_db([('五子棋', 0, 'G1', ['U1']),
              ('大富翁', 0, 'G1', ['U1'])])
    ts = _mid_today()
    metrics.record_unranked_match('大富翁', ['U2'], 'G1', ts=ts)          # 混合
    metrics.record_unranked_match('斗地主', ['U2', 'U3'], 'G1', ts=ts)    # 纯不计分
    metrics.record_unranked_match('斗地主', ['U2', 'U4'], 'G1', ts=ts + 60)

    g = metrics.query_game_stats()
    tags = {t['game_name']: (t['count'], t['unranked'])
            for t in g['top_games_today']}
    assert tags == {'斗地主': (2, True), '大富翁': (2, False), '五子棋': (1, False)}


def test_unranked_boards_merge_before_truncating_to_ten():
    """★ 先合并再排序取 TOP10 —— 只合并两边各自的前十,会漏掉「库里排 11、不计分局很多」的游戏。"""
    _make_db([('计分%02d' % i, 0, 'G1', ['U%d' % i]) for i in range(1, 12)])
    ts = _mid_today()
    for i in range(5):
        metrics.record_unranked_match('计分11', ['UX'], 'G1', ts=ts + i * 60)

    g = metrics.query_game_stats()
    top = {t['game_name']: t['count'] for t in g['top_games_today']}
    assert len(top) == 10
    assert top['计分11'] == 6                        # 1 计分 + 5 不计分,升到榜首
    assert g['top_games_today'][0]['game_name'] == '计分11'
    assert g['top_players_today'][0]['count'] == 5   # UX 只有不计分局,照样上榜


def test_unranked_stays_out_of_history_and_trend():
    """★ 历史窗口查询与近 10 日只读数据库 —— 账本只有两天,混进 10 天的序列
    会让曲线和 prev10 的对比同时失真;按需求历史视图也只显示计分游戏。"""
    _make_db([('五子棋', 0, 'G1', ['U1']),
              ('五子棋', 10, 'G1', ['U2'])])
    metrics.record_unranked_match('大富翁', ['U3'], 'G1', ts=_mid_today())

    g = metrics.query_game_stats()
    assert g['today_matches'] == 2                       # 今日口径合并了
    assert sum(t['count'] for t in g['trend_10d']) == 1  # 趋势没有
    assert g['prev10_matches'] == 1
    assert g['lgtbot_matches'] == 2                      # 累计也没有
    assert [t['count'] for t in g['top_games_week']] == [1]
    today = metrics.query_game_stats_for_date(datetime.now().strftime('%Y-%m-%d'))
    assert today['day_matches'] == 1                     # 历史视图只有计分局


def test_unranked_not_added_when_the_db_query_failed():
    """库里那半边查不出来时不补 —— 只报不计分的一半数字更误导。"""
    conn = sqlite3.connect(boot.DB_PATH)
    conn.execute(_SCHEMA[0])                             # 只有 match 表
    conn.commit()
    conn.close()
    metrics.record_unranked_match('大富翁', ['U1'], 'G1', ts=_mid_today())

    g = metrics.query_game_stats()
    assert g['today_matches'] == 1                       # match 表查得到 → 合并
    assert g['today_players'] is None                    # user_with_match 缺表
    assert any('today_players' in e for e in g['errors'])


def test_unranked_not_added_when_the_match_table_is_missing():
    """★ 对局数那一项也一样:库里查不出来就保持 None,补上不计分的那一半
    会让面板显示一个只统计了一小半的数字,比显示「—」更误导。"""
    conn = sqlite3.connect(boot.DB_PATH)
    conn.execute(_SCHEMA[2])                             # 只有 user 表
    conn.commit()
    conn.close()
    metrics.record_unranked_match('大富翁', ['U1'], 'G1', ts=_mid_today())

    g = metrics.query_game_stats()
    assert g['available'] is True
    assert g['today_matches'] is None
    assert g['yesterday_matches_same_span'] is None


def test_unranked_corrupt_ledger_renamed_and_recovers():
    os.makedirs(metrics.METRICS_DIR, exist_ok=True)
    with open(metrics.UNRANKED_PATH, 'w', encoding='utf-8') as f:
        f.write('{ not json')
    metrics.record_unranked_match('五子棋', ['U1'], 'G1')
    assert glob.glob(metrics.UNRANKED_PATH + '.corrupt_*')
    assert len(_unranked_file()[_day(0)]) == 1
