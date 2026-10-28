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


async def test_restart_forwards_reason_and_snapshots_before_release(tmp_path, monkeypatch):
    """★ API 重启同样要在释放引擎**之前**快照等待中房间(理由见 watcher 用例),
    并把可选的 ``{"reason"}`` 一起带给房间通知。"""
    build_api, restart_api = _apis()
    token = _token(monkeypatch, build_api, tmp_path)
    state.waiting_rooms['g:GWAIT'] = {'target_id': 'GWAIT', 'is_uid': False,
                                      'game': 'X', 'since': 0}

    def fake_check():
        state.waiting_rooms.clear()          # ← 释放引擎的真实副作用
        return True, '🔁 LGTBot 正在重启...'

    monkeypatch.setattr(dispatcher, 'check_and_prepare_restart', fake_check)
    monkeypatch.setattr(dispatcher, 'schedule_exec_after', lambda d=0.5: None)
    monkeypatch.setattr(restart_api.metrics, 'record_restart', lambda: None)
    monkeypatch.setattr(restart_api.audit, 'record', lambda *a, **k: None)
    seen = {}

    async def fake_notify(reason='', *, skip_keys=frozenset(), rooms=None):
        seen.update(reason=reason, skip=set(skip_keys), rooms=rooms)
        return 0
    monkeypatch.setattr(dispatcher, '_notify_restart_rooms', fake_notify)

    resp = await restart_api.restart_handler(
        _FakeReq(token=token, body={'reason': '修了个 bug'}))

    assert resp.status == 200
    assert [r['target_id'] for r in seen['rooms']] == ['GWAIT']
    assert seen['reason'] == '修了个 bug'
    assert seen['skip'] == set()             # API 重启不排除任何群


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
                        lambda immediate=False: ensured.append(immediate))
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
    assert ensured == [True]                               # watcher 已拉起(没有对局:立即重启)
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
                        lambda immediate=False: ensured.append(immediate))
    monkeypatch.setattr(restart_api.audit, 'record', lambda *a, **k: None)
    resp = await restart_api.planned_restart_handler(_FakeReq(
        token=token, body={'enable': True, 'reason': 'x'}))
    assert _body(resp)['auto'] is False
    assert state.is_planned_restart() and not state.is_planned_restart_auto()
    assert ensured == []


# ─────────────────────────────────────────────────────────────────────────
# 自动重启 watcher
# ─────────────────────────────────────────────────────────────────────────

async def test_auto_watcher_restarts_after_grace(monkeypatch):
    """有对局 → 等待;清空 → 先静默期(结算消息还在发送链路上),满期才
    原子预检 + 审计(自动)+ 指标 + 调度 execv。"""
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
    monkeypatch.setattr(dispatcher, '_AUTO_RESTART_GRACE', 0.08)
    notified = []

    async def fake_notify(reason):
        notified.append(reason)
    monkeypatch.setattr(dispatcher, '_notify_auto_restart', fake_notify)

    task = asyncio.get_running_loop().create_task(dispatcher._auto_restart_watcher())
    await asyncio.sleep(0.05)
    assert 'exec' not in calls                     # 对局还在,不触发
    state.active_matches.clear()
    await asyncio.sleep(0.04)
    assert 'exec' not in calls                     # 已清空但仍在静默期内
    await asyncio.wait_for(task, timeout=2.0)
    assert calls[-2:] == ['metric', 'exec'] and 'check' in calls
    a, kw = audits[0]
    assert a[1] == '自动重启' and '夜间升级' in a[2] and '静默' in a[2]
    assert kw.get('src') == '自动'
    assert notified == ['夜间升级']                # 通知群推送(带维护原因)


