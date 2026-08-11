#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""userinfo 模块测试 —— 框架库读取 / 昵称缓存与写回 / 活跃度三源合并。

用 tmp 目录里的**真 SQLite 文件**(按主框架 core/storage/_schema.py 与
statistics.py 的建表 SQL)+ FakeLogService(query/query_data/db_queue)模拟
绑定 bot;monkeypatch helpers.get_bound_bot/get_bound_appid 注入。
不依赖 aiohttp,本机可全量真跑。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta

import pytest

from plugins.LGTBot_ElainaBot.mod import helpers, userinfo


def _day(offset: int = 0) -> str:
    return (datetime.now().date() + timedelta(days=offset)).strftime('%Y-%m-%d')


# ──────── FakeLogService / FakeBot ────────────────────────────────────────

class FakeLogService:
    """按主框架 LogService 的查询语义路由到 tmp 下的真 SQLite 文件。"""

    def __init__(self, base_dir: str):
        self._base_dir = base_dir
        self.queued: list[tuple[str, tuple]] = []    # db_queue 调用记录

    def _path(self, log_type: str, date=None) -> str:
        if log_type == 'message':
            return os.path.join(self._base_dir, date or '', 'message.db')
        return os.path.join(self._base_dir, f'{log_type}.db')

    def query(self, log_type, sql, params=(), date=None):
        path = self._path(log_type, date)
        if not os.path.isfile(path):
            return []
        conn = sqlite3.connect(path)
        try:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def query_data(self, sql, params=()):
        return self.query('data', sql, params)

    def db_queue(self, sql, params=()):
        self.queued.append((sql, tuple(params)))


class FakeBot:
    def __init__(self, base_dir: str):
        self.log_service = FakeLogService(base_dir)


# ──────── 建库 helpers(schema 对齐主框架) ────────────────────────────────

def _init_data_db(base: str, users=(), members=(), groups=(), group_names=None) -> None:
    conn = sqlite3.connect(os.path.join(base, 'data.db'))
    conn.execute('CREATE TABLE users (user_id TEXT PRIMARY KEY, '
                 "name TEXT DEFAULT '', state INTEGER DEFAULT 0)")
    conn.execute('CREATE TABLE members (user_id TEXT PRIMARY KEY)')
    conn.execute('CREATE TABLE groups_users (group_id TEXT PRIMARY KEY, '
                 "users TEXT DEFAULT '[]', group_name TEXT DEFAULT '', "
                 'in_group INTEGER DEFAULT 1)')
    conn.executemany('INSERT INTO users (user_id, name) VALUES (?,?)', users)
    conn.executemany('INSERT INTO members (user_id) VALUES (?)',
                     [(m,) for m in members])
    conn.executemany('INSERT INTO groups_users (group_id, users, in_group) '
                     'VALUES (?,?,?)', groups)
    for gid, gname in (group_names or {}).items():
        conn.execute('INSERT INTO groups_users (group_id, group_name) VALUES (?,?) '
                     'ON CONFLICT(group_id) DO UPDATE SET group_name=excluded.group_name',
                     (gid, gname))
    conn.commit()
    conn.close()


def _init_wakeup_db(base: str, rows=()) -> None:
    conn = sqlite3.connect(os.path.join(base, 'wakeup.db'))
    conn.execute('CREATE TABLE log (openid TEXT PRIMARY KEY, last_msg_date TEXT, '
                 'wakeup_stage INTEGER, last_wakeup_date TEXT, updated_at TEXT)')
    conn.executemany('INSERT INTO log (openid, last_msg_date) VALUES (?,?)', rows)
    conn.commit()
    conn.close()


def _init_stats_db(base: str, rows=()) -> None:
    """rows: (userid, total, private, daily_messages_dict)"""
    conn = sqlite3.connect(os.path.join(base, 'statistics.db'))
    conn.execute('CREATE TABLE user_stats (userid TEXT PRIMARY KEY, '
                 'total_messages INTEGER, private_messages INTEGER, '
                 "group_messages TEXT, command_stats TEXT, "
                 'daily_messages TEXT, updated_at TEXT)')
    conn.executemany(
        'INSERT INTO user_stats (userid, total_messages, private_messages, '
        "group_messages, command_stats, daily_messages, updated_at) "
        "VALUES (?,?,?,'{}','{}',?,'')",
        [(u, t, p, json.dumps(d)) for u, t, p, d in rows])
    conn.commit()
    conn.close()


