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

注意第 2 步的 chdir **只保证主进程**那份 `k_markdown2image_path` 正确。游戏跑在
运行时才 fork 的 `match_game_runner` 子进程里，那时 CWD 已恢复，子进程自己那份
常量会指向框架根 —— 故另用 `_make_runner_wrapper()` 生成的 wrapper 启动 runner
（`cd build && exec runner`），详见该函数 docstring。
"""

from __future__ import annotations
import os
import sys
import ctypes
import glob

from core.base.logger import get_logger, PLUGIN

log = get_logger(PLUGIN, 'LGTBot')

# ──────── 路径常量 ────────────────────────────────────────────────────────
# __file__ → plugins/LGTBot_ElainaBot/mod/boot.py  → 插件根目录是其上一级的上一级
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR   = os.path.join(PLUGIN_DIR, 'data')

# 本地编译产物(build/)与下载的预编译包(build_prebuilt/)两个候选,
# 用哪个由 data/prebuilt/active marker 决定 —— 见 _resolve_active_build()。
#
# 布局差异(关键):本地编译时 .so 在插件根、编译产物在 build/;而预编译包解压后
# **保留了 zip 内的 build/ 前缀**(见 tools/pack_prebuilt.sh:打包时 .so 放包根、
# 其余进 build/),即:
#     build_prebuilt/LGTBot_ElainaBot.so          ← 桥接 .so
#     build_prebuilt/build/{libbot_core.so, runner, markdown2image, plugins/…}
# 所以预编译模式下,「桥接 .so 所在目录」(ENGINE_ROOT)与「编译产物目录」(BUILD_DIR)
# 相差一层,不能混为一谈。
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


# 上次预编译安装若因 build_prebuilt/ 被运行中引擎占用而暂存 (WSL /mnt、Windows语义盘上加载中的 .so 会锁住整目录 rename → EACCES),
# 此刻进程刚启动、尚未加载任何 .so,是完成换入的唯一安全窗口。必须在 _resolve_active_build() 选定 ENGINE_ROOT / RTLD_GLOBAL 预加载之前。
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
# 注:data/user_cache.db 是旧版私有昵称缓存的遗留文件
# 用户数据现全部读主框架数据库(mod/userinfo.py),本插件不再连接 / 读写 / 备份它;可删除。


os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(ENGINE_DIR, exist_ok=True)
os.makedirs(IMG_PATH, exist_ok=True)


# ──────── LGTBot 引擎配置文件预生成 ───────────────────────────────────────
# 启动时若 data/engine/lgtbot.json 不存在则写入空 JSON。引擎自身在 LoadConfig
# 阶段也会兜底创建，这里前置一次确保 Python 一侧可以直接传 CONF_PATH 给 Start。
def _ensure_lgtbot_conf():
    if os.path.isfile(CONF_PATH):
        return
    try:
        with open(CONF_PATH, 'w', encoding='utf-8') as f:
            f.write('{}\n')
    except OSError:
        pass


_ensure_lgtbot_conf()

# 让 `import LGTBot_ElainaBot` 能找到桥接 .so / .pyd。
# 预编译模式下 .so 在 build_prebuilt/,ENGINE_ROOT 必须排在插件根之前,盖过插件根里
# 可能残留的本地旧 .so(ABI 可能与预编译包不同);本地模式 ENGINE_ROOT == 插件根,等价原逻辑。
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
# CI 编译机的绝对路径会被烤进 match_game_runner / config_runner。预编译包解压到
# 用户任意路径后这些路径失效,需运行时覆盖:
#   · match_game_runner —— 认 LGTBOT_MATCH_RUNNER 环境变量(见 match.cc:ResolveRunnerExe)
#   · 子进程 runner 找 build/ 里的 libbot_core.so 等 —— 靠 LD_LIBRARY_PATH(preload
#     只对本 Python 进程生效,不传播给 spawn 出的 runner 子进程)
# (config_runner 无环境变量入口,由桥接层 Start() 传 config_runner_path_ 覆盖。)
# 本地编译路径正确时设这些是幂等的(值本就指向 build/),无副作用。
def _make_runner_wrapper(runner_exe: str) -> str:
    """生成「先 chdir 到 BUILD_DIR 再 exec runner」的 wrapper 脚本,返回其路径。

    引擎渲染 markdown 用的 ``k_markdown2image_path`` 是 ``bot_core/image.h`` 里的 **inline 全局常量**,
    在「谁加载它、谁当时的 cwd」那一刻就固化为 ``current_path()/markdown2image``。本模块只在 import 期间
    chdir 到 BUILD_DIR(所以**主进程**那份常量是对的),import 结束即恢复主框架 cwd;而游戏跑在**运行时**才
    fork+execvp 的 ``match_game_runner`` 子进程里(``bot_core/subprocess.cc`` 不设 cwd、``game_runner_main.cc``
    也不 chdir),它继承的是恢复后的框架根 → 子进程那份常量指向 ``<框架根>/markdown2image``(不存在)。

    wrapper 用 ``exec`` 顶替自身进程,**pid 不变** —— bot_core 要 waitpid / SignalStop 这个 pid,
    语义与直接 exec runner 完全一致。引擎侧入口是 ``bot_core/match.cc::ResolveRunnerExe``。
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
# LGTBot_ElainaBot.so 链接 libbot_core.so（位于 build/），但 ld.so 默认不搜
# build/，rpath 缺失时会报 "cannot open shared object file"。
# 用 ctypes.CDLL 显式按绝对路径预加载所有 build/lib*.so，配合 RTLD_GLOBAL
# 让符号进全局符号表，后续 LGTBot_ElainaBot.so 通过 dlopen 加载时直接命中。
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
# 插件热重载时，PluginManager 会把本插件的 Python 模块从 sys.modules 移除并
# 重新 import；但 C++ 扩展 `LGTBot_ElainaBot` 一旦被 dlopen 就常驻进程内，sys.modules
# 也保留缓存。利用这一点，把所有需要跨重载共享的可变容器挂到扩展模块对象上：
#
#   pending_buttons  - 命令触发的待附按钮（一次性消费, 见 callbacks.cb_match_event）
#   active_ref       - 被动消息配额状态（msg_id/event_id + count, 见 quota.py）
#   ref_waiters      - 配额满时等待的 asyncio.Event 列表（同上）
#   current_game     - 群/用户 → 当前游戏名（/规则 按钮回查用，见 state.py）
#   full_volume_groups - 已观测到 GROUP_MESSAGE_CREATE 的群 openid 集合
#                       （helpers.is_full_volume_group 唯一信号源）
#   group_push_cache - 群 → (可否主动推送, 过期时间) 的 TTL 缓存
#                       （helpers.can_push_group 按群点查 DB 的结果缓存）
#   log_attribution_ctxvar - log_attribution 模块的 ContextVar 实例
#                       （跨热重载身份保持,patched _log_push 闭包要捕获同一个对象）
#
# 这样旧 callback（持有旧模块引用）和新 dispatcher（新模块引用）操作的都是
# 同一份字典，热重载后玩家命令仍能正确路由到旧引擎里仍在进行的游戏。
_PERSIST_ATTR = '_elaina_persistent'
_ENGINE_RUNNING_ATTR = '_elaina_engine_running'

