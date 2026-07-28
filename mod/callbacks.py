#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""C++ 引擎回调实现（由 LGTBot_ElainaBot.so 调用，运行在 C++ 工作线程）

入口函数（同步）：
  · cb_get_user_name(uid)            返回昵称
  · cb_get_user_avatar_url(uid)      返回头像 URL
  · cb_send_text_message(...)        投递文本发送任务（fire-and-forget,瞬时返回）
  · cb_send_image_message(...)       投递图片发送任务（fire-and-forget,瞬时返回）

发送流程（跑在 asyncio loop,per-target Lock 串行）：
  · _serialized_text_send            Lock → _send_text_quota_managed → 消费教学标记
  · _serialized_image_send           Lock → _send_image_quota_managed → 消费教学标记
  · _send_text_quota_managed         配额管理 + 自动追加刷新按钮
  · _send_image_quota_managed        配额管理 + 上传 + media 字段（支持 event_id）

设计要点：cb_send_text/image_message 不再阻塞 C++ 调用线程 —— lgtbot 的 read
thread 在 OnPost 里只持 Match.mutex_ 几十 µs。修复了等刷新按钮 15s 期间 read
thread 持锁 → 玩家新指令排队 → 释放锁后紧接着 OnGameOver 把 child_in_ 置 NULL
→ 排队那条 SendExecute → WriteFrame(NULL) → SIGSEGV 的链式 race。
"""

from __future__ import annotations
import asyncio
import concurrent.futures
import os
import sys
import time

from core.base.logger import get_logger, PLUGIN
from . import state, quota, helpers, boot, uploader, userinfo, buttons, log_attribution, metrics
from .webui import page_logs

log = get_logger(PLUGIN, 'LGTBot')


# ──────── lgtbot 段错误恢复(C++ 桥接层 SigSegvHandler → 这里) ─────────────
# 一旦 lgtbot 内部触发 SIGSEGV/SIGBUS,bridge 的 wrapper 用 sigsetjmp/siglongjmp
# 把控制权拽回 Python,然后调本函数善后。注意此时 lgtbot 进程内状态损坏
# (mutex/heap/pipe 都可能是脏的),所以这里**不再调任何 lgtbot 函数**,只做
# Python 侧的事:发日志 + 给玩家道歉 + 调度 30s 后整进程 execv。

_LGTBOT_CRASH_DELAY_S = 30.0       # 倒计时 execv;给 framework 其他清理留 buffer
# 工作线程被阻塞等道歉 / 通知 HTTP 完成的最长秒数 —— 必须趁线程还没退出去
# 触发 SIGABRT 之前把"重要的事"发完。HTTP 往返通常 100–500ms,8s 是大头富余。
_CRASH_SEND_TIMEOUT_S = 8.0
_CRASH_APOLOGY_MD = (
    '## 💥 游戏模块发生致命错误\n'
    '\n'
    'LGT-Bot 引擎发生未预料的崩溃，**当前游戏已无法继续进行**。\n'
    '进程将在 **30 秒**后自动重启，所有进行中的对局会丢失。\n'
    '\n'
    '崩溃报告已自动转发至官方群，非常抱歉给您带来不便，我们会尽快修复 🌹'
)
# 补发路径(OnCxxTerminate marker)专用文案:发出时进程已经重启完成。
_CRASH_APOLOGY_MD_BELATED = (
    '## 💥 游戏模块发生致命错误\n'
    '\n'
    'LGT-Bot 引擎发生未预料的崩溃，**进行中的对局已丢失**。\n'
    '机器人已自动重启恢复服务，现在可以继续使用 ✅\n'
    '\n'
    '崩溃报告已自动转发至官方群，非常抱歉给您带来不便，我们会尽快修复 🌹'
)
# 受牵连对局的中断通知 —— 崩溃源在别处,本群/私聊的对局被连累中断。
_CRASH_COLLATERAL_MD = (
    '## 💥 对局意外中断\n'
    '\n'
    'LGT-Bot 引擎因**其他游戏**发生崩溃，**所有进行中的对局已丢失**，无法继续进行。\n'
    '进程正在自动重启，请稍后**重新开始游戏**。\n'
    '\n'
    '非常抱歉给您带来不便，我们会尽快修复 🌹'
)

# 严重问题通知群 openid —— 由 config.py::_apply_runtime_tunables 按 yaml 配置覆盖。
# 空字符串 = 不推送。设了的话,引擎崩溃时除了给玩家发道歉,还向此群主动推送
# 一条崩溃报告。通常填管理员监控的全量群 —— 该群在 QQ 后台开了全量推送权限,
# bot 才能向它走主动消息(没 msg_id 引用)。
CRASH_NOTIFY_GROUP: str = ''

# 私信主动直推资格 —— 两个变量均由 config.py::_apply_runtime_tunables 按 yaml 的
# sandbox_dm_users 覆盖:
#   · 列表恰好为 ['all'] → DM_PUSH_ALL=True,对**全部用户**私信直推。
#     官方现已默认允许 bot 向好友推送主动私信(默认开启,用户可在权限设置中自行关闭)。
#   · 其他 → 白名单老语义:仅列表内用户(沙箱测试号)直推,其余私信在无有效
#     msg_id 时丢弃。老逻辑保留 —— 官方将来收回全员推送权限时改回配置即可。
# 两种模式下「直推资格」都不跳过被动配额:前 5 条照常 msg_id 被动回复,
# 配额耗尽后才走主动消息(见 _send_text/image_quota_managed)。
SANDBOX_DM_USERS: frozenset = frozenset()
DM_PUSH_ALL: bool = False


def _is_sandbox_dm(target_id: str, is_uid: bool) -> bool:
    """私信目标是否具备「配额耗尽后主动直推」资格(all 模式 = 所有人)。"""
    return is_uid and (DM_PUSH_ALL or target_id in SANDBOX_DM_USERS)
# 信号编号 → 名称,日志里更可读。
# 数字 key 对应 SigSegvHandler 路径(C++ bridge 直接传 int);字符串 key
# 对应 OnCxxTerminate 写入 marker 文件里的 sig=<kind> 字段(目前只有
# cxx_terminate 一种,std::terminate / uncaught C++ exception 路径)。
_SIG_NAMES = {
    6: 'SIGABRT', 7: 'SIGBUS', 11: 'SIGSEGV',
    'cxx_terminate': 'C++ 异常未捕获',
    'sigabrt': 'SIGABRT (堆损坏 / double-free)',
}
_crash_handled = False             # 防多线程并发崩溃时重复触发善后


def cb_lgtbot_crashed(uid: str, gid: str, is_uid: bool, msg: str, sig: int) -> None:
    """C++ bridge → Python:lgtbot 触发 SIGSEGV/SIGBUS 被 wrapper 捕获恢复后调本函数。

    被调时 GIL 已由 wrapper 抢回(``PyGILState_Ensure``),Python C API 可用。
    实际工作放到 asyncio loop 上跑 —— 这里只做最少同步操作,然后调度异步善后。
    """
    global _crash_handled
    if _crash_handled:
        # 多线程并发崩溃只处理第一条 —— 后面那些都是同一波连锁反应,30s 内
        # 进程就会被 execv 替换,先把噪音压下去
        return
    _crash_handled = True

    sig_name = _SIG_NAMES.get(sig, f'sig{sig}')
    # 单行 target,仅供本地日志可读;通知群侧由 _try_send_crash_notification 用
    # uid/gid/is_uid 自行排版成多行,见下面的安全说明。
    target = (f'用户 {uid}' if is_uid else f'群聊 {gid} 用户 {uid}')
    preview = (msg or '')[:80].replace('\n', ' ')

    # 关键 ERROR 日志 —— 主框架 WebUI 消息日志 / 全局日志都能看到
    log.error('=' * 60)
    log.error(f'💥 LGTBot 引擎崩溃 ({sig_name})')
    log.error(f'   触发源: {target}')
    log.error(f'   消息内容: {preview!r}')
    # C++ bridge (SigSegvHandler → DumpCrashToFile) 已经把栈 dump 落盘到
    # <plugin_dir>/LGTBot_CRASH_DUMPS/crash_<sec>_<pid>_<tid>.log,
    # 管理员去那目录看最新文件就能拿到 backtrace + 信号上下文。
    crash_dir = os.path.join(boot.PLUGIN_DIR, 'LGTBot_CRASH_DUMPS')
    log.error(f'   栈 dump 目录: {crash_dir}/ (按 mtime 排序看最新)')
    log.error(f'   进程将在 {_LGTBOT_CRASH_DELAY_S:.0f}s 后 os.execv 自启，所有对局丢失')
    log.error('=' * 60)

    # 立刻挂掉引擎标记,避免 30s 重启窗口内 dispatcher 继续派发到已坏的 lgtbot
    state.started = False
    try:
        boot.mark_engine_running(False)
    except Exception:
        pass

    # 指标:崩溃累计(live 信号路径)。record 同步写盘且永不抛
    # 在腐败 heap上属 best-effort,但发生在 execv 之前,通常能成功落盘。
    metrics.record_crash(sig_name)

    # 异步善后:发道歉 + 倒计时 + execv。C++ wrapper 即将 return,不能在这里阻塞。
    loop = state.event_loop
    if loop is None or loop.is_closed():
        # 没 loop 就只能立即退出让 supervisor 重启 —— 道歉就送不出了,但
        # 不至于卡死。
        log.error('asyncio loop 不可用，直接 os.execv')
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            log.error(f'os.execv 失败，需 supervisor 兜底: {e}')
        return
    # preview(用户原文)只用于本地 log.error
    # admin 凭 target + 时间戳去服务端日志反查 preview 原文即可。
    msg_len = len(preview)

    # ── Phase 1: **阻塞当前工作线程**等道歉 + 通知 HTTP 发完 ───────────────────
    # 关键设计:cb_lgtbot_crashed 跑在出错的工作线程上,该线程一旦 return 就
    # 进入退出流程,极可能在 glibc tcache_thread_shutdown 撞坏 heap 触发 SIGABRT
    # (用户实测过的 case)。我们必须趁工作线程还活着,把"重要的事" —— 尤其
    # **崩溃报告推送给管理员通知群** —— 同步发完。
    # 用 run_coroutine_threadsafe + Future.result(timeout=) 实现跨线程阻塞等待;
    # 超时直接放弃当前未完成的发送,避免无限卡死。
    try:
        send_fut = asyncio.run_coroutine_threadsafe(
            _send_crash_messages(uid, gid, is_uid, sig_name, msg_len), loop)
        send_fut.result(timeout=_CRASH_SEND_TIMEOUT_S)
    except concurrent.futures.TimeoutError:
        log.warning(f'崩溃消息发送超时(>{_CRASH_SEND_TIMEOUT_S:.0f}s),仍继续重启流程')
    except Exception as e:
        log.warning(f'崩溃消息发送异常,仍继续重启流程: {e}')

    # ── Phase 2: 调度 30s 后整进程 execv (不阻塞,asyncio loop 跑) ────────────
    # 此时道歉 + 通知 HTTP 已经发出或失败,工作线程可以返回了。30s buffer 留给
    # 主框架其他清理工作(WebUI 日志 flush、框架写队列落盘等)。中途若工作线程退出
    # 触发 SIGABRT,C++ 桥接层的 SigAbrtHandler 会用预存的 execv 参数立即重启。
    try:
        asyncio.run_coroutine_threadsafe(
            _post_send_countdown(sig_name), loop)
    except Exception as e:
        log.error(f'调度重启倒计时失败,直接 os.execv: {e}')
        os.execv(sys.executable, [sys.executable] + sys.argv)


async def _send_crash_messages(uid: str, gid: str, is_uid: bool,
                               sig_name: str, msg_len: int) -> None:
    """同步阻塞路径:并发发道歉 + 通知,worker 线程通过 ``Future.result`` 等完。

    用 ``asyncio.gather(*, return_exceptions=True)`` 保证一边失败不影响另一边
    (尤其通知群推送是用户最重视的,不能被道歉发送失败牵连)。再加一层
    ``asyncio.wait_for`` 做内部超时兜底,免得 HTTP hung 把整个 future 拖到外层
    8s 超时才被砍。
    """
    coros = []
    # 优先级:通知群 > 道歉。先 append 表示在 gather 里优先调度,实际 HTTP 并发。
    notify_group = CRASH_NOTIFY_GROUP
    if notify_group:
        coros.append(_try_send_crash_notification(
            notify_group, sig_name, uid, gid, is_uid, msg_len))
    target_id = uid if is_uid else gid
    if target_id:
        coros.append(_try_send_crash_apology(target_id, is_uid))
    # fan-out:给其他进行中对局(群 / 私聊)发中断通知,去重崩溃源(它已收源道歉)。
    crash_id = uid if is_uid else gid
    active = {(r['target_id'], r['is_uid']) for r in state.active_matches.values()}
    collateral = _collateral_targets(active, crash_id, is_uid)
    if collateral:
        log.error(f'💥 向 {len(collateral)} 个进行中对局推送中断通知')
        for tid, tid_is_uid in collateral:
            coros.append(_send_collateral_notice(tid, tid_is_uid))
    if not coros:
        return
    try:
        # 0.5s 提前量给外层 future 框架开销;留点 slack 比触发外层 timeout 干净
        await asyncio.wait_for(
            asyncio.gather(*coros, return_exceptions=True),
            timeout=_CRASH_SEND_TIMEOUT_S - 0.5)
    except asyncio.TimeoutError:
        log.warning('崩溃消息内部 wait_for 超时,任务已取消')


async def _post_send_countdown(sig_name: str) -> None:
    """道歉/通知都已发完,倒计时再 execv;留 buffer 给框架其他清理。"""
    await asyncio.sleep(_LGTBOT_CRASH_DELAY_S)
    log.error(f'🔁 {_LGTBOT_CRASH_DELAY_S:.0f}s 倒计时结束，执行 os.execv 自启 (因 {sig_name})')
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        # execv 罕见失败(sys.executable 失踪等),supervisor 仍可兜底
        log.error(f'os.execv 失败，等待 supervisor 兜底: {e}')


async def _try_send_crash_apology(target_id: str, is_uid: bool,
                                  *, is_belated: bool = False) -> None:
    """走标准发送通道把道歉送达 —— 复用现有 quota/sender 设施。

    ``is_belated=True`` 走 ``_CRASH_APOLOGY_MD_BELATED`` —— 这条路径下进程已经
    重启完成,「30 秒后自动重启」不再适用,改为「已恢复服务」。SIGSEGV 路径
    保持默认 False,文案不变。

    SIGSEGV 路径下注意不挂额外按钮 —— 进程马上要 execv 重启,任何 callback 都
    会落空;补发路径下进程已稳定,挂按钮其实安全,但为了文案一致这里也共用
    同一组 build_support_buttons(那组本身是 link 按钮,不依赖回调)。
    """
    md = _CRASH_APOLOGY_MD_BELATED if is_belated else _CRASH_APOLOGY_MD
    try:
        page_logs.log_outgoing(target_id, is_uid, md)
        await _send_text_quota_managed(target_id, is_uid, md,
                                       buttons.build_support_buttons())
    except Exception as e:
        log.warning(f'崩溃道歉发送失败 ({target_id}): {e}')


def _collateral_targets(active: set, crash_id: str, crash_is_uid: bool) -> set:
    """从进行中对局缓存里选出要发中断通知的目标 —— 剔除崩溃源(群 / 私聊同理,它已单独收到源道歉)。返回快照,不随原缓存后续变动。"""
    out = set(active)
    out.discard((crash_id, crash_is_uid))
    return out


async def _send_collateral_notice(target_id: str, is_uid: bool) -> None:
    """给受牵连的进行中对局发中断通知。被动未超额发被动,超额**直接主动、不等刷新**
    (崩溃即将 execv,没时间等 15s;普通群主动会被 QQ 拒、全量群 / 私聊可达,尽力送达)。
    附官方群 / 反馈 link 按钮(不依赖回调,execv 前也安全)。"""
    try:
        key = helpers.target_key(target_id, is_uid)
        consumed = quota.try_consume_ref(key)
        if consumed:
            ref_type, ref_value, _count, ref_appid = consumed
            sender, kwargs = helpers.get_sender(ref_appid), {ref_type: ref_value}
        else:
            sender, kwargs = helpers.get_sender(''), {}
        if sender is None:
            return
        page_logs.log_outgoing(target_id, is_uid, _CRASH_COLLATERAL_MD)
        with log_attribution.mark_outbound():
            if is_uid:
                await sender.send_to_user(target_id, _CRASH_COLLATERAL_MD,
                                          buttons=buttons.build_support_buttons(), **kwargs)
            else:
                await sender.send_to_group(target_id, _CRASH_COLLATERAL_MD,
                                           buttons=buttons.build_support_buttons(), **kwargs)
    except Exception as e:
        log.warning(f'对局中断通知发送失败 ({target_id}): {e}')


async def _try_send_crash_notification(notify_group: str, sig_name: str,
                                       uid: str, gid: str, is_uid: bool,
                                       msg_len: int,
                                       *, is_belated: bool = False) -> None:
    """向严重问题通知群推送一条**主动消息**汇报崩溃。

    用 ``sender.send_to_group(group_id, content)`` 不带 ``msg_id``/``event_id``
    走 push API —— 仅在通知群 QQ 后台给本 bot 开了「全量推送」权限时能落地。
    没权限就会被 QQ 拒,这里只打 warning 不报错,30s 后 execv 照常进行。

    ``is_belated=True`` 走补发路径(OnCxxTerminate marker):此时进程已经重启
    完成,把「进程将在 N 秒后自动重启」这行替换为「机器人已自动重启恢复服务」,
    避免管理员误以为还在等。SIGSEGV 路径默认 False,文案不变。

    **安全约束:** 本消息走 bot 自己的 appid 发出,QQ 风控同样适用 —— 因此
    **不把触发崩溃的用户原文(preview)拼进 markdown**,避免用户故意发违规/
    敏感内容借崩溃路径让 bot 转发,触发风控扣分甚至封号。只展示机械生成、
    bot 完全可控的字段(信号名 / openid / 长度数字),全部塞进单个代码块里,
    QQ markdown 不会把里面的内容当指令解析。完整 preview 在服务端
    ``log.error`` 里,管理员凭 target + 时间戳去 WebUI「消息日志」或
    framework 全局日志反查即可,本地查完全无风险。
    """
    sender = helpers.get_sender('')
    if sender is None:
        log.warning('无可用 sender，跳过崩溃通知群推送')
        return

    # 触发源块:私聊单行,群聊两行(群号 + 用户号各占一行,提升可读性)
    if is_uid:
        target_block = f'用户 {uid}'
    else:
        target_block = f'群聊 {gid}\n用户 {uid}'

    if is_belated:
        status_line = '机器人已自动重启恢复服务 ✅'
    else:
        status_line = f'进程将在 **{_LGTBOT_CRASH_DELAY_S:.0f} 秒**后自动重启···'

    md = (
        '$$\\textcolor{red}{\\Huge\\text{错误推送}}$$'
        '\n'
        '## 💥 LGT-Bot 引擎崩溃\n'
        '\n'
        '> 引擎发生致命错误导致程序崩溃，所有进行中的对局丢失\n'
        '\n'
        '```崩溃信息\n'
        f'- 信号: {sig_name}\n'
        '- 触发源:\n'
        f'{target_block}\n'
        f'- 消息长度: {msg_len} 字符（详见服务端日志）\n'
        '```\n'
        '\n'
        f'{status_line}\n'
        '\n'
        '> 💡 此消息为自动推送，请尽快联系开发者排查修复'
    )
    page_logs.log_outgoing(notify_group, False, md)
    try:
        with log_attribution.mark_outbound():
            await sender.send_to_group(notify_group, md)
    except Exception as e:
        log.warning(f'崩溃通知群推送失败 ({notify_group}): {e}')


# ──────── 上一轮 C++ terminate 路径补发道歉/通知 ─────────────────────────
# 触发流:lgtbot 内部抛 std::bad_alloc 等 C++ 异常未被 catch → OnCxxTerminate
# 在 LGTBot_ElainaBot.cc 内执行:
#   1. async-signal-safe 写 marker 文件
#      `<plugin_dir>/LGTBot_CRASH_DUMPS/pending_apology_<sec>_<pid>_<tid>.txt`
#   2. execv 重启整进程
# 新进程 @on_load 调本模块 ``recover_pending_apologies``,异步补发道歉 + 通知,
# 然后删 marker。
#
# 与 SigSegvHandler → cb_lgtbot_crashed 路径互补:那条路径是「崩溃当下同步发」,
# 这条是「崩溃后重启再补发」—— 因为 OnCxxTerminate 跑在 c++ 异常上下文里,
# Python C API / heap 都不可信,只能落地到文件再让干净进程接力。
_PENDING_APOLOGY_PREFIX = 'pending_apology_'
_PENDING_APOLOGY_SUFFIX = '.txt'
# 启动后延迟 5s 再补发 —— 让 @on_load 完成、bot manager 就绪、网络通畅
_BELATED_APOLOGY_DELAY_S = 5.0

# ── 崩溃死循环熔断 ─────────────────────────────────────────────────────────
# C++ 侧 abort-class handler(SigAbrtHandler / OnCxxTerminate)在 execv 前往
# `LGTBot_CRASH_DUMPS/abort_restart_history` 追加一行重启时间戳。若 heap 腐败
# 是确定性的,会每次重启又立刻 abort → 紧凑 execv 死循环。check_crash_loop()
# 在 @on_load 读历史:窗口内重启 ≥ 阈值则判定死循环,暂停启动引擎(主框架仍
# 运行)+ 告警,并清空历史 —— 人工修复后存盘触发热重载即自动复位重试。
_ABORT_HISTORY_NAME = 'abort_restart_history'
_CRASH_LOOP_WINDOW_S = 120.0       # 统计窗口
_CRASH_LOOP_THRESHOLD = 4          # 窗口内重启达到该次数即熔断


def recover_pending_apologies() -> None:
    """启动时调一次。扫 LGTBot_CRASH_DUMPS/pending_apology_*.txt,有就异步补发。

    必须在 ``state.event_loop`` 已就绪后调用(我们的协程要走它)。
    不阻塞调用方 —— 每个 marker 是独立的 ``run_coroutine_threadsafe`` 投递。
    """
    dump_dir = os.path.join(boot.PLUGIN_DIR, 'LGTBot_CRASH_DUMPS')
    if not os.path.isdir(dump_dir):
        return
    try:
        names = sorted(
            n for n in os.listdir(dump_dir)
            if n.startswith(_PENDING_APOLOGY_PREFIX) and n.endswith(_PENDING_APOLOGY_SUFFIX)
        )
    except OSError as e:
        log.warning(f'扫描崩溃 marker 目录失败 ({dump_dir}): {e}')
        return
    if not names:
        return

    loop = state.event_loop
    if loop is None or loop.is_closed():
        log.warning(f'event_loop 未就绪,延后补发 {len(names)} 条道歉(marker 保留)')
        return

    log.warning('=' * 60)
    log.warning(f'⏪ 发现 {len(names)} 个上一轮 C++ 异常未捕获的待补发道歉,启动后将异步补发')
    log.warning('=' * 60)
    for name in names:
        path = os.path.join(dump_dir, name)
        try:
            info = _parse_apology_marker(path)
        except Exception as e:
            log.error(f'解析崩溃 marker 失败 {name}: {e},移到 .bad/')
            _quarantine_marker(path)
            continue
        try:
            asyncio.run_coroutine_threadsafe(_belated_apology(path, info), loop)
        except Exception as e:
            log.error(f'调度补发任务失败 ({name}): {e}')


def _parse_apology_marker(path: str) -> dict:
    """解析 KV + length-prefix 格式;返回 {sig, is_uid(bool), ts, uid, gid, msg}。

    格式约定见 ``LGTBot_ElainaBot.cc::WriteApologyMarker`` 注释。``*_len=N``
    后紧跟的一行 ``<name>=<N 字节原文>\\n``,N 字节内可含任意字节(换行 / 二进制)。
    """
    with open(path, 'rb') as f:
        raw = f.read()
    result: dict = {}
    i = 0
    n = len(raw)
    while i < n:
        nl = raw.find(b'\n', i)
        if nl < 0:
            break
        line = raw[i:nl]
        i = nl + 1
        eq = line.find(b'=')
        if eq < 0:
            continue
        key = line[:eq].decode('ascii', errors='replace')
        value = line[eq + 1:]
        if key.endswith('_len'):
            try:
                length = int(value)
            except ValueError:
                continue
            name = key[:-4]
            prefix = name.encode('ascii') + b'='
            if not raw[i:].startswith(prefix):
                continue
            i += len(prefix)
            payload = raw[i:i + length]
            i += length
            if i < n and raw[i:i + 1] == b'\n':
                i += 1
            result[name] = payload.decode('utf-8', errors='replace')
        else:
            result[key] = value.decode('utf-8', errors='replace')
    # is_uid 规范化为 bool
    result['is_uid'] = (result.get('is_uid', '0') == '1')
    return result


async def _belated_apology(marker_path: str, info: dict) -> None:
    """异步补发道歉 + 通知。无论成功失败都删 marker,避免反复打扰玩家/管理员。"""
    await asyncio.sleep(_BELATED_APOLOGY_DELAY_S)
    try:
        uid: str = info.get('uid', '') or ''
        gid: str = info.get('gid', '') or ''
        is_uid: bool = bool(info.get('is_uid', False))
        msg: str = info.get('msg', '') or ''
        sig_kind: str = info.get('sig', 'cxx_terminate') or 'cxx_terminate'
        sig_name = _SIG_NAMES.get(sig_kind, sig_kind)
        # 指标:崩溃累计(marker 路径,恰一次 —— 与 finally 的 marker 删除同生命周期);ts 用 marker 里记录的真实崩溃时刻
        try:
            _marker_ts = int(info.get('ts') or 0)
        except (TypeError, ValueError):
            _marker_ts = 0
        metrics.record_crash(sig_name, ts=_marker_ts or None)
        target = (f'用户 {uid}' if is_uid else f'群聊 {gid} 用户 {uid}')
        preview = msg[:80].replace('\n', ' ')
        msg_len = len(preview)

        log.error('=' * 60)
        log.error(f'⏪ 补发上次崩溃道歉 ({sig_name})')
        log.error(f'   触发源: {target}')
        log.error(f'   消息内容: {preview!r}')
        log.error(f'   marker: {os.path.basename(marker_path)}')
        log.error('=' * 60)

        coros = []
        notify_group = CRASH_NOTIFY_GROUP
        if notify_group:
            coros.append(_try_send_crash_notification(
                notify_group, sig_name, uid, gid, is_uid, msg_len,
                is_belated=True))
        target_id = uid if is_uid else gid
        if target_id:
            coros.append(_try_send_crash_apology(target_id, is_uid,
                                                 is_belated=True))
        if coros:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*coros, return_exceptions=True),
                    timeout=_CRASH_SEND_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.warning(f'补发道歉超时 (>{_CRASH_SEND_TIMEOUT_S:.0f}s): {os.path.basename(marker_path)}')
    except Exception as e:
        log.error(f'补发道歉异常: {e}')
    finally:
        try:
            os.remove(marker_path)
        except OSError as e:
            log.warning(f'删除 marker 失败 {marker_path}: {e}')


def _quarantine_marker(path: str) -> None:
    """格式损坏的 marker 改名 .bad,避免下次启动反复尝试解析。"""
    try:
        os.rename(path, path + '.bad')
    except OSError:
        pass


def check_crash_loop() -> bool:
    """读 abort_restart_history,判断是否进入崩溃死循环。返回 True = 已熔断。

    调用方(``main.py @on_load``)在 True 时应**跳过启动 LGTBot 引擎**,让主框架
    保持运行但暂停游戏功能,避免无限 execv 烧 CPU。

    熔断时清空历史 —— 引擎不启动 ⇒ 无 lgtbot 线程 ⇒ 不会再 abort,死循环被打断;
    管理员修复后在 Web 面板存盘触发热重载,@on_load 再跑时历史已空,自动重试启动。
    必须在 ``state.event_loop`` 就绪后调用(告警协程要走它)。
    """
    path = os.path.join(boot.PLUGIN_DIR, 'LGTBot_CRASH_DUMPS', _ABORT_HISTORY_NAME)
    if not os.path.isfile(path):
        return False
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            raw = f.read()
    except OSError as e:
        log.warning(f'读取 abort_restart_history 失败: {e}')
        return False

    now = time.time()
    recent = []
    for line in raw.split('\n'):
        line = line.strip()
        if not line:
            continue
        try:
            t = float(line)
        except ValueError:
            continue
        if 0 <= now - t <= _CRASH_LOOP_WINDOW_S:
            recent.append(t)

    if len(recent) >= _CRASH_LOOP_THRESHOLD:
        # 熔断:清空历史(下次热重载重新计数),告警,返回 True
        try:
            os.remove(path)
        except OSError:
            pass
        log.critical('=' * 60)
        log.critical(f'🛑 检测到 LGTBot 崩溃死循环：{_CRASH_LOOP_WINDOW_S:.0f}s 内自动重启 {len(recent)} 次')
        log.critical('   ▸ 已暂停启动 LGTBot 引擎（主框架保持运行），避免无限 execv')
        log.critical('   ▸ 查看 LGTBot_CRASH_DUMPS/ 下最新 crash_*.log 排查根因')
        log.critical('   ▸ 修复后在 Web 面板保存任意配置触发热重载即可恢复引擎')
        log.critical('=' * 60)
        loop = state.event_loop
        if loop is not None and not loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(
                    _alert_crash_loop_tripped(len(recent)), loop)
            except Exception as e:
                log.warning(f'调度崩溃熔断告警失败: {e}')
        return True

    # 未熔断:把历史裁剪成 recent 写回,防止文件无限增长
    try:
        with open(path, 'w', encoding='utf-8') as f:
            if recent:
                f.write('\n'.join(str(int(t)) for t in recent) + '\n')
    except OSError as e:
        log.warning(f'裁剪 abort_restart_history 失败: {e}')
    return False


async def _alert_crash_loop_tripped(count: int) -> None:
    """向通知群推送一条「崩溃死循环已熔断」主动消息。无通知群 / 无 sender 时静默跳过。"""
    notify_group = CRASH_NOTIFY_GROUP
    if not notify_group:
        return
    sender = helpers.get_sender('')
    if sender is None:
        log.warning('无可用 sender，跳过崩溃熔断告警')
        return
    md = (
        '$$\\textcolor{red}{\\Huge\\text{严重告警}}$$'
        '\n'
        '## 🛑 LGT-Bot 崩溃死循环已熔断\n'
        '\n'
        f'> 引擎在 {_CRASH_LOOP_WINDOW_S:.0f} 秒内自动重启 {count} 次，停止尝试重启\n'
        '\n'
        '```当前状态\n'
        '- 主框架仍在运行，但游戏功能发生致命错误无法启动\n'
        '- 已自动保存 backtrace 用于崩溃排查\n'
        '- 需在后台手动重启引擎才能恢复游戏模块运行\n'
        '```\n'
        '\n'
        '> 💡 此消息为自动推送，请尽快联系开发者排查修复'
    )
    page_logs.log_outgoing(notify_group, False, md)
    try:
        with log_attribution.mark_outbound():
            await sender.send_to_group(notify_group, md)
    except Exception as e:
        log.warning(f'崩溃熔断告警推送失败 ({notify_group}): {e}')


# ──────── 「消息回复限制」教学提示(新建房间触发,紧跟建房公告发出) ──────────
# 触发流:LGTBot_ElainaBot.cc::ClassifyMatchEvent 识别引擎「现在玩家可以…」建房
# 广播(/新游戏、/随机游戏 共用同一条 NewMatch 广播)→ 调 cb_match_event(kind='new_game') →
# 此处把 key 记入 _pending_tip_keys。**真正的发送时机被推迟到本帧的
# cb_send_text_message / cb_send_image_message 把那条建房公告排进 asyncio
# 发送队列之后**:
#   1. C++ 调 cb_match_event(只标记,不立刻发) → 立即返回
#   2. C++ 调 cb_send_text_message → 投递「房间已创建」send task 到 asyncio + per-key
#      Lock 排队(见下面 _send_locks);Lock 保证 QQ 端按 cb 调用顺序送达
#   3. 建房公告 send task 跑完后,我们才在同一个 task 末尾调 _consume_pending_tip
#      → 调度教学提示 task,后者再次抢同一把 Lock 排到建房公告后面 → 顺序得证。
#
# 为什么挂 new_game 而不是 game_started(旧实现):
# 建房广播是对 /新游戏 命令 msg_id 的**第 1 条**回复,教学提示紧随其后为第 2 条,必然在 5 条配额内送达;
# 而游戏开始的消息发得晚(开局刷屏高峰),常常已超 5 条把提示吞掉。单机局无 new_game 广播 → 不再发教学。
_pending_tip_keys: set[str] = set()

# ─────────────────────────────────────────────────────────────────────────
# 「带开局私信」游戏白名单 —— 此集合内的游戏在**全量群**里 cb_match_event(kind='new_game')
# 时会被记入 _pending_dm_warn_keys,在建房公告发完后追加一条「主动私信」提示给群内玩家。
# 与「消息回复限制」教学**互斥**:非全量群建房时发的是回复限制教学(覆盖面更广,
# 含刷新按钮机制),私信提示被抑制;全量群不需要教学,才轮到私信提示。
# **私信里新建游戏不提示**(玩家已在私信会话内)。
#
# 触发逻辑:
#   1. C++ 引擎调 cb_match_event(kind='new_game', game_name='XXX')
#   2. 若 'XXX' 在 _DM_LIMITED_GAMES 内**且为全量群**(群聊 + is_full_volume_group),
#      key 进 _pending_dm_warn_keys;非全量群该位置标记的是 _pending_tip_keys
#   3. 引擎随后调 cb_send_text_message 发出「房间已创建」公告
#   4. _serialized_text_send 在 Lock 内调 _consume_pending_dm_warn,
#      pop 出 key 并调度 _schedule_dm_warning —— 该 task 抢同把 Lock 排在
#      本条之后,QQ 端先看到「房间已创建」,再看到「私信限制」提示。
# ─────────────────────────────────────────────────────────────────────────
_DM_LIMITED_GAMES: frozenset = frozenset({
    '谁是牛头王',
    '阿瓦隆',
    'HP杀',
    '大海战',
    '漫漫长夜',
    '十七步',
    '同步麻将',
    'wordle',
    '德州波卡',
    '幸运波卡',
})

# 类似 _pending_tip_keys:cb_match_event 阶段只标记 key,等开局公告
# 通过 cb_send_text/image 发完后,在同一把 per-target Lock 内调度提示发送。
_pending_dm_warn_keys: set[str] = set()

# 老文案 —— 白名单模式(正式环境主动私信被拒,发出去会失败)下的受限警告
_DM_WARNING_TEXT_LEGACY = (
    '## ⚠️ 主动私信受限\n'
    '此游戏存在**主动私信**，会受到协议限制发送失败。\n'
    '请在游戏中**私信机器人**发送“赛况”来短暂激活私信和查看私信信息'
)

# 新文案 —— 全员直推(sandbox_dm_users: ['all'])模式:
# 私信发得出去,只需玩家加好友,且未关闭机器人的主动消息权限
_DM_WARNING_TEXT_ALL = (
    '## 💬 主动私信提醒\n'
    '此游戏存在**主动私信**。若无法接收，请点击机器人头像 → 右上角设置中进入「权限设置」中开启**主动消息**权限'
)

# ──────── per-target 串行化:发到同一 target 的消息按 cb 调用顺序送达 QQ ────────
# 引入背景:旧实现 cb_send_text_message 走 helpers.run_coro_blocking 同步等 15s
# (内部 wait_and_consume 等用户点刷新),期间 lgtbot 的 read thread 持有 Match.mutex_,
# 这窗口足以让玩家发出新指令进 Match::Request 排队 → 15s 后释放锁 → 紧接着
# OnGameOver 抢锁置 state=IS_OVER + CloseInput() 把 child_in_ 置 NULL → 排队那条
# SendExecute → WriteFrame(NULL) → SIGSEGV。
#
# 新实现 cb_send_text/image_message 改 fire-and-forget:投递到 asyncio loop 立即
# 返回,read thread 在 OnPost 里持锁只剩几十 µs。 per-target asyncio.Lock 保证发到
# 同一 target 的消息按 cb 调用顺序送达 QQ(asyncio FIFO + Lock 串行)。
_send_locks: dict[str, asyncio.Lock] = {}


def _get_send_lock(key: str) -> asyncio.Lock:
    """懒创建 per-target Lock。只能从 asyncio loop 调(单线程,dict get/setdefault 安全)。"""
    lock = _send_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _send_locks[key] = lock
    return lock

_REFRESH_TIP_BASE = (
    '## ⚠️ 消息回复限制\n'
    '机器人每条消息**最多回复5次**，且**5分钟**后失效。\n'
    '🔄 ***请及时点击刷新按钮***，否则将**影响机器人发消息和游戏进程**。'
)

# 全量申请段 —— 只在群聊里拼到末尾,私信里没有「群号」概念
_REFRESH_TIP_GROUP_TAIL = (
    '\n'
    '\n'
    '> 💡 群主授权群聊消息权限后可规避此限制，点击下方按钮或发送“全量申请”，然后按照提示进行操作'
)


async def _send_refresh_tip(target_id: str, is_uid: bool) -> None:
    """走标准 `_send_text_quota_managed` 通道发出教学提示。

    第 4 / 5 条配额上的真正刷新按钮由 ``_send_text_quota_managed`` 按 count
    自动挂载;教学消息本身视场景另带「全量申请」按钮。

    走 per-target Lock 排队 —— 跟 ``_serialized_text_send`` 共用同一把锁,保证
    教学提示永远在「开局公告」之后到达 QQ。

    分支:
      · 私信(``is_uid=True``):仅 BASE 段,无附加按钮 —— 私聊没有「群号」
        概念,「全量申请」段会显得突兀。
      · 群聊(``is_uid=False``):BASE + GROUP_TAIL 段,底部挂一行「全量申请」
        type=2 按钮(回填到输入框,用户自行补群号再发);实际命令由另一个
        插件实现,本插件只提供 UI 入口。
    """
    if is_uid:
        msg = _REFRESH_TIP_BASE
        extra = None
    else:
        msg = _REFRESH_TIP_BASE + _REFRESH_TIP_GROUP_TAIL
        extra = buttons.build_full_volume_apply_button()
    key = helpers.target_key(target_id, is_uid)
    try:
        async with _get_send_lock(key):
            page_logs.log_outgoing(target_id, is_uid, msg)
            await _send_text_quota_managed(target_id, is_uid, msg, extra)
    except Exception as e:
        log.debug(f'消息回复限制说明发送失败 ({target_id}): {e}')


def _schedule_refresh_tip(target_id: str, is_uid: bool) -> None:
    """C++ 工作线程安全地把 `_send_refresh_tip` 投到 asyncio loop,fire-and-forget。

    `asyncio.run_coroutine_threadsafe` 返回的 Future 故意不 await —— C++ 线程
    立即返回继续处理引擎下一帧。
    """
    loop = state.event_loop
    if loop is None or loop.is_closed():
        log.debug('事件循环不可用,跳过刷新按钮使用说明')
        return
    try:
        asyncio.run_coroutine_threadsafe(
            _send_refresh_tip(target_id, is_uid), loop)
    except Exception as e:
        log.debug(f'调度刷新按钮使用说明失败: {e}')


def _consume_pending_tip(key: str, target_id: str, is_uid: bool) -> None:
    """若本 key 之前在 cb_match_event(kind='new_game')里被打了标记,这里弹掉并发出。

    由 ``_serialized_text_send`` / ``_serialized_image_send`` 在 per-target Lock
    持有期间、``_send_text/image_quota_managed`` 已 await 完毕之后调用。
    教学提示走 ``_schedule_refresh_tip`` 投到 asyncio loop,内部再次抢同一把
    Lock —— 当前 send task 释放锁后,教学提示 task 自然排到下一位,QQ 端先
    看到「游戏开始」再看到「消息回复限制」教学。

    全量群里 bot 不被 5 条/msg_id 限制,refresh 按钮永远不会出现 —— 这条
    教学的整段文案(在讲怎么点刷新按钮)会变成误导。所以只清掉标记,不发送。
    沙箱私信用户同理:配额满后直接主动直推,不依赖刷新按钮,教学同样会误导。
    """
    if key not in _pending_tip_keys:
        return
    _pending_tip_keys.discard(key)
    if (not is_uid) and helpers.is_full_volume_group(target_id):
        log.debug(f'全量群 {target_id} 跳过刷新按钮使用说明')
        return
    if _is_sandbox_dm(target_id, is_uid):
        log.debug(f'直推私信用户 {target_id} 跳过刷新按钮使用说明')
        return
    _schedule_refresh_tip(target_id, is_uid)


# ─────────── 「带开局私信」游戏限制提示 ──────────────────────────────────
# 结构跟上面 _consume_pending_tip / _schedule_refresh_tip 完全对称 —— 在
# cb_match_event(kind='new_game') 阶段判定游戏名是否在 _DM_LIMITED_GAMES
# 内并打 _pending_dm_warn_keys 标记;真正的发送时机由 _serialized_text/
# image_send 在开局公告同步落地后调 _consume_pending_dm_warn 触发。

async def _send_dm_warning(target_id: str, is_uid: bool) -> None:
    """走标准 ``_send_text_quota_managed`` 通道发出「主动私信」提示。

    文案随模式切换:全员直推(``DM_PUSH_ALL``)用提醒版(加好友 + 开权限即可收到),
    白名单老模式用受限警告版。两版底部都挂「💫 添加好友」link 按钮,
    链接是 ``_build_robot_invite_link`` 的同款邀请页。

    与 ``_send_refresh_tip`` 同一把 per-target Lock,保证排在「房间已创建」公告之后到达 QQ。
    """
    key = helpers.target_key(target_id, is_uid)
    text = _DM_WARNING_TEXT_ALL if DM_PUSH_ALL else _DM_WARNING_TEXT_LEGACY
    extra = buttons.build_dm_warning_buttons()
    try:
        async with _get_send_lock(key):
            page_logs.log_outgoing(target_id, is_uid, text)
            await _send_text_quota_managed(target_id, is_uid, text, extra)
    except Exception as e:
        log.debug(f'主动私信提示发送失败 ({target_id}): {e}')


def _schedule_dm_warning(target_id: str, is_uid: bool) -> None:
    """C++ 工作线程安全地把 `_send_dm_warning` 投到 asyncio loop,fire-and-forget。"""
    loop = state.event_loop
    if loop is None or loop.is_closed():
        log.debug('事件循环不可用,跳过私信限制提示')
        return
    try:
        asyncio.run_coroutine_threadsafe(
            _send_dm_warning(target_id, is_uid), loop)
    except Exception as e:
        log.debug(f'调度私信限制提示失败: {e}')


def _consume_pending_dm_warn(key: str, target_id: str, is_uid: bool) -> None:
    """若 cb_match_event 标了私信限制 key,这里弹掉并发出提示。

    由 ``_serialized_text_send`` / ``_serialized_image_send`` 在开局公告
    发送完毕后调用 —— 与 ``_consume_pending_tip`` 并列。
    """
    if key not in _pending_dm_warn_keys:
        return
    _pending_dm_warn_keys.discard(key)
    _schedule_dm_warning(target_id, is_uid)


# ──────── 用户信息回调（被 LGTBot 引擎调用，需返回字符串） ─────────────────

def cb_match_event(target_id: str, is_uid: bool, kind: str, game_name: str):
    """C++ → Python：bridge 按消息内容分类后调用,把按钮 / 当前游戏名一次性敲定。

    bridge 端的分类逻辑见 ``LGTBot_ElainaBot.cc::ClassifyMatchEvent``;本侧只
    根据 ``kind`` 走 switch:

      ``announce``       仅刷新 ``state.current_game[key]``(brief 出现但非
                         新建/加入/退出场景,如 /设置 成功后的回执),不动按钮。
      ``new_game``       刷新游戏名;在下一条文本回复挂「加入 / 退出 + 规则」;
                         标记「消息回复限制」教学(建房公告后紧随发出;全量群
                         改标「主动私信」提示,两者互斥,教学优先)。
      ``join_leave``     刷新游戏名;同上挂「加入 / 退出 + 规则」(玩家加入/
                         退出时也补一个规则按钮,方便随时查阅)。
      ``all_left``       清空当前游戏名;挂「游戏列表 / 创建房间」引导。
      ``terminate``      清空当前游戏名,不挂按钮(/新游戏 前置解散 / 管理员
                         主动结束等场景,紧接着会有真正的新建消息覆盖,或就该
                         安静收尾)。
      ``mid_quit``       玩家中途强退广播。**仅私信**按 terminate 语义清理 ——
                         私信对局全员 LEFT 后的解散广播私发不到任何人(桥接层
                         2.6 注释),这条可送达的中途退出就是对局对该目标结束
                         的最后信号;群聊对局仍在继续,不动任何状态。
      ``game_over``      游戏自然结束的结算广播 —— 挂「📊 查看战绩 + 🔄 重开一局」。
                         结算广播无 brief,重开按钮的游戏名从 ``current_game`` 回查,
                         取完即清(对局已随结算释放)。
      ``game_over_unrecorded``  同上,但结算带「游戏结果不记录」(单机 / 非正式局 /
                         未连接数据库) —— 本局没进战绩,不挂「查看战绩」;
                         若游戏名也未知则整组不挂。
      ``game_started``   引擎 Match::GameStart 成功后的 BoardcastAtAll —— 不动
                         按钮,只做进行中对局跟踪(active_matches)。「消息回复
                         限制」教学已前移到 ``new_game`` 建房时标记:开局消息发
                         得晚,配额可能已耗尽把提示吞掉;建房公告是命令的第 1 条
                         回复,教学紧随其后必达。同时若建房游戏带「开局私信」,
                         仅在全量群(教学不发送)才改发「主动私信」提示 ——
                         两条提示互斥,回复限制优先。
      ``unknown_meta``       未参与游戏 / 不在本群的游戏 —— 挂「元指令帮助」。
      ``unknown_config``     等待房间里输错配置 —— 挂「配置帮助 + 元指令帮助」。
      ``unknown_game``       游戏进行中输错游戏指令 —— 挂「游戏帮助 + 元指令帮助」。
      ``unknown_game_name``  /新游戏 / /规则 等误输游戏名 —— 挂「🎲 游戏列表」。
      ``about``              /关于 命令回执 —— 挂「适配层仓库 + LGT-Bot 仓库」链接按钮。

    所有按钮通过 ``state.pending_buttons[key]`` 暂存,被随后的
    ``cb_send_text_message`` pop 出来一次性附上(bridge 调本回调 → 再调
    send_text_message,同步顺序,GIL 下读写安全)。
    """
    if not target_id:
        return
    key = helpers.target_key(target_id, is_uid)

    # 状态更新(mid_quit 仅私信按解散处理:群聊对局还有其他玩家,继续进行)
    if kind in ('all_left', 'terminate') or (kind == 'mid_quit' and is_uid):
        state.current_game.pop(key, None)
    elif game_name:
        state.current_game[key] = game_name

    # 进行中对局跟踪:game_started = 真正开局才记(等待房间 / 单机秒结算局不发此事件,不算进行中);结束 / 解散移除(pop 幂等,孤儿 game_over 也安全)。
    # 游戏名从current_game 快照(game_started 广播本身无 brief;多人局在此前的 new_game 已写入)。
    if kind == 'game_started':
        # 游戏名:多人局 current_game 已由 new_game 的 brief 写入,优先用;单机局引擎跳过
        # new_game、game_started 又无 brief → current_game 为空,回退到 dispatcher
        # 从「/新游戏 X」命令抓下的 pending 名。pop 无论命中与否都清掉 pending,不残留。
        pending = state.pending_new_game_name.pop(key, '')
        game = state.current_game.get(key) or pending
        if game:
            # 写回 current_game,让单机局结算时的「重开一局」按钮也能回查到游戏名
            state.current_game[key] = game
        state.active_matches[key] = {
            'target_id': target_id,
            'is_uid': is_uid,
            'game': game,
            'since': time.time(),
        }
    elif kind in ('game_over', 'game_over_unrecorded', 'all_left', 'terminate') \
            or (kind == 'mid_quit' and is_uid):
        state.active_matches.pop(key, None)

    # 按钮挂载 —— new_game / join_leave 都挂同样一组:
    #   · 群聊:  「加入 / 退出」+ 「📖《X》规则」 两行
    #   · 私聊:  仅「📖《X》规则」一行(DM 里 /加入 /退出 无意义,见 is_uid 分支)
    # 私聊场景下 build_game_action_buttons 返回的若是空列表(game_name 未知的
    # 极端情形),`if btns:` 跳过 pending_buttons 写入,避免给框架塞空按钮组。
    if kind in ('new_game', 'join_leave'):
        btns = buttons.build_game_action_buttons(
            state.current_game.get(key),
            include_rule=True,
            include_join_leave=not is_uid,
        )
        if btns:
            state.pending_buttons[key] = btns
        if kind == 'new_game':
            # 「消息回复限制」教学:新建房间即标记(见 _pending_tip_keys 段注释),
            # 建房公告 send task 末尾 consume → 提示作为第 2 条回复必达;是否真发
            # 由 _consume_pending_tip 按目标过滤(全量群 / 直推私信跳过)。
            _pending_tip_keys.add(key)
            # 带开局私信的游戏:仅当回复限制提示**不会**发送(全量群)时才发
            # 「主动私信」提示 —— 非全量群里两条提示都跟在建房公告后太吵,
            # 回复限制优先、私信提示抑制;私信里新建游戏不发私信提示
            # (玩家已在私信会话内)。
            if (not is_uid and game_name and game_name in _DM_LIMITED_GAMES
                    and helpers.is_full_volume_group(target_id)):
                _pending_dm_warn_keys.add(key)
    elif kind == 'all_left':
        state.pending_buttons[key] = buttons.build_dissolve_buttons()
    elif kind in ('game_over', 'game_over_unrecorded'):
        # 结算广播不带 brief(bridge 传来的 game_name 为空),重开按钮的游戏名
        # 从 current_game 回查;pop 取完即清 —— 对局已随结算释放,残留会让之后
        # 的按钮回查到已结束的游戏。「游戏结果不记录」的结算不挂「查看战绩」。
        btns = buttons.build_game_over_buttons(
            state.current_game.pop(key, None),
            include_record=(kind == 'game_over'),
        )
        if btns:
            state.pending_buttons[key] = btns
    elif kind == 'unknown_meta':
        state.pending_buttons[key] = buttons.build_unknown_meta_buttons()
    elif kind == 'unknown_config':
        state.pending_buttons[key] = buttons.build_unknown_config_buttons()
    elif kind == 'unknown_game':
        state.pending_buttons[key] = buttons.build_unknown_game_buttons()
    elif kind == 'unknown_game_name':
        state.pending_buttons[key] = buttons.build_game_list_buttons()
    elif kind == 'about':
        state.pending_buttons[key] = buttons.build_about_buttons()
    # 'announce' / 'terminate' / 'game_started' 不挂按钮
    # (「消息回复限制」教学已随 new_game 建房时标记,不再挂到 game_started ——
    #  开局时配额可能已耗尽把提示吞掉)


def cb_get_user_name(uid: str) -> str:
    """C++ → Python：返回用户昵称(主框架 data.db users 表;未命中返回 uid 兜底)

    ``userinfo.get_name`` 同步且线程安全(缓存命中零 I/O;未命中走框架
    log_service 的独立只读连接),可从引擎工作线程直调。昵称经
    ``helpers.sanitize_md_name`` 按 markdown 语境转义。

    非 markdown 出站路径(媒体兜底 msg_type=7 / WebUI 消息日志)在各自出口
    用 ``helpers.strip_md_escapes`` 还原,不会露出反斜杠。已知残留:引擎自渲
    的对局图片(HTML)里带特殊字符的昵称会显示 ``\\`` 前缀,暂无干净解法。
    """
    return helpers.sanitize_md_name(userinfo.get_name(uid) or uid)


def cb_get_user_avatar_url(uid: str) -> str:
    """C++ → Python：返回头像 URL(按绑定 bot 的 appid 即时推导,不落库)

    QQ 官方头像直链仅由 appid + openid 决定,推导即最新 —— 换绑 bot 后也
    天然正确。无绑定 bot 时返回 '',C++ 端 DownloadUserAvatar 会跳过下载。
    """
    return userinfo.avatar_url(uid)


# ──────── 文本发送 ────────────────────────────────────────────────────────

def cb_send_text_message(target_id: str, is_uid: bool, msg: str):
    """C++ → Python：发送文本消息（fire-and-forget,不阻塞 C++ 调用线程）

    旧实现走 ``helpers.run_coro_blocking`` 阻塞 C++ 线程至多 15s 等刷新按钮 ——
    那段时间内 lgtbot read thread 在 OnPost 里持有 ``Match.mutex_``,后续玩家
    指令在 Thread B 排队;15s 后释放,Thread B 几乎同时和 OnGameOver 抢锁 →
    Thread B 拿到锁后看 state 仍是 IS_STARTED → 解锁 → OnGameOver 紧接着拿锁
    置 IS_OVER + CloseInput → child_in_=NULL → Thread B 的 SendExecute →
    WriteFrame(NULL) → SIGSEGV。

    新实现:投递发送任务到 asyncio loop 立即返回,read thread 持锁时间从 ≤15s
    压到几十 µs。per-target Lock 保证发到同一 target 的消息按 cb 调用顺序
    送达 QQ。

    本条回复要附的按钮（若有）已由 bridge 先调用 cb_match_event 写进
    state.pending_buttons[key]——同一次 HandleMessages 内顺序调用,
    GIL 保护下读写安全。这里 pop 出来跟着 send task 走。
    """
    key = helpers.target_key(target_id, is_uid)
    extra_buttons = state.pending_buttons.pop(key, None)
    # 日志是纯文本展示语境:引擎文本里源头转义的昵称(\#foo)还原后再记录,
    # 实际发送仍用带转义的 msg(markdown 语境)
    page_logs.log_outgoing(target_id, is_uid, helpers.strip_md_escapes(msg))

    loop = state.event_loop
    if loop is None or loop.is_closed():
        log.warning('事件循环不可用，丢弃文本消息')
        return
    try:
        asyncio.run_coroutine_threadsafe(
            _serialized_text_send(key, target_id, is_uid, msg, extra_buttons),
            loop)
    except Exception as e:
        log.warning(f'调度文本发送失败: {e}')


async def _serialized_text_send(key: str, target_id: str, is_uid: bool,
                                msg: str, extra_buttons) -> None:
    """串行化的文本发送:per-target Lock 保证顺序,配额管理 + auto-refresh 按钮挂载。

    Lock 内顺序:
      ① ``_send_text_quota_managed``  实际把这条文本送出去
      ② ``_consume_pending_tip``      如本帧标了 game_started,调度教学提示 task
                                      —— 它也走同一把 Lock,会自动排在本条之后。
    """
    async with _get_send_lock(key):
        await _send_text_quota_managed(target_id, is_uid, msg, extra_buttons)
        _consume_pending_tip(key, target_id, is_uid)
        _consume_pending_dm_warn(key, target_id, is_uid)


async def _send_text_quota_managed(target_id, is_uid, msg, extra_buttons):
    """文本发送核心：配额管理 + 自动追加刷新按钮 + 配额满时等待续命

    全量群分支:配额耗尽时不再阻塞等刷新按钮,直接走主动消息(``kwargs={}``);
    且整个生命周期不追加 ``build_refresh_button``,因为全量群里 bot 不被
    5 条/msg_id 限制,这个教学按钮没有意义。
    """
    key = helpers.target_key(target_id, is_uid)
    msg_preview = (msg or '')[:30].replace('\n', ' ')

    # 直推私信(all 模式全员 / 白名单沙箱用户):逻辑与全量群完全一致 ——
    # 前 5 次仍用 msg_id 被动回复(消耗配额),仅配额耗尽后才直接主动消息。
    is_sandbox_dm = _is_sandbox_dm(target_id, is_uid)
    # 全量群判定:只看运行时观测到的事实(state.full_volume_groups),不再退回
    # 框架 non_at_message.* 配置 —— 配置可能与 QQ 后台权限不同步,误判会让
    # 非全量群也走主动消息(QQ 必拒,把 bot 的配额烧掉)。
    is_full = (not is_uid) and helpers.is_full_volume_group(target_id)
    # 主动直推资格(全量群 / 沙箱私信):配额满后可直接主动消息、不挂刷新按钮。
    # 注意"资格"不代表跳过被动配额 —— 前 5 次照常 try_consume_ref 走 msg_id。
    is_active_push = is_full or is_sandbox_dm

    consumed = quota.try_consume_ref(key)
    if consumed is None:
        # 指标:「真耗尽」= TTL 内引用的 5 条真用完(has_valid_ref True);
        # 无引用 / 已过期的场景(无事件上下文的推送、私信丢弃)不算配额压力。
        # 且仅统计**无主动直推资格**的目标:全量群 / 沙箱私信配额满后可无缝转主动消息、消息照常送达,没有实际影响,不计入配额压力。
        had_valid_ref = quota.has_valid_ref(key)
        if had_valid_ref and not is_active_push:
            metrics.record_quota_exhausted()
        if is_active_push:
            # 全量群 / 沙箱私信:配额满 → 直接主动消息,不等刷新按钮
            tag = '私信直推' if is_sandbox_dm else '全量直推'
            log.info(f'⚡ [{tag}] {key} 配额已满，走主动消息: {msg_preview!r}')
        elif is_uid and not had_valid_ref:
            # 普通私信 + 无有效 msg_id(从未私信过 / 已超 5 分钟过期):
            # 正式环境主动私信必拒 → 直接丢弃,只留一行 audit 日志。
            log.info(f'🗑️ [私信丢弃] {key} 无有效消息ID，丢弃: {msg_preview!r}')
            return
        else:
            # 群聊配额满 / 普通私信配额满(5 分钟内仍有引用) → 阻塞等待刷新,
            # 不预先尝试发送(直接发也会被 QQ 拒)。
            wait_start = time.monotonic()
            log.info(f'⏳ [配额已满] {key} 已用 {quota.REF_QUOTA}/{quota.REF_QUOTA}，'
                     f'阻塞等待刷新按钮 ≤{quota.REFRESH_WAIT_TIMEOUT:.0f}s | 待发: {msg_preview!r}')
            consumed = await quota.wait_and_consume(key, quota.REFRESH_WAIT_TIMEOUT)
            elapsed = time.monotonic() - wait_start
            if consumed is None:
                # 等待超时 → 改走主动消息(无 msg_id/event_id)。
                # bot 若在该群/用户上有主动 quota 还能落地,语义更干净。
                metrics.record_quota_wait_timeout()
                log.warning(f'⏰ [超时强发] {key} 经 {elapsed:.1f}s 无刷新，尝试发送主动消息')
            else:
                log.info(f'✅ [配额已刷新] {key} 等 {elapsed:.1f}s 后续命成功，重发文本')

    # 准备 sender / kwargs。consumed 仍为 None 即主动路径(全量直推 / 沙箱直推 / 刷新超时兜底)。
    if consumed is not None:
        ref_type, ref_value, count, ref_appid = consumed
        sender = helpers.get_sender(ref_appid)
        kwargs = {ref_type: ref_value}
    else:
        # 主动路径:无 ref / 无 appid;用任一可用 sender,kwargs 空
        sender = helpers.get_sender('')
        count = 0
        kwargs = {}
    if sender is None:
        log.warning(f'无可用 sender，丢弃文本消息 → {target_id}')
        return

    # 指标:无 ref 即主动消息(全量直推 / 沙箱直推 / 超时强发),按日分桶计数
    if consumed is None:
        metrics.record_active_push(target_id, is_uid)

    # 第 4 条起追加刷新按钮;第 5 条（达到上限）用「⚠️ 最终刷新」加强提示。
    # 主动直推(全量群 / 沙箱私信)从不追加(bot 不被 5 条/msg_id 限制)。
    btns = list(extra_buttons) if extra_buttons else []
    if not is_active_push and count >= quota.REFRESH_BUTTON_THRESHOLD:
        is_last = (count >= quota.REF_QUOTA)
        btns.append(quota.build_refresh_button(is_last=is_last))
        tag = '⚠️' if is_last else '🔄'
        log.info(f'📊 [配额追踪] {key} 已用 {count}/{quota.REF_QUOTA} → {tag}')
    btns_arg = btns if btns else None

    try:
        with log_attribution.mark_outbound():
            if is_uid:
                await sender.send_to_user(target_id, msg, buttons=btns_arg, **kwargs)
            else:
                await sender.send_to_group(target_id, msg, buttons=btns_arg, **kwargs)
    except Exception as e:
        log.warning(f'发送文本失败 ({target_id}): {e}')


# ──────── 图片发送 ────────────────────────────────────────────────────────

def cb_send_image_message(target_id: str, is_uid: bool, image_path: str, content: str = ''):
    """C++ → Python：发送图片（fire-and-forget,理由同 ``cb_send_text_message``）

    LGTBot 通过 popen 异步调用 markdown2image 生成图片，存在小概率回调到达
    时文件还未落盘，这里短暂轮询等待最多 2s。文件读完后投到 asyncio loop 串行
    发送,本函数立即返回让 C++ read thread 释放 Match.mutex_。
    """
    if not os.path.isfile(image_path):
        deadline = time.time() + 2.0
        while time.time() < deadline and not os.path.isfile(image_path):
            time.sleep(0.05)
    if not os.path.isfile(image_path):
        mk_bin = os.path.join(boot.BUILD_DIR, 'markdown2image')
        if not os.path.isfile(mk_bin):
            log.warning(f'markdown2image 二进制缺失: {mk_bin} —— 请重新执行 build.sh')
        else:
            log.warning(f'图片渲染失败 (markdown2image 调用未生成文件): {image_path}')
        return

    try:
        with open(image_path, 'rb') as f:
            data = f.read()
    except Exception as e:
        log.warning(f'读取图片失败: {e}')
        return

    raw_content = content or ''
    # 日志展示用 humanize + 去转义版（纯文本更可读），实际发送时再按通道决定
    page_logs.log_outgoing(
        target_id, is_uid,
        helpers.strip_md_escapes(helpers.humanize_mentions(raw_content)), image=True,
    )

    filename = os.path.basename(image_path) or 'lgtbot.png'
    key = helpers.target_key(target_id, is_uid)

    loop = state.event_loop
    if loop is None or loop.is_closed():
        log.warning('事件循环不可用，丢弃图片消息')
        return
    try:
        asyncio.run_coroutine_threadsafe(
            _serialized_image_send(key, target_id, is_uid, data, raw_content, filename),
            loop)
    except Exception as e:
        log.warning(f'调度图片发送失败: {e}')


async def _serialized_image_send(key: str, target_id: str, is_uid: bool,
                                 data: bytes, raw_content: str, filename: str) -> None:
    """串行化的图片发送 —— 与 ``_serialized_text_send`` 共享 per-target Lock。

    多图场景:lgtbot 把每张图一条 cb_send_image_message 投过来,第 1 张带
    ``raw_content`` (合并后的 caption),其余 raw_content 为空。``game_started``
    教学标记只在「胜利!」之类文本里出现,因此只有 raw_content 非空时才尝试
    ``_consume_pending_tip``;空 raw_content 看不到 key 也是 no-op。
    """
    async with _get_send_lock(key):
        await _send_image_quota_managed(target_id, is_uid, data, raw_content, filename)
        if raw_content:
            _consume_pending_tip(key, target_id, is_uid)
            _consume_pending_dm_warn(key, target_id, is_uid)


async def _send_image_quota_managed(target_id, is_uid, data, raw_content, filename):
    """图片发送核心：配额管理 + 优先图床+markdown，失败回退 media

    发送通道二选一：
      A. 图床 markdown：通过 image_hosting 上传图片得到 URL，用 markdown
         `![](url)` 内嵌；保留 `<@openid>` 原生 mention，可挂刷新按钮
      B. 媒体兜底：图床未启用 / 上传失败时走原有 msg_type=7 路径（content
         字段需 humanize mentions，无法挂按钮）

    主动直推(全量群 / 沙箱私信)时不等刷新按钮,直接主动消息(``ref_type=''``
    透传到下游)。私信无有效 msg_id 时直接丢弃(同 _send_text_quota_managed)。
    """
    key = helpers.target_key(target_id, is_uid)
    # 直推私信 / 全量群:前 5 次仍走 msg_id 被动回复,仅配额耗尽后主动直推
    # (逻辑同 _send_text_quota_managed,详见那里的注释)
    is_sandbox_dm = _is_sandbox_dm(target_id, is_uid)
    is_full = (not is_uid) and helpers.is_full_volume_group(target_id)
    is_active_push = is_full or is_sandbox_dm

    consumed = quota.try_consume_ref(key)
    if consumed is None:
        # 指标口径同 _send_text_quota_managed:仅 TTL 内引用真用完、且无主动直推资格(全量群 / 沙箱私信可转主动消息,无影响,不计)才算配额压力。
        had_valid_ref = quota.has_valid_ref(key)
        if had_valid_ref and not is_active_push:
            metrics.record_quota_exhausted()
        if is_active_push:
            tag = '私信直推' if is_sandbox_dm else '全量直推'
            log.info(f'⚡ [{tag}] {key} 配额已满，图片走主动消息')
        elif is_uid and not had_valid_ref:
            # 普通私信无有效 msg_id(从未私信过 / 已超 5 分钟):直接丢弃
            log.info(f'🗑️ [私信丢弃] {key} 无有效消息ID，丢弃图片')
            return
        else:
            wait_start = time.monotonic()
            log.info(f'⏳ [配额已满] {key} 已用 {quota.REF_QUOTA}/{quota.REF_QUOTA}，'
                     f'阻塞等待刷新按钮 ≤{quota.REFRESH_WAIT_TIMEOUT:.0f}s | 待发: [图片]')
            consumed = await quota.wait_and_consume(key, quota.REFRESH_WAIT_TIMEOUT)
            elapsed = time.monotonic() - wait_start
            if consumed is None:
                # 等待超时 → 改走主动消息(理由同 _send_text_quota_managed:
                # 过期 msg_id 强发必拒,主动消息至少留一条出路)。
                metrics.record_quota_wait_timeout()
                log.warning(f'⏰ [超时强发] {key} 经 {elapsed:.1f}s 无刷新，尝试发送图片主动消息')
            else:
                log.info(f'✅ [配额已刷新] {key} 等 {elapsed:.1f}s 后续命成功，重发图片')

    # 准备 sender / ref tuple。consumed 仍为 None 即主动路径(全量直推 / 沙箱
    # 直推 / 刷新超时兜底),用空 ref_type/ref_value 透传到下游,下游靠 ref_type
    # 为空切换 kwargs={}。
    if consumed is not None:
        ref_type, ref_value, count, ref_appid = consumed
        sender = helpers.get_sender(ref_appid)
    else:
        ref_type, ref_value, count = '', '', 0
        sender = helpers.get_sender('')
    if sender is None:
        log.warning(f'无可用 sender，丢弃图片 → {target_id}')
        return

    # 指标:ref_type 为空即主动消息(同文本路径口径),按日分桶计数
    if not ref_type:
        metrics.record_active_push(target_id, is_uid)

    # ── 通道 A：尝试图床 → markdown 内嵌 ─────────────────────────────────
    # target 一并透传:qq_file 图床用当前消息目标作上传作用域(其余图床忽略)
    user_id_for_cos = target_id if is_uid else ''
    image_url = await uploader.upload_image(data, filename, user_id=user_id_for_cos,
                                            target_id=target_id, target_is_uid=is_uid)
    if image_url:
        if await _send_markdown_image(sender, target_id, is_uid, ref_type, ref_value,
                                      raw_content, image_url, data, count,
                                      is_full=is_active_push):
            return
        # markdown 发送失败（极少见，比如域名未报备被 QQ 拒）→ 落回 media

    # ── 通道 B：媒体兜底（msg_type=7）────────────────────────────────────
    await _send_media_fallback(sender, target_id, is_uid, ref_type, ref_value, raw_content, data)


async def _send_markdown_image(sender, target_id, is_uid, ref_type, ref_value,
                               raw_content, image_url, data, count,
                               *, is_full: bool = False) -> bool:
    """构造 markdown 文本 + 图片 + 按钮，调 send_to_*。成功返回 True。

    ``ref_type=''`` 表示主动消息(全量群 / 沙箱私信 / 配额耗尽超时路径):
    kwargs 留空,不带 msg_id/event_id。``is_full=True``(调用方传 is_active_push,
    即全量群或沙箱私信)时同样跳过刷新按钮追加。
    """
    width, height = uploader.get_image_size(data)
    parts = []
    if raw_content:
        parts.append(raw_content)
    parts.append(f'![image #{width}px #{height}px]({image_url})')
    md = '\n\n'.join(parts)

    # markdown 通道支持挂按钮（不像 msg_type=7);全量群从不挂刷新按钮
    btns: list = []
    if not is_full and count >= quota.REFRESH_BUTTON_THRESHOLD:
        is_last = (count >= quota.REF_QUOTA)
        btns.append(quota.build_refresh_button(is_last=is_last))
    btns_arg = btns if btns else None

    kwargs = {ref_type: ref_value} if ref_type else {}
    try:
        with log_attribution.mark_outbound():
            if is_uid:
                await sender.send_to_user(target_id, md, buttons=btns_arg, **kwargs)
            else:
                await sender.send_to_group(target_id, md, buttons=btns_arg, **kwargs)
        return True
    except Exception as e:
        log.warning(f'markdown 图片发送失败 ({target_id}): {e}, 回退到媒体消息')
        return False


async def _send_media_fallback(sender, target_id, is_uid, ref_type, ref_value,
                               raw_content, data):
    """msg_type=7 媒体消息兜底：上传 file_info → send_to_* with media。
    media 不解析 <@openid>，content 这里要先 humanize 成可读 @昵称。

    ``ref_type=''`` 表示主动消息(全量群配额耗尽路径):kwargs 留空。
    """
    from core.message.media import upload_media_bytes  # 延迟导入

    prefix = 'users' if is_uid else 'groups'
    upload_ep = f"/v2/{prefix}/{target_id}/files"
    try:
        file_info = await upload_media_bytes(sender, data, 1, upload_ep)
    except Exception as e:
        log.warning(f'图片上传异常: {e}')
        return
    if not file_info:
        log.warning(f'图片上传失败 → {target_id}')
        return

    # msg_type=7 的 content 是纯文本(QQ 不按 markdown 解析):humanize 提及后
    # 再把源头(cb_get_user_name)给昵称加的 md 转义还原,避免露出反斜杠
    rendered_content = helpers.strip_md_escapes(helpers.humanize_mentions(raw_content))
    media_dict = {'file_info': file_info}
    kwargs = {ref_type: ref_value} if ref_type else {}
    try:
        with log_attribution.mark_outbound():
            if is_uid:
                await sender.send_to_user(target_id, rendered_content, media=media_dict, **kwargs)
            else:
                await sender.send_to_group(target_id, rendered_content, media=media_dict, **kwargs)
    except Exception as e:
        log.warning(f'发送图片失败 ({target_id}): {e}')
