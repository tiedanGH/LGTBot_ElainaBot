#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""@全体成员:发送前校验(当日次数 / 主动消息额度 / 管理员身份 / 主动推送权限)、发送与记账、API 的错误码。

sender 全部替身:bot_state 返回给定的群状态,send_to_group 只记下发出去的内容。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time

import pytest

from plugins.LGTBot_ElainaBot.mod import at_all, callbacks, helpers, metrics

GID = 'G_AT_ALL'


class _Sender:
    """假 sender:``role`` / ``push`` 是 bot_state 里机器人的群身份与主动推送权限。"""

    def __init__(self, role='admin', push=True):
        self.state = {'member_role': role, 'allow_proactive_msg': push}
        self.send_ret = (True, {'id': 'M1'}, {})
        self.sent = []
        self.state_calls = 0

    async def get_group_bot_state(self, gid, return_error=False):
        self.state_calls += 1
        return (self.state, None) if self.state is not None else (None, {'code': 11264})

    async def send_to_group(self, gid, content, **kw):
        self.sent.append((gid, content))
        return self.send_ret


@pytest.fixture(autouse=True)
def _clean_metrics():
    shutil.rmtree(metrics.METRICS_DIR, ignore_errors=True)
    at_all._inflight.clear()
    yield
    shutil.rmtree(metrics.METRICS_DIR, ignore_errors=True)


@pytest.fixture
def sender(monkeypatch):
    s = _Sender()
    monkeypatch.setattr(helpers, 'get_sender', lambda appid='': s)
    monkeypatch.setattr(callbacks, 'ACTIVE_PUSH_DAILY_LIMIT', 1000)
    return s


async def _refused(gid=GID, content='明晚八点开赛') -> at_all.AtAllError:
    with pytest.raises(at_all.AtAllError) as ei:
        await at_all.send(gid, content)
    return ei.value


# ─────────────────────────────────────────────────────────────────────────
# 发送与记账
# ─────────────────────────────────────────────────────────────────────────

async def test_sends_one_at_everyone_before_the_content(sender):
    """★ 发出去的是一个 @全体成员 标签 + 换行 + 正文;记一次 @全体(累计 / 上次时间 / 该群今日)和一条主动消息。"""
    before = int(time.time())
    res = await at_all.send(GID, '明晚八点开赛')
    assert sender.sent == [(GID, '<qqbot-at-everyone />\n明晚八点开赛')]
    assert res == {'used_today': 1, 'daily_limit': 1}
    snap = metrics.snapshot()
    assert snap['at_all_total'] == 1 and snap['last_at_all_ts'] >= before
    assert metrics.at_all_used(GID) == 1
    assert metrics.active_push_used(GID, False) == 1


async def test_content_cannot_add_mentions_of_its_own(sender):
    """★ 正文里现成的 @ 标签一律拆掉:每次请求只 @ 一次全体,当日次数才对得上。"""
    await at_all.send(GID, '<qqbot-at-everyone /> 再来一次 <QQBOT-AT-USER id="X" />')
    sent = sender.sent[0][1]
    assert sent.count('<qqbot-at-everyone />') == 1 and sent.startswith('<qqbot-at-everyone />\n')
    assert '<qqbot-at-user' not in sent.lower()
    assert '＜qqbot-at-everyone />' in sent              # 文字原样留着,只是不再是标签


async def test_second_send_on_the_same_day_is_refused(sender):
    """★ 官方每群每天 1 次:第二次在本地就拒绝,不再打 bot_state,也不发。"""
    await at_all.send(GID, '第一条')
    err = await _refused(content='第二条')
    assert (err.status, err.code) == (429, 'at_all_limit')
    assert len(sender.sent) == 1 and sender.state_calls == 1
    assert metrics.snapshot()['at_all_total'] == 1


async def test_daily_count_resets_the_next_day(sender):
    metrics.record_at_all(GID, ts=time.time() - 86400)        # 昨天用过
    assert metrics.at_all_used(GID) == 0
    await at_all.send(GID, '今天的')
    assert metrics.at_all_used(GID) == 1
    assert metrics.snapshot()['at_all_total'] == 2


async def test_each_group_has_its_own_daily_count(sender):
    await at_all.send(GID, 'A 群')
    await at_all.send('G_OTHER', 'B 群')
    assert [g for g, _c in sender.sent] == [GID, 'G_OTHER']


