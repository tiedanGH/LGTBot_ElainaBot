#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""编译 API 测试 —— token 生成 / 认证 / 状态码约定 / 同步等待编译结果。

build_api 顶部 import aiohttp,dev 机常无 → importorskip 守卫(同 page_users)。
真正的子进程调度(_start_build / _kill_build / get_build_state)全部
monkeypatch —— 这里只测 API 层的协议:HTTP 状态码 + JSON 形状。
"""

from __future__ import annotations

import json
import os

import pytest

from plugins.LGTBot_ElainaBot.mod import state


def _api():
    pytest.importorskip('aiohttp')
    from plugins.LGTBot_ElainaBot.mod.webui import build_api
    return build_api


class _FakeReq:
    """模拟 aiohttp Request:headers / query dict + async json()。"""

    def __init__(self, token=None, body=None, json_error=False):
        self.headers = {}
        if token is not None:
            self.headers['Authorization'] = f'Bearer {token}'
        self.query = {}
        self._body = body
        self._json_error = json_error

    async def json(self):
        if self._json_error or self._body is None:
            raise ValueError('no body')
        return self._body


def _local_mode(monkeypatch, api):
    """预编译检查放行:running/selected 都是 local。"""
    monkeypatch.setattr(api.prebuilt, 'mode_info',
                        lambda: {'running': 'local', 'selected': 'local'})


def _touch_build_env(api):
    """建 build/ 目录 + build.sh(服务可用性检查要求两者都在)。"""
    build_dir = os.path.join(api.boot.PLUGIN_DIR, 'build')
    os.makedirs(build_dir, exist_ok=True)
    sh = os.path.join(api.boot.PLUGIN_DIR, 'build.sh')
    if not os.path.isfile(sh):
        with open(sh, 'w', encoding='utf-8') as f:
            f.write('#!/bin/bash\n')


def _body(resp) -> dict:
    return json.loads(resp.text)


# ─────────────────────────────────────────────────────────────────────────
# token
# ─────────────────────────────────────────────────────────────────────────

def test_token_created_lazily_and_stable(tmp_path, monkeypatch):
    api = _api()
    monkeypatch.setattr(api, 'TOKEN_PATH', str(tmp_path / 'api_token'))
    t1 = api.get_or_create_api_token()
    assert len(t1) == 64 and os.path.isfile(api.TOKEN_PATH)
    assert api.get_or_create_api_token() == t1          # 二次读取不轮换

    os.remove(api.TOKEN_PATH)                            # 文件删了 → 重新生成
    t2 = api.get_or_create_api_token()
    assert len(t2) == 64 and t2 != t1


def test_token_too_short_regenerated(tmp_path, monkeypatch):
    """损坏 / 过短的 token 文件视同不存在,重新生成(避免弱 token 留存)。"""
    api = _api()
    p = tmp_path / 'api_token'
    p.write_text('short\n', encoding='utf-8')
    monkeypatch.setattr(api, 'TOKEN_PATH', str(p))
    t = api.get_or_create_api_token()
    assert len(t) == 64
    assert p.read_text(encoding='utf-8').strip() == t


# ─────────────────────────────────────────────────────────────────────────
# compile:认证 / 服务可用性 / 参数
# ─────────────────────────────────────────────────────────────────────────

async def test_compile_rejects_missing_or_bad_token(tmp_path, monkeypatch):
    api = _api()
    monkeypatch.setattr(api, 'TOKEN_PATH', str(tmp_path / 'api_token'))
    resp = await api.compile_handler(_FakeReq(token=None))
    assert resp.status == 401
    resp = await api.compile_handler(_FakeReq(token='wrong'))
    assert resp.status == 401 and _body(resp)['success'] is False


async def test_compile_rejects_prebuilt_mode(tmp_path, monkeypatch):
    """引擎在用(或已选择)预编译包 → 503 服务不可用。"""
    api = _api()
    monkeypatch.setattr(api, 'TOKEN_PATH', str(tmp_path / 'api_token'))
    token = api.get_or_create_api_token()
    for running, selected in (('prebuilt', 'prebuilt'), ('prebuilt', 'local'),
                              ('local', 'prebuilt')):
        monkeypatch.setattr(api.prebuilt, 'mode_info',
                            lambda r=running, s=selected: {'running': r, 'selected': s})
        resp = await api.compile_handler(_FakeReq(token=token, body={'target': 'x'}))
        assert resp.status == 503
        assert '预编译' in _body(resp)['error']


async def test_compile_validates_target(tmp_path, monkeypatch):
    api = _api()
    monkeypatch.setattr(api, 'TOKEN_PATH', str(tmp_path / 'api_token'))
    token = api.get_or_create_api_token()
    _local_mode(monkeypatch, api)
    _touch_build_env(api)

    resp = await api.compile_handler(_FakeReq(token=token, json_error=True))
    assert resp.status == 400                       # 无 body / 缺 target
    resp = await api.compile_handler(_FakeReq(token=token, body={'target': 'a;rm -rf'}))
    assert resp.status == 400                       # 非法字符
    assert 'target' in _body(resp)['error']


async def test_compile_conflict_when_already_running(tmp_path, monkeypatch):
    api = _api()
    monkeypatch.setattr(api, 'TOKEN_PATH', str(tmp_path / 'api_token'))
    token = api.get_or_create_api_token()
    _local_mode(monkeypatch, api)
    _touch_build_env(api)
    monkeypatch.setattr(api.page_build, 'get_build_state',
                        lambda: {'running': True, 'pid': 42, 'cmd_display': 'x'})
    resp = await api.compile_handler(_FakeReq(token=token, body={'target': 'numcomb'}))
    assert resp.status == 409


async def test_compile_success_returns_elapsed_and_matches(tmp_path, monkeypatch):
    """成功路径:200 + 用时 + 进行中对局数;桥接层 target 原样透传 argv。"""
    api = _api()
    monkeypatch.setattr(api, 'TOKEN_PATH', str(tmp_path / 'api_token'))
    token = api.get_or_create_api_token()
    _local_mode(monkeypatch, api)
    _touch_build_env(api)
    monkeypatch.setattr(api, '_POLL_INTERVAL', 0.0)

    states = iter([
        {'running': False},                                          # 启动前检查
        {'running': True, 'pid': 7, 'cmd_display': 'x'},             # 等待第 1 轮
        {'running': False, 'returncode': 0, 'elapsed_sec': 42},      # 完成
    ])
    started = {}

    def fake_start(argv, display, kind='build', src=''):
        started['argv'], started['src'] = argv, src
        return {'success': True, 'pid': 7}

    monkeypatch.setattr(api.page_build, 'get_build_state', lambda: next(states))
    monkeypatch.setattr(api.page_build, '_start_build', fake_start)
    state.active_matches['m1'] = {'target_id': 'g1', 'is_uid': False}
    state.active_matches['m2'] = {'target_id': 'u2', 'is_uid': True}

    resp = await api.compile_handler(
        _FakeReq(token=token, body={'target': 'LGTBot_ElainaBot'}))
    assert resp.status == 200
    data = _body(resp)
    assert data['success'] is True
    assert data['elapsed_sec'] == 42
    assert data['active_matches'] == 2
    assert started['argv'] == ['bash', 'build.sh', '-i', '-t', 'LGTBot_ElainaBot']
    assert started['src'] == 'API'


async def test_compile_failure_returns_500_with_log_tail(tmp_path, monkeypatch):
    api = _api()
    monkeypatch.setattr(api, 'TOKEN_PATH', str(tmp_path / 'api_token'))
    token = api.get_or_create_api_token()
    _local_mode(monkeypatch, api)
    _touch_build_env(api)
    monkeypatch.setattr(api, '_POLL_INTERVAL', 0.0)

    states = iter([
        {'running': False},
        {'running': False, 'returncode': 2, 'elapsed_sec': 5},
    ])
    monkeypatch.setattr(api.page_build, 'get_build_state', lambda: next(states))
    monkeypatch.setattr(api.page_build, '_start_build',
                        lambda *a, **k: {'success': True, 'pid': 7})
    monkeypatch.setattr(api.page_build, '_read_log_tail',
                        lambda n=0: '\x1b[31merror: no rule to make target\x1b[0m\n')

    resp = await api.compile_handler(_FakeReq(token=token, body={'target': 'nope'}))
    assert resp.status == 500
    data = _body(resp)
    assert data['returncode'] == 2
    assert '退出码 2' in data['error']
    assert data['log_tail'] == 'error: no rule to make target'   # ANSI 已剥


async def test_compile_timeout_returns_504(tmp_path, monkeypatch):
    """超时:504 且不杀进程(响应里提示可 terminate)。"""
    api = _api()
    monkeypatch.setattr(api, 'TOKEN_PATH', str(tmp_path / 'api_token'))
    token = api.get_or_create_api_token()
    _local_mode(monkeypatch, api)
    _touch_build_env(api)
    monkeypatch.setattr(api, '_WAIT_TIMEOUT', 0.0)     # 立即超时

    states = iter([{'running': False}])                # 仅启动前检查被消费
    monkeypatch.setattr(api.page_build, 'get_build_state', lambda: next(states))
    monkeypatch.setattr(api.page_build, '_start_build',
                        lambda *a, **k: {'success': True, 'pid': 7})

    resp = await api.compile_handler(_FakeReq(token=token, body={'target': 'slow'}))
    assert resp.status == 504
    assert 'terminate' in _body(resp)['error']


# ─────────────────────────────────────────────────────────────────────────
# terminate
# ─────────────────────────────────────────────────────────────────────────

async def test_terminate_requires_token(tmp_path, monkeypatch):
    api = _api()
    monkeypatch.setattr(api, 'TOKEN_PATH', str(tmp_path / 'api_token'))
    resp = await api.terminate_handler(_FakeReq(token=None))
    assert resp.status == 401


async def test_terminate_conflict_when_idle(tmp_path, monkeypatch):
    api = _api()
    monkeypatch.setattr(api, 'TOKEN_PATH', str(tmp_path / 'api_token'))
    token = api.get_or_create_api_token()
    monkeypatch.setattr(api.page_build, 'get_build_state', lambda: {'running': False})
    resp = await api.terminate_handler(_FakeReq(token=token))
    assert resp.status == 409


async def test_terminate_kills_running_build(tmp_path, monkeypatch):
    api = _api()
    monkeypatch.setattr(api, 'TOKEN_PATH', str(tmp_path / 'api_token'))
    token = api.get_or_create_api_token()
    monkeypatch.setattr(api.page_build, 'get_build_state',
                        lambda: {'running': True, 'pid': 7, 'cmd_display': 'x'})
    seen = {}

    def fake_kill(src=''):
        seen['src'] = src
        return {'success': True, 'message': '已终止编译进程 (PID 7)'}

    monkeypatch.setattr(api.page_build, '_kill_build', fake_kill)
    resp = await api.terminate_handler(_FakeReq(token=token))
    assert resp.status == 200
    assert _body(resp)['success'] is True
    assert seen['src'] == 'API'
