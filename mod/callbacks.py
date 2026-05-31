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
from . import state, quota, helpers, boot, uploader, userdb, buttons, log_attribution
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
# 严重问题通知群 openid —— 由 config.py::_apply_runtime_tunables 按 yaml 配置覆盖。
# 空字符串 = 不推送。设了的话,引擎崩溃时除了给玩家发道歉,还向此群主动推送
# 一条崩溃报告。通常填管理员监控的全量群 —— 该群在 QQ 后台开了全量推送权限,
# bot 才能向它走主动消息(没 msg_id 引用)。
CRASH_NOTIFY_GROUP: str = ''

# 沙箱私信用户 openid 集合 —— 由 config.py::_apply_runtime_tunables 按 yaml 的
# sandbox_dm_users 列表覆盖。当前 QQ 官方机器人**只有沙箱环境**的私信能发
# 主动消息(无 msg_id 引用),正式环境私信主动消息一律被拒。所以把沙箱测试
# 账号的 openid 填进来:这些用户的私信跳过被动配额,直接走主动消息直推
SANDBOX_DM_USERS: frozenset = frozenset()
# 信号编号 → 名称,日志里更可读
_SIG_NAMES = {6: 'SIGABRT', 7: 'SIGBUS', 11: 'SIGSEGV'}
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
    # 主框架其他清理工作(WebUI 日志 flush、userdb 收尾等)。中途若工作线程退出
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


async def _try_send_crash_apology(target_id: str, is_uid: bool) -> None:
    """走标准发送通道把道歉送达 —— 复用现有 quota/sender 设施。

    注意不能挂额外按钮 —— 进程马上要 execv 重启,任何 callback 都会落空。
    """
    try:
        page_logs.log_outgoing(target_id, is_uid, _CRASH_APOLOGY_MD)
        await _send_text_quota_managed(target_id, is_uid, _CRASH_APOLOGY_MD,
                                       buttons.build_support_buttons())
    except Exception as e:
        log.warning(f'崩溃道歉发送失败 ({target_id}): {e}')


async def _try_send_crash_notification(notify_group: str, sig_name: str,
                                       uid: str, gid: str, is_uid: bool,
                                       msg_len: int) -> None:
    """向严重问题通知群推送一条**主动消息**汇报崩溃。

    用 ``sender.send_to_group(group_id, content)`` 不带 ``msg_id``/``event_id``
    走 push API —— 仅在通知群 QQ 后台给本 bot 开了「全量推送」权限时能落地。
    没权限就会被 QQ 拒,这里只打 warning 不报错,30s 后 execv 照常进行。

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
        f'进程将在 **{_LGTBOT_CRASH_DELAY_S:.0f} 秒**后自动重启···\n'
        '\n'
        '> 💡 此消息为自动推送，请尽快联系开发者排查修复'
    )
    page_logs.log_outgoing(notify_group, False, md)
    try:
        with log_attribution.mark_outbound():
            await sender.send_to_group(notify_group, md)
    except Exception as e:
        log.warning(f'崩溃通知群推送失败 ({notify_group}): {e}')


# ──────── 「刷新按钮使用说明」教学提示(game_started 触发,紧跟开局公告发出) ────
# 触发流:LGTBot_ElainaBot.cc::ClassifyMatchEvent 识别引擎「游戏开始,您可以使用」
# 广播 → 调 cb_match_event(kind='game_started') → 此处把 key 记入
# _pending_tip_keys。**真正的发送时机被推迟到本帧的 cb_send_text_message /
# cb_send_image_message 把引擎那条开局公告排进 asyncio 发送队列之后**:
#   1. C++ 调 cb_match_event(只标记,不立刻发) → 立即返回
#   2. C++ 调 cb_send_text_message → 投递「开局公告」send task 到 asyncio + per-key
#      Lock 排队(见下面 _send_locks);Lock 保证 QQ 端按 cb 调用顺序送达
#   3. 开局公告 send task 跑完后,我们才在同一个 task 末尾调 _consume_pending_tip
#      → 调度教学提示 task,后者再次抢同一把 Lock 排到开局公告后面 → 顺序得证。
_pending_tip_keys: set[str] = set()

# ─────────────────────────────────────────────────────────────────────────
# 「带开局私信」游戏白名单 —— 此集合内的游戏在 cb_match_event(kind='new_game')
# 时会被记入 _pending_dm_warn_keys,在开局公告发完后追加一条「主动私信受限」
# 提示给用户。
#
# 触发逻辑:
#   1. C++ 引擎调 cb_match_event(kind='new_game', game_name='XXX')
#   2. 若 'XXX' 在 _DM_LIMITED_GAMES 内,key 进 _pending_dm_warn_keys
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

_DM_WARNING_TEXT = (
    '## ⚠️ 主动私信受限\n'
    '此游戏存在**主动私信**，会受到协议限制发送失败。\n'
    '请在游戏中**私信机器人**发送“赛况”来短暂激活私信和查看私信信息'
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

# 全量申请段 —— 只在群聊里拼到末尾,私信里没有「群号」概念,这段会显得突兀
_REFRESH_TIP_GROUP_TAIL = (
    '\n'
    '\n'
    '> 💡 群主授权群聊消息权限后可规避此限制，点击下方按钮发送：\n'
    '> **全量申请 <本群群号>**\n'
    '> 然后按照图片提示进行操作'
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
    """若本 key 之前在 cb_match_event 里被打了 game_started 标记,这里弹掉并发出。

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
    if is_uid and target_id in SANDBOX_DM_USERS:
        log.debug(f'沙箱私信用户 {target_id} 跳过刷新按钮使用说明')
        return
    _schedule_refresh_tip(target_id, is_uid)


