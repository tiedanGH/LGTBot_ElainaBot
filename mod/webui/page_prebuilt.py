#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「📦 预编译部署」标签 —— 镜像测速/选择 / 预编译包下载 / 本地·预编译切换。

数据 / 动作分工:
  · ``get_data()``          首屏本地快数据:构建来源 mode + 下载进度(不打网络)。
  · ``render_list``         远程预编译包列表(同步 urllib,同 page_dashboard 检查更新)。
  · ``render_state``        下载进度片段(前端轮询)。
  · ``render_switch_*``     切换本地 / 预编译 marker(重启生效)。
  · ``test_mirrors_handler````POST /api/ext/lgtbot/prebuilt/test-mirrors`` {customs}
                            —— 内置 + 自定义镜像并发测速(ms)。
  · ``mirror_select_handler``POST .../prebuilt/mirror {mirror} —— 记住下载用镜像。
  · ``download_handler``    POST .../prebuilt/download {name, mirror?} —— 后台起下载,
                            进度走 render_state 轮询。
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import os
import time

from aiohttp import web

from core.base.logger import get_logger, PLUGIN
from .. import audit, prebuilt

log = get_logger(PLUGIN, 'LGTBot')

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')

# 持有后台下载 task 的强引用 —— asyncio 只对运行中的 task 持弱引用,不保存的话
# task 可能在下载途中被 GC 掉,下载静默中断、state 停在 running。done 回调里移除。
_bg_tasks: set = set()
# 当前正在跑的下载 task(供「取消下载」定位并 task.cancel());done 时清空。
_download_task = None


def _load(name: str) -> str:
    with open(os.path.join(_TEMPLATE_DIR, name), 'r', encoding='utf-8') as f:
        return f.read()


TAB_HTML = _load('prebuilt/prebuilt.html')
TAB_CSS = _load('prebuilt/prebuilt.css')
TAB_JS = _load('prebuilt/prebuilt.js')


def _fragment(payload: dict) -> str:
    body = json.dumps(payload, ensure_ascii=False, default=str)
    return f'<pre id="result">{_html.escape(body)}</pre>'


def get_data() -> str:
    """首屏本地快数据(不打网络):构建来源模式 + 下载进度 + 已选镜像。"""
    payload = {
        'mode': prebuilt.mode_info(),
        'state': prebuilt.read_state(),
        'selected_mirror': prebuilt.get_selected_mirror(),
    }
    return json.dumps(payload, ensure_ascii=False, default=str).replace('</script>', '<\\/script>')


# ──────── 无参 fragment 端点 ───────────────────────────────────────────────

def render_list() -> str:
    """远程预编译包列表(同步 urllib,经排序镜像)。"""
    try:
        data = prebuilt.list_remote()
    except Exception as e:
        log.error(f'[prebuilt] 列表异常: {e}')
        return _fragment({'success': False, 'message': f'获取列表异常: {e}'})
    return _fragment(data)


def render_state() -> str:
    return _fragment({'success': True, 'state': prebuilt.read_state()})


def render_cancel() -> str:
    """取消当前下载 —— 停止后台 task 并删除已下载的未完成文件,解除「下载中」死锁。

    有活动 task:对其 ``task.cancel()``,终态 + 临时文件由 download() 的 CancelledError /
    finally 收尾(不在这里抢写 state,避免与仍在退出的 task 竞态)。
    无活动 task(卡死残留 / 进程重启后 state 仍 running):直接强制清理。
    """
    task = _download_task
    if task is not None and not task.done():
        task.cancel()
        cancelled, msg = True, '正在取消下载,已删除未完成的文件'
    else:
        prebuilt.cancel_cleanup()
        cancelled, msg = False, '已清理残留下载状态'
    audit.record('build', '取消预编译下载', '' if cancelled else '(无活动下载,强制清理)',
                 ok=True, src=audit.SRC_PANEL)
    return _fragment({'success': True, 'cancelled': cancelled, 'message': msg})


def render_switch_local() -> str:
    r = prebuilt.set_mode(False)
    r['mode_info'] = prebuilt.mode_info()
    # 切换构建来源计入审计,归「引擎编译」类(build/ ↔ build_prebuilt/ 影响引擎加载)
    audit.record('build', '切换构建来源 → 本地编译',
                 '' if r.get('success') else r.get('message', ''),
                 ok=bool(r.get('success')), src=audit.SRC_PANEL)
    return _fragment(r)


def render_switch_prebuilt() -> str:
    r = prebuilt.set_mode(True)
    r['mode_info'] = prebuilt.mode_info()
    audit.record('build', '切换构建来源 → 预编译包',
                 '' if r.get('success') else r.get('message', ''),
                 ok=bool(r.get('success')), src=audit.SRC_PANEL)
    return _fragment(r)