async def test_auto_watcher_snapshots_rooms_before_releasing_engine(monkeypatch):
    """★ 等待中房间必须在 ``check_and_prepare_restart`` **之前**快照:释放引擎会让上游
    ``Terminate`` 所有 match,``terminate`` 回调随即清空 ``state.waiting_rooms``,先释放后读表一条通知都发不出去。

    顺带钉住通知群去重:自动重启已经给通知群单独推过一条,房间通知要跳过它们。
    """
    from plugins.LGTBot_ElainaBot.mod import callbacks as _cb, helpers
    monkeypatch.setattr(_cb, 'NOTIFY_GROUPS', ('GNOTIFY',))
    state.set_planned_restart(True, '夜间升级', auto=True)
    state.waiting_rooms['g:GWAIT'] = {'target_id': 'GWAIT', 'is_uid': False,
                                      'game': '某游戏', 'since': 0}

    def fake_check():
        state.waiting_rooms.clear()          # ← 释放引擎的真实副作用
        return True, 'ok'

    monkeypatch.setattr(dispatcher, 'check_and_prepare_restart', fake_check)
    monkeypatch.setattr(dispatcher, 'schedule_exec_after', lambda d=0.5: None)
    monkeypatch.setattr(dispatcher.metrics, 'record_restart', lambda: None)
    monkeypatch.setattr(dispatcher.audit, 'record', lambda *a, **k: None)
    monkeypatch.setattr(dispatcher, '_AUTO_WATCH_INTERVAL', 0.01)
    monkeypatch.setattr(dispatcher, '_AUTO_RESTART_GRACE', 0.02)

    async def fake_auto_notify(reason):
        pass
    monkeypatch.setattr(dispatcher, '_notify_auto_restart', fake_auto_notify)
    seen = {}

    async def fake_rooms_notify(reason='', *, skip_keys=frozenset(), rooms=None):
        seen.update(reason=reason, skip=set(skip_keys), rooms=rooms)
        return 0
    monkeypatch.setattr(dispatcher, '_notify_restart_rooms', fake_rooms_notify)

    task = asyncio.get_running_loop().create_task(dispatcher._auto_restart_watcher())
    await asyncio.wait_for(task, timeout=2.0)

    assert [r['target_id'] for r in seen['rooms']] == ['GWAIT']
    assert seen['reason'] == '夜间升级'
    assert seen['skip'] == {helpers.target_key('GNOTIFY', False)}


def _patch_restart_path(monkeypatch, calls: list, audits: list, notified: list) -> None:
    """自动重启路径上会动引擎 / 换进程 / 发消息的环节全部替身,依次记进 calls。"""
    monkeypatch.setattr(dispatcher, 'check_and_prepare_restart',
                        lambda: calls.append('check') or (True, 'ok'))
    monkeypatch.setattr(dispatcher, 'schedule_exec_after', lambda d=0.5: calls.append('exec'))
    monkeypatch.setattr(dispatcher.metrics, 'record_restart', lambda: None)
    monkeypatch.setattr(dispatcher.audit, 'record', lambda *a, **k: audits.append((a, k)))

    async def fake_notify(reason):
        notified.append(reason)
    monkeypatch.setattr(dispatcher, '_notify_auto_restart', fake_notify)

    async def fake_rooms(reason='', *, skip_keys=frozenset(), rooms=None):
        return 0
    monkeypatch.setattr(dispatcher, '_notify_restart_rooms', fake_rooms)


def _watcher_task():
    from plugins.LGTBot_ElainaBot.mod import boot
    return boot._get_persistent().get(dispatcher._AUTO_WATCH_KEY)


async def test_enabling_without_matches_restarts_without_the_grace(monkeypatch):
    """★ 开启自动模式时已经没有进行中的对局:不进静默期,回执先发出去再重启,照样推自动重启通知。"""
    calls, audits, notified = [], [], []
    _patch_restart_path(monkeypatch, calls, audits, notified)
    # 静默期与轮询间隔都拉长:走的若是原来的轮询路径,超时前等不到重启
    monkeypatch.setattr(dispatcher, '_AUTO_RESTART_GRACE', 60.0)
    monkeypatch.setattr(dispatcher, '_AUTO_WATCH_INTERVAL', 60.0)
    monkeypatch.setattr(dispatcher, '_AUTO_RESTART_NOW_DELAY', 0.01)

    on, msg = dispatcher.set_planned_mode(True, '夜间升级', auto=True)
    assert on and '立即执行重启' in msg and '夜间升级' in msg
    assert calls == []                              # 回执返回时引擎还没动
    await asyncio.wait_for(_watcher_task(), timeout=2.0)
    assert calls == ['check', 'exec']
    a, kw = audits[0]
    assert a[1] == '自动重启' and '立即' in a[2] and '夜间升级' in a[2] and '静默' not in a[2]
    assert kw.get('src') == '自动'
    assert notified == ['夜间升级']                 # 通知群照常收到自动重启说明


