#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「💥 崩溃转储」标签 —— 只读列出 / 查看 / 下载 ``LGTBot_CRASH_DUMPS/`` 里的崩溃栈 dump。

引擎(C++ 桥接层 SigSegvHandler → DumpCrashToFile)在 SIGSEGV/SIGBUS/SIGABRT
时把 async-signal-safe 的崩溃信息(信号 / si_addr / pid / tid + backtrace_symbols_fd 栈)
落盘到 ``<plugin_dir>/LGTBot_CRASH_DUMPS/crash_<sec>_<pid>_<tid>.log``。
本标签给管理员一个面板入口:列表(按时间倒序)、查看全文 backtrace、下载单个 dump。

★ 安全准则 ★
  · 列表(``render_list`` fragment)+ 查看 / 下载 / 删除(三个 register_route)。
    删除需前端二次确认;dump 是排障证据,删前建议先下载留存。
  · 文件名一律经 ``_safe_dump_path`` 白名单校验(纯 basename + ``crash_*.log`` 正则
    + 必须落在 dump 目录内),杜绝路径穿越 —— 查看 / 下载 / 删除共用同一道校验。

Python 侧职责:
  · ``TAB_HTML`` / ``TAB_CSS`` / ``TAB_JS`` 从 ``templates/crash/`` 加载
  · ``get_data()`` 首屏 payload(dump 列表元数据 + 触发源 + 崩溃重启概况,不含正文)
  · ``render_list`` —— 无参刷新 endpoint(``<pre id="result">JSON</pre>`` 协议)
  · ``view_handler`` —— 带 ``?name=`` 的真路由,返回 dump 正文 + 解析好的元信息 meta
    (信号 / si_addr / pid / tid / 触发源 uid·gid / 原始消息),供大弹窗展示
  · ``download_handler`` —— 带 ``?name=`` 的真路由(附件下载)
  · ``delete_handler`` —— 带多个 ``?name=`` 的真路由(批量删除,写操作审计)
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import time

from aiohttp import web

from core.base.logger import get_logger, PLUGIN
from .. import audit, boot, metrics

log = get_logger(PLUGIN, 'LGTBot')

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')


def _load(name: str) -> str:
    with open(os.path.join(_TEMPLATE_DIR, name), 'r', encoding='utf-8') as f:
        return f.read()


TAB_HTML = _load('crash/crash.html')
TAB_CSS = _load('crash/crash.css')
TAB_JS = _load('crash/crash.js')

# dump 目录 + 文件命名(与桥接层 DumpCrashToFile 的 crash_<sec>_<pid>_<tid>.log 一致)。
# 桥接层 DeriveCrashDumpDir 已把 dump 目录**固定为插件根** LGTBot_CRASH_DUMPS(本地 / 预编译统一),故这里只读这一个规范目录。
CRASH_DIR = os.path.join(boot.PLUGIN_DIR, 'LGTBot_CRASH_DUMPS')
_DUMP_RE = re.compile(r'^crash_\d+_\d+_\d+\.log$')
_MAX_VIEW_BYTES = 256 * 1024        # 查看 / 下载读取上限,防异常超大文件撑爆内存
# 信号号 → 名称(与 callbacks._SIG_NAMES 同源:引擎只可能落这几种)
_SIG_NAMES = {4: 'SIGILL', 6: 'SIGABRT', 7: 'SIGBUS', 8: 'SIGFPE', 11: 'SIGSEGV'}


def _fragment(payload: dict) -> str:
    """把 ``payload`` 包成 ``<pre id="result">…</pre>`` —— 同 page_audit / page_backup。"""
    body = json.dumps(payload, ensure_ascii=False, default=str)
    return f'<pre id="result">{_html.escape(body)}</pre>'


def _safe_dump_path(name: str) -> str | None:
    """校验 ``name`` 为纯文件名且匹配 crash dump 命名,返回规范目录下的绝对路径;非法(路径穿越 / 命名不符)或不存在返回 None。"""
    if not name or os.path.basename(name) != name or not _DUMP_RE.match(name):
        return None
    path = os.path.join(CRASH_DIR, name)
    return path if os.path.isfile(path) else None


def _sig_name(sig: 'int | None') -> str:
    return _SIG_NAMES.get(sig, (f'sig{sig}' if sig is not None else '未知'))


def _parse_header(text: str) -> dict:
    """从 dump 头部解析元信息(signal / si_addr / pid / tid + 触发源 uid/gid/is_uid/msg)。

    触发源(``is_uid`` / ``uid`` / ``gid``)只在 ``\\nmsg: `` 之前的区域里找 —— msg 是
    用户原始消息,可能多行、可能含 ``gid:`` 之类字样,不能让它污染字段解析。msg 本身
    从 ``\\nmsg: `` 取到 backtrace 分隔线为止。桥接层 DumpCrashToFile 落盘顺序固定:
    signal → [si_addr/si_code] → pid → tid → is_uid → uid → gid → msg。"""
    msg_idx = text.find('\nmsg: ')
    head = text[:msg_idx] if msg_idx >= 0 else text[:4096]
    meta = {}
    for key in ('signal', 'si_code', 'pid', 'tid', 'is_uid'):
        m = re.search(r'^' + key + r':\s*(\d+)', head, re.M)
        meta[key] = int(m.group(1)) if m else None
    m = re.search(r'^si_addr:\s*(\S+)', head, re.M)
    meta['si_addr'] = m.group(1) if m else None
    for key in ('uid', 'gid'):
        m = re.search(r'^' + key + r':\s*(.*)$', head, re.M)
        meta[key] = m.group(1).strip() if m else ''
    if msg_idx >= 0:
        rest = text[msg_idx + len('\nmsg: '):]
        end = rest.find('\n\n--- backtrace ---')
        meta['msg'] = rest[:end] if end >= 0 else rest
    else:
        meta['msg'] = ''
    meta['signal_name'] = _sig_name(meta['signal'])
    return meta


