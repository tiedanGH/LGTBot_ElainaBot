#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""pytest 全局 conftest —— 把 mod/boot.py 替换成 fake stub,绕开 C++ 扩展加载。

为什么必须在 collection 前 inject:

  mod/boot.py 在 import 时会做 chdir / RTLD_GLOBAL / ctypes.CDLL 预加载 / 真
  ``import LGTBot_ElainaBot`` (Boost.Python .so),CI runner 没编译过 .so 必
  挂。直接给它 mock 是不够的 —— 其他 mod 间用 ``from . import boot`` 相对
  import,Python 会优先走 sys.modules 已有条目,所以把 fake boot 模块**塞进
  ``sys.modules['plugins.LGTBot_ElainaBot.mod.boot']`` 比真模块更早**,后续所有
  ``from . import boot`` 都拿到这个 fake。

conftest.py 是 pytest 在 collect tests 之前最先执行的文件之一,把 inject 做
在模块顶层就赶在了任何 ``from plugins.LGTBot_ElainaBot.mod.X import ...`` 之前。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
from unittest.mock import MagicMock

import pytest

# ─────────────────────────────────────────────────────────────────────────
# 1. 把 fake boot 注入 sys.modules ——
# 必须在 import 任何 mod/* 之前完成,因为 quota / uploader / dispatcher /
# callbacks 都顶部 `from . import boot`,会触发真 boot.py 求值。
# ─────────────────────────────────────────────────────────────────────────

# 临时插件目录(在 tmp 下,不污染开发树),供 fake boot 暴露的路径常量用。
# 不需要跨进程持久;每次 pytest run 拿到一个新 tmp dir。
#
# 关键:必须**还原真实布局** ``<root>/plugins/LGTBot_ElainaBot``,因为
# backup.py 在 import 期算 ``BACKUP_DIR = dirname(dirname(PLUGIN_DIR))/data/backup/lgtbot``。
# 嵌成 ``<tmp>/plugins/LGTBot_ElainaBot`` 后祖父 = ``<tmp>``,备份落在 ``<tmp>/data/``(tmp 内可写),
# 与真生产环境的相对结构一致。
_TEST_ROOT = tempfile.mkdtemp(prefix='lgtbot_pytest_')
_TEST_PLUGIN_DIR = os.path.join(_TEST_ROOT, 'plugins', 'LGTBot_ElainaBot')
os.makedirs(_TEST_PLUGIN_DIR, exist_ok=True)
_TEST_DATA_DIR = os.path.join(_TEST_PLUGIN_DIR, 'data')
_TEST_BUILD_DIR = os.path.join(_TEST_PLUGIN_DIR, 'build')
os.makedirs(_TEST_DATA_DIR, exist_ok=True)
os.makedirs(_TEST_BUILD_DIR, exist_ok=True)

# 跨重载持久化字典 —— quota 模块在 import 时 ``_active_ref = _p['active_ref']``,
# 所以同一个 _persistent 必须**贯穿整个 pytest run**。每个测试函数级清空内容,
# 但 dict 对象本身不能换(否则 quota._active_ref 指向旧 dict)。
_persistent: dict = {
    'active_ref': {},
    'ref_waiters': {},
    # message_log 等模块也可能要的 key,占位避免 KeyError
    'pending_buttons': {},
    'current_game': {},
    'active_matches': {},
    'pending_new_game_name': {},
    'full_volume_groups': set(),
    'group_push_cache': {},
    'group_push_probe_at': {},
    'mention_rewrites': {},
    'force_interrupt_hints': {},
}