def _init_message_db(base: str, date: str, rows=()) -> None:
    """rows: (timestamp, user_id, direction)"""
    day_dir = os.path.join(base, date)
    os.makedirs(day_dir, exist_ok=True)
    conn = sqlite3.connect(os.path.join(day_dir, 'message.db'))
    conn.execute('CREATE TABLE log (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                 'timestamp TEXT, user_id TEXT, direction TEXT)')
    conn.executemany('INSERT INTO log (timestamp, user_id, direction) '
                     'VALUES (?,?,?)', rows)
    conn.commit()
    conn.close()


@pytest.fixture()
def fake_bot(tmp_path, monkeypatch):
    """空库的 FakeBot + 注入绑定;各测试按需再灌数据。"""
    base = str(tmp_path)
    bot = FakeBot(base)
    monkeypatch.setattr(helpers, 'get_bound_bot', lambda: bot)
    monkeypatch.setattr(helpers, 'get_bound_appid', lambda: 'APP123')
    userinfo.clear_cache()
    yield bot
    userinfo.clear_cache()


# ──────── 昵称读取 / 缓存 ─────────────────────────────────────────────────

def test_get_name_hit_miss_and_empty_not_cached(fake_bot):
    base = fake_bot.log_service._base_dir
    _init_data_db(base, users=[('U1', '爱丽丝'), ('U2', '')])
    assert userinfo.get_name('U1') == '爱丽丝'
    assert userinfo.get_name('U2') == ''          # 名为空
    assert userinfo.get_name('U9') == ''          # 不存在
    # 空名不缓存:框架库补上后立即可见
    conn = sqlite3.connect(os.path.join(base, 'data.db'))
    conn.execute("UPDATE users SET name='鲍勃' WHERE user_id='U2'")
    conn.commit()
    conn.close()
    assert userinfo.get_name('U2') == '鲍勃'


def test_get_name_caches_nonempty(fake_bot):
    base = fake_bot.log_service._base_dir
    _init_data_db(base, users=[('U1', '爱丽丝')])
    assert userinfo.get_name('U1') == '爱丽丝'
    # 改库后仍读缓存(非空名不可变语义:框架不会改,写回路径会同步刷缓存)
    conn = sqlite3.connect(os.path.join(base, 'data.db'))
    conn.execute("UPDATE users SET name='别名' WHERE user_id='U1'")
    conn.commit()
    conn.close()
    assert userinfo.get_name('U1') == '爱丽丝'
    userinfo.clear_cache()
    assert userinfo.get_name('U1') == '别名'


def test_get_name_without_bot_returns_empty(monkeypatch):
    monkeypatch.setattr(helpers, 'get_bound_bot', lambda: None)
    userinfo.clear_cache()
    assert userinfo.get_name('U1') == ''


# ──────── 昵称写回 note_username ─────────────────────────────────────────

def test_note_username_gates_and_writeback(fake_bot):
    base = fake_bot.log_service._base_dir
    _init_data_db(base, users=[('U1', '旧名')])
    q = fake_bot.log_service.queued

    userinfo.note_username('U1', '')              # 闸 1:空 username
    assert q == []
    userinfo.note_username('U1', '旧名')          # 闸 3:与库一致 → 只填缓存
    assert q == [] and userinfo.get_name('U1') == '旧名'
    userinfo.note_username('U1', '旧名')          # 闸 2:与缓存一致
    assert q == []

    userinfo.note_username('U1', '新名')          # 真变化 → 写回
    assert len(q) == 1
    sql, params = q[0]
    assert 'ON CONFLICT(user_id) DO UPDATE SET name=excluded.name' in sql
    assert 'WHERE' not in sql                     # 无守卫 upsert
    assert params == ('U1', '新名')
    assert userinfo.get_name('U1') == '新名'      # 缓存同步刷新

    userinfo.note_username('U1', '又改名')        # 冷却窗内:只更新缓存不写库
    assert len(q) == 1
    assert userinfo.get_name('U1') == '又改名'


def test_note_username_first_write_on_freshly_booted_host(fake_bot, monkeypatch):
    """回归:monotonic 起点为系统启动,刚开机(now < 冷却窗)时首次写回不得被
    误判为「冷却中」吞掉(CI runner 必现;生产主机重启后前 10 分钟同理)。"""
    base = fake_bot.log_service._base_dir
    _init_data_db(base, users=[('U1', '旧名')])
    monkeypatch.setattr(userinfo.time, 'monotonic', lambda: 5.0)   # 开机 5 秒
    userinfo.note_username('U1', '新名')
    assert len(fake_bot.log_service.queued) == 1                   # 首次写回必须发生