# ──────── 带参 / 长任务 HTTP route handlers ────────────────────────────────

async def download_handler(request: 'web.Request') -> 'web.Response':
    """``POST .../prebuilt/download`` body ``{name, mirror?}`` —— 后台起下载。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    name = (body.get('name') or '').strip()
    mirror = body.get('mirror')     # None = 用已记住的 / 自动
    if not name:
        return web.json_response({'success': False, 'message': '缺少包名 name'}, status=400)
    st = prebuilt.read_state()
    if st.get('running'):
        return web.json_response({'success': False, 'message': '已有下载正在进行'}, status=409)

    async def _run():
        try:
            await prebuilt.download(name, preferred_mirror=mirror)
        except Exception as e:
            log.error(f'[prebuilt] 后台下载 {name} 异常: {e}')

    global _download_task
    task = asyncio.create_task(_run())
    _download_task = task
    _bg_tasks.add(task)

    def _on_done(t):
        global _download_task
        _bg_tasks.discard(t)
        if _download_task is t:
            _download_task = None

    task.add_done_callback(_on_done)
    return web.json_response({'success': True, 'started': True, 'message': '已开始下载'})


async def upload_handler(request: 'web.Request') -> 'web.Response':
    """``POST .../prebuilt/upload``(multipart,字段 ``file``)—— 手动上传预编译包 zip。

    边收边写进度 state(前端可轮询显示上传进度),收完后走与下载完全相同的校验 + 原子换入(``prebuilt.install_uploaded``)。
    整个请求期间 state.running=True,据此拒绝并发的下载 / 上传。
    """
    st = prebuilt.read_state()
    if st.get('running'):
        return web.json_response({'success': False, 'message': '已有下载 / 安装正在进行'}, status=409)
    try:
        total = int(request.headers.get('Content-Length') or 0)
    except (TypeError, ValueError):
        total = 0

    os.makedirs(prebuilt._PREBUILT_DATA, exist_ok=True)
    prebuilt._write_state(running=True, stage='upload', asset='(本地上传)',
                          progress=0, downloaded=0, total=total, error='')
    try:
        reader = await request.multipart()
        field = await reader.next()
        while field is not None and field.name != 'file':
            field = await reader.next()
        if field is None:
            prebuilt._write_state(running=False, stage='error', asset='(本地上传)', error='缺少文件字段 file')
            return web.json_response({'success': False, 'message': '缺少上传文件'}, status=400)
        received = 0
        last = 0.0
        with open(prebuilt._DOWNLOAD_TMP, 'wb') as f:
            while True:
                chunk = await field.read_chunk(65536)
                if not chunk:
                    break
                f.write(chunk)
                received += len(chunk)
                now = time.monotonic()
                if now - last >= 0.4:      # 限流写盘
                    last = now
                    pct = int(received / total * 100) if total else 0
                    prebuilt._write_state(running=True, stage='upload', asset='(本地上传)',
                                          progress=min(pct, 99), downloaded=received, total=total, error='')
    except Exception as e:
        log.error(f'[prebuilt] 接收上传失败: {e}')
        prebuilt._write_state(running=False, stage='error', asset='(本地上传)', error=str(e))
        return web.json_response({'success': False, 'message': f'上传失败: {e}'})

    # 收完 → 校验并安装(executor,避免阻塞事件循环)
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, prebuilt.install_uploaded, prebuilt._DOWNLOAD_TMP)
    return web.json_response(result)


async def test_mirrors_handler(request: 'web.Request') -> 'web.Response':
    """``POST .../prebuilt/test-mirrors`` body ``{customs?:[...]}`` —— 内置 + 自定义并发测速。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    customs = body.get('customs') if isinstance(body, dict) else None
    if not isinstance(customs, list):
        customs = None
    try:
        results = await prebuilt.test_mirrors(customs=customs)
    except Exception as e:
        return web.json_response({'success': False, 'message': f'测速失败: {e}'})
    return web.json_response({'success': True, 'mirrors': results,
                              'selected': prebuilt.get_selected_mirror()})


async def mirror_select_handler(request: 'web.Request') -> 'web.Response':
    """``POST .../prebuilt/mirror`` body ``{mirror}`` —— 记住下载用镜像(空串=直连)。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    mirror = (body.get('mirror') or '') if isinstance(body, dict) else ''
    prebuilt.set_selected_mirror(mirror)
    return web.json_response({'success': True, 'selected': prebuilt.get_selected_mirror()})
