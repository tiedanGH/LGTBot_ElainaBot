#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""重启 / 计划重启 API + 自动重启 watcher 测试。

token 体系与编译 API 共用(_check_auth);os.execv / 引擎释放全部 monkeypatch,
这里只测协议(状态码 / JSON 形状 / state 置位)与 watcher 的触发语义。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from plugins.LGTBot_ElainaBot.mod import dispatcher, state


def _apis():
    pytest.importorskip('aiohttp')
    from plugins.LGTBot_ElainaBot.mod.webui import build_api, restart_api
    return build_api, restart_api


class _FakeReq:
    def __init__(self, token=None, body=None, raw_invalid=False):
        self.headers = {}
        if token is not None:
            self.headers['Authorization'] = f'Bearer {token}'
        self._body = body
        self._raw_invalid = raw_invalid
        self.can_read_body = body is not None or raw_invalid

    async def json(self):
        if self._raw_invalid:
            raise ValueError('bad json')
        return self._body


def _body(resp) -> dict:
    return json.loads(resp.text)


@pytest.fixture(autouse=True)
def _clean_planned():
    """planned 标志与 watcher task 逐测清理(conftest 不管这些 key)。"""
    from plugins.LGTBot_ElainaBot.mod import boot
    state.set_planned_restart(False)
    boot._get_persistent().pop(dispatcher._AUTO_WATCH_KEY, None)
    yield
    t = boot._get_persistent().pop(dispatcher._AUTO_WATCH_KEY, None)
    if t is not None:
        t.cancel()
    state.set_planned_restart(False)


def _token(monkeypatch, api, tmp_path):
    monkeypatch.setattr(api, 'TOKEN_PATH', str(tmp_path / 'api_token'))
    return api.get_or_create_api_token()


# ─────────────────────────────────────────────────────────────────────────
# POST /restart
# ─────────────────────────────────────────────────────────────────────────

async def test_restart_requires_token(tmp_path, monkeypatch):
    build_api, restart_api = _apis()
    _token(monkeypatch, build_api, tmp_path)
    resp = await restart_api.restart_handler(_FakeReq(token=None))
    assert resp.status == 401


async def test_restart_rejected_with_active_matches(tmp_path, monkeypatch):
    build_api, restart_api = _apis()
    token = _token(monkeypatch, build_api, tmp_path)
    monkeypatch.setattr(dispatcher, 'check_and_prepare_restart',
                        lambda: (False, '⚠️ 当前存在进行中的游戏'))
    state.active_matches['m1'] = {'target_id': 'g1', 'is_uid': False}
    audits = []
    monkeypatch.setattr(restart_api.audit, 'record',
                        lambda *a, **k: audits.append((a, k)))
    resp = await restart_api.restart_handler(_FakeReq(token=token, body={}))
    assert resp.status == 409
    data = _body(resp)
    assert data['active_matches'] == 1
    assert audits[0][1].get('src') == 'API' and audits[0][1].get('ok') is False


async def test_restart_success_schedules_exec(tmp_path, monkeypatch):
    build_api, restart_api = _apis()
    token = _token(monkeypatch, build_api, tmp_path)
    calls = []
    monkeypatch.setattr(dispatcher, 'check_and_prepare_restart',
                        lambda: (True, '🔁 LGTBot 正在重启...'))
    monkeypatch.setattr(dispatcher, 'schedule_exec_after',
                        lambda d=0.5: calls.append(d))
    monkeypatch.setattr(restart_api.metrics, 'record_restart',
                        lambda: calls.append('metric'))
    monkeypatch.setattr(restart_api.audit, 'record', lambda *a, **k: None)
    resp = await restart_api.restart_handler(_FakeReq(token=token))
    assert resp.status == 200
    assert _body(resp)['success'] is True
    assert 0.5 in calls and 'metric' in calls


# ─────────────────────────────────────────────────────────────────────────
# POST /planned-restart
# ─────────────────────────────────────────────────────────────────────────

async def test_planned_requires_boolean_enable(tmp_path, monkeypatch):
    build_api, restart_api = _apis()
    token = _token(monkeypatch, build_api, tmp_path)
    resp = await restart_api.planned_restart_handler(_FakeReq(token=token, body={}))
    assert resp.status == 400
    resp = await restart_api.planned_restart_handler(
        _FakeReq(token=token, body={'enable': 'yes'}))          # 非 bool 同样 400
    assert resp.status == 400
    resp = await restart_api.planned_restart_handler(_FakeReq(token=token, raw_invalid=True))
    assert resp.status == 400


