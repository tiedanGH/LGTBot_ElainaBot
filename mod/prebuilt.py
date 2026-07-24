#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""预编译包下载 / 安装 / 切换 —— 让用户无需本地 Boost.Python + C++20 工具链。

CI(`.github/workflows/prebuilt.yml`)三发行版各编一份运行时子集,发布到同仓库
滚动 `prebuilt` 预发布 tag。本模块负责:

  · ``list_remote()``  —— 读 release assets(仅元数据,不下整包),按 asset 名
    ``lgtbot-<os>-py<X.Y>-<sha7>.zip`` 解析,标注与本机匹配 / 是否最新 / 已安装。
  · ``download()`` / ``install_uploaded()`` —— 临时文件(下载分块 / 上传流)→ 校验
    (zip 签名 + manifest sha256)→ 解压到 staging → 原子换入 ``build_prebuilt/``。
    全程不碰正在运行的 ``build/`` / 旧 ``build_prebuilt/``,失败不损坏现有包。
  · ``set_mode()`` / ``mode_info()`` —— ``data/prebuilt/active`` marker 切换 / 查询
    本地 / 预编译。真相以 marker 为准(boot 最先 import,读 marker 决定 BUILD_DIR);
    切换需重启。
  · 镜像测速 / 选择(``test_mirrors`` / ``get|set_selected_mirror``)。

注:**依赖自检**(runtime + compile deps)逻辑在 ``webui/page_dashboard.py`` —— 自检
UI 在仪表盘,就近内聚;本模块只提供 ``mode_info()`` 供其组装 mode 字段。

镜像:优先复用主框架 ``web.tools._updater``(镜像表 + 测速 + 排序缓存),import
失败时兜底一份内置精简镜像表,不硬依赖框架内部结构。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.request

from core.base.logger import get_logger, PLUGIN
from . import _prebuilt_swap, boot

log = get_logger(PLUGIN, 'LGTBot')


# ──────── 路径常量 ────────────────────────────────────────────────────────
PREBUILT_DIR   = boot.PREBUILT_DIR                              # build_prebuilt/(桥接 .so 所在)
LOCAL_BUILD_DIR = boot.LOCAL_BUILD_DIR                          # build/
# 预编译包解压后保留 build/ 前缀 → 真正的编译产物在 build_prebuilt/build/(见 boot.py)。
# 「是否已安装可用」以该子目录为准(仅有空的 build_prebuilt/ 不算,boot 会回落本地)。
_PREBUILT_BUILD_DIR = os.path.join(PREBUILT_DIR, 'build')
_STAGING_DIR   = os.path.join(boot.PLUGIN_DIR, 'build_prebuilt.staging')
_PREBUILT_DATA = os.path.join(boot.DATA_DIR, 'prebuilt')
ACTIVE_MARKER  = os.path.join(_PREBUILT_DATA, 'active')         # 'build' | 'build_prebuilt'
STATE_PATH     = os.path.join(_PREBUILT_DATA, 'state.json')     # 下载进度
_DOWNLOAD_TMP  = os.path.join(_PREBUILT_DATA, '.download.tmp')
INSTALLED_MANIFEST = os.path.join(PREBUILT_DIR, 'manifest.json')

PREBUILT_TAG = 'prebuilt'
_DOWNLOAD_TIMEOUT_S = 1800.0     # 预编译包大,给足超时
# sha 段一般是 7~40 位十六进制;但打包机若 git 不可用(如容器里 dubious ownership),
# pack_prebuilt.sh 会退化成字面量 ``unknown`` —— 也接受,否则该档会被静默丢弃、列表里看不到。
_ASSET_RE = re.compile(r'^lgtbot-(?P<os>.+)-py(?P<py>\d+\.\d+)-(?P<sha>[0-9a-f]{7,40}|unknown)\.zip$')

# import 失败时的兜底镜像前缀(空串 = GitHub 直连)。框架可用时以框架排序为准。
_FALLBACK_MIRRORS = ['', 'https://ghproxy.cc/', 'https://gh-proxy.com/',
                     'https://mirror.ghproxy.com/', 'https://gh.llkk.cc/']


