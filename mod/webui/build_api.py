#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""编译 API —— 面向框架内其他插件的 HTTP 接口(独立 token 认证,JSON 响应)。

与「引擎编译」面板标签(page_build)共用同一套子进程调度(``_start_build`` / ``_kill_build`` / ``get_build_state``):
API 触发的编译同样落 state.json + build.log,面板打开就能看到实时日志;反之面板起的编译也会让 API 返回 409。

端点(webui/main.py 以 ``auth=False`` 注册,面板登录态不适用,靠自有 token):
  · ``POST /api/ext/lgtbot/build/compile``    {"target": "<name>"} 增量编译单个
    目标(桥接层传 ``LGTBot_ElainaBot``)。**同步等待**编译结束再响应 ——
    完整 / 增量全量编译动辄十几分钟,HTTP 语义下必超时,故 API 只开放单目标。
  · ``POST /api/ext/lgtbot/build/terminate``  强制中断当前编译(含面板起的)。

认证:``Authorization: Bearer <token>`` 或 ``X-API-Token: <token>``。
token 落 ``data/build/api_token``(独立文件,不存在则随机生成;放 data/build/ 而非编译产物 build/
后者会被 --clean / 面板「删除 build 目录」整体清掉,token 若随之轮换,调用方会毫无征兆地集体 401)。
面板「引擎编译」标签有一键复制按钮。

状态码约定(响应体一律 JSON):
  200 编译成功(elapsed_sec 用时 / active_matches 进行中对局数)或中断成功
  400 缺 target / target 名非法
  401 token 缺失或错误
  409 已有编译在进行(compile)/ 没有编译在进行(terminate)/ build 目录缺失
  500 启动失败或编译退出码非 0(带 returncode + log_tail 供诊断)
  503 引擎在用预编译包(本地编译服务不可用)/ build.sh 缺失
  504 编译超时未结束(进程保留,可轮询面板或调 terminate)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time

from aiohttp import web

from core.base.logger import get_logger, PLUGIN

from .. import audit, boot, prebuilt
from .. import state as _plugin_state
from . import page_build

log = get_logger(PLUGIN, 'LGTBot')

TOKEN_PATH = os.path.join(page_build.BUILD_DATA_DIR, 'api_token')

# 同步等待编译结束的上限。单目标增量编译通常秒级~几分钟(桥接层含 Boost.Python 模板膨胀,慢机器可能到 10 分钟);
# 超过视为异常,响应 504 但 **不杀进程** —— 编译照常跑完,调用方可去面板看结果或调 terminate。
_WAIT_TIMEOUT = 600.0
_POLL_INTERVAL = 1.0

_ROUTE_COMPILE = '/api/ext/lgtbot/build/compile'
_ROUTE_TERMINATE = '/api/ext/lgtbot/build/terminate'

# 日志尾巴的 ANSI / 控制字符清洗(API 消费方是程序,不需要颜色)
_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b[=>()]?[0-9A-Za-z]')


# ─────────────────────────────────────────────────────────────────────────
# token
# ─────────────────────────────────────────────────────────────────────────

def get_or_create_api_token() -> str:
    """读 ``data/build/api_token``;缺失 / 空 / 过短则随机生成并落盘。

    64 位 hex(256 bit 熵)。写入时尽力 chmod 600(Windows 下 no-op)。
    生成是惰性的:第一次被读(面板复制按钮 / 第一个 API 请求)才创建。
    """
    try:
        with open(TOKEN_PATH, 'r', encoding='utf-8') as f:
            token = f.read().strip()
        if len(token) >= 32:
            return token
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning(f'[build-api] 读 token 失败,将重新生成: {e}')

    token = secrets.token_hex(32)
    try:
        os.makedirs(page_build.BUILD_DATA_DIR, exist_ok=True)
        with open(TOKEN_PATH, 'w', encoding='utf-8') as f:
            f.write(token + '\n')
        try:
            os.chmod(TOKEN_PATH, 0o600)
        except OSError:
            pass
        log.info(f'[build-api] 已生成新的 API token: {TOKEN_PATH}')
    except OSError as e:
        log.warning(f'[build-api] 写 token 失败(本次 token 仅内存有效): {e}')
    return token


def _check_auth(request) -> bool:
    """常数时间比较请求携带的 token。只认 header,不认 query(避免进访问日志)。"""
    header = request.headers.get('Authorization') or ''
    provided = header[7:].strip() if header.lower().startswith('bearer ') else ''
    if not provided:
        provided = (request.headers.get('X-API-Token') or '').strip()
    if not provided:
        return False
    return secrets.compare_digest(provided, get_or_create_api_token())


# ─────────────────────────────────────────────────────────────────────────
# 公共检查
# ─────────────────────────────────────────────────────────────────────────

def _err(status: int, error: str, **extra) -> 'web.Response':
    return web.json_response({'success': False, 'error': error, **extra}, status=status)


