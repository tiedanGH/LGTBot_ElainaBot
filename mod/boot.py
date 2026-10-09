#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""C++ 扩展导入 + 路径常量

import 副作用顺序敏感，需要在所有依赖 LGTBot_ElainaBot C++ 扩展的子模块之前加载：

  1. 把插件目录加入 sys.path，让 `import LGTBot_ElainaBot` 能找到 .so
  2. 临时 chdir 到 build/，让 libbot_core.so 加载时静态初始化的
     `k_markdown2image_path = current_path() / "markdown2image"` 捕获到正确路径
  3. 设置 RTLD_GLOBAL 标志，使 libbot_core.so 静态依赖的 glog/gflags 等符号
     对后续 dlopen 的 libgame.so 可见（否则报 undefined symbol: ...LogMessage...）
  4. import 完成后立即恢复 CWD 和 dlopen flags，避免影响主框架其他相对路径

第 2 步的 chdir **只保证主进程**那份 `k_markdown2image_path` 正确；运行时才 fork 的
`match_game_runner` 子进程由 `_make_runner_wrapper()` 生成的 wrapper 另行修正。
"""

from __future__ import annotations
import os
import sys
import ctypes
import glob

from core.base.logger import get_logger, PLUGIN

log = get_logger(PLUGIN, 'LGTBot')

# ──────── 路径常量 ────────────────────────────────────────────────────────
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR   = os.path.join(PLUGIN_DIR, 'data')

# 本地编译产物(build/)与下载的预编译包(build_prebuilt/)二选一,由 data/prebuilt/active marker 决定(见 _resolve_active_build)。
# 预编译包解压后**保留了 zip 内的 build/ 前缀**(桥接 .so 在 build_prebuilt/,其余在 build_prebuilt/build/,见 tools/pack_prebuilt.sh),
# 所以预编译模式下 ENGINE_ROOT(桥接 .so 目录)与 BUILD_DIR(编译产物目录)相差一层,不能混为一谈。
LOCAL_BUILD_DIR      = os.path.join(PLUGIN_DIR, 'build')
PREBUILT_DIR         = os.path.join(PLUGIN_DIR, 'build_prebuilt')
_PREBUILT_BUILD_DIR  = os.path.join(PREBUILT_DIR, 'build')   # 预编译包内真正的编译产物目录
_ACTIVE_BUILD_MARKER = os.path.join(DATA_DIR, 'prebuilt', 'active')


def _resolve_active_build() -> tuple[str, str]:
    """决定引擎加载来源,返回 ``(engine_root, build_dir)``。

    - ``engine_root``:``import LGTBot_ElainaBot`` 找桥接 .so 的目录。
    - ``build_dir``:``libbot_core.so`` / runner / markdown2image / ``plugins/`` 所在目录。

    读 marker ``data/prebuilt/active``:内容 == ``build_prebuilt`` 且预编译包已解压
    (``build_prebuilt/build/`` 存在)→ 用预编译(engine_root=``build_prebuilt/``,
    build_dir=``build_prebuilt/build/``);其余一切情况(marker 缺失 / == ``build`` /
    未下载)一律回落本地(engine_root=插件根,build_dir=``build/``)。
    逻辑放在 boot 内(而非 prebuilt.py)因为 boot 最先 import,不能依赖后加载的模块;
    切换 marker 后需重启进程才生效(引擎只在 start 时按此路径加载一次)。
    """
    try:
        with open(_ACTIVE_BUILD_MARKER, 'r', encoding='utf-8') as f:
            choice = f.read().strip()
    except OSError:
        choice = ''
    if choice == 'build_prebuilt' and os.path.isdir(_PREBUILT_BUILD_DIR):
        return PREBUILT_DIR, _PREBUILT_BUILD_DIR
    return PLUGIN_DIR, LOCAL_BUILD_DIR


# 完成上次暂存的预编译安装:此刻尚未加载任何 .so,是换入的唯一安全窗口,必须在 _resolve_active_build() 选定 ENGINE_ROOT / RTLD_GLOBAL 预加载之前。
# _prebuilt_swap 零依赖,不会引入循环 import。
from . import _prebuilt_swap
_pending_ok, _pending_msg = _prebuilt_swap.finalize_pending(PREBUILT_DIR)
if _pending_msg:
    (log.info if _pending_ok else log.warning)(f'[prebuilt] {_pending_msg}')

ENGINE_ROOT, BUILD_DIR = _resolve_active_build()     # (桥接 .so 目录, 编译产物目录)
ENGINE_DIR = os.path.join(DATA_DIR, 'engine')        # LGTBot 引擎内部文件目录
GAME_PATH  = os.path.join(BUILD_DIR, 'plugins')      # 各 libgame.so 所在目录
# 引擎自身的数据 —— 全部归入 data/engine/，让 data/ 根只放插件级用户数据
DB_PATH    = os.path.join(ENGINE_DIR, 'lgtbot.db')
IMG_PATH   = os.path.join(ENGINE_DIR, 'images')
# 引擎自身的配置文件 —— 放在 data/engine/ 子目录避免污染 Web UI 的「插件 → 配置」
# 入口（该入口非递归扫描 data/，子文件夹自动不可见，与 config.yaml 区分清楚）
CONF_PATH  = os.path.join(ENGINE_DIR, 'lgtbot.json')


os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(ENGINE_DIR, exist_ok=True)
os.makedirs(IMG_PATH, exist_ok=True)


# ──────── LGTBot 引擎配置文件预生成 ───────────────────────────────────────
# 引擎在 LoadConfig 阶段也会兜底创建，这里前置一次确保 Python 一侧可以直接传 CONF_PATH 给 Start。
def _ensure_lgtbot_conf():
    if os.path.isfile(CONF_PATH):
        return
    try:
        with open(CONF_PATH, 'w', encoding='utf-8') as f:
            f.write('{}\n')
    except OSError:
        pass


_ensure_lgtbot_conf()

# ENGINE_ROOT 必须排在插件根之前:预编译模式下要盖过插件根里可能残留的本地 .so(ABI 可能与预编译包不同)。
if ENGINE_ROOT in sys.path:
    sys.path.remove(ENGINE_ROOT)
sys.path.insert(0, ENGINE_ROOT)


# ──────── C++ 扩展加载 ────────────────────────────────────────────────────
LGTBOT_AVAILABLE = False
IMPORT_ERROR = ''
LGTBot_ElainaBot = None  # 模块对象，导入成功后赋值

_old_cwd = os.getcwd()
_chdir_ok = os.path.isdir(BUILD_DIR)
if _chdir_ok:
    os.chdir(BUILD_DIR)


# ──────── 预编译包可重定位 env ────────────────────────────────────────────
# CI 编译机的绝对路径会被烤进 match_game_runner / config_runner,预编译包解压到任意路径后失效,需运行时覆盖:
#   · match_game_runner —— 认 LGTBOT_MATCH_RUNNER 环境变量(见 match.cc:ResolveRunnerExe)
#   · 子进程 runner 找 build/ 里的 libbot_core.so 等 —— 靠 LD_LIBRARY_PATH(preload 不传播给子进程)
#   · config_runner 无环境变量入口,由桥接层 Start() 传 config_runner_path_ 覆盖
# 本地编译时这些值本就指向 build/,无副作用。
def _make_runner_wrapper(runner_exe: str) -> str:
    """生成「先 chdir 到 BUILD_DIR 再 exec runner」的 wrapper 脚本,返回其路径。

    ``k_markdown2image_path``(``bot_core/image.h`` 的 inline 全局常量)按加载那一刻的 cwd 固化。
    本模块只在 import 期间 chdir,而运行时才 fork 的 ``match_game_runner`` 子进程(引擎 fork 时不设 cwd,
    runner 自己也不 chdir)继承的是框架根 → 子进程那份常量指向不存在的 ``<框架根>/markdown2image``。

    wrapper 必须用 ``exec`` 顶替自身进程、**pid 不变** —— bot_core 要 waitpid / SignalStop 这个 pid。
    """
    # 放 data/ 而非 build/:预编译包切换会整体覆盖 build/,wrapper 会被冲掉
    path = os.path.join(DATA_DIR, 'match_runner_cwd.sh')
    body = (
        '#!/bin/sh\n'
        '# 由 mod/boot.py 自动生成,请勿手工编辑(每次插件加载都会重写)。\n'
        '# 作用:把 cwd 切到编译产物目录,让游戏子进程能找到 markdown2image。\n'
        f'cd "{BUILD_DIR}" || exit 1\n'
        f'exec "{runner_exe}" "$@"\n'
    )
    # 内容不变时不重写,避免每次热重载都动 mtime
    try:
        with open(path, 'r', encoding='utf-8') as f:
            if f.read() == body:
                os.chmod(path, 0o755)
                return path
    except OSError:
        pass
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(body)
    os.chmod(path, 0o755)
    return path


if _chdir_ok:
    _match_runner = os.path.join(BUILD_DIR, 'match_game_runner')
    if os.path.isfile(_match_runner):
        # 默认指向 runner 本身;wrapper 生成成功则改指 wrapper(修正子进程 cwd)
        os.environ['LGTBOT_MATCH_RUNNER'] = _match_runner
        if os.name == 'posix':
            try:
                os.environ['LGTBOT_MATCH_RUNNER'] = _make_runner_wrapper(_match_runner)
            except OSError as _e:
                # 失败不致命:退回直接 exec runner,只是留档赛况图仍存不下来
                log.warning(f'生成 match_runner wrapper 失败,赛况图留档可能失效: {_e}')
    _ld = os.environ.get('LD_LIBRARY_PATH', '')
    if BUILD_DIR not in _ld.split(os.pathsep):
        os.environ['LD_LIBRARY_PATH'] = BUILD_DIR + (os.pathsep + _ld if _ld else '')


# ──────── 预加载本地共享库 ────────────────────────────────────────────────
# LGTBot_ElainaBot.so 链接 build/ 里的 libbot_core.so，但 ld.so 默认不搜 build/（rpath 缺失报 "cannot open shared object file"）；
# 按绝对路径 RTLD_GLOBAL 预加载所有 build/lib*.so，后续 dlopen 直接命中。
if _chdir_ok:
    _libs = sorted(glob.glob(os.path.join(BUILD_DIR, 'lib*.so')))
    # 两趟：A 依赖 B 时第一趟 A 失败、第二趟 B 已就位则 A 成功
    for _ in range(2):
        for _lib in _libs:
            try:
                ctypes.CDLL(_lib, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass


# ──────── 跨插件热重载持久化容器 ──────────────────────────────────────────
# 热重载会重建本插件的 Python 模块，但 C++ 扩展 `LGTBot_ElainaBot` 常驻进程、sys.modules 也保留它；
# 跨重载共享的可变容器都挂到扩展模块对象上，旧 callback 与新 dispatcher 操作的才是同一份字典。
_PERSIST_ATTR = '_elaina_persistent'
_ENGINE_RUNNING_ATTR = '_elaina_engine_running'

# 持久化字典的所有默认 key 集中在这里,从 _get_persistent() 取就保证 key 一定存在。
# (log_attribution_ctxvar 是 ContextVar 实例,延迟构造 —— 这里只占位 None,
#  log_attribution._get_ctxvar() 检测到 None 时再 ContextVar(...) 一次性填上。)
_PERSIST_DEFAULTS: dict = {
    'pending_buttons':         {},
    'active_ref':              {},
    'ref_waiters':             {},
    'current_game':            {},
    'active_matches':          {},
    'pending_new_game_name':   {},
    'group_push_cache':        {},
    'log_attribution_ctxvar':  None,
    'mention_rewrites':        {},
    'force_interrupt_hints':   {},
    'waiting_rooms':           {},
    'nickname_review_queue':   {},
    'nickname_review_flagged': set(),
}


def _get_persistent() -> dict:
    """返回挂在 C++ 扩展上的持久化容器，缺失则创建并补齐所有默认 key。

    热重载时复用已有的 dict，只补齐旧 dict 没有的 key；容器类默认值每次构造新实例，
    **不要共享** ``_PERSIST_DEFAULTS`` 里的那一份。
    """
    if LGTBot_ElainaBot is None:
        # 扩展未编译：返回一次性的 fallback dict（不会跨重载共享，但避免 None）
        return {k: (type(v)() if isinstance(v, (dict, set, list)) else v)
                for k, v in _PERSIST_DEFAULTS.items()}
    p = getattr(LGTBot_ElainaBot, _PERSIST_ATTR, None)
    if p is None:
        p = {}
        try:
            setattr(LGTBot_ElainaBot, _PERSIST_ATTR, p)
        except Exception:
            pass
    for k, v in _PERSIST_DEFAULTS.items():
        if k not in p:
            p[k] = type(v)() if isinstance(v, (dict, set, list)) else v
    # 老版本的内存 user_cache：迁移后已无人读，pop 掉避免长期占内存
    p.pop('user_cache', None)
    return p


def is_engine_running() -> bool:
    """LGTBot C++ 引擎在上次 / 本次 plugin load 中已成功 start 且未释放？"""
    if LGTBot_ElainaBot is None:
        return False
    return bool(getattr(LGTBot_ElainaBot, _ENGINE_RUNNING_ATTR, False))


def mark_engine_running(running: bool):
    """记录引擎运行状态到扩展模块属性（跨重载持久）"""
    if LGTBot_ElainaBot is not None:
        try:
            setattr(LGTBot_ElainaBot, _ENGINE_RUNNING_ATTR, bool(running))
        except Exception:
            pass

def _import_extension() -> tuple[object, str]:
    """import 真正的 C++ 扩展并校验身份。返回 ``(module, '')`` 或 ``(None, err)``。

    .so 不存在时,同名**目录** ``LGTBot_ElainaBot/``(开发副本、插件目录本身)会被当成**命名空间包**导入,
    不抛 ImportError,得到一个没有任何扩展函数的空模块,``LGTBOT_AVAILABLE`` 随之假阳性。
    用扩展一定导出的 ``start`` 作探针;不是真扩展就当作未加载,并从 sys.modules 剔除,免得污染后续导入。
    """
    try:
        import LGTBot_ElainaBot as _lib  # noqa: F401
    except ImportError as e:
        return None, str(e)
    if not hasattr(_lib, 'start'):
        where = getattr(_lib, '__file__', None) or getattr(_lib, '__path__', '?')
        sys.modules.pop('LGTBot_ElainaBot', None)
        return None, f'非 C++ 扩展: {where}'
    return _lib, ''


if hasattr(sys, 'setdlopenflags') and hasattr(os, 'RTLD_GLOBAL'):
    # 仅 POSIX；Windows 上 sys.setdlopenflags 不存在，对应平台也不需要此操作
    _old_flags = sys.getdlopenflags()
    sys.setdlopenflags(os.RTLD_NOW | os.RTLD_GLOBAL)
    try:
        _lib, IMPORT_ERROR = _import_extension()
    finally:
        sys.setdlopenflags(_old_flags)
else:
    _lib, IMPORT_ERROR = _import_extension()

if _lib is not None:
    LGTBot_ElainaBot = _lib
    LGTBOT_AVAILABLE = True

# 立即恢复主框架的 CWD（避免全局 CWD 漂移导致 ElainaBot 自身路径错乱）
if _chdir_ok:
    os.chdir(_old_cwd)
