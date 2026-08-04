#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""log_attribution 的发送失败计数补丁 —— 挂点必须在框架层的回归。

背景:引擎复用时 C++ 持有**旧 callbacks 模块**的回调函数(热重载遇到进行中
对局不重新 start,CLAUDE.md §5),挂在 callbacks 调用点的新代码不在旧回调的
执行路径上 —— 生产观察到发送失败从未被计数。收敛点因此下沉到
``MessageSender._send_push`` 类级补丁(常驻进程,新旧模块统一经过),由
``mark_outbound`` 的 ContextVar 决定是否计数。这里测纯函数 ``_note_push_result``
与 ContextVar 门控语义;真实类补丁依赖框架 MessageSender,由生产路径覆盖。
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

from plugins.LGTBot_ElainaBot.mod import log_attribution, metrics


def _capture(monkeypatch):
    seen = []
    monkeypatch.setattr(metrics, 'record_send_failure', lambda code: seen.append(code))
    assert sys.modules.get('plugins.LGTBot_ElainaBot.mod.metrics') is metrics
    return seen


def test_note_push_result_records_only_definite_failure(monkeypatch):
    seen = _capture(monkeypatch)
    log_attribution._note_push_result((False, {'message': 'x', 'code': 40034102}, {}))
    log_attribution._note_push_result((False, {'err_code': 22009}, {}))   # code 缺失退 err_code
    log_attribution._note_push_result((False, 'not-a-dict', {}))          # 无错误体 → code None
    assert seen == [40034102, 22009, None]


def test_note_push_result_ignores_success_and_mocks(monkeypatch):
    seen = _capture(monkeypatch)
    log_attribution._note_push_result((True, {'id': 'M1'}, {}))     # 成功
    log_attribution._note_push_result(MagicMock())                  # mock 形状不猜
    log_attribution._note_push_result(None)                         # 异常形状不抛
    log_attribution._note_push_result((1, {}, {}))                  # truthy 非 bool 不计
    assert seen == []


def test_mark_outbound_gates_ctxvar():
    """mark_outbound 内 ContextVar 为 True(补丁据此只计本插件出站),退出复位。"""
    cv = log_attribution._get_ctxvar()
    assert cv.get() is False
    with log_attribution.mark_outbound():
        assert cv.get() is True
    assert cv.get() is False