# ─────────── 「带开局私信」游戏限制提示 ──────────────────────────────────
# 结构跟上面 _consume_pending_tip / _schedule_refresh_tip 完全对称 —— 在
# cb_match_event(kind='new_game') 阶段判定游戏名是否在 _DM_LIMITED_GAMES
# 内并打 _pending_dm_warn_keys 标记;真正的发送时机由 _serialized_text/
# image_send 在开局公告同步落地后调 _consume_pending_dm_warn 触发。

async def _send_dm_warning(target_id: str, is_uid: bool) -> None:
    """走标准 ``_send_text_quota_managed`` 通道发出「私信限制」提示。

    与 ``_send_refresh_tip`` 同一把 per-target Lock,保证排在「房间已创建」
    公告之后到达 QQ。底部挂一个「💫 添加好友」link 按钮,链接是
    ``_build_robot_invite_link`` 的同款邀请页 —— 用户点开后 QQ 客户端会让
    他选「添加为好友」或「邀请到群」。
    """
    key = helpers.target_key(target_id, is_uid)
    extra = buttons.build_dm_warning_buttons()
    try:
        async with _get_send_lock(key):
            page_logs.log_outgoing(target_id, is_uid, _DM_WARNING_TEXT)
            await _send_text_quota_managed(target_id, is_uid, _DM_WARNING_TEXT, extra)
    except Exception as e:
        log.debug(f'私信限制提示发送失败 ({target_id}): {e}')


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
      ``new_game``       刷新游戏名;在下一条文本回复挂「加入 / 退出 + 规则」。
      ``join_leave``     刷新游戏名;同上挂「加入 / 退出 + 规则」(玩家加入/
                         退出时也补一个规则按钮,方便随时查阅)。
      ``all_left``       清空当前游戏名;挂「游戏列表 / 创建房间」引导。
      ``terminate``      清空当前游戏名,不挂按钮(/新游戏 前置解散 / 管理员
                         主动结束等场景,紧接着会有真正的新建消息覆盖,或就该
                         安静收尾)。
      ``game_started``   引擎 Match::GameStart 成功后的 BoardcastAtAll —— 不动
                         按钮,只把 key 记入 ``_pending_tip_keys``;真正发出
                         「刷新按钮使用说明」由本帧 cb_send_text/image 同步
                         送完开局公告后立刻触发(_consume_pending_tip)。比
                         Python 侧匹配 /开始 用户输入更可靠:用户瞎敲 /开始
                         不在房间里时引擎不会广播这条,自然不会误教学。
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

    # 状态更新
    if kind in ('all_left', 'terminate'):
        state.current_game.pop(key, None)
    elif game_name:
        state.current_game[key] = game_name

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
        # 新建房间时若该游戏带「开局私信」,先打标;待开局公告通过 cb_send_text
        # 同步发完后,_consume_pending_dm_warn 会调度独立 task 追发「私信限制」
        # 提示(per-target Lock 保证顺序在公告之后)。
        if kind == 'new_game' and game_name and game_name in _DM_LIMITED_GAMES:
            _pending_dm_warn_keys.add(key)
    elif kind == 'all_left':
        state.pending_buttons[key] = buttons.build_dissolve_buttons()
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
    elif kind == 'game_started':
        # 仅标记 —— 本帧 cb_send_text_message / cb_send_image_message 在引擎
        # 开局公告同步落地后调 _consume_pending_tip 真正发出教学提示,保证 QQ
        # 端的顺序是「游戏开始」→「刷新按钮使用说明」,而不是反过来。
        _pending_tip_keys.add(key)
    # 'announce' / 'terminate' 不挂按钮


