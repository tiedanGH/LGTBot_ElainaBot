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


# ─────────────────────────────────────────────────────────────────────────
# 游戏子进程 core 文件(转储列表下方的新栏目)
# ─────────────────────────────────────────────────────────────────────────

class _FakeCoreReq:
    """模拟 aiohttp Request:name 与 d 各自成列表(按下标配对)。"""

    def __init__(self, pairs, *, dirs=None):
        self.query = self
        self._names = [p[0] for p in pairs]
        self._dirs = list(dirs) if dirs is not None else [str(p[1]) for p in pairs]

    def getall(self, key, default=None):
        if key == 'name':
            return list(self._names)
        if key == 'd':
            return list(self._dirs)
        return default if default is not None else []

    def get(self, key, default=None):
        if key == 'name':
            return self._names[0] if self._names else default
        if key == 'd':
            return self._dirs[0] if self._dirs else default
        return default


def _write_core(pc, dir_idx: int, name: str, mtime: int, body: bytes = b'\x7fELFjunk') -> str:
    d = pc.CORE_DIRS[dir_idx]
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    with open(path, 'wb') as f:
        f.write(body)
    os.utime(path, (mtime, mtime))
    return path


def _clear_cores(pc) -> None:
    for d in pc.CORE_DIRS:
        try:
            for n in os.listdir(d):
                if pc._CORE_RE.match(n):
                    os.remove(os.path.join(d, n))
        except OSError:
            pass


def test_core_dirs_cover_local_and_prebuilt():
    """★ 两种部署的 core 落点不同(本地 build/ vs 预编译 build_prebuilt/build/),
    而且切换模式后旧 core 还留在另一边 —— 两个目录都必须在扫描范围里。"""
    pc = _crash()
    from plugins.LGTBot_ElainaBot.mod import boot
    assert boot.LOCAL_BUILD_DIR in pc.CORE_DIRS
    assert os.path.join(boot.PREBUILT_DIR, 'build') in pc.CORE_DIRS
    assert len(set(pc.CORE_DIRS)) == len(pc.CORE_DIRS)      # 去重过,当前生效目录不重复


def test_core_name_pattern_matches_kernel_variants():
    """core_pattern 的几种常见形态都要认;非 core 文件一律不认。"""
    pc = _crash()
    for ok in ('core', 'core.12345', 'core-match_game_runn-42418-1786852068'):
        assert pc._CORE_RE.match(ok), ok
    for bad in ('libgame.so', 'match_game_runner', 'score-1', 'core/../x', 'mycore'):
        assert not pc._CORE_RE.match(bad), bad


def test_list_cores_sorts_by_crash_time_and_reads_name(tmp_path):
    """时间取文件名里的秒数(比 mtime 更贴近崩溃瞬间),倒序;pid 也从名字里取。"""
    pc = _crash()
    _clear_cores(pc)
    try:
        # mtime 故意与名字里的时间戳相反,验证排序用的是名字里的
        _write_core(pc, 0, 'core-match_game_runn-111-1700000000', mtime=9999)
        _write_core(pc, 0, 'core-match_game_runn-222-1800000000', mtime=1111)
        cores = pc._list_cores(analyze=False)
        assert [c['pid'] for c in cores] == [222, 111]
        assert cores[0]['crash_ts'] == 1800000000
        assert cores[0]['dir_idx'] == 0 and cores[0]['dir'] == pc.CORE_DIRS[0]
    finally:
        _clear_cores(pc)


def test_payload_counts_cores_and_bytes(tmp_path):
    """★ 顶部统计:游戏崩溃数量 + 总占用空间(跨两个目录汇总)。"""
    pc = _crash()
    _clear_cores(pc)
    try:
        _write_core(pc, 0, 'core-a-1-100', 100, body=b'x' * 300)
        idx2 = 1 if len(pc.CORE_DIRS) > 1 else 0
        _write_core(pc, idx2, 'core-b-2-200', 200, body=b'y' * 700)
        p = pc._payload()
        assert p['core_count'] == 2
        assert p['core_bytes'] == 1000
        assert list(p['core_dirs']) == list(pc.CORE_DIRS)
    finally:
        _clear_cores(pc)


def test_safe_core_path_rejects_traversal_and_bad_index(tmp_path):
    """定位参数两道闸:目录下标必须在范围内,文件名必须是纯 basename + core 命名。"""
    pc = _crash()
    _clear_cores(pc)
    try:
        _write_core(pc, 0, 'core-ok-1-100', 100)
        assert pc._safe_core_path('core-ok-1-100', 0)
        assert pc._safe_core_path('core-ok-1-100', '0')          # 字符串下标也接受
        assert pc._safe_core_path('core-ok-1-100', len(pc.CORE_DIRS)) is None   # 越界
        assert pc._safe_core_path('core-ok-1-100', -1) is None
        assert pc._safe_core_path('core-ok-1-100', 'x') is None
        assert pc._safe_core_path('../../etc/passwd', 0) is None  # 穿越
        assert pc._safe_core_path('libgame.so', 0) is None        # 非 core 命名
        assert pc._safe_core_path('core-missing-9-9', 0) is None  # 不存在
    finally:
        _clear_cores(pc)


