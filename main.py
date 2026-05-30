#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LGTBot × ElainaBot 集成插件 (QQ Official Bot) —— 入口文件

各功能拆分到 app/ 子模块（详见 app/__init__.py），本文件只负责：

  1. 声明插件元数据
  2. 在 module top-level 捕获 PluginContext（PluginManager 仅在加载窗口期暴露）
  3. 顺序触发各子模块加载（boot 第一个，处理 C++ 扩展副作用）
  4. 实现 @on_load / @on_unload 生命周期

部署：见同目录 DEPLOY.md
"""

__plugin_meta__ = {
    'name': 'LGTBot 机器人',
    'author': '铁蛋',
    'description': '基于 C++ 的 LGTBot 游戏裁判机器人',
    'version': '2.1.0',
    'github': 'https://github.com/tiedanGH/LGTBot_ElainaBot',
}

import os
import sys
import asyncio

from core.plugin.decorators import on_load, on_unload
from core.plugin import context as _ctx_mod
from core.base.logger import get_logger, PLUGIN

# ──────── 关键步骤：捕获 PluginContext ────────────────────────────────────
# PluginManager 加载流程：
#   1. _ctx_mod.ctx = plugin_ctx   ← set
#   2. 执行本文件顶层代码（这里读到 ctx）
#   3. _ctx_mod.ctx = None         ← reset
#   4. 调用 @on_load 函数（此时 ctx 已是 None）
# 所以必须在模块顶层捕获，不能延迟到 @on_load 内
from plugins.LGTBot_ElainaBot.mod import state as _state
_state.plugin_ctx = _ctx_mod.ctx

# ──────── 触发各子模块加载 ────────────────────────────────────────────────
# 顺序敏感：boot 必须最先（处理 C++ 扩展导入 + chdir + RTLD_GLOBAL 副作用），
# 其他模块依赖 boot.LGTBot_ElainaBot / boot.BUILD_DIR / boot.LGTBOT_AVAILABLE 等
from plugins.LGTBot_ElainaBot.mod import boot              # noqa: F401  C++ 引擎与路径
from plugins.LGTBot_ElainaBot.mod import userdb            # noqa: F401  用户昵称 / 头像 SQLite
from plugins.LGTBot_ElainaBot.mod.webui import main as webui  # noqa: F401  Web 面板侧边栏页面入口
from plugins.LGTBot_ElainaBot.mod.webui import message_log # noqa: F401  日志缓冲(callbacks / dispatcher 直接调用)
from plugins.LGTBot_ElainaBot.mod import dispatcher        # noqa: F401  @handler 注册（消息派发 + INTERACTION）
from plugins.LGTBot_ElainaBot.mod import callbacks         # C++ 回调（被 LGTBot_ElainaBot.start 注入）
from plugins.LGTBot_ElainaBot.mod import config as _config
from plugins.LGTBot_ElainaBot.mod import log_attribution as _log_attribution  # noqa: F401

log = get_logger(PLUGIN, 'LGTBot')


# ──────── 生命周期 ────────────────────────────────────────────────────────

@on_load
async def _setup():
    # 主框架 MessageSender 的 push 路径打补丁(让本插件的 push 日志带上正确
    # plugin_name),幂等;放在最早 —— 任何 send_to_* 之前必须就位
    _log_attribution.install_once()

    # 预存 execv 自启参数到 C++ 桥接层 —— SIGABRT handler 在 heap 已坏时
    # 没法做任何分配,必须提前用 fixed buffer 固化 sys.executable + sys.argv。
    # 这样 lgtbot SEGV 后即便工作线程退出引发 double-free → SIGABRT,我们的
    # SigAbrtHandler 也能立刻 execv 整进程自启,不让进程真死。
    if boot.LGTBOT_AVAILABLE:
        try:
            boot.LGTBot_ElainaBot.set_restart_args(sys.executable, list(sys.argv))
        except Exception as e:
            log.warning(f'set_restart_args 失败,SIGABRT 兜底自启不可用: {e}')

    # 注册 Web 面板拓展页（无论 LGTBot 是否可用，让用户先能看到日志页）
    webui.register()

    # 加载 / 创建配置（让 Web UI「插件 → 配置」入口立刻可见 config.yaml）
    admins = _config.load_plugin_config()

    if not boot.LGTBOT_AVAILABLE:
        log.error('=' * 60)
        log.error(f'LGTBot_ElainaBot C++ 扩展未编译或导入失败：{boot.IMPORT_ERROR}')
        log.error('请先按 plugins/LGTBot_ElainaBot/DEPLOY.md 编译后再启动')
        log.error('=' * 60)
        return

    # 捕获主事件循环 —— C++ 工作线程通过 run_coroutine_threadsafe 调度到此循环
    _state.event_loop = asyncio.get_running_loop()

    # 启动 userdb 5 分钟周期 flusher（在所有 early-return 之前，热重载复用
    # 旧引擎那条路径上 dispatcher 仍会 mark_dirty，flusher 必须就位）
    userdb.start_flusher(_state.event_loop)

    # ── 热重载检测：上一轮的引擎可能还活着 ─────────────────────────────────
    # 若此时再调 LGTBot_ElainaBot.start()，C++ 会覆盖 g_bot_core，旧引擎实例被丢弃，
    # 进行中的游戏全部失联（玩家命令进入新引擎找不到 match）。
    # 解决：检测到引擎已在运行时，先尝试干净释放；释放失败（有游戏在跑）则
    # 跳过 start()，复用现有引擎，让玩家可以继续游戏。
    if boot.is_engine_running():
        if boot.LGTBot_ElainaBot.release_bot_if_not_processing_games():
            boot.mark_engine_running(False)
            log.info('🔁 [热重载] 旧引擎已干净释放，将重新初始化')
        else:
            log.warning('=' * 60)
            log.warning('🔁 [热重载] 检测到引擎已在运行 + 有进行中的游戏')
            log.warning('   ▸ 已跳过引擎重启，复用现有引擎，玩家可继续游戏')
            log.warning('   ▸ 注意：本次不刷新游戏列表 / 配置项；待所有游戏结束后')
            log.warning('     再次保存任意文件触发热重载，将自动完成完整重启')
            log.warning('=' * 60)
            _state.started = True   # 让新 dispatcher 正常派发消息
            return

    # 检查游戏目录是否存在已编译的游戏 .so
    if not os.path.isdir(boot.GAME_PATH):
        log.error('=' * 60)
        log.error(f'游戏插件目录不存在: {boot.GAME_PATH}')
        log.error('请先在 plugins/LGTBot_ElainaBot/ 下执行 bash build.sh 完成编译')
        log.error('=' * 60)
        return
    game_count = sum(
        1 for d in os.listdir(boot.GAME_PATH)
        if os.path.isfile(os.path.join(boot.GAME_PATH, d, 'libgame.so'))
    )
    if game_count == 0:
        log.error('=' * 60)
        log.error(f'未在 {boot.GAME_PATH} 下发现任何 libgame.so')
        log.error('请检查 build.sh 是否带 --no-games 关闭了游戏编译')
        log.error('=' * 60)
        return

    log.info(f'初始化 LGTBot 引擎: 游戏数={game_count}, db={boot.DB_PATH}, conf={boot.CONF_PATH}')
    ok = boot.LGTBot_ElainaBot.start(
        boot.GAME_PATH, boot.DB_PATH, boot.CONF_PATH, boot.IMG_PATH, admins,
        callbacks.cb_get_user_name, callbacks.cb_get_user_avatar_url,
        callbacks.cb_send_text_message, callbacks.cb_send_image_message,
        callbacks.cb_match_event,
    )
    if not ok:
        log.error('LGTBot 引擎启动失败 (查看上方 stderr 输出)')
        return

    boot.mark_engine_running(True)
    _state.started = True
    log.info('✅ LGTBot 引擎已就绪')


@on_unload
async def _teardown():
    # userdb：先停 flusher，再同步 flush 一次 pending 到盘，最后关连接。
    # 任何 SQLite 异常吞掉，避免阻断后续 webui.unregister / 引擎释放
    try:
        userdb.stop_flusher()
        userdb.flush_now()
    except Exception as e:
        log.warning(f'userdb 关停异常: {e}')

    # 注销 Web 面板页面（无论引擎状态如何）
    try:
        webui.unregister()
    except Exception:
        pass

    if not _state.started or not boot.LGTBOT_AVAILABLE:
        return
    if boot.LGTBot_ElainaBot.release_bot_if_not_processing_games():
        _state.started = False
        boot.mark_engine_running(False)
        log.info('LGTBot 引擎已安全关闭')
    else:
        # 关键：保留 mark_engine_running(True)，下次 @on_load 据此跳过 start()
        log.warning('存在进行中的游戏 —— 引擎未释放，热重载后将复用旧引擎以保持游戏状态')

    # 注意:不在此处调用 userdb.close() —— 跨重载共享的 SQLite 连接放在
    # boot._get_persistent() 字典里 (见 userdb.py::_get_state),关掉它会让
    # 旧 callbacks → 旧 userdb 路径在重载窗口里查询昵称全部返空,游戏内
    # 玩家昵称会突然变成截断 openid。连接的生命周期与进程绑定即可:
    # `os.execv` 整进程重启时 OS 自动关 fd,WAL checkpoint 走 sqlite 自身
    # 的 `PRAGMA journal_mode = WAL` 写时机制即可,无需显式 close。