# ──────── GitHub 仓库 / 平台标识 ───────────────────────────────────────────

def _github_owner_repo() -> tuple[str, str]:
    """从 main.py ``__plugin_meta__.github`` 解析 (owner, repo);失败用已知默认。"""
    url = ''
    try:
        m = sys.modules.get('plugins.LGTBot_ElainaBot')
        meta = getattr(m, '__plugin_meta__', None) if m else None
        if isinstance(meta, dict):
            url = meta.get('github', '') or ''
    except Exception:
        pass
    try:
        s = url.rstrip('/')
        if s.endswith('.git'):
            s = s[:-4]
        parts = s.split('/')
        if len(parts) >= 5 and 'github.com' in parts[2]:
            return parts[3], parts[4]
    except Exception:
        pass
    return 'tiedanGH', 'LGTBot_ElainaBot'


def local_python_tag() -> str:
    """本机 Python 小版本标签,如 ``3.11`` —— 与桥接 .so 的 Boost.Python ABI 对应。"""
    return f'{sys.version_info.major}.{sys.version_info.minor}'


def local_os_tag() -> str:
    """探测发行版标签(ubuntu-22.04 / ubuntu-24.04 / debian-12 …),匹配预编译档。

    读 ``/etc/os-release`` 的 ID + VERSION_ID;非 Linux / 读不到时返回 ``unknown``。
    """
    data: dict[str, str] = {}
    try:
        with open('/etc/os-release', 'r', encoding='utf-8') as f:
            for line in f:
                if '=' in line:
                    k, _, v = line.partition('=')
                    data[k.strip()] = v.strip().strip('"')
    except OSError:
        return 'unknown'
    osid = data.get('ID', '').lower()
    ver = data.get('VERSION_ID', '')
    if osid and ver:
        return f'{osid}-{ver}'
    return osid or 'unknown'


# ──────── 模式 marker(本地编译 / 预编译)────────────────────────────────

def current_mode() -> str:
    """实时读 marker 返回 ``'prebuilt'`` 或 ``'local'``(缺省 local)。

    注意与 ``boot.BUILD_DIR`` 的区别:后者是**本进程启动时**定格的;
    本函数读盘反映**最新**选择,用于 UI 展示「切换后待重启」。
    """
    try:
        with open(ACTIVE_MARKER, 'r', encoding='utf-8') as f:
            if f.read().strip() == 'build_prebuilt':
                return 'prebuilt'
    except OSError:
        pass
    return 'local'


def prebuilt_ready() -> bool:
    """预编译包是否已下载并可用 —— 以 ``build_prebuilt/build/`` 存在为准。

    与 boot._resolve_active_build() 的判据一致:仅有空的 ``build_prebuilt/`` 不算可用
    (boot 会因缺 build/ 子目录回落本地),避免 UI 报「已安装」但切过去仍是本地的错觉。
    """
    return os.path.isdir(_PREBUILT_BUILD_DIR)


def active_mode_running() -> str:
    """本进程**实际加载**的模式(以 boot 启动时定格的 ENGINE_ROOT 为准)。

    注意用 ENGINE_ROOT 而非 BUILD_DIR:预编译模式下 BUILD_DIR = build_prebuilt/build/
    (深一层),ENGINE_ROOT = build_prebuilt/ 才等于 PREBUILT_DIR。
    """
    return 'prebuilt' if boot.ENGINE_ROOT == PREBUILT_DIR else 'local'


def mode_info() -> dict:
    """构建来源状态(轻量,不做依赖扫描)—— 供预编译 tab 的当前运行徽章 / 切换用。"""
    return {
        'running': active_mode_running(),     # 本进程实际加载的
        'selected': current_mode(),           # marker 最新选择(可能待重启)
        'prebuilt_installed': prebuilt_ready(),
        # 有暂存待换入的新版本(安装时目录被占用降级)→ 前端提示重启后自动完成
        'pending_install': os.path.isdir(_prebuilt_swap.pending_dir(PREBUILT_DIR)),
    }