async def test_core_delete_pairs_name_with_dir(tmp_path, monkeypatch):
    """★ name 与 d 必须**按下标配对** —— 同名 core 可能两个目录都有,配错就删错文件。
    数量不匹配直接 400;删除结果计入审计并回报释放的字节数。"""
    pc = _crash()
    if len(pc.CORE_DIRS) < 2:
        pytest.skip('只有一个 core 目录,配对语义无从验证')
    _clear_cores(pc)
    audit_calls = []
    from plugins.LGTBot_ElainaBot.mod import audit
    monkeypatch.setattr(audit, 'record', lambda *a, **k: audit_calls.append((a, k)))
    try:
        same = 'core-match_game_runn-1-100'
        _write_core(pc, 0, same, 100, body=b'a' * 10)
        _write_core(pc, 1, same, 100, body=b'b' * 20)
        # 只删目录 1 的那个 —— 目录 0 的同名文件必须留下
        resp = await pc.core_delete_handler(_FakeCoreReq([(same, 1)]))
        import json as _json
        data = _json.loads(resp.text)
        assert data['success'] is True and data['freed_bytes'] == 20
        assert os.path.exists(os.path.join(pc.CORE_DIRS[0], same))
        assert not os.path.exists(os.path.join(pc.CORE_DIRS[1], same))
        assert len(audit_calls) == 1 and audit_calls[0][0][0] == 'cache'

        # name / d 数量不匹配 → 400,不动文件
        resp2 = await pc.core_delete_handler(
            _FakeCoreReq([(same, 0)], dirs=['0', '1']))
        assert resp2.status == 400
        assert os.path.exists(os.path.join(pc.CORE_DIRS[0], same))
    finally:
        _clear_cores(pc)


async def test_core_delete_empty_and_bad_names(tmp_path, monkeypatch):
    """未指定 → 400;非法名进 failed 且不误删合法文件。"""
    pc = _crash()
    _clear_cores(pc)
    from plugins.LGTBot_ElainaBot.mod import audit
    monkeypatch.setattr(audit, 'record', lambda *a, **k: None)
    try:
        resp = await pc.core_delete_handler(_FakeCoreReq([]))
        assert resp.status == 400
        _write_core(pc, 0, 'core-keep-1-100', 100)
        import json as _json
        resp2 = await pc.core_delete_handler(
            _FakeCoreReq([('../../etc/passwd', 0), ('core-nope-9-9', 0)]))
        data = _json.loads(resp2.text)
        assert data['success'] is False and len(data['failed']) == 2
        assert os.path.exists(os.path.join(pc.CORE_DIRS[0], 'core-keep-1-100'))
    finally:
        _clear_cores(pc)


async def test_core_download_rejects_bad_and_serves_good(tmp_path):
    pc = _crash()
    _clear_cores(pc)
    try:
        _write_core(pc, 0, 'core-dl-1-100', 100)
        bad = await pc.core_download_handler(_FakeCoreReq([('../x', 0)]))
        assert bad.status == 400
        good = await pc.core_download_handler(_FakeCoreReq([('core-dl-1-100', 0)]))
        assert 'attachment' in good.headers.get('Content-Disposition', '')
    finally:
        _clear_cores(pc)


def test_list_cores_attaches_analysis(tmp_path, monkeypatch):
    """列表默认带 ELF 解析结果;解析失败也照常列出(core 仍可下载给 gdb 看)。"""
    pc = _crash()
    _clear_cores(pc)
    try:
        _write_core(pc, 0, 'core-junk-1-100', 100, body=b'not an elf file at all')
        cores = pc._list_cores()
        assert len(cores) == 1
        a = cores[0]['analysis']
        assert a['ok'] is False and a['error']          # 解析不出,但条目在
    finally:
        _clear_cores(pc)


def test_core_section_frontend_contract():
    """前端契约:新栏目在转储列表下方,统计卡 + 两列(游戏 / 崩溃模块)+ 下载 / 批量删除,
    且明确写了「完整调用栈要 gdb」。"""
    pc = _crash()
    html, js, css = pc.TAB_HTML, pc.TAB_JS, pc.TAB_CSS
    for frag in ('id="crash-core-body"', 'id="crash-core-count"', 'id="crash-core-size"',
                 'id="crash-core-delete"', 'id="crash-core-check-all"',
                 'crash-col-game', 'crash-col-mod'):
        assert frag in html, frag
    assert html.index('id="crash-core-body"') > html.index('id="crash-table-body"')
    assert 'gdb' in html                                  # 说明了做不到什么
    assert 'coreApplyData' in js and 'coreDeleteSelected' in js
    assert 'core-download' in js and 'core-delete' in js
    assert "'&d=' + encodeURIComponent" in js             # 删除 / 下载都带目录下标
    assert '.crash-core-unknown' in css
