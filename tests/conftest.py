#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""pytest 全局 conftest —— 把 mod/boot.py 替换成 fake stub,绕开 C++ 扩展加载。

真 boot.py 在 import 时就要 chdir / RTLD_GLOBAL 预加载 / import 编译好的 .so,没编译过必挂;
各 mod 都 ``from . import boot``,所以 fake 必须在任何 mod 被 import 之前塞进 ``sys.modules``
—— conftest 顶层先于 collection 执行,正好赶得上。
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
# 1. 把 fake boot 注入 sys.modules
# ─────────────────────────────────────────────────────────────────────────

# 临时插件目录(不污染开发树),供 fake boot 的路径常量用。必须还原真实布局
# ``<root>/plugins/LGTBot_ElainaBot``:backup.py 在 import 期按 PLUGIN_DIR 的祖父目录算 BACKUP_DIR,
# 这样备份才落在 tmp 内。
_TEST_ROOT = tempfile.mkdtemp(prefix='lgtbot_pytest_')
_TEST_PLUGIN_DIR = os.path.join(_TEST_ROOT, 'plugins', 'LGTBot_ElainaBot')
os.makedirs(_TEST_PLUGIN_DIR, exist_ok=True)
_TEST_DATA_DIR = os.path.join(_TEST_PLUGIN_DIR, 'data')
_TEST_BUILD_DIR = os.path.join(_TEST_PLUGIN_DIR, 'build')
os.makedirs(_TEST_DATA_DIR, exist_ok=True)
os.makedirs(_TEST_BUILD_DIR, exist_ok=True)

# 跨重载持久化字典 —— 各模块 import 时就把里面的容器绑成模块级变量,
# 所以它必须**贯穿整个 pytest run**:每个测试只清内容,对象本身不能换。
_persistent: dict = {
    'active_ref': {},
    'ref_waiters': {},
    # state 等模块直接取下标的 key,占位避免 KeyError
    'pending_buttons': {},
    'current_game': {},
    'active_matches': {},
    'pending_new_game_name': {},
    'group_push_cache': {},
    'group_push_probe_at': {},
    'mention_rewrites': {},
    'force_interrupt_hints': {},
    'waiting_rooms': {},
    'nickname_review_queue': {},
    'nickname_review_flagged': set(),
}


def _make_fake_boot() -> types.ModuleType:
    m = types.ModuleType('plugins.LGTBot_ElainaBot.mod.boot')
    # 路径常量 —— 模仿 boot.py 的形状
    m.PLUGIN_DIR = _TEST_PLUGIN_DIR
    m.DATA_DIR = _TEST_DATA_DIR
    m.BUILD_DIR = _TEST_BUILD_DIR
    # prebuilt.py 顶层引用:build/(本地编译)与 build_prebuilt/(下载包)两个候选目录;
    # ENGINE_ROOT 是桥接 .so 所在目录(本地模式即插件根)。
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
    m._get_persistent = lambda: _persistent
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

    quota 的 _active_ref / _ref_waiters 就是 _persistent 里的 dict,清 _persistent 即同时清掉。
    """
    _persistent['active_ref'].clear()
    _persistent['ref_waiters'].clear()
    _persistent['pending_buttons'].clear()
    _persistent['current_game'].clear()
    _persistent['active_matches'].clear()
    _persistent['pending_new_game_name'].clear()
    _persistent['group_push_cache'].clear()
    _persistent['group_push_probe_at'].clear()
    _persistent['mention_rewrites'].clear()
    _persistent['force_interrupt_hints'].clear()
    _persistent['waiting_rooms'].clear()
    _persistent['nickname_review_queue'].clear()
    _persistent['nickname_review_flagged'].clear()

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

    _persistent['active_ref'].clear()
    _persistent['ref_waiters'].clear()


@pytest.fixture
def event_loop():
    """pytest-asyncio 默认 fixture override —— 每个测试一个新 loop,异步 case 之间不串 loop 状态。"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()

