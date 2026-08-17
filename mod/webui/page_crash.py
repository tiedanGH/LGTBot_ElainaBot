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

另有一块**游戏子进程 core 文件**(见 ``_CORE_DIRS`` 段注释):游戏代码崩溃只打死
``match_game_runner`` 子进程,主进程无感也不产生 crash_*.log,只在编译产物目录留下
内核 core。``_list_cores`` 列出它们并用 ``mod/corefile`` 解析出信号 / 游戏名 /
崩溃模块,``core_download_handler`` / ``core_delete_handler`` 提供下载与批量删除。
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import time

from aiohttp import web

from core.base.logger import get_logger, PLUGIN
from .. import audit, boot, corefile, metrics

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


# ──────── 游戏子进程 core 文件 ────────────────────────────────────────────
# 游戏崩溃只打死 fork 出来的 match_game_runner 子进程,主进程无感、也不会留 crash_*.log(那是桥接层信号处理器写的,只覆盖主进程)。
# 内核按 core_pattern 把 core 落在子进程 cwd —— boot._make_runner_wrapper 的 wrapper 会先 `cd "$BUILD_DIR"`,所以 core 就在编译产物目录里。
#
# **两种部署的目录不同**,而且切换模式后旧 core 还留在另一边,所以两个都扫:
#   · 本地编译  <plugin>/build/
#   · 预编译包  <plugin>/build_prebuilt/build/   ← 包内保留了 build/ 前缀
# 列表用「目录下标 + 文件名」定位(同名 core 可能两边都有),下标越界即拒。
_CORE_DIRS = []
for _d in (getattr(boot, 'LOCAL_BUILD_DIR', ''),
           os.path.join(getattr(boot, 'PREBUILT_DIR', ''), 'build'),
           boot.BUILD_DIR):
    if _d and _d not in _CORE_DIRS:
        _CORE_DIRS.append(_d)
CORE_DIRS = tuple(_CORE_DIRS)
# 内核默认 `core`、常见 `core.<pid>`,以及本项目实测的 `core-%e-%p-%t`
# (如 core-match_game_runn-42418-1786852068)。收紧到 core 开头 + 无路径分隔符。
_CORE_RE = re.compile(r'^core(?:[.-][A-Za-z0-9._-]+)?$')
# core-<exe>-<pid>-<秒>:从名字里取 pid / 时间(比 mtime 更贴近崩溃时刻)
_CORE_NAME_RE = re.compile(r'^core-(?P<exe>[^-]+)-(?P<pid>\d+)-(?P<ts>\d+)$')


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


def _safe_core_path(name: str, dir_idx) -> str | None:
    """校验 core 文件定位参数,返回绝对路径;非法 / 不存在返回 None。

    ``dir_idx`` 是 ``CORE_DIRS`` 的下标 —— 用下标而不是让前端传目录,调用方
    永远碰不到任意路径;``name`` 再过纯 basename + core 命名白名单。
    """
    try:
        idx = int(dir_idx)
    except (TypeError, ValueError):
        return None
    if not 0 <= idx < len(CORE_DIRS):
        return None
    if not name or os.path.basename(name) != name or not _CORE_RE.match(name):
        return None
    path = os.path.join(CORE_DIRS[idx], name)
    return path if os.path.isfile(path) else None


def _list_cores(analyze: bool = True) -> list:
    """列出各 build 目录下的 core 文件(按时间倒序),可选带 ELF 解析结果。

    ``analyze=False`` 用于只要总数 / 体积的场合,省掉逐个读 note 段。
    """
    out = []
    for idx, d in enumerate(CORE_DIRS):
        try:
            names = os.listdir(d)
        except OSError:
            continue                          # 目录不存在(未编译 / 未装预编译包)
        for name in names:
            if not _CORE_RE.match(name):
                continue
            path = os.path.join(d, name)
            try:
                stt = os.stat(path)
            except OSError:
                continue
            if not os.path.isfile(path):
                continue
            m = _CORE_NAME_RE.match(name)
            item = {
                'name': name,
                'dir_idx': idx,
                'dir': d,
                'size': stt.st_size,
                'mtime': int(stt.st_mtime),
                # 文件名里的秒数比 mtime 更贴近崩溃瞬间;没有就退回 mtime
                'crash_ts': int(m.group('ts')) if m else int(stt.st_mtime),
                'pid': int(m.group('pid')) if m else None,
            }
            if analyze:
                item['analysis'] = corefile.analyze(path)
            out.append(item)
    out.sort(key=lambda c: c['crash_ts'], reverse=True)
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
    cores = _list_cores()
    return {
        'dumps': dumps,
        'count': len(dumps),
        'total_bytes': sum(d['size'] for d in dumps),
        'crash_dir': CRASH_DIR,
        # 游戏子进程 core:数量 / 总占用进顶部统计,列表进「转储列表」下方新栏目
        'cores': cores,
        'core_count': len(cores),
        'core_bytes': sum(c['size'] for c in cores),
        'core_dirs': list(CORE_DIRS),
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


async def core_download_handler(request: 'web.Request') -> 'web.Response':
    """``GET /api/ext/lgtbot/crash/core-download?name=<core...>&d=<目录下标>``。

    core 文件动辄几十上百 MB,用 ``FileResponse`` 流式回,不读进内存。
    """
    name = (request.query.get('name') or '').strip()
    path = _safe_core_path(name, request.query.get('d'))
    if not path:
        return web.json_response({'success': False, 'message': '无效的 core 文件'}, status=400)
    # name 已过 core 命名白名单(仅字母数字 . _ -),可安全放进 header
    return web.FileResponse(path, headers={
        'Content-Disposition': f'attachment; filename="{name}"',
    })


async def core_delete_handler(request: 'web.Request') -> 'web.Response':
    """``GET /api/ext/lgtbot/crash/core-delete?name=a&d=0&name=b&d=1`` —— 批量删除。

    ``name`` 与 ``d`` **按下标一一配对**(同名 core 在本地 / 预编译两个目录里都可能
    存在,只给名字无法定位)。每对都过 ``_safe_core_path`` 白名单;删除结果计入审计。
    """
    names = [n.strip() for n in request.query.getall('name', []) if n.strip()]
    dirs = request.query.getall('d', [])
    if not names:
        return web.json_response({'success': False, 'message': '未指定要删除的 core'}, status=400)
    if len(dirs) != len(names):
        return web.json_response(
            {'success': False, 'message': 'name 与 d 数量不匹配'}, status=400)
    deleted, failed, freed = [], [], 0
    for name, d in zip(names, dirs):
        path = _safe_core_path(name, d)
        if not path:
            failed.append(name)
            continue
        try:
            size = os.path.getsize(path)
            os.remove(path)
            deleted.append(name)
            freed += size
        except OSError as e:
            log.warning(f'[crash] 删除 core 失败 {name}: {e}')
            failed.append(name)
    ok = bool(deleted) and not failed
    audit.record('cache', '删除游戏 core',
                 f'删除 {len(deleted)} 个、释放 {freed} 字节'
                 + (f',失败 {len(failed)} 个' if failed else ''),
                 ok=ok, src=audit.SRC_PANEL)
    msg = (f'已删除 {len(deleted)} 个 core 文件'
           + (f',{len(failed)} 个失败(不存在 / 名称非法)' if failed else ''))
    return web.json_response({'success': ok, 'deleted': deleted, 'failed': failed,
                              'freed_bytes': freed, 'message': msg})
