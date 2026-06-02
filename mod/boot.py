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
BUILD_DIR  = os.path.join(PLUGIN_DIR, 'build')
DATA_DIR   = os.path.join(PLUGIN_DIR, 'data')
ENGINE_DIR = os.path.join(DATA_DIR, 'engine')        # LGTBot 引擎内部文件目录
GAME_PATH  = os.path.join(BUILD_DIR, 'plugins')      # 各 libgame.so 所在目录
# 引擎自身的数据 —— 全部归入 data/engine/，让 data/ 根只放插件级用户数据
DB_PATH    = os.path.join(ENGINE_DIR, 'lgtbot.db')
IMG_PATH   = os.path.join(ENGINE_DIR, 'images')
# 引擎自身的配置文件 —— 放在 data/engine/ 子目录避免污染 Web UI 的「插件 → 配置」
# 入口（该入口非递归扫描 data/，子文件夹自动不可见，与 config.yaml 区分清楚）
CONF_PATH  = os.path.join(ENGINE_DIR, 'lgtbot.json')
# 插件管理的用户昵称 / 头像缓存（userdb 模块用），放 data/ 根方便用户感知
USER_CACHE_DB = os.path.join(DATA_DIR, 'user_cache.db')


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

# 让 `import LGTBot_ElainaBot` 能找到同目录下的 .so / .pyd
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)


# ──────── C++ 扩展加载 ────────────────────────────────────────────────────
LGTBOT_AVAILABLE = False
IMPORT_ERROR = ''
LGTBot_ElainaBot = None  # 模块对象，导入成功后赋值

_old_cwd = os.getcwd()
_chdir_ok = os.path.isdir(BUILD_DIR)
if _chdir_ok:
    os.chdir(BUILD_DIR)


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
#   log_attribution_ctxvar - log_attribution 模块的 ContextVar 实例
#                       （跨热重载身份保持,patched _log_push 闭包要捕获同一个对象）
#
# 这样旧 callback（持有旧模块引用）和新 dispatcher（新模块引用）操作的都是
# 同一份字典，热重载后玩家命令仍能正确路由到旧引擎里仍在进行的游戏。
#
# 注：早期版本这里还有 'user_cache'，已迁移到 SQLite 持久化（见 userdb.py），
# 跨重载共享走 DB 而非内存 dict；旧版本残留的该 key 会被下面 pop 掉。
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
    'full_volume_groups':      set(),
    'log_attribution_ctxvar':  None,
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

if hasattr(sys, 'setdlopenflags') and hasattr(os, 'RTLD_GLOBAL'):
    # 仅 POSIX；Windows 上 sys.setdlopenflags 不存在，对应平台也不需要此操作
    _old_flags = sys.getdlopenflags()
    sys.setdlopenflags(os.RTLD_NOW | os.RTLD_GLOBAL)
    try:
        import LGTBot_ElainaBot as _lib  # noqa: F401
        LGTBot_ElainaBot = _lib
        LGTBOT_AVAILABLE = True
    except ImportError as e:
        IMPORT_ERROR = str(e)
    finally:
        sys.setdlopenflags(_old_flags)
else:
    try:
        import LGTBot_ElainaBot as _lib
        LGTBot_ElainaBot = _lib
        LGTBOT_AVAILABLE = True
    except ImportError as e:
        IMPORT_ERROR = str(e)

# 立即恢复主框架的 CWD（避免全局 CWD 漂移导致 ElainaBot 自身路径错乱）
if _chdir_ok:
    os.chdir(_old_cwd)