def _list_dumps() -> list:
    """列出 dump 元数据(名 / 大小 / mtime / 信号 + 触发源),按 mtime 倒序(最新在前)。"""
    out = []
    try:
        names = os.listdir(CRASH_DIR)
    except OSError:
        return out                      # 目录还不存在 = 从未崩溃过
    for name in names:
        if not _DUMP_RE.match(name):
            continue
        path = os.path.join(CRASH_DIR, name)
        try:
            stt = os.stat(path)
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                head = f.read(4096)     # 头部含全部触发源字段,4KB 足够(uid/gid ≤128B)
        except OSError:
            continue
        meta = _parse_header(head)
        out.append({
            'name': name,
            'size': stt.st_size,
            'mtime': int(stt.st_mtime),
            'signal': meta['signal'],
            'signal_name': meta['signal_name'],
            'is_uid': meta['is_uid'],
            'uid': meta['uid'],
            'gid': meta['gid'],
        })
    out.sort(key=lambda d: d['mtime'], reverse=True)
    return out


def _restart_stats() -> dict:
    """引擎崩溃重启概况 —— 与「指标面板 · 运行指标」同源(mod/metrics 持久计数器),同格式展示(累计次数 / 分信号 / 最近一次)。"""
    snap = metrics.snapshot()
    return {
        'crash_total': int(snap.get('crash_total') or 0),
        'crash_by_sig': snap.get('crash_by_sig') or {},
        'last_crash_ts': int(snap.get('last_crash_ts') or 0),
        'last_crash_sig': snap.get('last_crash_sig') or '',
    }


def _payload() -> dict:
    dumps = _list_dumps()
    return {
        'dumps': dumps,
        'count': len(dumps),
        'total_bytes': sum(d['size'] for d in dumps),
        'crash_dir': CRASH_DIR,
        'restart': _restart_stats(),
        'query_time': int(time.time()),
    }


def get_data() -> str:
    """返回可嵌入 ``<script id="crash-data">`` 的 JSON(仅元数据,正文按需拉)。"""
    data_json = json.dumps(_payload(), ensure_ascii=False, default=str)
    return data_json.replace('</script>', '<\\/script>')


def render_list() -> str:
    """最新 dump 列表(「🔄 刷新」按钮用)。"""
    return _fragment({'success': True, **_payload()})


async def view_handler(request: 'web.Request') -> 'web.Response':
    """``GET /api/ext/lgtbot/crash/view?name=<crash_*.log>`` —— 返回 dump 正文 JSON。"""
    name = (request.query.get('name') or '').strip()
    path = _safe_dump_path(name)
    if not path:
        return web.json_response({'success': False, 'message': '无效的 dump 文件名'}, status=400)
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read(_MAX_VIEW_BYTES + 1)
    except OSError as e:
        return web.json_response({'success': False, 'message': f'读取失败: {e}'}, status=500)
    truncated = len(content) > _MAX_VIEW_BYTES
    content = content[:_MAX_VIEW_BYTES]
    try:
        stt = os.stat(path)
        size, mtime = stt.st_size, int(stt.st_mtime)
    except OSError:
        size, mtime = None, None
    meta = _parse_header(content)       # 大弹窗展示的「文件信息」(信号 / 触发源 / 消息…)
    meta['name'] = name
    meta['size'] = size
    meta['mtime'] = mtime
    return web.json_response({
        'success': True, 'name': name, 'meta': meta,
        'content': content, 'truncated': truncated,
    })


async def download_handler(request: 'web.Request') -> 'web.Response':
    """``GET /api/ext/lgtbot/crash/download?name=<crash_*.log>`` —— 附件下载单个 dump。"""
    name = (request.query.get('name') or '').strip()
    path = _safe_dump_path(name)
    if not path:
        return web.json_response({'success': False, 'message': '无效的 dump 文件名'}, status=400)
    # name 已过 crash_<数字>_<数字>_<数字>.log 正则,无引号 / 特殊字符,可安全放进 header
    return web.FileResponse(path, headers={
        'Content-Disposition': f'attachment; filename="{name}"',
    })


async def delete_handler(request: 'web.Request') -> 'web.Response':
    """``GET /api/ext/lgtbot/crash/delete?name=a&name=b`` —— 删除选中的 dump(可多选)。

    每个 name 都过 ``_safe_dump_path`` 白名单校验(拒绝路径穿越 / 非 dump 命名 / 不存在);
    前端已做二次确认。删除结果计入操作审计。"""
    names = [n.strip() for n in request.query.getall('name', []) if n.strip()]
    if not names:
        return web.json_response({'success': False, 'message': '未指定要删除的 dump'}, status=400)
    deleted, failed = [], []
    for name in names:
        path = _safe_dump_path(name)
        if not path:
            failed.append(name)
            continue
        try:
            os.remove(path)
            deleted.append(name)
        except OSError as e:
            log.warning(f'[crash] 删除 dump 失败 {name}: {e}')
            failed.append(name)
    ok = bool(deleted) and not failed
    audit.record('cache', '删除崩溃转储',
                 f'删除 {len(deleted)} 个' + (f',失败 {len(failed)} 个' if failed else ''),
                 ok=ok, src=audit.SRC_PANEL)
    msg = f'已删除 {len(deleted)} 个转储' + (f',{len(failed)} 个失败(不存在 / 名称非法)' if failed else '')
    return web.json_response({'success': ok, 'deleted': deleted, 'failed': failed, 'message': msg})
