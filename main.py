#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LGTBot × ElainaBot 集成插件 (QQ Official Bot) —— 入口文件

各功能拆分到 mod/ 子模块（详见 mod/__init__.py），本文件只负责：

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
    'version': '2.12.6',
    'github': 'https://github.com/tiedanGH/LGTBot_ElainaBot',
}

import os
import sys
import asyncio

from core.plugin.decorators import on_load, on_unload
from core.plugin import context as _ctx_mod
from core.base.logger import get_logger, PLUGIN

# ──────── 关键步骤：捕获 PluginContext ────────────────────────────────────
# PluginManager 只在执行本文件顶层代码期间设置 _ctx_mod.ctx，调 @on_load 前已复位为 None，所以必须在模块顶层捕获
from plugins.LGTBot_ElainaBot.mod import state as _state
_state.plugin_ctx = _ctx_mod.ctx

# ──────── 触发各子模块加载 ────────────────────────────────────────────────
# 顺序敏感：boot 必须最先（处理 C++ 扩展导入 + chdir + RTLD_GLOBAL 副作用），
# 其他模块依赖 boot.LGTBot_ElainaBot / boot.BUILD_DIR / boot.LGTBOT_AVAILABLE 等
from plugins.LGTBot_ElainaBot.mod import boot              # noqa: F401  C++ 引擎与路径
from plugins.LGTBot_ElainaBot.mod.webui import main as webui  # noqa: F401  Web 面板侧边栏页面入口
# page_logs 兼做日志缓冲(callbacks / dispatcher 调 log_incoming / log_outgoing),显式早 import 确保数据层先就位
from plugins.LGTBot_ElainaBot.mod.webui import page_logs  # noqa: F401
from plugins.LGTBot_ElainaBot.mod.webui import page_dashboard as _page_dashboard  # 启动更新自检入口
from plugins.LGTBot_ElainaBot.mod import dispatcher        # noqa: F401  @handler 注册（消息派发 + INTERACTION）
from plugins.LGTBot_ElainaBot.mod import callbacks         # C++ 回调（被 LGTBot_ElainaBot.start 注入）
from plugins.LGTBot_ElainaBot.mod import config as _config
from plugins.LGTBot_ElainaBot.mod import backup as _backup            # noqa: F401  数据库备份(自动 + 手动 + 恢复)
from plugins.LGTBot_ElainaBot.mod import log_attribution as _log_attribution  # noqa: F401

log = get_logger(PLUGIN, 'LGTBot')


# ──────── 生命周期 ────────────────────────────────────────────────────────

@on_load
async def _setup():
    # 给 MessageSender 的 push 打日志归属补丁(幂等);放在最早 —— 任何 send_to_* 之前必须就位
    _log_attribution.install_once()

    # 预存 execv 自启参数到 C++ 桥接层 —— SIGABRT handler 在 heap 已坏时
    # 没法做任何分配,必须提前用 fixed buffer 固化 sys.executable + sys.argv。
    if boot.LGTBOT_AVAILABLE:
        try:
            boot.LGTBot_ElainaBot.set_restart_args(sys.executable, list(sys.argv))
        except Exception as e:
            log.warning(f'set_restart_args 失败,SIGABRT 兜底自启不可用: {e}')

    # 注册 Web 面板拓展页（无论 LGTBot 是否可用，让用户先能看到日志页）
    webui.register()

    # 桥接层更新自检放在 LGTBOT_AVAILABLE 早退之前,引擎没编译好也照常提示更新。
    try:
        _page_dashboard.schedule_startup_update_check()
    except Exception as e:
        log.warning(f'调度启动更新自检失败: {e}')

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


    # 计划重启的「自动重启」watcher:热重载后若模式仍开启且旧 task 已死,补拉起
    try:
        dispatcher.ensure_auto_restart_watcher_on_load()
    except Exception as e:
        log.warning(f'恢复自动重启 watcher 失败: {e}')

    # 上一轮 std::terminate 路径若留下 pending_apology_* marker,在干净进程里补发道歉 + 通知群推送;失败不阻断引擎启动。
    try:
        callbacks.recover_pending_apologies()
    except Exception as e:
        log.warning(f'扫描待补发道歉异常: {e}')

    # 崩溃死循环熔断:若 abort 类崩溃在短窗口内反复 execv 自启,暂停启动引擎,
    # 主框架保持运行 + 告警,避免无限 execv 烧 CPU。修复后热重载自动复位重试。
    try:
        if callbacks.check_crash_loop():
            return
    except Exception as e:
        log.warning(f'崩溃死循环检测异常: {e}')

    # ── 热重载检测：上一轮的引擎可能还活着 ─────────────────────────────────
    # 再调 start() 会覆盖 g_bot_core，进行中的游戏全部失联；所以先尝试干净释放，释放失败（有游戏在跑）就跳过 start() 复用现有引擎。
    if boot.is_engine_running():
        if boot.LGTBot_ElainaBot.release_bot_if_not_processing_games():
            boot.mark_engine_running(False)
            log.info('🔁 [热重载] 旧引擎已成功释放，将重新初始化')
        else:
            log.warning('=' * 60)
            log.warning('🔁 [热重载] 检测到引擎已在运行 + 有进行中的游戏')
            log.warning('   ▸ 已跳过引擎重启，复用现有引擎，玩家可继续游戏')
            log.warning('   ▸ 注意：本次不刷新游戏列表 / 配置项，需引擎重启后才生效')
            log.warning('=' * 60)
            _state.started = True   # 让新 dispatcher 正常派发消息
            # 复用旧引擎也属于热重载成功,触发备份检查(若距上次 > 24h 才真备)
            _backup.schedule_on_load_check()
            return

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

    # 距上次备份 > 24h 时自动备份一次;只在每次 reload / restart 时检查,没有常驻定时器。
    _backup.schedule_on_load_check()


@on_unload
async def _teardown():
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