async def test_immediate_restart_waits_for_the_reply_first(monkeypatch):
    """★ 指令开启时立即重启:回执发完才动引擎 —— 回执要走网络,先释放引擎再换进程,管理员可能收不到这条回执。"""
    from unittest.mock import MagicMock
    calls, audits, notified = [], [], []
    _patch_restart_path(monkeypatch, calls, audits, notified)
    monkeypatch.setattr(dispatcher.helpers, 'is_foreign_event', lambda e: False)
    monkeypatch.setattr(dispatcher, '_AUTO_RESTART_NOW_DELAY', 0.05)

    async def slow_reply(msg):
        await asyncio.sleep(0.01)                   # 发送回执的网络往返
        calls.append('reply')
    ev = MagicMock()
    ev.reply = slow_reply
    await dispatcher.lgtbot_planned_restart(ev, _planned_match('计划重启 升级引擎'))
    await asyncio.wait_for(_watcher_task(), timeout=2.0)
    assert calls == ['reply', 'check', 'exec']


async def test_enabling_with_matches_still_waits_for_them(monkeypatch):
    """还有对局在进行:不立即重启,照旧轮询等对局结束。"""
    calls, audits, notified = [], [], []
    _patch_restart_path(monkeypatch, calls, audits, notified)
    monkeypatch.setattr(dispatcher, '_AUTO_WATCH_INTERVAL', 0.01)
    monkeypatch.setattr(dispatcher, '_AUTO_RESTART_NOW_DELAY', 0.01)
    state.active_matches['m1'] = {'target_id': 'g1', 'is_uid': False}

    _on, msg = dispatcher.set_planned_mode(True, '', auto=True)
    assert '在无人进行对局时自动执行重启' in msg and '立即' not in msg
    await asyncio.sleep(0.05)
    assert calls == []
    state.set_planned_restart(False)
    await asyncio.wait_for(_watcher_task(), timeout=2.0)


async def test_immediate_restart_backs_off_when_a_match_starts_first(monkeypatch):
    """开启时没有对局,但立即重启之前有人开了局:不重启,退回正常轮询。"""
    calls, audits, notified = [], [], []
    _patch_restart_path(monkeypatch, calls, audits, notified)
    monkeypatch.setattr(dispatcher, '_AUTO_WATCH_INTERVAL', 0.01)
    monkeypatch.setattr(dispatcher, '_AUTO_RESTART_NOW_DELAY', 0.03)

    dispatcher.set_planned_mode(True, '', auto=True)
    state.active_matches['m1'] = {'target_id': 'g1', 'is_uid': False}
    await asyncio.sleep(0.08)
    assert calls == []
    state.set_planned_restart(False)
    await asyncio.wait_for(_watcher_task(), timeout=2.0)


async def test_immediate_restart_is_dropped_when_cancelled_first(monkeypatch):
    """开启后、立即重启之前管理员又取消了:不重启,watcher 随模式关闭退出。"""
    calls, audits, notified = [], [], []
    _patch_restart_path(monkeypatch, calls, audits, notified)
    monkeypatch.setattr(dispatcher, '_AUTO_RESTART_NOW_DELAY', 0.03)

    dispatcher.set_planned_mode(True, '', auto=True)
    dispatcher.set_planned_mode(False)
    await asyncio.wait_for(_watcher_task(), timeout=2.0)
    assert calls == [] and audits == [] and notified == []


async def test_refused_precheck_leaves_the_engine_running(monkeypatch):
    """原子预检被拒(引擎里其实还有对局):不审计、不推通知、不换进程,回到轮询等下一次机会。"""
    calls, audits, notified = [], [], []
    _patch_restart_path(monkeypatch, calls, audits, notified)
    monkeypatch.setattr(dispatcher, 'check_and_prepare_restart',
                        lambda: calls.append('check') or (False, '⚠️ 当前存在进行中的游戏'))
    monkeypatch.setattr(dispatcher, '_AUTO_WATCH_INTERVAL', 0.01)
    monkeypatch.setattr(dispatcher, '_AUTO_RESTART_NOW_DELAY', 0.01)

    dispatcher.set_planned_mode(True, '', auto=True)
    await asyncio.sleep(0.05)
    assert calls == ['check'] and audits == [] and notified == []
    state.set_planned_restart(False)
    await asyncio.wait_for(_watcher_task(), timeout=2.0)


async def test_enabling_again_keeps_the_running_watcher(monkeypatch):
    """已经有 watcher 在跑(自动模式下再开一次):沿用它的节奏,不另起,回执也不说立即重启。"""
    calls, audits, notified = [], [], []
    _patch_restart_path(monkeypatch, calls, audits, notified)
    monkeypatch.setattr(dispatcher, '_AUTO_WATCH_INTERVAL', 0.01)
    state.active_matches['m1'] = {'target_id': 'g1', 'is_uid': False}
    dispatcher.set_planned_mode(True, '', auto=True)
    first = _watcher_task()

    state.active_matches.clear()
    _on, msg = dispatcher.set_planned_mode(True, '', auto=True)
    assert _watcher_task() is first
    assert '立即' not in msg
    state.set_planned_restart(False)
    await asyncio.wait_for(first, timeout=2.0)
    assert calls == []


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