def set_mode(use_prebuilt: bool) -> dict:
    """写 marker 切换模式。切预编译前要求预编译包已下载可用(``build_prebuilt/build/`` 在)。

    只写盘、不重启 —— 引擎只在启动时按 BUILD_DIR 加载一次,调用方(UI)提示用户手动重启后生效。
    """
    if use_prebuilt and not prebuilt_ready():
        return {'success': False, 'message': '尚未下载预编译包,无法切换'}
    os.makedirs(_PREBUILT_DATA, exist_ok=True)
    value = 'build_prebuilt' if use_prebuilt else 'build'
    try:
        with open(ACTIVE_MARKER, 'w', encoding='utf-8') as f:
            f.write(value + '\n')
    except OSError as e:
        return {'success': False, 'message': f'写切换标记失败: {e}'}
    log.info(f'[prebuilt] 切换构建来源 → {value}(重启后生效)')
    return {'success': True, 'mode': 'prebuilt' if use_prebuilt else 'local',
            'message': '已切换,重启 LGTBot 后生效'}


def installed_manifest() -> dict | None:
    """读已安装预编译包的 ``build_prebuilt/manifest.json``;无 / 损坏返回 None。"""
    try:
        with open(INSTALLED_MANIFEST, 'r', encoding='utf-8') as f:
            m = json.load(f)
        return m if isinstance(m, dict) else None
    except (OSError, ValueError):
        return None


# ──────── 下载进度 state ───────────────────────────────────────────────────