# ─────────────────────────────────────────────────────────────────────────
# 发送前校验
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('role', ['member', ''])
async def test_non_admin_is_refused(sender, role):
    """★ 机器人不是群管理员:拒绝,不发也不记账。"""
    sender.state['member_role'] = role
    err = await _refused()
    assert (err.status, err.code) == (403, 'not_admin')
    assert sender.sent == [] and metrics.at_all_used(GID) == 0


async def test_group_owner_counts_as_admin(sender):
    sender.state['member_role'] = 'owner'
    await at_all.send(GID, 'x')
    assert len(sender.sent) == 1


async def test_group_without_push_permission_is_refused(sender):
    """不挂在任何对话上,只能走主动消息:群没开主动推送就发不出去。"""
    sender.state['allow_proactive_msg'] = False
    err = await _refused()
    assert (err.status, err.code) == (403, 'no_push_permission')
    assert sender.sent == []


async def test_active_push_limit_is_checked_before_bot_state(sender, monkeypatch):
    """★ 该群今日主动消息已到上限:拒绝,而且不再为此打一次 bot_state;上限设 0 表示不限。"""
    monkeypatch.setattr(metrics, 'active_push_used', lambda tid, is_uid: 1000)
    err = await _refused()
    assert (err.status, err.code) == (429, 'push_limit')
    assert sender.state_calls == 0 and sender.sent == []

    monkeypatch.setattr(callbacks, 'ACTIVE_PUSH_DAILY_LIMIT', 0)
    await at_all.send(GID, 'x')
    assert len(sender.sent) == 1


async def test_bot_state_failure_is_refused(sender, monkeypatch):
    """查不到机器人在群里的身份就不发(按非管理员处理太武断,单独报错)。"""
    sender.state = None
    err = await _refused()
    assert (err.status, err.code) == (502, 'bot_state_failed')

    async def boom(gid, return_error=False):
        raise RuntimeError('timeout')
    monkeypatch.setattr(sender, 'get_group_bot_state', boom)
    err = await _refused()
    assert (err.status, err.code) == (502, 'bot_state_failed')
    assert sender.sent == []


async def test_no_bot_is_refused(monkeypatch):
    monkeypatch.setattr(helpers, 'get_sender', lambda appid='': None)
    err = await _refused()
    assert (err.status, err.code) == (503, 'bot_unavailable')


@pytest.mark.parametrize('fail', ['rejected', 'raised'])
async def test_failed_send_does_not_use_up_the_day(sender, monkeypatch, fail):
    """★ QQ 拒绝 / 发送抛异常:报 send_failed,不记账 —— 这一次不占当日次数,修好原因还能再发。"""
    if fail == 'rejected':
        sender.send_ret = (False, {'code': 40054010, 'message': 'x'}, {})
    else:
        async def boom(gid, content, **kw):
            raise RuntimeError('network')
        monkeypatch.setattr(sender, 'send_to_group', boom)
    err = await _refused()
    assert (err.status, err.code) == (502, 'send_failed')
    if fail == 'rejected':
        assert '40054010' in err.message
    assert metrics.at_all_used(GID) == 0 and metrics.snapshot()['at_all_total'] == 0
    assert metrics.active_push_used(GID, False) == 0


async def test_concurrent_requests_for_one_group(sender, monkeypatch):
    """★ 同一个群的第二个请求在第一个还没记账时进来:直接回 busy,不会两条都发出去;别的群不受影响。"""
    gate = asyncio.Event()
    real_send = sender.send_to_group

    async def slow_send(gid, content, **kw):
        if gid == GID:
            # 限时等:并发没拦住时第二个请求也会停在这里,超时让它报 send_failed,而不是把整个测试挂死
            await asyncio.wait_for(gate.wait(), timeout=2.0)
        return await real_send(gid, content, **kw)
    monkeypatch.setattr(sender, 'send_to_group', slow_send)

    first = asyncio.get_running_loop().create_task(at_all.send(GID, '第一条'))
    await asyncio.sleep(0.01)
    err = await _refused(content='第二条')
    assert (err.status, err.code) == (409, 'busy')
    await at_all.send('G_OTHER', '别的群')                    # 别的群照常
    gate.set()
    await asyncio.wait_for(first, timeout=2.0)
    assert [c for g, c in sender.sent if g == GID] == ['<qqbot-at-everyone />\n第一条']
    assert GID not in at_all._inflight