# ─────────────────────────────────────────────────────────────────────────
# 指令「计划重启 [手动|自动] [原因]」的 auto 解析(默认自动)
# ─────────────────────────────────────────────────────────────────────────

def _planned_match(text):
    import re
    return re.match(dispatcher._P_PLANNED, text)


async def _run_planned_cmd(monkeypatch, text, ensure=lambda immediate=False: False):
    """跑一条「计划重启 …」指令,返回回执。``ensure`` 顶替真正拉起 watcher 的 _ensure_auto_restart_watcher。"""
    from unittest.mock import AsyncMock, MagicMock
    monkeypatch.setattr(dispatcher.helpers, 'is_foreign_event', lambda e: False)
    monkeypatch.setattr(dispatcher.audit, 'record', lambda *a, **k: None)
    monkeypatch.setattr(dispatcher, '_ensure_auto_restart_watcher', ensure)
    ev = MagicMock()
    ev.reply = AsyncMock()
    await dispatcher.lgtbot_planned_restart(ev, _planned_match(text))
    return ev.reply.await_args.args[0]


async def test_planned_command_says_it_restarts_now_without_matches(monkeypatch):
    """★ 开启时已经没有进行中的对局:watcher 按「立即」拉起,回执写明立即重启;有对局时照旧等对局结束。"""
    started = []

    def ensure(immediate=False):
        started.append(immediate)
        return True
    msg = await _run_planned_cmd(monkeypatch, '计划重启 升级引擎', ensure)
    assert started == [True]
    assert '立即执行重启' in msg and '升级引擎' in msg
    await _run_planned_cmd(monkeypatch, '计划重启')          # 关闭

    started.clear()
    state.active_matches['m1'] = {'target_id': 'g1', 'is_uid': False}
    msg = await _run_planned_cmd(monkeypatch, '计划重启', ensure)
    assert started == [False]
    assert '自动执行重启' in msg and '立即' not in msg and '剩余进行中对局 1 局' in msg
    await _run_planned_cmd(monkeypatch, '计划重启')


async def test_planned_command_auto_keyword(monkeypatch):
    """「计划重启 自动 <原因>」首词恰为「自动」→ auto=True + 原因剥离。"""
    msg = await _run_planned_cmd(monkeypatch, '计划重启 自动 升级引擎')
    assert state.is_planned_restart() and state.is_planned_restart_auto()
    assert state.planned_restart_reason() == '升级引擎'
    assert '自动重启' in msg and '自动执行重启' in msg
    await _run_planned_cmd(monkeypatch, '计划重启')          # 再触发 = 关闭
    assert not state.is_planned_restart()


async def test_planned_command_defaults_to_auto(monkeypatch):
    """★ 不带关键词就是自动模式 —— 与面板弹窗默认一致:不拦新游戏,对局散场后自己重启。"""
    msg = await _run_planned_cmd(monkeypatch, '计划重启')
    assert state.is_planned_restart() and state.is_planned_restart_auto()
    assert '自动执行重启' in msg
    await _run_planned_cmd(monkeypatch, '计划重启')          # 关闭

    msg = await _run_planned_cmd(monkeypatch, '计划重启 升级引擎')
    assert state.is_planned_restart_auto()
    assert state.planned_restart_reason() == '升级引擎'
    await _run_planned_cmd(monkeypatch, '计划重启')


async def test_planned_command_manual_keyword(monkeypatch):
    """★ 首词恰为「手动」才退回手动:拦新游戏,关键词剥掉不进原因。"""
    msg = await _run_planned_cmd(monkeypatch, '计划重启 手动 升级引擎')
    assert state.is_planned_restart() and not state.is_planned_restart_auto()
    assert state.planned_restart_reason() == '升级引擎'
    assert '已禁用新游戏创建' in msg and '自动执行重启' not in msg
    await _run_planned_cmd(monkeypatch, '计划重启')

    await _run_planned_cmd(monkeypatch, '计划重启 手动')      # 只有关键词
    assert not state.is_planned_restart_auto()
    assert state.planned_restart_reason() == ''
    await _run_planned_cmd(monkeypatch, '计划重启')


