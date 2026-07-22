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
                    'quota_exhausted': 0, 'quota_wait_timeout': 0}


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
    """matches: [(game, days_ago, group_id, players[])] → 建库插数。"""
    conn = sqlite3.connect(boot.DB_PATH)
    try:
        for sql in _SCHEMA:
            conn.execute(sql)
        for i, (game, days_ago, group_id, players) in enumerate(matches, start=1):
            conn.execute(
                'INSERT INTO match(match_id, game_name, finish_time, group_id,'
                ' host_user_id, user_count, multiple) VALUES (?,?,?,?,?,?,1)',
                (i, game, _ts(days_ago), group_id, players[0] if players else 'U0',
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


def test_top_players_nickname_and_mask(monkeypatch):
    _make_db([('五子棋', 0, 'G1', ['user_openid_abcdefgh', 'U2']),
              ('五子棋', 0, 'G1', ['user_openid_abcdefgh']),
              ('五子棋', 3, 'G1', ['user_openid_abcdefgh'])])    # 本周内、非今日
    monkeypatch.setattr(metrics.userdb, 'get_name',
                        lambda uid: '铁蛋' if uid == 'user_openid_abcdefgh' else '')
    g = metrics.query_game_stats()
    # 本周口径含 3 天前那局;今日口径只含 2 局
    assert g['top_players_week'][0] == {'display': '铁蛋', 'count': 3}
    assert g['top_players_week'][1] == {'display': 'U2', 'count': 1}   # ≤6 字符原样
    assert g['top_players_today'][0] == {'display': '铁蛋', 'count': 2}

    monkeypatch.setattr(metrics.userdb, 'get_name', lambda uid: '')
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