def test_untrusted_text_cannot_carry_an_at_everyone():
    """★ 引擎回显的玩家输入、昵称里现成的 @全体 标签会被拆掉,只有 API 能真的 @全体。"""
    assert helpers.neutralize_qq_tags('<qqbot-at-everyone />') == '＜qqbot-at-everyone />'
    assert helpers.neutralize_qq_tags('< QQBOT-at-everyone/>') == '＜ QQBOT-at-everyone/>'
    assert '<qqbot' not in helpers.sanitize_md_name('<qqbot-at-everyone />')
    assert helpers.outbound_text('<qqbot-at-everyone />', True) == '＜qqbot-at-everyone />'


# ─────────────────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────────────────

def _api():
    pytest.importorskip('aiohttp')
    from plugins.LGTBot_ElainaBot.mod.webui import at_all_api, build_api
    return at_all_api, build_api


class _Req:
    def __init__(self, token=None, body=None, raw_invalid=False):
        self.headers = {} if token is None else {'Authorization': f'Bearer {token}'}
        self._body, self._raw_invalid = body, raw_invalid
        self.can_read_body = body is not None or raw_invalid

    async def json(self):
        if self._raw_invalid:
            raise ValueError('bad json')
        return self._body


@pytest.fixture
def token(monkeypatch, tmp_path):
    _at_all_api, build_api = _api()
    monkeypatch.setattr(build_api, 'TOKEN_PATH', str(tmp_path / 'api_token'))
    return build_api.get_or_create_api_token()


def _body(resp) -> dict:
    return json.loads(resp.text)


async def test_api_requires_the_build_api_token(token):
    at_all_api, _b = _api()
    resp = await at_all_api.at_all_handler(_Req(body={'group_openid': GID, 'content': 'x'}))
    assert resp.status == 401 and _body(resp)['code'] == 'unauthorized'
    resp = await at_all_api.at_all_handler(_Req(token='wrong', body={'group_openid': GID, 'content': 'x'}))
    assert resp.status == 401


@pytest.mark.parametrize('body', [
    None,                                          # 非法 JSON(见 raw_invalid)
    {'content': 'x'},
    {'group_openid': GID},
    {'group_openid': '  ', 'content': 'x'},
    {'group_openid': GID, 'content': '   '},
    {'group_openid': 123, 'content': 'x'},
])
async def test_api_rejects_bad_requests(token, sender, body):
    at_all_api, _b = _api()
    req = _Req(token=token, raw_invalid=True) if body is None else _Req(token=token, body=body)
    resp = await at_all_api.at_all_handler(req)
    data = _body(resp)
    assert resp.status == 400 and data['code'] == 'bad_request' and data['success'] is False
    assert sender.sent == []


async def test_api_sends_and_reports_today_usage(token, sender):
    """★ 成功:200 + 当日用量;可选的 send_at 只是预留,收下但不影响发送(立即发)。"""
    at_all_api, _b = _api()
    resp = await at_all_api.at_all_handler(_Req(token=token, body={
        'group_openid': f' {GID} ', 'content': ' 明晚八点开赛 ', 'send_at': '2026-11-01 20:00'}))
    assert resp.status == 200
    data = _body(resp)
    assert data['success'] is True and data['group_openid'] == GID
    assert (data['used_today'], data['daily_limit']) == (1, 1)
    assert sender.sent == [(GID, '<qqbot-at-everyone />\n明晚八点开赛')]


@pytest.mark.parametrize('setup, status, code', [
    (lambda s: s.state.update(member_role='member'), 403, 'not_admin'),
    (lambda s: s.state.update(allow_proactive_msg=False), 403, 'no_push_permission'),
    (lambda s: metrics.record_at_all(GID), 429, 'at_all_limit'),
])
async def test_api_maps_refusals_to_status_and_code(token, sender, setup, status, code):
    """校验没过:HTTP 状态码 + 错误码 + 说明,统一 ``{success: false, error, code}``。"""
    at_all_api, _b = _api()
    setup(sender)
    resp = await at_all_api.at_all_handler(_Req(token=token, body={'group_openid': GID, 'content': 'x'}))
    data = _body(resp)
    assert resp.status == status and data['code'] == code
    assert data['success'] is False and data['error']


def test_copied_token_lists_the_at_all_endpoint(token):
    """「复制 API Token」的端点说明里有 @全体成员,且就是实际注册的路径。"""
    import html
    _a, build_api = _api()
    from plugins.LGTBot_ElainaBot.mod.webui import main as wm
    frag = build_api.render_api_token()
    payload = json.loads(html.unescape(frag[len('<pre id="result">'):-len('</pre>')]))
    assert payload['endpoints']['at_all'] == wm._AT_ALL_API_ROUTE == '/api/ext/lgtbot/at-all'