async def test_planned_command_keyword_must_stand_alone(monkeypatch):
    """关键词没独立成词(原因叫"手动升级")→ 仍按默认自动,整串作原因。"""
    await _run_planned_cmd(monkeypatch, '计划重启 手动升级')
    assert state.is_planned_restart_auto()
    assert state.planned_restart_reason() == '手动升级'
    await _run_planned_cmd(monkeypatch, '计划重启')


async def test_auto_watcher_grace_resets_on_new_match(monkeypatch):
    """静默期内已建房间开出新局 → 计时清零,不会带着旧计时重启。"""
    state.set_planned_restart(True, '', auto=True)
    state.active_matches.clear()
    monkeypatch.setattr(dispatcher, 'schedule_exec_after',
                        lambda d=0.5: pytest.fail('静默期被打断后不应触发重启'))
    monkeypatch.setattr(dispatcher, '_AUTO_WATCH_INTERVAL', 0.01)
    monkeypatch.setattr(dispatcher, '_AUTO_RESTART_GRACE', 0.06)

    task = asyncio.get_running_loop().create_task(dispatcher._auto_restart_watcher())
    await asyncio.sleep(0.03)                      # 静默期进行中
    state.active_matches['m2'] = {'target_id': 'g2', 'is_uid': False}   # 新局出现
    await asyncio.sleep(0.08)                      # 若未重置早就超过 0.06s 了
    state.set_planned_restart(False)               # 收尾:关模式让 task 退出
    await asyncio.wait_for(task, timeout=2.0)


def test_should_block_new_game_only_in_manual_mode():
    """维护闸:手动模式拦 /新游戏;自动模式放行;未开启不拦。"""
    assert not dispatcher._should_block_new_game('/新游戏')      # 模式未开启
    state.set_planned_restart(True, '', auto=False)
    assert dispatcher._should_block_new_game('/新游戏')          # 手动:拦
    assert dispatcher._should_block_new_game('/随机游戏')
    assert not dispatcher._should_block_new_game('/加入')        # 非新建不拦
    state.set_planned_restart(True, '', auto=True)
    assert not dispatcher._should_block_new_game('/新游戏')      # 自动:放行
    state.set_planned_restart(False)


async def test_notify_auto_restart_sends_markdown(monkeypatch):
    """通知内容含「自动重启」标识与维护原因;未配置通知群 / 无 sender 时静默跳过。"""
    from unittest.mock import AsyncMock, MagicMock
    from plugins.LGTBot_ElainaBot.mod import callbacks, helpers
    sender = MagicMock()
    sender.send_to_group = AsyncMock(return_value=(True, {}, {}))
    monkeypatch.setattr(helpers, 'get_sender', lambda appid='': sender)
    monkeypatch.setattr(callbacks, 'NOTIFY_GROUPS', ('GNOTIFY',))

    await dispatcher._notify_auto_restart('夜间升级')
    args = sender.send_to_group.await_args.args
    assert args[0] == 'GNOTIFY'
    assert '自动重启' in args[1] and '夜间升级' in args[1]

    sender.send_to_group.reset_mock()
    await dispatcher._notify_auto_restart('')      # 无原因 → 整个栏目不展示
    md = sender.send_to_group.await_args.args[1]
    assert '更新内容' not in md and '未填写' not in md
    assert '自动重启' in md                        # 主体通知仍完整

    sender.send_to_group.reset_mock()
    monkeypatch.setattr(callbacks, 'NOTIFY_GROUPS', ())
    await dispatcher._notify_auto_restart('x')     # 未配置通知群 → 不发送
    sender.send_to_group.assert_not_called()


async def test_notify_auto_restart_fans_out_to_every_group(monkeypatch):
    """★ 配了多个通知群 → **每个群都收到**同一条自动重启说明。"""
    from unittest.mock import AsyncMock, MagicMock
    from plugins.LGTBot_ElainaBot.mod import callbacks, helpers
    sender = MagicMock()
    sender.send_to_group = AsyncMock(return_value=(True, {}, {}))
    monkeypatch.setattr(helpers, 'get_sender', lambda appid='': sender)
    monkeypatch.setattr(callbacks, 'NOTIFY_GROUPS', ('G1', 'G2', 'G3'))

    await dispatcher._notify_auto_restart('夜间升级')
    sent = {c.args[0]: c.args[1] for c in sender.send_to_group.await_args_list}
    assert set(sent) == {'G1', 'G2', 'G3'}
    assert len({md for md in sent.values()}) == 1      # 三个群内容一致
    assert '自动重启' in next(iter(sent.values()))