def cb_get_user_name(uid: str) -> str:
    """C++ → Python：返回用户昵称（DB 未命中时返回 uid 兜底）"""
    return userdb.get_name(uid) or uid


def cb_get_user_avatar_url(uid: str) -> str:
    """C++ → Python：返回头像 URL

    优先从 userdb 取（写入时 appid 正确，多 bot 部署下用 DB 缓存的 URL
    比临时 fallback 更准确）；DB 未命中（如历史排行榜里的离线用户）则
    用任一活跃 Bot 的 appid 临时推导，不写回 DB —— 等用户下次发消息时
    dispatcher 会用真正的 event.appid 落盘。
    C++ 端 DownloadUserAvatar 会用 libcurl 下载，失败则跳过。
    """
    cached = userdb.get_avatar(uid)
    if cached:
        return cached
    try:
        from core.bot.manager import _bot_manager_ref
        if _bot_manager_ref and _bot_manager_ref._bots:
            appid = next(iter(_bot_manager_ref._bots.keys()))
            return helpers.QQ_AVATAR_URL.format(appid=appid, openid=uid)
    except Exception:
        pass
    return ''


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
    page_logs.log_outgoing(target_id, is_uid, msg)

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

    # 沙箱私信用户:QQ 仅沙箱环境私信可发主动消息。逻辑与全量群完全一致 ——
    # 前 5 次仍用 msg_id 被动回复(消耗配额),仅配额耗尽后才直接主动消息。
    is_sandbox_dm = is_uid and (target_id in SANDBOX_DM_USERS)
    # 全量群判定:只看运行时观测到的事实(state.full_volume_groups),不再退回
    # 框架 non_at_message.* 配置 —— 配置可能与 QQ 后台权限不同步,误判会让
    # 非全量群也走主动消息(QQ 必拒,把 bot 的配额烧掉)。
    is_full = (not is_uid) and helpers.is_full_volume_group(target_id)
    # 主动直推资格(全量群 / 沙箱私信):配额满后可直接主动消息、不挂刷新按钮。
    # 注意"资格"不代表跳过被动配额 —— 前 5 次照常 try_consume_ref 走 msg_id。
    is_active_push = is_full or is_sandbox_dm

    consumed = quota.try_consume_ref(key)
    if consumed is None:
        if is_active_push:
            # 全量群 / 沙箱私信:配额满 → 直接主动消息,不等刷新按钮
            tag = '私信直推' if is_sandbox_dm else '全量直推'
            log.info(f'⚡ [{tag}] {key} 配额已满，走主动消息: {msg_preview!r}')
        elif is_uid and not quota.has_valid_ref(key):
            # 普通私信 + 无有效 msg_id(从未私信过 / 已超 5 分钟过期):正式环境
            # 主动私信必拒 → 直接丢弃,只留一行 audit 日志。
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
    # 日志展示用 humanize 版（更可读），实际发送时再按通道决定是否 humanize
    page_logs.log_outgoing(
        target_id, is_uid,
        helpers.humanize_mentions(raw_content), image=True,
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
    # 沙箱私信 / 全量群:前 5 次仍走 msg_id 被动回复,仅配额耗尽后主动直推
    # (逻辑同 _send_text_quota_managed,详见那里的注释)
    is_sandbox_dm = is_uid and (target_id in SANDBOX_DM_USERS)
    is_full = (not is_uid) and helpers.is_full_volume_group(target_id)
    is_active_push = is_full or is_sandbox_dm

    consumed = quota.try_consume_ref(key)
    if consumed is None:
        if is_active_push:
            tag = '私信直推' if is_sandbox_dm else '全量直推'
            log.info(f'⚡ [{tag}] {key} 配额已满，图片走主动消息')
        elif is_uid and not quota.has_valid_ref(key):
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

    # ── 通道 A：尝试图床 → markdown 内嵌 ─────────────────────────────────
    user_id_for_cos = target_id if is_uid else ''
    image_url = await uploader.upload_image(data, filename, user_id=user_id_for_cos)
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

    rendered_content = helpers.humanize_mentions(raw_content)
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
