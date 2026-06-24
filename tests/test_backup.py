#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""backup 模块测试 —— 路径解析 / zip 打包 / 列表排序 / 轮转 / SQLite 锁兜底
/ 自动触发判断。

被测重点(对应 plan):
  · _FRAMEWORK_ROOT 从 boot.PLUGIN_DIR 反推正确
  · create_backup() 产出 zip,包含期望的 data/ 目录条目
  · 不包含 engine/images/ 渲染缓存
  · list_backups() 按 mtime 降序
  · prune_old() 保留 N 份
  · SQLite backup 失败时跳过该文件继续打包(不让整个 backup 失败)
  · schedule_on_load_check 距上次备份 < 24h 时跳过
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
import zipfile
from unittest.mock import patch

import pytest

# conftest.py 已 inject 假 boot,这里安全 import
from plugins.LGTBot_ElainaBot.mod import backup, boot


# ─────────────────────────────────────────────────────────────────────────
# Helpers — 在 conftest 假 boot 提供的 tmp PLUGIN_DIR 里造源文件
# ─────────────────────────────────────────────────────────────────────────


def _make_dummy_sqlite(path: str, table: str = 't', rows: int = 3) -> None:
    """造一个有真实结构的小 sqlite db,确保 backup() API 能正常拷贝。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(f'CREATE TABLE IF NOT EXISTS {table} (id INTEGER, name TEXT)')
        for i in range(rows):
            conn.execute(f'INSERT INTO {table} VALUES (?, ?)', (i, f'row{i}'))
        conn.commit()
    finally:
        conn.close()


def _make_plain_file(path: str, content: str = 'hello') -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


@pytest.fixture(autouse=True)
def _clean_backup_dir():
    """每个测试前清 BACKUP_DIR + 重建 data/ 源目录(冷启动状态)"""
    # 清备份目录
    if os.path.isdir(backup.BACKUP_DIR):
        for entry in os.scandir(backup.BACKUP_DIR):
            try:
                if entry.is_file():
                    os.remove(entry.path)
                else:
                    import shutil
                    shutil.rmtree(entry.path, ignore_errors=True)
            except OSError:
                pass
    else:
        os.makedirs(backup.BACKUP_DIR, exist_ok=True)
    # 清并重建 data/ 源(每个测试自己造需要的文件)
    import shutil
    if os.path.isdir(boot.DATA_DIR):
        shutil.rmtree(boot.DATA_DIR, ignore_errors=True)
    os.makedirs(boot.ENGINE_DIR, exist_ok=True)
    yield


# ─────────────────────────────────────────────────────────────────────────
# 1. 路径解析
# ─────────────────────────────────────────────────────────────────────────


def test_resolve_framework_root():
    """BACKUP_DIR 应在 PLUGIN_DIR 的**上两级**之上 —— 不在插件目录内,卸载安全。"""
    # boot.PLUGIN_DIR = <fake_root>/  (conftest 直接给的临时 dir,没真嵌在 plugins/)
    # 但 backup._FRAMEWORK_ROOT 仍按"上两级"算 —— 这是真生产环境的逻辑
    assert backup._FRAMEWORK_ROOT == os.path.dirname(os.path.dirname(boot.PLUGIN_DIR))
    # 备份路径形如 <FRAMEWORK_ROOT>/data/backup/lgtbot
    assert backup.BACKUP_DIR.endswith(os.path.join('data', 'backup', 'lgtbot'))
    # 关键安全断言:备份目录**不在**插件目录内
    assert not backup.BACKUP_DIR.startswith(boot.PLUGIN_DIR + os.sep)


# ─────────────────────────────────────────────────────────────────────────
# 2-3. create_backup 产出 zip + 内容正确 + 不含 images
# ─────────────────────────────────────────────────────────────────────────


def test_create_backup_produces_zip_with_expected_content():
    # 造源数据(user_cache.db 不入备份范围,无需造)
    _make_dummy_sqlite(boot.DB_PATH)
    _make_plain_file(boot.CONF_PATH, '{"k":"v"}')
    _make_plain_file(os.path.join(boot.DATA_DIR, 'config.yaml'), 'admin_uids: []\n')
    _make_plain_file(os.path.join(boot.DATA_DIR, 'update_notice.txt'), 'hello')

    result = backup.create_backup()

    assert result['success']
    assert os.path.isfile(result['zip_path'])
    # 应至少含 4 个文件(1 sqlite + 1 json + 1 yaml + 1 txt)
    assert len(result['included']) >= 4
    # zip 内含期望的归档条目
    with zipfile.ZipFile(result['zip_path'], 'r') as zf:
        names = set(zf.namelist())
    assert 'data/engine/lgtbot.db' in names
    assert 'data/engine/lgtbot.json' in names
    assert 'data/config.yaml' in names
    assert 'data/update_notice.txt' in names
    # user_cache.db 即使源文件存在也不应入包(被 _collect_sources 排除)
    assert 'data/user_cache.db' not in names


def test_create_backup_omits_images_and_build_dirs():
    """zip 内不应有 engine/images/ 渲染缓存或 build/ 编译产物 —— 这俩是可重生产物,
    备份白吃存储。"""
    _make_dummy_sqlite(boot.DB_PATH)
    # 造一个 images 子目录假装有几张渲染图,看是否被排除
    images_path = os.path.join(boot.IMG_PATH, 'fake_render.png')
    _make_plain_file(images_path, 'PNG fake bytes')
    # 造一个 build 假装编译产物
    build_path = os.path.join(boot.BUILD_DIR, 'LGTBot_ElainaBot.so')
    _make_plain_file(build_path, 'ELF fake bytes')

    result = backup.create_backup()

    assert result['success']
    with zipfile.ZipFile(result['zip_path'], 'r') as zf:
        names = zf.namelist()
    # 不应含任何 images/ 或 build/ 路径
    for n in names:
        assert 'images' not in n, f'unexpected images entry: {n}'
        assert 'build' not in n, f'unexpected build entry: {n}'


# ─────────────────────────────────────────────────────────────────────────
# 4. list_backups 按 mtime 降序
# ─────────────────────────────────────────────────────────────────────────


def test_list_backups_sorted_by_mtime_desc():
    """构造 3 个 zip,手动调 mtime,验证 list_backups 按 mtime **降序**(最新在前)"""
    now = time.time()
    files = []
    for i, age_s in enumerate([0, 3600, 7200]):     # 现在 / 1h 前 / 2h 前
        name = f'LGTBot_test_{i}.zip'
        path = os.path.join(backup.BACKUP_DIR, name)
        with zipfile.ZipFile(path, 'w') as zf:
            zf.writestr('placeholder.txt', f'#{i}')
        os.utime(path, (now - age_s, now - age_s))
        files.append(name)

    backups = backup.list_backups()
    assert len(backups) == 3
    # 第一个应是 i=0(最新),最后一个应是 i=2(最老)
    assert backups[0]['name'] == files[0]
    assert backups[-1]['name'] == files[2]
    # mtime 严格降序
    assert backups[0]['mtime_ts'] >= backups[1]['mtime_ts'] >= backups[2]['mtime_ts']


# ─────────────────────────────────────────────────────────────────────────
# 5. prune_old 保留 N 份
# ─────────────────────────────────────────────────────────────────────────


def test_prune_old_keeps_retention():
    """造 8 个 zip,prune_old(retention=3) 应删 5 个,保留最新 3 个。"""
    now = time.time()
    expected_keep = []
    for i in range(8):
        name = f'LGTBot_p_{i:02d}.zip'
        path = os.path.join(backup.BACKUP_DIR, name)
        with zipfile.ZipFile(path, 'w') as zf:
            zf.writestr('x.txt', str(i))
        # i 越大 mtime 越新(最后入参为最大 mtime 值,即 i=7 最新)
        os.utime(path, (now - (8 - i) * 60, now - (8 - i) * 60))
        if i >= 5:
            expected_keep.append(name)   # i=5,6,7 应保留(最新 3 个)

    deleted = backup.prune_old(retention=3)
    assert len(deleted) == 5

    remaining = {b['name'] for b in backup.list_backups()}
    assert remaining == set(expected_keep), \
        f'expected {expected_keep!r}, got {remaining!r}, deleted {deleted!r}'


def test_prune_old_retention_zero_noop():
    """retention<=0 不轮转(防误传)"""
    # 造 3 个
    for i in range(3):
        path = os.path.join(backup.BACKUP_DIR, f'LGTBot_z_{i}.zip')
        with zipfile.ZipFile(path, 'w') as zf:
            zf.writestr('x', '')
    deleted = backup.prune_old(retention=0)
    assert deleted == []
    assert len(backup.list_backups()) == 3


# ─────────────────────────────────────────────────────────────────────────
# 6. SQLite 锁兜底:某个 db backup 失败,zip 仍生成(跳过该项,记 skipped)
# ─────────────────────────────────────────────────────────────────────────


def test_create_backup_with_sqlite_failure_skips_and_continues():
    """mock _backup_sqlite_to_tmp 让 lgtbot.db 备份失败 → zip 仍生成,
    skipped 列表里有该 db 的记录,success=True(因为还有 yaml / json / txt)。
    """
    _make_dummy_sqlite(boot.DB_PATH)
    _make_plain_file(os.path.join(boot.DATA_DIR, 'config.yaml'), 'k: v')
    _make_plain_file(boot.CONF_PATH, '{}')

    def always_fail(src_path, tmp_path):
        return False    # 模拟 sqlite backup() 失败(锁超时等)

    with patch.object(backup, '_backup_sqlite_to_tmp', side_effect=always_fail):
        result = backup.create_backup()

    assert result['success'], '应仍生成 zip(只是少 lgtbot.db)'
    skipped_paths = {s['path'] for s in result['skipped']}
    assert 'data/engine/lgtbot.db' in skipped_paths
    # 其他 plain 文件仍应在
    with zipfile.ZipFile(result['zip_path'], 'r') as zf:
        names = set(zf.namelist())
    assert 'data/config.yaml' in names
    assert 'data/engine/lgtbot.json' in names
    assert 'data/engine/lgtbot.db' not in names


# ─────────────────────────────────────────────────────────────────────────
# 6.5. restore_backup —— 原子覆盖 + sidecar 清理 + 引擎安全
# ─────────────────────────────────────────────────────────────────────────


def test_restore_backup_rejects_invalid_name():
    for bad_name in ('', '../etc/passwd', 'foo/bar.zip', 'foo\\bar.zip', '..foo.zip'):
        result = backup.restore_backup(bad_name)
        assert not result['success'], f'非法文件名 {bad_name!r} 应被拒绝'


def test_restore_backup_rejects_missing_file():
    result = backup.restore_backup('LGTBot_nonexistent.zip')
    assert not result['success']
    assert '不存在' in result['message']


def test_restore_backup_replaces_files_with_backup_content():
    """restore_backup 应把 data/ 下的文件还原为备份内容。"""
    _make_dummy_sqlite(boot.DB_PATH, table='t_backup')
    _make_plain_file(boot.CONF_PATH, '{"v": "backup"}')
    _make_plain_file(os.path.join(boot.DATA_DIR, 'config.yaml'), 'role: backup')
    create_result = backup.create_backup()
    assert create_result['success']
    zip_name = create_result['zip_name']

    # 修改当前 data 让它和备份不同
    _make_dummy_sqlite(boot.DB_PATH, table='t_current')
    _make_plain_file(boot.CONF_PATH, '{"v": "current"}')
    _make_plain_file(os.path.join(boot.DATA_DIR, 'config.yaml'), 'role: current')

    restore_result = backup.restore_backup(zip_name)
    assert restore_result['success'], restore_result.get('message')
    replaced = restore_result['replaced_files']
    assert any(f.endswith('data/engine/lgtbot.db') for f in replaced)
    assert any(f.endswith('data/engine/lgtbot.json') for f in replaced)
    assert any(f.endswith('data/config.yaml') for f in replaced)

    # 文件内容应该匹配备份(不是 "current" 那份)
    with open(boot.CONF_PATH) as f:
        assert f.read() == '{"v": "backup"}'
    with open(os.path.join(boot.DATA_DIR, 'config.yaml')) as f:
        assert f.read() == 'role: backup'
    conn = sqlite3.connect(boot.DB_PATH)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert 't_backup' in tables and 't_current' not in tables
    finally:
        conn.close()


def test_restore_backup_sweeps_stale_sqlite_sidecars():
    """restored *.db 旁边遗留的 -journal / -wal / -shm 必须改名到 .stale_<ts>,
    否则下次启动 SQLite 会用 stale journal rollback 把恢复的数据回滚成残缺态。
    """
    _make_dummy_sqlite(boot.DB_PATH)
    _make_plain_file(boot.CONF_PATH, '{}')
    create_result = backup.create_backup()
    assert create_result['success']

    # 在 db 旁边造 stale journal 和 wal
    journal = boot.DB_PATH + '-journal'
    wal = boot.DB_PATH + '-wal'
    _make_plain_file(journal, 'stale journal content')
    _make_plain_file(wal, 'stale wal content')

    restore_result = backup.restore_backup(create_result['zip_name'])
    assert restore_result['success']

    # 原 path 应不在(已被改名)
    assert not os.path.isfile(journal)
    assert not os.path.isfile(wal)
    # swept_sidecars 列表里应有这俩
    swept = restore_result['swept_sidecars']
    assert any('lgtbot.db-journal.stale_' in s for s in swept)
    assert any('lgtbot.db-wal.stale_' in s for s in swept)


@pytest.mark.skipif(os.name != 'posix',
                    reason='POSIX inode 语义:os.replace 替换 dirent 而非截断 inode')
def test_restore_preserves_old_inode_for_open_fd():
    """关键安全保证:引擎打开 db 后,在它运行期间 restore_backup 必须**不**破坏
    它的 fd —— os.replace 替换 dirent 到新 inode,旧 inode 仍由 fd 持有可读,
    避免引擎下次 read 拿到截断 / 损坏的 SQLite 页面 → null deref SEGV。
    """
    _make_dummy_sqlite(boot.DB_PATH, rows=3)
    _make_plain_file(boot.CONF_PATH, '{}')
    create_result = backup.create_backup()
    assert create_result['success']

    # 模拟引擎: 打开 db fd 时它有 3 行
    fd = os.open(boot.DB_PATH, os.O_RDONLY)
    try:
        ino_before = os.fstat(fd).st_ino

        # 改 disk 上的 db,让它和备份不一样(10 行)
        _make_dummy_sqlite(boot.DB_PATH, rows=10)

        # restore: dirent → 新 inode(包含备份的 3 行内容)
        restore_result = backup.restore_backup(create_result['zip_name'])
        assert restore_result['success']

        # path 现在应当指向新 inode
        ino_after_path = os.stat(boot.DB_PATH).st_ino
        assert ino_after_path != ino_before, \
            'os.replace 应替换 dirent 到新 inode(不要原地 truncate)'

        # fd 仍指向旧 inode(它的 stat 不变,可读)
        ino_via_fd = os.fstat(fd).st_ino
        assert ino_via_fd == ino_before, '旧 inode 必须由 fd 保持存活'

        # 旧 inode 的内容应当与新 path 内容不同
        os.lseek(fd, 0, 0)
        via_fd_head = os.read(fd, 100)
        with open(boot.DB_PATH, 'rb') as f:
            via_path_head = f.read(100)
        assert via_fd_head != via_path_head, '通过 fd 读到的应是旧数据,通过 path 读到的应是新数据'
    finally:
        os.close(fd)


# ─────────────────────────────────────────────────────────────────────────
# 7. schedule_on_load_check 时效判断
# ─────────────────────────────────────────────────────────────────────────


async def test_on_load_check_skips_if_recent_backup_exists():
    """已有 zip < AUTO_INTERVAL_S(24h)→ 协程不触发新备份。"""
    from plugins.LGTBot_ElainaBot.mod import state as _state
    _state.event_loop = asyncio.get_running_loop()

    # 造一个 "1 分钟前" 的 zip
    fresh_path = os.path.join(backup.BACKUP_DIR, 'LGTBot_fresh.zip')
    with zipfile.ZipFile(fresh_path, 'w') as zf:
        zf.writestr('x', 'x')
    os.utime(fresh_path, (time.time() - 60, time.time() - 60))

    # mock create_backup 看是否被调
    with patch.object(backup, 'create_backup') as mock_create, \
         patch.object(backup, '_ON_LOAD_DELAY_S', 0):    # 跳过 60s 等待
        await backup._on_load_check_coro()

    mock_create.assert_not_called(), 'recent backup exists, create_backup 不应被调'


async def test_on_load_check_triggers_when_stale_or_missing():
    """无 zip(冷启动)→ 触发首次备份。"""
    from plugins.LGTBot_ElainaBot.mod import state as _state
    _state.event_loop = asyncio.get_running_loop()

    # 备份目录是空的(_clean_backup_dir fixture 已清)
    with patch.object(backup, 'create_backup') as mock_create, \
         patch.object(backup, '_ON_LOAD_DELAY_S', 0):
        mock_create.return_value = {'success': True, 'zip_name': 'mocked.zip'}
        await backup._on_load_check_coro()

    mock_create.assert_called_once()