def test_note_username_cooldown_expiry(fake_bot, monkeypatch):
    base = fake_bot.log_service._base_dir
    _init_data_db(base, users=[('U1', '')])
    q = fake_bot.log_service.queued
    userinfo.note_username('U1', '名A')
    assert len(q) == 1
    # 快进冷却窗:把上次写入时间拨回窗外
    userinfo._LAST_WRITE_TS['U1'] -= (userinfo._WRITE_COOLDOWN_S + 1)
    userinfo.note_username('U1', '名B')
    assert len(q) == 2 and q[1][1] == ('U1', '名B')


# ──────── 头像推导 ────────────────────────────────────────────────────────

def test_avatar_url_with_and_without_appid(fake_bot, monkeypatch):
    assert userinfo.avatar_url('OPENID1') == \
        'https://q.qlogo.cn/qqapp/APP123/OPENID1/100'
    assert userinfo.avatar_url('OPENID1', size=640).endswith('/640')
    monkeypatch.setattr(helpers, 'get_bound_appid', lambda: '')
    assert userinfo.avatar_url('OPENID1') == ''
    assert userinfo.avatar_url('') == ''


# ──────── 列表合并 / 计数 ─────────────────────────────────────────────────

def test_list_users_merges_three_day_sources(fake_bot):
    base = fake_bot.log_service._base_dir
    _init_data_db(
        base,
        users=[('UA', '甲'), ('UB', '乙'), ('UC', '丙'), ('UD', '')],
        groups=[('G1', json.dumps([
            {'userid': 'UB', 'last_active': _day(-1)},      # 乙:群内昨天
            {'userid': 'UC', 'last_active': _day(-9)},
            # 仅入群未互动的成员(进群事件入名单,不在 users 表)—— 不得进列表,
            # 否则行数会超过「总用户」并挤爆 limit(实测 923 总数 vs 1000 行)
            {'userid': 'UX_ROSTER_ONLY', 'last_active': _day(0)},
        ]), 1)],
    )
    _init_wakeup_db(base, rows=[('UA', _day(0))])            # 甲:私信今天
    _init_stats_db(base, rows=[
        ('UC', 42, 7, {_day(-3): 5}),                        # 丙:统计 3 天前
        ('UE', 9, 9, {_day(-2): 9}),                         # 统计源独有(不在 users 表)
    ])
    out = userinfo.list_users()
    by_id = {r['openid']: r for r in out}
    # 基准集 = users 表:行数与 count_users 同口径
    assert len(out) == 4 == userinfo.count_users()
    assert 'UX_ROSTER_ONLY' not in by_id                     # 名单独有 → 不建行
    assert 'UE' not in by_id                                 # 统计独有 → 不建行
    assert [r['openid'] for r in out[:3]] == ['UA', 'UB', 'UC']   # 日期倒序
    assert by_id['UA']['last_active_date'] == _day(0)
    assert by_id['UB']['last_active_date'] == _day(-1)
    assert by_id['UC']['last_active_date'] == _day(-3)       # 统计日 > 群内 9 天前
    assert by_id['UC']['total_messages'] == 42
    assert by_id['UA']['total_messages'] is None             # 无统计行
    assert by_id['UD']['last_active_date'] == ''             # 无任何活跃源
    assert by_id['UA']['avatar'].endswith('/UA/100')


def test_list_users_limit_and_missing_stats_db(fake_bot):
    base = fake_bot.log_service._base_dir
    _init_data_db(base, users=[(f'U{i}', f'名{i}') for i in range(5)])
    out = userinfo.list_users(limit=3)
    assert len(out) == 3
    assert all(r['total_messages'] is None for r in out)     # statistics.db 缺失


def test_list_users_default_all_and_offset_blocks(fake_bot):
    """回归:默认无上限(1000+ 全量);limit+offset 分块与全量逐段一致。"""
    base = fake_bot.log_service._base_dir
    _init_data_db(base, users=[(f'U{i:04d}', f'名{i}') for i in range(1005)])
    out = userinfo.list_users()
    assert len(out) == 1005 == userinfo.count_users()
    b0 = userinfo.list_users(limit=1000)
    b1 = userinfo.list_users(limit=1000, offset=1000)
    assert len(b0) == 1000 and len(b1) == 5
    # 分块拼接 == 全量(排序全局一致,块间无重叠 / 无遗漏)
    assert [r['openid'] for r in b0 + b1] == [r['openid'] for r in out]
    assert userinfo.list_users(limit=1000, offset=2000) == []   # 越界块为空