def _service_unavailable_reason() -> str | None:
    """编译服务不可用的原因;可用返回 None。

    预编译模式看两个维度:``running``(本进程实际加载的)和 ``selected``(marker 最新选择,可能待重启生效)。
    任一是 prebuilt 都拒绝 —— 前者编出的 .so 当前进程根本不加载,后者重启后不加载,对调用方都是无效编译。
    """
    mode = prebuilt.mode_info()
    if mode.get('running') == 'prebuilt' or mode.get('selected') == 'prebuilt':
        return '引擎当前使用预编译包，本地编译服务不可用'
    if not os.path.isfile(os.path.join(boot.PLUGIN_DIR, 'build.sh')):
        return 'build.sh 不存在，编译服务不可用'
    return None


def _plain_log_tail(max_bytes: int = 8192, max_lines: int = 40) -> str:
    """build.log 末尾,剥 ANSI / 控制字符,截最后 max_lines 行(诊断用)。"""
    text = page_build._read_log_tail(max_bytes)
    text = _ANSI_RE.sub('', text)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    return '\n'.join(text.split('\n')[-max_lines:]).strip()


def _active_match_count() -> int:
    try:
        return len(_plugin_state.active_matches)
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────
# handlers
# ─────────────────────────────────────────────────────────────────────────

async def compile_handler(request: 'web.Request') -> 'web.Response':
    """``POST /api/ext/lgtbot/build/compile``  body: ``{"target": "<name>"}``。

    同步语义:等编译子进程退出后才响应,成功带用时与进行中对局数
    (调用方据此判断能否安全触发重启 —— 有对局在跑时引擎拒绝释放,新 .so 不会被加载)。
    """
    if not _check_auth(request):
        return _err(401, 'token 缺失或错误')

    reason = _service_unavailable_reason()
    if reason:
        return _err(503, reason)

    try:
        body = await request.json()
    except Exception:
        body = None
    target = str((body or {}).get('target') or '').strip()
    if not target:
        return _err(400, '缺少 target 参数(桥接层请传 LGTBot_ElainaBot)')
    if not page_build._validate_target_name(target):
        return _err(400, f'target 名称非法: {target!r}(仅字母/数字/下划线/连字符,'
                         f'1-63 字符,首字符为字母或下划线)')

    if not os.path.isdir(os.path.join(boot.PLUGIN_DIR, 'build')):
        return _err(409, 'build/ 目录不存在，请先在面板完成一次完整编译')
    if page_build.get_build_state()['running']:
        return _err(409, '已有编译在进行中，请稍后重试或调用 terminate 中断')

    started = page_build._start_build(
        ['bash', 'build.sh', '-i', '-t', target],
        f'增量编译目标 {target}', src=audit.SRC_API)
    if not started.get('success'):
        return _err(500, started.get('message') or '编译启动失败')

    # 轮询等子进程退出。get_build_state 在探测到 PID 已死时会 finalize
    # state(补 returncode / elapsed_sec),这里直接消费它的结果。
    deadline = time.monotonic() + _WAIT_TIMEOUT
    while time.monotonic() < deadline:
        st = page_build.get_build_state()
        if not st['running']:
            break
        await asyncio.sleep(_POLL_INTERVAL)
    else:
        return _err(504, f'编译超过 {_WAIT_TIMEOUT:.0f}s 未结束，进程仍在运行'
                         f'（可在面板查看日志，或调用 terminate 中断）',
                    target=target)

    rc = st.get('returncode')
    elapsed = st.get('elapsed_sec')
    if rc == 0:
        return web.json_response({
            'success': True,
            'target': target,
            'message': '编译成功',
            'elapsed_sec': elapsed,
            'active_matches': _active_match_count(),
        })
    detail = f'编译失败 (退出码 {rc})' if rc is not None else '编译进程被终止（无退出码）'
    return _err(500, detail, target=target, returncode=rc,
                elapsed_sec=elapsed, log_tail=_plain_log_tail())


async def terminate_handler(request: 'web.Request') -> 'web.Response':
    """``POST /api/ext/lgtbot/build/terminate`` —— 强制中断当前编译(不限发起方)。"""
    if not _check_auth(request):
        return _err(401, 'token 缺失或错误')
    if not page_build.get_build_state()['running']:
        return _err(409, '当前没有编译在进行')
    result = page_build._kill_build(src=audit.SRC_API)
    if not result.get('success'):
        return _err(500, result.get('message') or '终止失败')
    return web.json_response({'success': True, 'message': result.get('message', '已终止')})


# ─────────────────────────────────────────────────────────────────────────
# 面板集成:token 复制按钮的数据端点(隐藏 action,走面板登录态)
# ─────────────────────────────────────────────────────────────────────────

def render_api_token() -> str:
    """fragment: token + 端点说明,供「🔑 复制 API Token」按钮取值。"""
    import html as _html
    payload = {
        'success': True,
        'token': get_or_create_api_token(),
        'path': os.path.abspath(TOKEN_PATH),
        'endpoints': {'compile': _ROUTE_COMPILE, 'terminate': _ROUTE_TERMINATE},
    }
    body = json.dumps(payload, ensure_ascii=False)
    return f'<pre id="result">{_html.escape(body)}</pre>'
