#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""page_build 进程存活探测(_is_alive)测试。

核心场景是**僵尸收尸**:_start_build 不保留 Popen 对象,子进程退出后没人 wait 就是僵尸,
单靠 kill(0) 会误判"仍在编译"——编译 API 无人值守时 running 永远 True
(编译实际成功但 API 不返回,还能被"成功终止")。修复后 _is_alive 先 waitpid(WNOHANG) 当场收尸再判死。

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
    """已退出但未 wait 的子进程(僵尸):_is_alive 应当场收尸并判死。
    修复前 kill(0) 对僵尸返回成功 → 误判存活。"""
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