def read_state() -> dict:
    try:
        with open(STATE_PATH, 'r', encoding='utf-8') as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(**kw) -> None:
    # 原子写:先写 .tmp 再 os.replace,避免前端轮询恰好读到半写文件 → json 解析失败
    # 退化成空 state,进而误判「已结束」而收尾。
    os.makedirs(_PREBUILT_DATA, exist_ok=True)
    tmp = STATE_PATH + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(kw, f, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def cancel_cleanup() -> None:
    """兜底清理:删掉下载临时文件 + 把 state 置为「已取消」(running=False)。

    两种场景都用它:① 用户取消时若已无活动下载 task(卡死残留 / 进程重启后 state 仍
    running);② 活动 task 被 task.cancel() 后,其自身的 CancelledError 分支已写终态,
    此处不重复调用。核心目的:让前端一定能从「下载中」死锁里解除。
    """
    try:
        os.remove(_DOWNLOAD_TMP)
    except OSError:
        pass
    _write_state(running=False, stage='cancelled', asset='', error='')


# ──────── 镜像(复用框架 web.tools._updater,失败兜底)──────────────────────

def _build_mirror_url(mirror: str, url: str) -> str:
    """把 github url 经镜像前缀改写;空 mirror = 直连。优先用框架实现。"""
    try:
        from web.tools._updater.shared import _build_mirror_url as fw  # type: ignore
        return fw(url, mirror)
    except Exception:
        return (mirror.rstrip('/') + '/' + url) if mirror else url


def _ranked_mirror_prefixes() -> list[str]:
    """返回按测速排序的镜像前缀(含直连)。框架有缓存则用,否则兜底表。"""
    try:
        from web.tools._updater.shared import _load_mirror_cache  # type: ignore
        cached = _load_mirror_cache() or []
        prefixes = [c.get('mirror', '') for c in cached if c.get('success')]
        if prefixes:
            # 保证直连也在候选内
            if '' not in prefixes:
                prefixes.append('')
            return prefixes
    except Exception:
        pass
    return list(_FALLBACK_MIRRORS)


_MIRROR_MARKER = os.path.join(_PREBUILT_DATA, 'mirror')       # 用户选定的下载镜像前缀
_MIRROR_TEST_TIMEOUT_S = 5.0


def get_selected_mirror() -> str | None:
    """读用户选定的镜像前缀。None = 未选(按排序自动);'' = 显式选 GitHub 直连。"""
    try:
        with open(_MIRROR_MARKER, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return None


def set_selected_mirror(mirror: str) -> None:
    os.makedirs(_PREBUILT_DATA, exist_ok=True)
    try:
        with open(_MIRROR_MARKER, 'w', encoding='utf-8') as f:
            f.write((mirror or '').strip() + '\n')
    except OSError:
        pass


async def test_mirrors(customs: list[str] | None = None) -> list[dict]:
    """并发测速:内置排序镜像 + 用户自定义镜像,HEAD 一个 github 文件计延迟(ms)。

    自成一体(不依赖框架的固定列表 API),这样自定义镜像能一起参与测速。
    返回 ``[{mirror, latency_ms, success}]``,成功在前、延迟升序。空 mirror = 直连。
    """
    import aiohttp
    owner, repo = _github_owner_repo()
    test_url = f'https://github.com/{owner}/{repo}/raw/HEAD/README.md'
    seen: list[str] = []
    for m in _ranked_mirror_prefixes() + list(customs or []):
        m = (m or '').strip()
        if m not in seen:
            seen.append(m)

    timeout = aiohttp.ClientTimeout(total=_MIRROR_TEST_TIMEOUT_S)

    async def _one(mirror: str) -> dict:
        url = _build_mirror_url(mirror, test_url)
        t0 = time.monotonic()
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.head(url, ssl=False, allow_redirects=False) as r:
                    ok = r.status < 400 or r.status == 405
        except Exception:
            return {'mirror': mirror, 'latency_ms': 0, 'success': False}
        return {'mirror': mirror, 'latency_ms': round((time.monotonic() - t0) * 1000), 'success': ok}

    results = await asyncio.gather(*[_one(m) for m in seen])
    results.sort(key=lambda r: (not r['success'], r['latency_ms']))
    return results


# ──────── 远程包列表 ───────────────────────────────────────────────────────

def _parse_asset(a: dict) -> dict | None:
    """把一个 release asset dict 解析成 {name, os, python_tag, sha, size, updated_at, url}。"""
    name = a.get('name', '')
    m = _ASSET_RE.match(name)
    if not m:
        return None
    return {
        'name': name,
        'os': m.group('os'),
        'python_tag': m.group('py'),
        'sha': m.group('sha'),
        'size': a.get('size', 0),
        'updated_at': a.get('updated_at', ''),
        'url': a.get('browser_download_url', ''),
    }


def _annotate(assets: list[dict]) -> list[dict]:
    """给每个 asset 标 os_match / py_match / matches_local / is_latest / installed。

    系统(发行版)与 Python 版本**分别**对比 —— 前端据此细分「系统不匹配 /
    Python 不匹配」标签,下载确认也区分完全 / 部分不匹配(系统权重更高)。"""
    my_os, my_py = local_os_tag(), local_python_tag()
    inst = installed_manifest() or {}
    inst_sha = inst.get('bridge_sha', '')
    # is_latest:同 os 分组内 updated_at 最新者
    latest_by_os: dict[str, str] = {}
    for a in assets:
        prev = latest_by_os.get(a['os'], '')
        if a['updated_at'] > prev:
            latest_by_os[a['os']] = a['updated_at']
    for a in assets:
        a['os_match'] = (a['os'] == my_os)
        a['py_match'] = (a['python_tag'] == my_py)
        a['matches_local'] = a['os_match'] and a['py_match']
        a['is_latest'] = bool(a['updated_at']) and a['updated_at'] == latest_by_os.get(a['os'])
        a['installed'] = bool(inst_sha) and a['sha'] == inst_sha
    return assets


def _api_get_json(path: str, timeout: float = 10.0):
    """GET api.github.com{path},按排序镜像逐个尝试,返回解析后的 JSON 或抛异常。"""
    owner, repo = _github_owner_repo()
    api_url = f'https://api.github.com/repos/{owner}/{repo}{path}'
    last_err: Exception | None = None
    for mirror in _ranked_mirror_prefixes():
        url = _build_mirror_url(mirror, api_url)
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'LGTBot-Prebuilt',
                'Accept': 'application/vnd.github+json',
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode('utf-8') or 'null')
        except Exception as e:
            last_err = e
            continue
    raise last_err or RuntimeError('no mirror available')


def list_remote() -> dict:
    """列出滚动 `prebuilt` release 的预编译包(仅用 asset 元数据,不下整包)。

    同步实现(内部 urllib + 本地读盘)—— 与 page_dashboard 的检查更新同款,
    供 WebUI 的同步 fragment 端点直接调用;短暂阻塞可接受。
    返回 ``{success, assets:[...], local:{os,python}, installed:{...}|None, error}``。
    """
    try:
        rel = _api_get_json(f'/releases/tags/{PREBUILT_TAG}')
    except Exception as e:
        return {'success': False, 'assets': [], 'error': f'获取预编译列表失败: {e}',
                'local': {'os': local_os_tag(), 'python': local_python_tag()},
                'installed': installed_manifest()}
    raw_assets = (rel or {}).get('assets', []) if isinstance(rel, dict) else []
    parsed = [p for a in raw_assets if (p := _parse_asset(a))]
    # 新的在上:主键 updated_at 倒序;同批(时间相同)内按 os / py 稳定排序
    parsed.sort(key=lambda a: (a['os'], a['python_tag']))
    parsed.sort(key=lambda a: a['updated_at'], reverse=True)
    return {
        'success': True,
        'assets': _annotate(parsed),
        'local': {'os': local_os_tag(), 'python': local_python_tag()},
        'installed': installed_manifest(),
        'error': '',
    }


# ──────── 下载 + 校验 + 原子换入 ───────────────────────────────────────────

def _safe_members(zf) -> list[str]:
    """zip-slip 防护 + 白名单:仅允许 LGTBot_ElainaBot.so / manifest.json / build/**。

    返回全部成员名(校验通过);任一非法即抛 ValueError。
    """
    names = zf.namelist()
    for name in names:
        norm = os.path.normpath(name)
        if norm.startswith('..') or os.path.isabs(norm):
            raise ValueError(f'非法路径(zip slip): {name!r}')
        top = norm.replace(os.sep, '/').split('/', 1)[0]
        if norm not in ('LGTBot_ElainaBot.so', 'manifest.json') and top != 'build':
            raise ValueError(f'包含预期外路径: {name!r}')
    return names


def _verify_manifest(root: str) -> None:
    """按 staging 内 manifest.json 校验每个文件 sha256(完整性),不符抛 ValueError。"""
    mpath = os.path.join(root, 'manifest.json')
    try:
        with open(mpath, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError(f'manifest.json 缺失或损坏: {e}')
    for entry in manifest.get('files', []):
        rel, want = entry.get('path', ''), entry.get('sha256', '')
        fp = os.path.join(root, rel.replace('/', os.sep))
        if not os.path.isfile(fp):
            raise ValueError(f'包内缺文件: {rel}')
        h = hashlib.sha256()
        with open(fp, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        if want and h.hexdigest() != want:
            raise ValueError(f'文件 sha256 不符: {rel}')


_EXECUTABLES = ('build/markdown2image', 'build/match_game_runner', 'build/config_runner')


def _extract_and_swap(zip_path: str) -> dict:
    """校验 zip → 解压到 staging → chmod +x → 原子换入 build_prebuilt/。同步阻塞。

    换入失败(引擎运行中,加载中的 .so 在 WSL /mnt、Windows 语义盘上会锁住整目录 rename → EACCES)时**降级为暂存**:
    staging 挪到 ``build_prebuilt.pending``,boot 下次启动最早期(尚未加载任何 .so)完成换入 —— 预编译包本就需重启生效,
    对用户流程无额外负担。返回 ``{'success': True, 'pending': bool}``。"""
    import zipfile
    shutil.rmtree(_STAGING_DIR, ignore_errors=True)
    os.makedirs(_STAGING_DIR, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            if zf.testzip() is not None:
                raise ValueError('zip 内容损坏(CRC 校验失败)')
            members = _safe_members(zf)
            zf.extractall(_STAGING_DIR, members=members)
        _verify_manifest(_STAGING_DIR)
        # 可执行位:zipfile.extractall 不还原权限,手动补
        for rel in _EXECUTABLES:
            fp = os.path.join(_STAGING_DIR, rel.replace('/', os.sep))
            if os.path.isfile(fp):
                os.chmod(fp, 0o755)
        # 原子换入:旧目录先改名再删,失败也不至于两头空
        old = PREBUILT_DIR + '.old'
        shutil.rmtree(old, ignore_errors=True)
        try:
            if os.path.isdir(PREBUILT_DIR):
                os.rename(PREBUILT_DIR, old)
            os.rename(_STAGING_DIR, PREBUILT_DIR)
        except OSError as e:
            # build_prebuilt 被运行中引擎占用 → 暂存,重启时由 boot 完成换入
            if not os.path.isdir(PREBUILT_DIR) and os.path.isdir(old):
                os.rename(old, PREBUILT_DIR)          # 第二步才失败的回滚(罕见)
            _prebuilt_swap.stage_pending(_STAGING_DIR, PREBUILT_DIR)
            log.warning(f'[prebuilt] build_prebuilt 被占用({e}),'
                        f'新版本已暂存为 pending,重启 LGTBot 后自动完成安装')
            return {'success': True, 'pending': True}
        shutil.rmtree(old, ignore_errors=True)
        # 直接换入成功 → 作废过时的暂存,防止重启时旧 pending 反把新安装盖掉
        shutil.rmtree(_prebuilt_swap.pending_dir(PREBUILT_DIR), ignore_errors=True)
    finally:
        shutil.rmtree(_STAGING_DIR, ignore_errors=True)
    return {'success': True, 'pending': False}


def _download_mirror_order(preferred: str | None) -> list[str]:
    """下载镜像顺序:用户选定(参数 > marker)的排最前,其余按排序兜底。"""
    order = _ranked_mirror_prefixes()
    sel = preferred if preferred is not None else get_selected_mirror()
    if sel is not None:
        sel = sel.strip()
        order = [sel] + [m for m in order if m != sel]
    return order


async def download(asset_name: str, preferred_mirror: str | None = None) -> dict:
    """下载并安装指定预编译包。临时文件 → 校验 → staging → 原子换入 build_prebuilt/。

    ``preferred_mirror`` 优先(其次读 marker),失败再按排序兜底其余镜像。
    """
    # 立刻落盘「下载中」—— 必须在慢速 list_remote() 之前:下载在后台 task 里跑,
    # 前端收到 HTTP 响应后按 1s 轮询读 state.json。若首个 running=True 迟到(卡在 list_remote 的网络调用后),
    # 轮询会先读到空 / 上一次遗留的旧 state → 进度条闪一下即隐藏、并误判「已结束」而停止轮询,真实进度再不刷新(刷新页面才恢复)。
    os.makedirs(_PREBUILT_DATA, exist_ok=True)
    _write_state(running=True, stage='download', asset=asset_name,
                 progress=0, downloaded=0, total=0, error='')

    loop = asyncio.get_running_loop()
    remote = await loop.run_in_executor(None, list_remote)
    if not remote.get('success'):
        msg = remote.get('error') or '无法获取远程列表'
        _write_state(running=False, stage='error', asset=asset_name, error=msg)
        return {'success': False, 'message': msg}
    asset = next((a for a in remote['assets'] if a['name'] == asset_name), None)
    if asset is None:
        msg = f'未找到预编译包: {asset_name}'
        _write_state(running=False, stage='error', asset=asset_name, error=msg)
        return {'success': False, 'message': msg}

    _write_state(running=True, stage='download', asset=asset_name,
                 progress=0, downloaded=0, total=asset.get('size', 0), error='')
    try:
        import aiohttp
    except ImportError:
        _write_state(running=False, stage='error', error='缺少 aiohttp')
        return {'success': False, 'message': '缺少 aiohttp 依赖'}

    src_url = asset['url']
    last_err = ''
    try:
        for mirror in _download_mirror_order(preferred_mirror):
            url = _build_mirror_url(mirror, src_url)
            try:
                await _download_one(url, asset)
                break
            except Exception as e:
                last_err = str(e)
                continue
        else:
            _write_state(running=False, stage='error', asset=asset_name,
                         error=f'所有镜像下载失败: {last_err}')
            return {'success': False, 'message': f'下载失败: {last_err}'}

        _write_state(running=True, stage='verify', asset=asset_name, progress=100)
        loop = asyncio.get_running_loop()
        swap = await loop.run_in_executor(None, _extract_and_swap, _DOWNLOAD_TMP)
    except asyncio.CancelledError:
        # 用户取消(page_prebuilt 对本 task 调 task.cancel()):置「已取消」终态,
        # finally 会删掉未完成的临时文件。re-raise 让 task 正常标记为已取消。
        _write_state(running=False, stage='cancelled', asset=asset_name, error='')
        log.info(f'[prebuilt] 下载已取消: {asset_name}')
        raise
    except Exception as e:
        log.error(f'[prebuilt] 安装 {asset_name} 失败: {e}')
        _write_state(running=False, stage='error', asset=asset_name, error=str(e))
        return {'success': False, 'message': f'安装失败: {e}'}
    finally:
        try:
            os.remove(_DOWNLOAD_TMP)
        except OSError:
            pass

    note = ('目录被运行中引擎占用，新版本已暂存；重启 LGTBot 后自动完成安装'
            if swap.get('pending') else '')
    _write_state(running=False, stage='done', asset=asset_name, progress=100, error='',
                 note=note)
    log.info(f'[prebuilt] ✅ 已安装 {asset_name} → '
             + ('build_prebuilt.pending/(暂存,重启后换入)' if swap.get('pending') else 'build_prebuilt/'))
    return {'success': True, 'asset': asset_name, 'pending': swap.get('pending', False),
            'message': (note or '下载安装完成,切到「📦 用预编译包」并重启 LGTBot 生效')}


async def _download_one(url: str, asset: dict) -> None:
    """单镜像分块下载到临时文件,边下边写进度 state。失败抛异常由上层换镜像。"""
    import aiohttp
    total = int(asset.get('size', 0) or 0)
    timeout = aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, ssl=False, allow_redirects=True) as resp:
            if resp.status != 200:
                raise RuntimeError(f'HTTP {resp.status}')
            clen = resp.headers.get('content-length')
            if clen and clen.isdigit():
                total = int(clen)
            downloaded = 0
            last_write = 0.0
            with open(_DOWNLOAD_TMP, 'wb') as f:
                async for chunk in resp.content.iter_chunked(65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    now = time.monotonic()
                    if now - last_write >= 0.4:      # 限流写盘,~2.5 次/秒
                        last_write = now
                        pct = int(downloaded / total * 100) if total else 0
                        _write_state(running=True, stage='download', asset=asset['name'],
                                     progress=pct, downloaded=downloaded, total=total, error='')
    # 完整性:大小对得上(sha256 全量校验留给解压后 _verify_manifest)
    if total and os.path.getsize(_DOWNLOAD_TMP) != total:
        raise RuntimeError('下载不完整(大小不符)')


def install_uploaded(zip_path: str) -> dict:
    """安装用户**手动上传**的预编译包 zip —— 复用下载路径的校验 + 原子换入。

    与 ``download`` 的区别仅在来源(本地上传 vs 远程下载),校验(zip 签名 +
    manifest sha256 + zip-slip 白名单)、staging、原子换入 ``build_prebuilt/``
    完全一致。同步实现,调用方(upload_handler)已把上传流写到 ``zip_path``,
    这里在 executor 里跑避免阻塞事件循环。取完即删临时文件。
    """
    _write_state(running=True, stage='verify', asset='(本地上传)', progress=100, error='')
    try:
        swap = _extract_and_swap(zip_path)
    except Exception as e:
        log.error(f'[prebuilt] 安装上传包失败: {e}')
        _write_state(running=False, stage='error', asset='(本地上传)', error=str(e))
        return {'success': False, 'message': f'安装失败: {e}'}
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass
    note = ('目录被运行中引擎占用，新版本已暂存；重启 LGTBot 后自动完成安装'
            if swap.get('pending') else '')
    _write_state(running=False, stage='done', asset='(本地上传)', progress=100, error='',
                 note=note)
    log.info('[prebuilt] ✅ 已从上传包安装 → '
             + ('build_prebuilt.pending/(暂存,重启后换入)' if swap.get('pending') else 'build_prebuilt/'))
    return {'success': True, 'pending': swap.get('pending', False),
            'message': (note or '上传包已安装,切到「📦 用预编译包」并重启 LGTBot 生效')}
