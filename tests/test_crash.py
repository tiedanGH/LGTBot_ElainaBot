#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""page_crash 崩溃转储标签测试 —— 列表 / 排序 / 信号识别 / 文件名安全校验。

page_crash 顶部 import aiohttp,dev 机常无 → 用 importorskip 守卫(CI 有 aiohttp 时才真跑);
boot 由 conftest 假桩提供,CRASH_DIR 落在 tmp 插件目录内。
"""

from __future__ import annotations

import os

import pytest


def _crash():
    pytest.importorskip('aiohttp')
    from plugins.LGTBot_ElainaBot.mod.webui import page_crash
    return page_crash


def _write_dump(pc, name: str, signal: int, mtime: int, *,
                is_uid=None, uid: str = '', gid: str = '', msg=None) -> None:
    """写一个贴近桥接层 DumpCrashToFile 落盘格式的 dump。给了 is_uid 才写触发源块
    (is_uid / uid / gid / msg),模拟带消息上下文的崩溃。"""
    os.makedirs(pc.CRASH_DIR, exist_ok=True)
    path = os.path.join(pc.CRASH_DIR, name)
    lines = ['=== LGTBot crash captured ===',
             f'time_sec: {mtime}', 'time_nsec: 0', f'signal: {signal}',
             'si_addr: 0x0', 'si_code: 1', 'pid: 1', 'tid: 2']
    if is_uid is not None:
        lines += [f'is_uid: {is_uid}', f'uid: {uid}', f'gid: {gid}', f'msg: {msg or ""}']
    text = '\n'.join(lines) + '\n\n--- backtrace ---\n  #0 frame\n=== end ===\n'
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    os.utime(path, (mtime, mtime))


def test_list_sorts_newest_first_and_ignores_foreign(tmp_path):
    pc = _crash()
    import shutil
    shutil.rmtree(pc.CRASH_DIR, ignore_errors=True)
    _write_dump(pc, 'crash_1000_1_1.log', 11, 1000)
    _write_dump(pc, 'crash_3000_1_2.log', 6, 3000)
    _write_dump(pc, 'crash_2000_1_3.log', 7, 2000)
    with open(os.path.join(pc.CRASH_DIR, 'notes.txt'), 'w') as f:
        f.write('x')                                   # 非 dump 命名 → 应被忽略
    try:
        pl = pc._payload()
        assert pl['count'] == 3                         # notes.txt 不计
        assert [d['name'] for d in pl['dumps']] == [
            'crash_3000_1_2.log', 'crash_2000_1_3.log', 'crash_1000_1_1.log']  # mtime 倒序
        assert [d['signal_name'] for d in pl['dumps']] == ['SIGABRT', 'SIGBUS', 'SIGSEGV']
        assert pl['total_bytes'] > 0
    finally:
        shutil.rmtree(pc.CRASH_DIR, ignore_errors=True)


def test_safe_dump_path_rejects_traversal_and_bad_names(tmp_path):
    pc = _crash()
    import shutil
    shutil.rmtree(pc.CRASH_DIR, ignore_errors=True)
    _write_dump(pc, 'crash_1000_1_1.log', 11, 1000)
    try:
        assert pc._safe_dump_path('crash_1000_1_1.log') is not None
        assert pc._safe_dump_path('crash_9999_9_9.log') is None      # 不存在
        assert pc._safe_dump_path('../../etc/passwd') is None        # 路径穿越
        assert pc._safe_dump_path('crash_1000_1_1.log/../x') is None  # 非纯 basename
        assert pc._safe_dump_path('notes.txt') is None               # 非 dump 命名
        assert pc._safe_dump_path('') is None
    finally:
        shutil.rmtree(pc.CRASH_DIR, ignore_errors=True)


class _FakeReq:
    """模拟 aiohttp Request:query.getall('name') 返回全部 name;query.get('name') 返回首个。"""
    def __init__(self, names):
        self.query = self
        self._names = list(names)
    def getall(self, key, default=None):
        return list(self._names) if key == 'name' else (default if default is not None else [])
    def get(self, key, default=None):
        if key == 'name':
            return self._names[0] if self._names else default
        return default


async def test_delete_handler_removes_selected_and_rejects_bad(tmp_path, monkeypatch):
    """删除选中:合法 dump 真删;路径穿越 / 不存在 / 非 dump 命名进 failed 不误删;结果计入审计。"""
    pc = _crash()
    import shutil
    shutil.rmtree(pc.CRASH_DIR, ignore_errors=True)
    _write_dump(pc, 'crash_1000_1_1.log', 11, 1000)
    _write_dump(pc, 'crash_2000_1_2.log', 6, 2000)
    audit_calls = []
    from plugins.LGTBot_ElainaBot.mod import audit
    monkeypatch.setattr(audit, 'record', lambda *a, **k: audit_calls.append((a, k)))
    try:
        # 删一个合法 + 一个穿越 + 一个不存在 —— 只合法的应被删,非法项不误删
        await pc.delete_handler(_FakeReq([
            'crash_1000_1_1.log', '../../../etc/passwd', 'crash_9999_9_9.log']))
        assert not os.path.exists(os.path.join(pc.CRASH_DIR, 'crash_1000_1_1.log'))  # 删掉
        assert os.path.exists(os.path.join(pc.CRASH_DIR, 'crash_2000_1_2.log'))      # 未点,仍在
        assert len(audit_calls) == 1 and audit_calls[0][0][0] == 'cache'             # 一条 cache 审计
    finally:
        shutil.rmtree(pc.CRASH_DIR, ignore_errors=True)


async def test_delete_handler_empty_is_rejected():
    """未指定 name → 400,不动任何文件。"""
    pc = _crash()
    resp = await pc.delete_handler(_FakeReq([]))
    # web.json_response(status=400):status 属性可读
    assert getattr(resp, 'status', None) == 400


def test_list_reads_trigger_source_and_resists_msg_injection(tmp_path):
    """列表读出触发源:群聊(is_uid=0,群 gid + 用户 uid)/ 私信(is_uid=1,gid 空);
    uid/gid 只从 msg 之前的头部解析 —— msg 里伪造的 'gid:/uid:' 行不能污染。"""
    pc = _crash()
    import shutil
    shutil.rmtree(pc.CRASH_DIR, ignore_errors=True)
    _write_dump(pc, 'crash_3000_1_1.log', 11, 3000,
                is_uid=0, uid='realUID', gid='realGID',
                msg='boom\ngid: EVILGID\nuid: EVILUID')      # 注入行
    _write_dump(pc, 'crash_2000_1_2.log', 6, 2000,
                is_uid=1, uid='dmUser', gid='')              # 私信,无 gid
    _write_dump(pc, 'crash_1000_1_3.log', 7, 1000)          # 老 dump,无触发源块
    try:
        rec = {d['name']: d for d in pc._list_dumps()}
        pub = rec['crash_3000_1_1.log']
        assert pub['is_uid'] == 0 and pub['uid'] == 'realUID' and pub['gid'] == 'realGID'
        dm = rec['crash_2000_1_2.log']
        assert dm['is_uid'] == 1 and dm['uid'] == 'dmUser' and dm['gid'] == ''
        old = rec['crash_1000_1_3.log']
        assert old['is_uid'] is None and old['uid'] == '' and old['gid'] == ''
    finally:
        shutil.rmtree(pc.CRASH_DIR, ignore_errors=True)


def test_payload_includes_restart_stats(tmp_path):
    """payload 带崩溃重启概况(与指标面板同源同字段)。"""
    pc = _crash()
    import shutil
    shutil.rmtree(pc.CRASH_DIR, ignore_errors=True)
    try:
        r = pc._payload()['restart']
        assert set(r) >= {'crash_total', 'crash_by_sig', 'last_crash_ts', 'last_crash_sig'}
        assert isinstance(r['crash_total'], int) and isinstance(r['crash_by_sig'], dict)
    finally:
        shutil.rmtree(pc.CRASH_DIR, ignore_errors=True)


async def test_view_handler_returns_meta_and_backtrace(tmp_path):
    """查看返回解析好的 meta(信号 / 触发源 / 消息 / 名·大小)+ 含 backtrace 的正文。"""
    import json
    pc = _crash()
    import shutil
    shutil.rmtree(pc.CRASH_DIR, ignore_errors=True)
    _write_dump(pc, 'crash_1000_1_1.log', 11, 1000,
                is_uid=0, uid='userAAA', gid='groupBBB', msg='hello world')
    try:
        resp = await pc.view_handler(_FakeReq(['crash_1000_1_1.log']))
        data = json.loads(resp.text)
        assert data['success'] is True
        m = data['meta']
        assert m['signal_name'] == 'SIGSEGV' and m['signal'] == 11
        assert m['is_uid'] == 0 and m['uid'] == 'userAAA' and m['gid'] == 'groupBBB'
        assert m['msg'] == 'hello world'
        assert m['name'] == 'crash_1000_1_1.log' and m['size'] > 0
        assert '--- backtrace ---' in data['content']
    finally:
        shutil.rmtree(pc.CRASH_DIR, ignore_errors=True)


def test_render_list_fragment_shape(tmp_path):
    import html
    import json
    pc = _crash()
    import shutil
    shutil.rmtree(pc.CRASH_DIR, ignore_errors=True)
    _write_dump(pc, 'crash_1000_1_1.log', 11, 1000)
    try:
        frag = pc.render_list()
        assert frag.startswith('<pre id="result">') and frag.endswith('</pre>')
        body = html.unescape(frag[len('<pre id="result">'):-len('</pre>')])
        data = json.loads(body)
        assert data['success'] is True and data['count'] == 1
        assert data['dumps'][0]['signal_name'] == 'SIGSEGV'
    finally:
        shutil.rmtree(pc.CRASH_DIR, ignore_errors=True)