def test_list_users_corrupt_group_json_skipped(fake_bot):
    base = fake_bot.log_service._base_dir
    _init_data_db(base, users=[('UA', '甲')],
                  groups=[('G1', '{not json!!', 1),
                          ('G2', json.dumps([{'userid': 'UA',
                                              'last_active': _day(0)}]), 1)])
    out = userinfo.list_users()
    assert out[0]['openid'] == 'UA'
    assert out[0]['last_active_date'] == _day(0)             # 坏 JSON 不拖垮好群


def test_count_users(fake_bot):
    base = fake_bot.log_service._base_dir
    _init_data_db(base, users=[('U1', 'a'), ('U2', 'b')])
    assert userinfo.count_users() == 2


# ──────── 精确最后活跃 / 单用户查询 / 私信近活 ────────────────────────────

def test_last_active_exact_scans_recent_days(fake_bot):
    base = fake_bot.log_service._base_dir
    # 今天无日库(跳过);昨天有该用户 receive 行 + 更新的 send 行(应忽略 send)
    _init_message_db(base, _day(-1), rows=[
        (f'{_day(-1)} 10:00:00', 'U1', 'receive'),
        (f'{_day(-1)} 12:34:56', 'U1', 'receive'),
        (f'{_day(-1)} 23:59:59', 'U1', 'send'),
    ])
    assert userinfo.last_active_exact('U1') == f'{_day(-1)} 12:34:56'
    assert userinfo.last_active_exact('U9') == ''


def test_get_user_found_and_none(fake_bot):
    base = fake_bot.log_service._base_dir
    _init_data_db(base, users=[('U1', '爱丽丝')])
    _init_wakeup_db(base, rows=[('U1', _day(-2))])
    _init_stats_db(base, rows=[('U1', 100, 40, {_day(-1): 3})])
    u = userinfo.get_user('U1')
    assert u is not None
    assert u['name'] == '爱丽丝'
    assert u['last_active_date'] == _day(-1)                 # 统计日最新
    assert u['total_messages'] == 100 and u['private_messages'] == 40
    assert u['avatar'].endswith('/U1/100')
    assert userinfo.get_user('U404') is None
    assert userinfo.get_user('') is None


def test_dm_active_count_window(fake_bot):
    base = fake_bot.log_service._base_dir
    _init_data_db(base)
    _init_wakeup_db(base, rows=[
        ('U1', _day(0)),      # 今天 → 计
        ('U2', _day(-6)),     # 窗口边界(近 7 日含今天)→ 计
        ('U3', _day(-7)),     # 窗外 → 不计
    ])
    assert userinfo.dm_active_count(7) == 2
    assert userinfo.dm_active_count(8) == 3


# ──────── 群名批量查询(仪表盘「进行中的对局」展示名用) ────────────────────

def test_get_group_names_batches_and_filters(fake_bot, tmp_path):
    """一次 IN 查完全部群(进行中对局通常个位数,不逐个往返);
    只回传真的查到非空名字的群 —— 空名 / 查不到的交给调用方降级。"""
    _init_data_db(str(tmp_path), group_names={'G1': '铁蛋的游戏群', 'G2': '', 'G3': '测试群'})
    got = userinfo.get_group_names(['G1', 'G2', 'G3', 'GX'])
    assert got == {'G1': '铁蛋的游戏群', 'G3': '测试群'}   # G2 空名、GX 不存在都不回传


def test_get_group_names_edge_cases(fake_bot, tmp_path):
    """空入参 / 全是空串 → 不查库直接 {};无 bot 同样 {}(调用方回退 openid)。"""
    _init_data_db(str(tmp_path), group_names={'G1': '群一'})
    assert userinfo.get_group_names([]) == {}
    assert userinfo.get_group_names(['', None]) == {}
    monkey = userinfo._bound_bot
    userinfo._bound_bot = lambda: None
    try:
        assert userinfo.get_group_names(['G1']) == {}
    finally:
        userinfo._bound_bot = monkey