def _make_fake_boot() -> types.ModuleType:
    m = types.ModuleType('plugins.LGTBot_ElainaBot.mod.boot')
    # 路径常量 —— 模仿 boot.py:28-40 的形状
    m.PLUGIN_DIR = _TEST_PLUGIN_DIR
    m.DATA_DIR = _TEST_DATA_DIR
    m.BUILD_DIR = _TEST_BUILD_DIR
    # 预编译特性:build/(本地编译)与 build_prebuilt/(下载包)两个候选目录 +
    # 当前生效目录 BUILD_DIR(测试里默认指向本地 build)。prebuilt.py 顶层引用。
    # ENGINE_ROOT = 桥接 .so 所在目录(本地模式 == 插件根);active_mode_running() 用它判定。
    m.LOCAL_BUILD_DIR = _TEST_BUILD_DIR
    m.ENGINE_ROOT = _TEST_PLUGIN_DIR
    m.PREBUILT_DIR = os.path.join(_TEST_PLUGIN_DIR, 'build_prebuilt')
    m.ENGINE_DIR = os.path.join(_TEST_DATA_DIR, 'engine')
    m.GAME_PATH = os.path.join(_TEST_BUILD_DIR, 'plugins')
    m.DB_PATH = os.path.join(m.ENGINE_DIR, 'lgtbot.db')
    m.IMG_PATH = os.path.join(m.ENGINE_DIR, 'images')
    m.CONF_PATH = os.path.join(m.ENGINE_DIR, 'lgtbot.json')
    os.makedirs(m.ENGINE_DIR, exist_ok=True)
    os.makedirs(m.GAME_PATH, exist_ok=True)
    os.makedirs(m.IMG_PATH, exist_ok=True)
    # C++ 扩展 stub —— 测试不会真调,但代码引用要存在
    m.LGTBot_ElainaBot = MagicMock()
    m.LGTBOT_AVAILABLE = False
    m.IMPORT_ERROR = '(pytest stub)'
    # 跨重载持久化 dict 必须**始终返回同一个对象**,否则模块级
    # `_active_ref = boot._get_persistent()['active_ref']` 一次性赋值后就失效
    m._get_persistent = lambda: _persistent
    # 引擎状态查询 / 设置 stub
    m.is_engine_running = lambda: False
    m.mark_engine_running = lambda x: None
    return m


sys.modules['plugins.LGTBot_ElainaBot.mod.boot'] = _make_fake_boot()


# ─────────────────────────────────────────────────────────────────────────
# 2. fixtures —— 每个测试前后状态清理
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_runtime_state():
    """每个测试前清空 quota / uploader 模块的全局可变状态,避免串扰。

    autouse=True —— 所有测试自动应用,无需在 test 函数签名里显式声明。
    quota 的 _active_ref / _ref_waiters 是 fake_boot._persistent dict 里的 key,
    所以在这里清 _persistent 即可同时清掉 quota 那边的引用(同一个 dict)。
    """
    # 测试前清理
    _persistent['active_ref'].clear()
    _persistent['ref_waiters'].clear()
    _persistent['pending_buttons'].clear()
    _persistent['current_game'].clear()
    _persistent['active_matches'].clear()
    _persistent['pending_new_game_name'].clear()
    _persistent['full_volume_groups'].clear()
    _persistent['group_push_cache'].clear()
    _persistent['group_push_probe_at'].clear()
    _persistent['mention_rewrites'].clear()
    _persistent['force_interrupt_hints'].clear()

    # 清 uploader 模块状态(若已被 import)
    try:
        from plugins.LGTBot_ElainaBot.mod import uploader
        uploader._inflight.clear()
        uploader._url_cache_v2.clear()
        if hasattr(uploader, '_url_cache'):
            uploader._url_cache.clear()
        # 恢复默认 TTL,防上个测试改过没还原
        uploader.URL_CACHE_TTL = 60.0
        uploader.SELECTED_BACKEND = ''
    except ImportError:
        pass

    yield

    # 测试后再清一次(防最后一个测试串到下次 pytest run 的 leftover)
    _persistent['active_ref'].clear()
    _persistent['ref_waiters'].clear()


@pytest.fixture
def event_loop():
    """pytest-asyncio 默认 fixture override —— 每个测试一个新 loop。

    避免 wait_and_consume 等异步 case 之间 loop 状态串。
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()