# 持久化字典的所有默认 key 集中在这里 —— state.py / log_attribution.py 不再
# 各自 setdefault,直接从 _get_persistent() 取就保证 key 一定存在。
# (log_attribution_ctxvar 是 ContextVar 实例,延迟构造 —— 这里只占位 None,
#  log_attribution._get_ctxvar() 检测到 None 时再 ContextVar(...) 一次性填上。)
_PERSIST_DEFAULTS: dict = {
    'pending_buttons':         {},
    'active_ref':              {},
    'ref_waiters':             {},
    'current_game':            {},
    'active_matches':          {},
    'pending_new_game_name':   {},
    'full_volume_groups':      set(),
    'group_push_cache':        {},
    'log_attribution_ctxvar':  None,
    'mention_rewrites':        {},
}


def _get_persistent() -> dict:
    """返回挂在 C++ 扩展上的持久化容器，缺失则创建并补齐所有默认 key。

    第一次插件加载：扩展模块上没有 _elaina_persistent → 创建新 dict
    后续热重载：直接复用已有的 dict；如果新增了某 key 但旧 dict 没有,在此补齐
    (容器类型 default 用 dict/set/list 实例,**不要共享**,每次 setdefault 单独
    构造一个新的;None 默认值天然安全)。
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
    # 补齐缺失的 key (老版本 dict 没有的字段在新版本里仍可用)
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

    关键陷阱:仓库根 / plugins 下存在同名**目录** ``LGTBot_ElainaBot/``(开发副本、
    插件目录本身),当 .so 不存在时 ``import LGTBot_ElainaBot`` 不会抛 ImportError,
    而是把该目录当**命名空间包**导入 —— 得到一个没有任何扩展函数的空模块对象。
    若不校验,``LGTBOT_AVAILABLE`` 会假阳性为 True:自检误报「已加载」、重启走到
    ``release_bot_if_not_processing_games()`` 直接 AttributeError 500。
    这里用扩展一定导出的 ``start`` 作探针;不是真扩展就当作未加载,并把命名空间包
    从 sys.modules 剔除,免得缓存污染后续导入。
    """
    try:
        import LGTBot_ElainaBot as _lib  # noqa: F401
    except ImportError as e:
        return None, str(e)
    if not hasattr(_lib, 'start'):
        where = getattr(_lib, '__file__', None) or getattr(_lib, '__path__', '?')
        sys.modules.pop('LGTBot_ElainaBot', None)
        return None, f'导入到的不是 C++ 扩展(疑似同名目录被当成命名空间包): {where}'
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
