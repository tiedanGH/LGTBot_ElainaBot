#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""page_build 进程存活探测(_is_alive)测试。

核心场景是**僵尸收尸**:_start_build 不保留 Popen 对象,子进程退出后没人 wait 就是僵尸,
单靠 kill(0) 会误判"仍在编译",所以 _is_alive 先 waitpid(WNOHANG) 当场收尸再判死。

僵尸语义仅 POSIX;真子进程用例在 Windows 上自动跳过(CI 的 ubuntu 会跑)。
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from plugins.LGTBot_ElainaBot.mod.webui import page_build

_posix_only = pytest.mark.skipif(os.name != 'posix', reason='僵尸/收尸语义仅 POSIX')


def test_is_alive_rejects_bogus_pids():
    """None / 0 / 负数 / 非数字一律 False —— 负数尤其关键:
    waitpid(-N) 是「进程组任意子进程」语义,绝不能透传。"""
    assert page_build._is_alive(None) is False
    assert page_build._is_alive(0) is False
    assert page_build._is_alive(-1) is False
    assert page_build._is_alive('x') is False
    assert page_build._is_alive(10 ** 30) is False       # OverflowError 路径


@_posix_only
def test_is_alive_reaps_zombie_child():
    """已退出但未 wait 的子进程(僵尸):_is_alive 应当场收尸并判死 —— kill(0) 对僵尸也返回成功。"""
    if not os.path.isdir('/proc'):
        pytest.skip('需要 /proc(Linux)')
    p = subprocess.Popen(['/bin/sh', '-c', 'exit 0'])
    # 等子进程退出变僵尸(持有 Popen 引用,保证没人抢先收尸)
    for _ in range(100):
        with open(f'/proc/{p.pid}/stat') as f:
            if f.read().rsplit(')', 1)[1].split()[0] == 'Z':
                break
        time.sleep(0.05)
    else:
        pytest.fail('子进程 5s 内未变僵尸')
    assert page_build._is_alive(p.pid) is False
    # 已被 _is_alive 收尸:PID 应当彻底不存在
    with pytest.raises(ProcessLookupError):
        os.kill(p.pid, 0)
    p.poll()      # Popen 对 ECHILD 有兜底,不抛;防 __del__ ResourceWarning


@_posix_only
def test_is_alive_running_child_and_foreign_pid():
    """真在跑的子进程 → True(waitpid 返回 (0,0) 不收尸);
    非本进程子进程(拿自身 PID 代表)→ ChildProcessError 回退 kill(0) → True。"""
    p = subprocess.Popen(['sleep', '30'])
    try:
        assert page_build._is_alive(p.pid) is True
    finally:
        p.terminate()
        p.wait()
    assert page_build._is_alive(os.getpid()) is True


# ─────────────────────────────────────────────────────────────────────────
# 操作审计:启动只记「等待中」,结束 / 终止时改写同一条
# ─────────────────────────────────────────────────────────────────────────

class _FakeProc:
    pid = 4242


@pytest.fixture
def build_env(tmp_path, monkeypatch):
    """状态 / 日志 / 退出码文件指到 tmp,Popen 换成假进程;返回 (设存活, 设退出码) 两个开关。"""
    import shutil
    from plugins.LGTBot_ElainaBot.mod import audit
    shutil.rmtree(audit.AUDIT_DIR, ignore_errors=True)
    for name in ('STATE_PATH', 'LOG_PATH', 'STATUS_PATH'):
        monkeypatch.setattr(page_build, name, str(tmp_path / name.lower()))
    monkeypatch.setattr(page_build.subprocess, 'Popen', lambda *a, **k: _FakeProc())
    alive = {'v': True}
    monkeypatch.setattr(page_build, '_is_alive', lambda pid: alive['v'])
    clock = {'t': 1_700_000_000.0}
    monkeypatch.setattr(page_build.time, 'time', lambda: clock['t'])

    def set_exit(code):
        alive['v'] = False
        clock['t'] += 125                     # 每次都「编译」了 2 分 5 秒
        if code is not None:
            (tmp_path / 'status_path').write_text(str(code), encoding='utf-8')
    yield set_exit, audit
    shutil.rmtree(audit.AUDIT_DIR, ignore_errors=True)


def _build_entries(audit):
    return [e for e in reversed(audit.get_entries()) if e['cat'] == 'build']


@pytest.mark.parametrize('kind, code, ok, detail', [
    ('build', 0, True, '编译成功 (PID 4242，用时 2 分 5 秒)'),
    ('build', 2, False, '编译失败 (PID 4242，退出码 2，用时 2 分 5 秒)'),
    ('build', None, False, '已中断 (PID 4242，用时 2 分 5 秒)'),
    ('meta', 0, True, '已完成 (PID 4242)'),
])
def test_build_audit_waits_then_shows_the_result(build_env, kind, code, ok, detail):
    """★ 启动时审计只记「等待中」(已启动 + PID);结束后同一条改成实际结果,带 PID 与编译用时(列出目标这类非编译任务不带用时)。"""
    set_exit, audit = build_env
    assert page_build._start_build(['bash', 'build.sh'], '完整编译', kind=kind)['success']
    (e,) = _build_entries(audit)
    assert e['pending'] is True and e['detail'] == '已启动 (PID 4242)' and e['action'] == '完整编译'
    page_build.get_build_state()                      # 还在跑:不落定
    assert _build_entries(audit)[0]['pending'] is True
    set_exit(code)
    page_build.get_build_state()
    page_build.get_build_state()                      # 再读也不重复改写 / 新增
    (e,) = _build_entries(audit)
    assert 'pending' not in e and e['ok'] is ok and e['detail'] == detail


def test_killed_build_audit_shows_terminated(build_env, monkeypatch):
    """手动终止:启动那条改成「已终止」(失败),另有一条「终止编译」记录这次操作本身。"""
    set_exit, audit = build_env
    page_build._start_build(['bash', 'build.sh'], '完整编译')
    monkeypatch.setattr(page_build.os, 'getpgid', lambda pid: pid, raising=False)
    monkeypatch.setattr(page_build.os, 'killpg', lambda pgid, sig: set_exit(None), raising=False)
    monkeypatch.setattr(page_build.time, 'sleep', lambda s: None)
    assert page_build._kill_build()['success']
    started, killed = _build_entries(audit)
    assert 'pending' not in started and started['ok'] is False and started['detail'] == '已终止 (PID 4242，用时 2 分 5 秒)'
    assert killed['action'] == '终止编译' and killed['detail'] == 'PID 4242'


@pytest.mark.parametrize('sec, text', [(0, '0 秒'), (59, '59 秒'), (60, '1 分 0 秒'), (3599, '59 分 59 秒'),
                                       (3600, '1 时 0 分'), (3725, '1 时 2 分')])
def test_audit_duration_matches_the_panel_format(sec, text):
    """审计里的编译用时与编译面板「用时」标签同一套写法(build.js 的 buildFmtDuration)。"""
    assert page_build._fmt_duration(sec) == text


def test_no_build_starts_or_second_remove_while_build_dir_is_being_removed(build_env):
    """★ 删除 build/ 在工作线程里跑:删的期间起编译直接拒绝(否则编译和 rmtree 同时动 build/),再点删除也拒绝。"""
    lock = page_build._build_dir_lock()
    assert lock.acquire(blocking=False)
    try:
        r = page_build._start_build(['bash', 'build.sh'], '完整编译')
        assert r['success'] is False and '删除 build/' in r['message']
        assert '请勿重复' in page_build.render_build_remove()
    finally:
        lock.release()
    assert page_build._start_build(['bash', 'build.sh'], '完整编译')['success'] is True
