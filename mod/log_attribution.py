#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""对主框架 ``MessageSender`` 的两处类级补丁:push 日志归类 + 发送失败计数。

① 日志归类:框架 ``_log_push`` 把每条 push 日志记成 ``log_type='proactive'`` +
空 ``plugin_name``。本插件的 C++ 回调没有 event 上下文,只能走 push API,
正确归属应是「LGTBot 消息派发」。

② 失败计数:``_send_push`` 被 QQ 拒绝时**不抛异常**,只返回 ``(ok=False, 错误体,
payload)``。计数放在类级补丁而非 callbacks 各调用点:引擎复用时 C++ 持有的是
旧 callbacks 模块的函数,只有常驻 ``core.message.sender`` 的补丁新旧出站都会经过。

CLAUDE.md §1 禁改 ``core/``,所以补丁从插件侧落:

  1. 类级 monkey-patch(``__slots__`` 禁了实例级 attribute,只能改类);
     两个补丁各自独立的幂等 flag,只打过其中一个的常驻进程热重载后仍能补装另一个。
  2. 用 ``contextvars.ContextVar`` 区分本插件与其他插件的 push:上下文里为 True
     才归类 / 计数,其他插件的 push 行为与原框架一致。
  3. ContextVar 实例经 ``boot._get_persistent()`` 跨热重载共享 —— 补丁闭包捕获的是
     第一次的实例,新模块另建一个的话闭包永远读到 default=False。
  4. metrics 经 ``sys.modules`` 延迟解析 —— 补丁闭包跨热重载常驻,直捕模块对象会
     永远用第一次加载的旧 metrics。

用法:
  · ``@on_load`` 里调 ``install_once()`` —— 幂等,第二次直接跳过。
  · 本插件每次 ``sender.send_to_*`` 调用前用 ``with mark_outbound():`` 包住。
"""

from __future__ import annotations

import contextvars
import json

from . import boot

_PERSIST_KEY = 'log_attribution_ctxvar'
_PATCHED_FLAG = '_lgtbot_log_push_patched'
_SEND_PUSH_FLAG = '_lgtbot_send_push_patched'
_PLUGIN_NAME = 'LGTBot 消息派发'   # 与 dispatcher.lgtbot_dispatch 的 @handler name 对齐


def _get_ctxvar() -> contextvars.ContextVar:
    """跨热重载共享的同一个 ContextVar 实例 (放在 boot 持久化字典里)。"""
    p = boot._get_persistent()
    cv = p.get(_PERSIST_KEY)
    if cv is None:
        cv = contextvars.ContextVar('lgtbot_push_attribution', default=False)
        p[_PERSIST_KEY] = cv
    return cv


class mark_outbound:
    """``with mark_outbound():`` 包住本插件的 ``sender.send_to_*`` 调用。

    上下文内 ContextVar 为 True,patched ``_log_push`` 据此把日志的
    ``plugin_name`` 填成 ``LGTBot 消息派发``;退出 ``with`` 后自动 reset,
    其他协程的发送行为不受影响。
    """
    __slots__ = ('_token',)

    def __enter__(self):
        self._token = _get_ctxvar().set(True)
        return self

    def __exit__(self, exc_type, exc, tb):
        _get_ctxvar().reset(self._token)


def _note_push_result(ret) -> None:
    """从 ``_send_push`` 的返回值 ``(ok, data, payload)`` 提取失败并计入指标。

    仅在 ``mark_outbound`` 上下文内被 patched ``_send_push`` 调用(本插件出站)。
    metrics 每次经 ``sys.modules`` 现取(原因见模块 docstring)。永不抛;非标准返回静默忽略。
    """
    try:
        ok, data = ret[0], ret[1]
        if ok is not False:                    # 仅确定的 False 才计(mock / 未知形状不猜)
            return
        code = None
        if isinstance(data, dict):
            code = data.get('code') or data.get('err_code')
        import sys
        m = sys.modules.get('plugins.LGTBot_ElainaBot.mod.metrics')
        if m is not None:
            m.record_send_failure(code)
    except Exception:
        pass


def install_once() -> None:
    """对 ``MessageSender`` 做类级补丁(``_log_push`` 归类 + ``_send_push`` 计失败),幂等。

    在 ``@on_load`` 内调用。两个补丁各自独立 flag,都已 patched 则 no-op
    (``core.message.sender`` 模块跨重载不卸载,补丁常驻进程内)。
    """
    try:
        from core.message.sender import MessageSender
    except Exception:
        return

    ctxvar = _get_ctxvar()  # 闭包捕获持久化对象

    if not getattr(MessageSender, _PATCHED_FLAG, False):
        def patched_log_push(self, endpoint, payload, content, resp_data=None):
            """复刻框架原 ``_log_push``,差别只在最后调 ``_emit_log`` 时按 ContextVar
            决定要不要把 ``plugin_name`` 填成 ``LGTBot 消息派发``。"""
            parts = endpoint.strip('/').split('/')
            group_id = user_id = ''
            if len(parts) >= 3:
                if parts[1] == 'groups':
                    group_id = parts[2]
                elif parts[1] == 'users':
                    user_id = parts[2]
            text = self._extract_log_text(payload, content)
            raw_msg = json.dumps(payload, ensure_ascii=False, default=str)
            msg_id = ''
            if isinstance(resp_data, dict):
                msg_id = resp_data.get('id') or resp_data.get('msg_id') or ''
            plugin_name = _PLUGIN_NAME if ctxvar.get() else ''
            self._emit_log(text, user_id, group_id, raw_msg, 'proactive',
                           plugin_name=plugin_name, message_id=msg_id)

        MessageSender._log_push = patched_log_push
        setattr(MessageSender, _PATCHED_FLAG, True)

    if not getattr(MessageSender, _SEND_PUSH_FLAG, False):
        orig_send_push = MessageSender._send_push

        async def patched_send_push(self, *args, **kwargs):
            """透传原 ``_send_push``,仅在本插件出站(ContextVar 为 True)且
            ``ok=False`` 时计入发送失败指标 —— 返回值与异常行为原样保留。"""
            ret = await orig_send_push(self, *args, **kwargs)
            if ctxvar.get():
                _note_push_result(ret)
            return ret

        MessageSender._send_push = patched_send_push
        setattr(MessageSender, _SEND_PUSH_FLAG, True)
