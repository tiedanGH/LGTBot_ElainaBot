#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""audit 模块测试 —— 落盘 / 排序 / 500 截断 / 损坏容错 / 静默失败 / 并发。

被测重点(对应 plan):
  · record() 原子落盘,字段类型正确,detail 截 500 字符
  · 文件内正序存储,get_entries() 新 → 旧
  · MAX_ENTRIES 滚动淘汰最旧
  · 损坏文件(非法 JSON / 根非 list)→ 改名 .corrupt_* 留证 + 从空续记,不抛
  · 写盘失败时 record() 静默(永不影响业务)
  · 中文 / emoji 往返无损;多线程并发不死锁不丢条
"""

from __future__ import annotations

import json
import glob
import os
import re
import shutil
import threading

import pytest

# conftest.py 已 inject 假 boot,这里安全 import
from plugins.LGTBot_ElainaBot.mod import audit


@pytest.fixture(autouse=True)
def _clean_audit_dir():
    """每个测试前后清空审计目录(含 .corrupt_* / .tmp 残留)。"""
    shutil.rmtree(audit.AUDIT_DIR, ignore_errors=True)
    yield
    shutil.rmtree(audit.AUDIT_DIR, ignore_errors=True)


def _read_file() -> list:
    with open(audit.AUDIT_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────────────────────
# 1-2. 基本落盘 + 字段 + 顺序
# ─────────────────────────────────────────────────────────────────────────


def test_record_creates_valid_file_with_expected_fields():
    audit.record('build', '完整编译', '已启动 (PID 42)', ok=True, src=audit.SRC_PANEL)

    assert os.path.isfile(audit.AUDIT_PATH)
    entries = _read_file()
    assert isinstance(entries, list) and len(entries) == 1
    e = entries[0]
    assert isinstance(e['ts'], int)
    assert e['cat'] == 'build'
    assert e['action'] == '完整编译'
    assert e['detail'] == '已启动 (PID 42)'
    assert e['ok'] is True
    assert e['src'] == '面板'
    # 成功路径不留 .tmp 残留
    assert not os.path.exists(audit.AUDIT_PATH + '.tmp')


def test_detail_truncated_to_500_chars():
    audit.record('config', '保存 config.yaml', 'x' * 600)
    assert len(_read_file()[0]['detail']) == 500


def test_file_stores_oldest_first_and_get_entries_newest_first():
    for name in ('第一', '第二', '第三'):
        audit.record('cache', name)
    assert [e['action'] for e in _read_file()] == ['第一', '第二', '第三']
    assert [e['action'] for e in audit.get_entries()] == ['第三', '第二', '第一']


# ─────────────────────────────────────────────────────────────────────────
# 3. MAX_ENTRIES 滚动淘汰
# ─────────────────────────────────────────────────────────────────────────


def test_cap_evicts_oldest_beyond_max_entries():
    # 直接预置 519 条(逐条 record 会做 519 次全量重写,没必要),再 record 1 条
    os.makedirs(audit.AUDIT_DIR, exist_ok=True)
    seed = [{'ts': i, 'cat': 'cache', 'action': f'a{i}', 'detail': '',
             'ok': True, 'src': '面板'} for i in range(519)]
    with open(audit.AUDIT_PATH, 'w', encoding='utf-8') as f:
        json.dump(seed, f, ensure_ascii=False)

    audit.record('cache', 'a519')

    entries = _read_file()
    assert len(entries) == audit.MAX_ENTRIES == 500
    assert entries[0]['action'] == 'a20'     # 最旧 20 条被淘汰
    assert entries[-1]['action'] == 'a519'


# ─────────────────────────────────────────────────────────────────────────
# 4-5. 损坏容错
# ─────────────────────────────────────────────────────────────────────────


def test_corrupt_json_renamed_and_recovers():
    os.makedirs(audit.AUDIT_DIR, exist_ok=True)
    with open(audit.AUDIT_PATH, 'w', encoding='utf-8') as f:
        f.write('{ not valid json !!!')

    assert audit.get_entries() == []                       # 不抛
    corrupts = glob.glob(audit.AUDIT_PATH + '.corrupt_*')
    assert len(corrupts) == 1                              # 现场留证
    assert not os.path.exists(audit.AUDIT_PATH)            # 原文件已移走

    audit.record('backup', '创建备份', 'x.zip')             # 从空续记
    assert [e['action'] for e in _read_file()] == ['创建备份']


def test_valid_json_but_dict_root_treated_as_corrupt():
    os.makedirs(audit.AUDIT_DIR, exist_ok=True)
    with open(audit.AUDIT_PATH, 'w', encoding='utf-8') as f:
        json.dump({'not': 'a list'}, f)

    assert audit.get_entries() == []
    assert glob.glob(audit.AUDIT_PATH + '.corrupt_*')


# ─────────────────────────────────────────────────────────────────────────
# 6. record 永不抛
# ─────────────────────────────────────────────────────────────────────────


def test_record_swallows_write_failure(monkeypatch):
    def _boom(*a, **k):
        raise OSError('disk full')
    monkeypatch.setattr(audit.os, 'replace', _boom)

    audit.record('build', '完整编译')          # 不应抛出
    assert not os.path.exists(audit.AUDIT_PATH)

    monkeypatch.undo()
    audit.record('build', '完整编译')          # 恢复后正常
    assert len(_read_file()) == 1


# ─────────────────────────────────────────────────────────────────────────
# 7. 中文 / emoji 往返
# ─────────────────────────────────────────────────────────────────────────


def test_unicode_round_trip():
    detail = '🚧 维护模式已启用:新游戏创建已禁用 → 「测试」'
    audit.record('restart', '计划重启模式', detail, src=audit.SRC_CMD)
    got = audit.get_entries()[0]
    assert got['detail'] == detail
    assert got['src'] == '指令'
    # 落盘为非转义 UTF-8(ensure_ascii=False)
    with open(audit.AUDIT_PATH, 'r', encoding='utf-8') as f:
        assert '维护模式' in f.read()


# ─────────────────────────────────────────────────────────────────────────
# 8. 并发
# ─────────────────────────────────────────────────────────────────────────


def test_concurrent_records_no_deadlock_no_loss():
    threads = [
        threading.Thread(target=lambda i=i: [audit.record('cache', f't{i}-{j}')
                                             for j in range(25)])
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), '并发 record 疑似死锁'
    assert len(_read_file()) == 200


# ─────────────────────────────────────────────────────────────────────────
# 9-10. file_status
# ─────────────────────────────────────────────────────────────────────────


def test_file_status_empty():
    assert audit.file_status() == {'count': 0, 'oldest_ts': None, 'size_bytes': 0}


def test_file_status_matches_file():
    audit.record('bind', '换绑机器人', '→ 10001')
    audit.record('bind', '换绑机器人', '→ 10002')
    st = audit.file_status()
    entries = _read_file()
    assert st['count'] == 2
    assert st['oldest_ts'] == entries[0]['ts']
    assert st['size_bytes'] == os.path.getsize(audit.AUDIT_PATH)


# ─────────────────────────────────────────────────────────────────────────
# 11. 类别 / 来源常量 —— page_audit 的徽标与筛选按钮全靠它们
# ─────────────────────────────────────────────────────────────────────────


def test_every_recorded_category_is_declared():
    """★ 漂移闸:源码里 ``audit.record('<cat>', ...)`` 用到的每个短码都必须在 ``CATEGORIES`` 中登记。
    漏登记不会报错 —— 只是面板上那条记录的类别徽标渲染成空白、筛选按钮里也找不到它,极易长期无人察觉。"""
    mod_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'mod')
    used = set()
    for root, _dirs, files in os.walk(mod_dir):
        for fn in files:
            if not fn.endswith('.py'):
                continue
            with open(os.path.join(root, fn), 'r', encoding='utf-8') as f:
                used |= set(re.findall(r"""audit\.record\(\s*['"]([a-z_]+)['"]""",
                                       f.read()))
    assert used, '没扫到任何 audit.record 调用,正则或目录布局变了'
    assert used <= set(audit.CATEGORIES), \
        f'未在 CATEGORIES 登记的类别: {sorted(used - set(audit.CATEGORIES))}'


def test_categories_entries_are_emoji_label_pairs():
    """page_audit._payload 直接解包成 (emoji, label),形状错了会 ValueError。"""
    for cat, val in audit.CATEGORIES.items():
        assert isinstance(cat, str) and cat
        emoji, label = val                     # 解包即校验二元组
        assert emoji and label


def test_source_constants_are_distinct():
    """四个来源在面板上各有徽标配色,值重复会让 API / 自动触发的记录混进面板类。"""
    srcs = [audit.SRC_PANEL, audit.SRC_CMD, audit.SRC_AUTO, audit.SRC_API]
    assert len(set(srcs)) == 4 and all(isinstance(s, str) and s for s in srcs)


def test_record_defaults_to_panel_source_and_ok():
    """省略 ok / src 时的默认值 —— 大多数面板端点都靠这个默认。"""
    audit.record('cache', '清理缓存')
    e = _read_file()[-1]
    assert e['ok'] is True and e['src'] == audit.SRC_PANEL and e['detail'] == ''