async def test_planned_enable_with_auto_and_reason(tmp_path, monkeypatch):
    build_api, restart_api = _apis()
    token = _token(monkeypatch, build_api, tmp_path)
    ensured = []
    monkeypatch.setattr(dispatcher, '_ensure_auto_restart_watcher',
                        lambda: ensured.append(1))
    audits = []
    monkeypatch.setattr(restart_api.audit, 'record',
                        lambda *a, **k: audits.append((a, k)))

    resp = await restart_api.planned_restart_handler(_FakeReq(
        token=token, body={'enable': True, 'auto': True, 'reason': '升级引擎'}))
    assert resp.status == 200
    data = _body(resp)
    assert data['enabled'] is True and data['auto'] is True
    assert data['reason'] == '升级引擎'
    assert state.is_planned_restart() and state.is_planned_restart_auto()
    assert state.planned_restart_reason() == '升级引擎'
    assert ensured == [1]                                  # watcher 已拉起
    assert '自动重启' in audits[0][0][2] and audits[0][1].get('src') == 'API'

    # 关闭:reason / auto 一并清掉
    resp = await restart_api.planned_restart_handler(_FakeReq(
        token=token, body={'enable': False}))
    data = _body(resp)
    assert data['enabled'] is False and data['auto'] is False
    assert not state.is_planned_restart() and not state.is_planned_restart_auto()


async def test_planned_manual_mode_does_not_start_watcher(tmp_path, monkeypatch):
    """默认手动:auto 缺省 False,不拉起 watcher。"""
    build_api, restart_api = _apis()
    token = _token(monkeypatch, build_api, tmp_path)
    ensured = []
    monkeypatch.setattr(dispatcher, '_ensure_auto_restart_watcher',
                        lambda: ensured.append(1))
    monkeypatch.setattr(restart_api.audit, 'record', lambda *a, **k: None)
    resp = await restart_api.planned_restart_handler(_FakeReq(
        token=token, body={'enable': True, 'reason': 'x'}))
    assert _body(resp)['auto'] is False
    assert state.is_planned_restart() and not state.is_planned_restart_auto()
    assert ensured == []


# ─────────────────────────────────────────────────────────────────────────
# 自动重启 watcher
# ─────────────────────────────────────────────────────────────────────────

async def test_auto_watcher_restarts_when_matches_clear(monkeypatch):
    """有对局 → 等待;对局清空 → 原子预检 + 审计(自动)+ 指标 + 调度 execv。"""
    state.set_planned_restart(True, '夜间升级', auto=True)
    state.active_matches['m1'] = {'target_id': 'g1', 'is_uid': False}
    calls, audits = [], []
    monkeypatch.setattr(dispatcher, 'check_and_prepare_restart',
                        lambda: (True, 'ok') if calls.append('check') is None else None)
    monkeypatch.setattr(dispatcher, 'schedule_exec_after',
                        lambda d=0.5: calls.append('exec'))
    monkeypatch.setattr(dispatcher.metrics, 'record_restart',
                        lambda: calls.append('metric'))
    monkeypatch.setattr(dispatcher.audit, 'record',
                        lambda *a, **k: audits.append((a, k)))
    monkeypatch.setattr(dispatcher, '_AUTO_WATCH_INTERVAL', 0.01)

    task = asyncio.get_running_loop().create_task(dispatcher._auto_restart_watcher())
    await asyncio.sleep(0.05)
    assert 'exec' not in calls                     # 对局还在,不触发
    state.active_matches.clear()
    await asyncio.wait_for(task, timeout=2.0)
    assert calls[-2:] == ['metric', 'exec'] and 'check' in calls
    a, kw = audits[0]
    assert a[1] == '自动重启' and '夜间升级' in a[2]
    assert kw.get('src') == '自动'


async def test_auto_watcher_exits_when_mode_disabled(monkeypatch):
    """维护模式被手动关闭 → watcher 自然退出,不触发重启。"""
    state.set_planned_restart(True, '', auto=True)
    state.active_matches['m1'] = {'target_id': 'g1', 'is_uid': False}
    monkeypatch.setattr(dispatcher, 'schedule_exec_after',
                        lambda d=0.5: pytest.fail('不应触发重启'))
    monkeypatch.setattr(dispatcher, '_AUTO_WATCH_INTERVAL', 0.01)
    task = asyncio.get_running_loop().create_task(dispatcher._auto_restart_watcher())
    await asyncio.sleep(0.03)
    state.set_planned_restart(False)               # 关闭模式
    await asyncio.wait_for(task, timeout=2.0)      # 正常退出
