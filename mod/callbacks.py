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
  · _serialized_mixed_send           Lock → _send_mixed_message → 消费教学标记
  · _send_text_quota_managed         自动追加刷新按钮,交给 _deliver 发送
  · _send_mixed_message              图文混排:全图上传成功 → 单条 markdown 排版内联;否则退回逐图媒体通道
  · _send_image_quota_managed        上传 + markdown / media 二选一,交给 _deliver 发送
  · _deliver                         选通道(_pick_route:被动引用 / 主动 / 丢弃)+ 发送失败的换引用 / 频控退避重发

设计要点：cb_send_text/image_message 不阻塞 C++ 调用线程 —— 回调期间引擎持有
Match.mutex_(见 cb_send_text_message)。
"""

from __future__ import annotations
import asyncio
import concurrent.futures
import os
import random
import re
import sys
import time

from core.base.logger import get_logger, PLUGIN
from . import (state, quota, helpers, boot, uploader, userinfo, buttons,
               log_attribution, metrics, audit)
from .webui import page_logs

log = get_logger(PLUGIN, 'LGTBot')


# ──────── lgtbot 段错误恢复(C++ 桥接层 SigSegvHandler → 这里) ─────────────
# 一旦 lgtbot 内部触发 SIGSEGV/SIGBUS,bridge 的 wrapper 用 sigsetjmp/siglongjmp 把控制权拽回 Python,然后调本函数善后。
# 注意此时 lgtbot 进程内状态损坏(mutex/heap/pipe 都可能是脏的),所以这里**不再调任何 lgtbot 函数**,只做Python 侧的事:
# 发日志 + 给玩家道歉 + 调度 30s 后整进程 execv。

_LGTBOT_CRASH_DELAY_S = 30.0       # 倒计时 execv;给 framework 其他清理留 buffer
# 工作线程阻塞等道歉 / 通知 HTTP 发完的最长秒数:必须趁线程退出触发 SIGABRT 之前发完
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
    '机器人已自动重启恢复服务，可以重新开局 ✅\n'
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

# 通知群 openid 列表 —— 由 config.py::_apply_runtime_tunables 按 yaml 的
# ``notify_groups`` 覆盖。空 = 不推送。三类通知都会**向列表里的全部群**推送:
#   · 引擎崩溃报告(_try_send_crash_notification)
#   · 崩溃死循环熔断告警(_alert_crash_loop_tripped)
#   · 自动重启说明(dispatcher._notify_auto_restart)
# 通常填管理员监控的全量群 —— 这些群要给本 bot 开全量推送权限,主动消息(没 msg_id 引用)才能落地。
NOTIFY_GROUPS: tuple = ()

# 私信主动直推资格 —— 两个变量均由 config.py::_apply_runtime_tunables 按 yaml 的
# sandbox_dm_users 覆盖:
#   · 列表恰好为 ['all'] → DM_PUSH_ALL=True,对**全部用户**私信直推
#     (QQ 默认允许 bot 向好友推送主动私信,用户可在权限设置中自行关闭)。
#   · 其他 → 白名单:仅列表内用户(沙箱测试号)直推,其余私信在无有效 msg_id 时丢弃。
# 两种模式下「直推资格」都不跳过被动配额:被动次数用完才走主动消息(见 _pick_route)。
SANDBOX_DM_USERS: frozenset = frozenset()
DM_PUSH_ALL: bool = False


# 单群 / 单用户每日主动消息上限(QQ 官方接口限制)。由 config.py 的 ``active_push_daily_limit`` 覆盖;0 = 不限制。
# 用满后**当日**该目标失去「主动直推资格」,退回刷新按钮机制;计数走 metrics 的日分桶,**次日 0 点自然重置**。
# 每条消息独立判定、不缓存资格,进行中的对局跨天也无需特殊处理。
ACTIVE_PUSH_DAILY_LIMIT: int = 1000


def _active_push_allowed(target_id: str, is_uid: bool) -> bool:
    """该目标今日主动消息额度是否还有剩余(未配额度 / 上限 0 = 恒 True)。"""
    limit = ACTIVE_PUSH_DAILY_LIMIT
    if limit <= 0:
        return True
    return metrics.active_push_used(target_id, is_uid) < limit


def _is_sandbox_dm(target_id: str, is_uid: bool) -> bool:
    """私信目标是否具备「配额耗尽后主动直推」资格(all 模式 = 所有人)。"""
    return is_uid and (DM_PUSH_ALL or target_id in SANDBOX_DM_USERS)
# 信号编号 → 名称,日志里更可读。数字 key 对应 SigSegvHandler 路径(C++ bridge 直接传 int);
# 字符串 key 对应崩溃 marker 文件里的 sig=<kind> 字段。
_SIG_NAMES = {
    6: 'SIGABRT', 7: 'SIGBUS', 11: 'SIGSEGV',
    'cxx_terminate': 'C++ 异常未捕获',
    'sigabrt': 'SIGABRT (堆损坏 / double-free)',
}
_crash_handled = False             # 防多线程并发崩溃时重复触发善后


def cb_lgtbot_crashed(uid: str, gid: str, is_uid: bool, msg: str, sig: int) -> None:
    """C++ bridge → Python:lgtbot 触发 SIGSEGV/SIGBUS 被 wrapper 捕获恢复后调本函数。

    被调时 GIL 已由 wrapper 抢回(``PyGILState_Ensure``),Python C API 可用。
    实际发送放到 asyncio loop 上跑。
    """
    global _crash_handled
    if _crash_handled:
        # 多线程并发崩溃只处理第一条 —— 后面那些都是同一波连锁反应,进程很快会被 execv 替换
        return
    _crash_handled = True

    sig_name = _SIG_NAMES.get(sig, f'sig{sig}')
    # 单行 target 只进本地日志 / 审计;通知群消息由 _try_send_crash_notification 另行排版
    target = (f'用户 {uid}' if is_uid else f'群聊 {gid} 用户 {uid}')
    preview = (msg or '')[:80].replace('\n', ' ')

    # 关键 ERROR 日志 —— 主框架 WebUI 消息日志 / 全局日志都能看到
    log.error('=' * 60)
    log.error(f'💥 LGTBot 引擎崩溃 ({sig_name})')
    log.error(f'   触发源: {target}')
    log.error(f'   消息内容: {preview!r}')
    # C++ bridge (SigSegvHandler → DumpCrashToFile) 已经把栈 dump 落盘到：
    # <plugin_dir>/LGTBot_CRASH_DUMPS/crash_<sec>_<pid>_<tid>.log
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

    # 指标 + 审计同步写盘且永不抛,必须趁现在写:工作线程 return 时若撞出 SIGABRT,C++ 侧会立刻 execv,后面的代码都跑不到。
    metrics.record_crash(sig_name)
    audit.record('restart', '引擎崩溃自动重启',
                 f'{sig_name}；触发源 {target}；'
                 f'{_LGTBOT_CRASH_DELAY_S:.0f}s 后 os.execv 自启',
                 src=audit.SRC_AUTO)

    # 异步善后:发道歉 + 倒计时 + execv。
    loop = state.event_loop
    if loop is None or loop.is_closed():
        # 没 loop 就只能立即退出让 supervisor 重启 —— 道歉就送不出了,但不至于卡死。
        log.error('asyncio loop 不可用，直接 os.execv')
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            log.error(f'os.execv 失败，需 supervisor 兜底: {e}')
        return
    # preview(用户原文)只用于本地 log.error,通知里只给长度(见 _try_send_crash_notification 的安全约束)
    msg_len = len(preview)

    # ── Phase 1: **阻塞当前工作线程**等道歉 + 通知 HTTP 发完 ───────────────────
    # 本函数跑在出错的工作线程上,它一 return 就进入退出流程,极可能在 glibc tcache_thread_shutdown 撞坏 heap 触发 SIGABRT;
    # 必须趁它还活着把崩溃报告与道歉同步发完,超时就放弃未完成的发送。
    try:
        send_fut = asyncio.run_coroutine_threadsafe(
            _send_crash_messages(uid, gid, is_uid, sig_name, msg_len), loop)
        send_fut.result(timeout=_CRASH_SEND_TIMEOUT_S)
    except concurrent.futures.TimeoutError:
        log.warning(f'崩溃消息发送超时(>{_CRASH_SEND_TIMEOUT_S:.0f}s),仍继续重启流程')
    except Exception as e:
        log.warning(f'崩溃消息发送异常,仍继续重启流程: {e}')

    # ── Phase 2: 调度 30s 后整进程 execv (不阻塞,asyncio loop 跑) ────────────
    # 30s 留给主框架其他清理(WebUI 日志 flush、框架写队列落盘等);
    # 其间工作线程退出若触发 SIGABRT,C++ 桥接层的 SigAbrtHandler 会用预存的 execv 参数立即重启。
    try:
        asyncio.run_coroutine_threadsafe(
            _post_send_countdown(sig_name), loop)
    except Exception as e:
        log.error(f'调度重启倒计时失败,直接 os.execv: {e}')
        os.execv(sys.executable, [sys.executable] + sys.argv)


async def _send_crash_messages(uid: str, gid: str, is_uid: bool,
                               sig_name: str, msg_len: int) -> None:
    """同步阻塞路径:并发发道歉 + 通知,worker 线程通过 ``Future.result`` 等完。

    ``return_exceptions=True`` 保证一边失败不影响另一边(尤其通知群推送);内层 ``wait_for`` 兜底,
    免得 HTTP hung 把整个 future 拖到外层超时才被砍。
    """
    coros = []
    # 优先级:通知群 > 道歉。先 append 表示在 gather 里优先调度,实际 HTTP 并发。
    if NOTIFY_GROUPS:
        coros.append(_try_send_crash_notification(
            sig_name, uid, gid, is_uid, msg_len))
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

    ``is_belated=True``(补发路径,进程已经重启完成)用 ``_CRASH_APOLOGY_MD_BELATED``。
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
    (崩溃即将 execv,没时间等 15s;普通群主动会被 QQ 拒、全量群 / 私聊可达,尽力送达)。"""
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


async def broadcast_notify(md: str, label: str, *, timeout: float = 8.0) -> int:
    """把一条**主动消息**广播到 ``NOTIFY_GROUPS`` 的全部群,返回成功条数。

    不带 ``msg_id``/``event_id`` 走 push API —— 仅在该群 QQ 后台给本 bot 开了「全量推送」权限时能落地。

      · **并发发送 + ``return_exceptions=True``**:一个群失败绝不能连累其他群。
      · **每条独立 ``wait_for``**:崩溃善后路径整体只有 8s 预算,不能让一个 hung 住的 HTTP 把其他群的推送一起拖死。
      · 失败只 ``warning``,绝不抛 —— 调用方(崩溃善后 / 重启流程)不能被通知推送反过来打断。
      · 未配置通知群 / 拿不到 sender → 返回 0 静默跳过。
    """
    if not NOTIFY_GROUPS:
        return 0
    sender = helpers.get_sender('')
    if sender is None:
        log.warning(f'无可用 sender，跳过{label}推送')
        return 0

    async def _one(group_id: str) -> bool:
        page_logs.log_outgoing(group_id, False, md)
        try:
            with log_attribution.mark_outbound():
                await asyncio.wait_for(sender.send_to_group(group_id, md),
                                       timeout=timeout)
            return True
        except Exception as e:
            log.warning(f'{label}推送失败 ({group_id}): {e}')
            return False

    results = await asyncio.gather(*(_one(g) for g in NOTIFY_GROUPS),
                                   return_exceptions=True)
    ok = sum(1 for r in results if r is True)
    if ok < len(NOTIFY_GROUPS):
        log.warning(f'{label}:{len(NOTIFY_GROUPS)} 个通知群中 {ok} 个送达')
    return ok


def restart_room_message(reason: str) -> str:
    """重启前发给「等待中房间」的简要通知。"""
    parts = ['## 🔁 LGT-Bot 即将重启', '',
             '> 本群有**尚未开始**的房间，已被清理']
    if reason:
        parts += ['', f'📌 更新内容：{helpers.sanitize_md_name(reason)}']
    parts += ['', '⏳ 预计 10 秒内恢复服务...']
    return '\n'.join(parts)


def snapshot_waiting_rooms() -> list:
    """快照当前等待中房间(带 ``key``),供重启路径**在释放引擎之前**取。"""
    return [dict(r, key=k) for k, r in state.waiting_rooms.items()]


async def notify_restart_rooms(reason: str = '', *, skip_keys=(),
                               rooms=None) -> int:
    """重启前给所有**等待中房间**所在的群推一条通知,返回送达数。

    ``rooms`` 传 ``snapshot_waiting_rooms()`` 的结果(重启路径必须这么传);
    省略时读当前的 ``state.waiting_rooms``。

    只发**有主动推送权限**的群:重启这一刻多半没有可用的被动引用,没权限的群直接丢弃。
    私信房间不在范围内:私信 ``/新游戏`` 直接开局,不存在「等待中」的房间。

    ``skip_keys`` 用来去重:自动重启已经给通知群推过一条了,那些群不必再收一条。

    并发发送 + 每条独立超时:夹在「引擎已释放」与 ``os.execv`` 之间,不能被某个群的慢请求拖住整个重启。
    """
    if rooms is None:
        rooms = snapshot_waiting_rooms()
    picked = [r for r in rooms
              if not r.get('is_uid') and r.get('target_id')
              and r.get('key') not in skip_keys]
    targets = [r['target_id'] for r in picked if helpers.can_push_group(r['target_id'])]
    log.info(f'🔁 [重启通知] 等待中房间 {len(rooms)} 个，'
             f'跳过(私信 / 已通知) {len(rooms) - len(picked)} 个，'
             f'可主动推送 {len(targets)} 个')
    if not targets:
        return 0
    sender = helpers.get_sender('')
    if sender is None:
        log.warning('无可用 sender，跳过重启房间通知')
        return 0
    md = restart_room_message(reason)

    async def _one(gid: str) -> bool:
        page_logs.log_outgoing(gid, False, md)
        try:
            with log_attribution.mark_outbound():
                await asyncio.wait_for(sender.send_to_group(gid, md), timeout=5.0)
            return True
        except Exception as e:
            log.warning(f'重启通知推送失败 ({gid}): {e}')
            return False

    results = await asyncio.gather(*(_one(g) for g in targets),
                                   return_exceptions=True)
    ok = sum(1 for r in results if r is True)
    log.warning(f'🔁 [重启通知] {ok}/{len(targets)} 个群送达')
    return ok


async def _try_send_crash_notification(sig_name: str, uid: str, gid: str,
                                       is_uid: bool, msg_len: int,
                                       *, is_belated: bool = False) -> None:
    """向全部通知群推送一条**主动消息**汇报崩溃(发送细节见 broadcast_notify)。

    ``is_belated=True`` 走补发路径(OnCxxTerminate marker):此时进程已经重启完成,
    状态行改为「机器人已自动重启恢复服务」。

    **安全约束:** **不把触发崩溃的用户原文(preview)拼进 markdown** —— 否则用户可故意发违规/敏感内容
    借崩溃路径让 bot 转发,触发风控扣分甚至封号。只展示 bot 完全可控的字段(信号名 / openid / 长度数字),
    全部塞进单个代码块里;完整 preview 只在服务端 ``log.error`` 里。
    """
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
    await broadcast_notify(md, '崩溃通知群')


# ──────── 上一轮 C++ terminate 路径补发道歉/通知 ─────────────────────────
# 触发流:lgtbot 内部抛 std::bad_alloc 等 C++ 异常未被 catch → OnCxxTerminate
# 在 LGTBot_ElainaBot.cc 内执行:
#   1. async-signal-safe 写 marker 文件
#      `<plugin_dir>/LGTBot_CRASH_DUMPS/pending_apology_<sec>_<pid>_<tid>.txt`
#   2. execv 重启整进程
# 新进程 @on_load 调本模块 ``recover_pending_apologies``,异步补发道歉 + 通知,
# 然后删 marker。不像 cb_lgtbot_crashed 那样当场发:OnCxxTerminate 跑在 C++ 异常上下文里,
# Python C API / heap 都不可信,只能落地到文件再让干净进程接力。
_PENDING_APOLOGY_PREFIX = 'pending_apology_'
_PENDING_APOLOGY_SUFFIX = '.txt'
# 启动后延迟 5s 再补发 —— 让 @on_load 完成、bot manager 就绪、网络通畅
_BELATED_APOLOGY_DELAY_S = 5.0

# ── 崩溃死循环熔断 ─────────────────────────────────────────────────────────
# C++ 侧 abort-class handler(SigAbrtHandler / OnCxxTerminate)在 execv 前往
# `LGTBot_CRASH_DUMPS/abort_restart_history` 追加一行重启时间戳。
# 若 heap 腐败是确定性的,会每次重启又立刻 abort → 紧凑 execv 死循环,由 check_crash_loop 熔断。
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
        if NOTIFY_GROUPS:
            coros.append(_try_send_crash_notification(
                sig_name, uid, gid, is_uid, msg_len, is_belated=True))
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
    """向全部通知群推送一条「崩溃死循环已熔断」主动消息。未配置通知群时静默跳过。"""
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
        '> 💡 此消息为自动推送，请尽快联系开发者手动重启'
    )
    await broadcast_notify(md, '崩溃熔断告警')


# ──────── 「消息回复限制」教学提示(新建房间触发,紧跟建房公告发出) ──────────
# 触发流:LGTBot_ElainaBot.cc::ClassifyMatchEvent 识别引擎「现在玩家可以…」建房
# 广播(/新游戏、/随机游戏 共用同一条 NewMatch 广播)→ 调 cb_match_event(kind='new_game') →
# 此处把 key 记入 _pending_tip_keys。**真正的发送时机被推迟到建房公告发完之后**:
#   1. C++ 调 cb_match_event(只标记,不立刻发) → 立即返回
#   2. C++ 调 cb_send_text_message → 投递「房间已创建」send task 到 asyncio + per-key
#      Lock 排队(见下面 _send_locks);Lock 保证 QQ 端按 cb 调用顺序送达
#   3. 建房公告 send task 跑完后,我们才在同一个 task 末尾调 _consume_pending_tip
#      → 调度教学提示 task,后者再次抢同一把 Lock 排到建房公告后面 → 顺序得证。

# 挂在建房而非开局:建房广播是对 /新游戏 命令 msg_id 的**第 1 条**回复,教学提示紧随其后必在被动配额内;
# 开局消息发得晚(开局刷屏高峰),常常已超配额把提示吞掉。单机局无 new_game 广播 → 不发教学。
_pending_tip_keys: set[str] = set()

# ─────────────────────────────────────────────────────────────────────────
# 「带开局私信」游戏白名单 —— 此集合内的游戏在**能主动推送的群**里建房(new_game)时
# 记入 _pending_dm_warn_keys,建房公告发完后追加一条「主动私信」提示,排队方式同上面的教学提示。
# 与「消息回复限制」教学**互斥**:不能推送的群发回复限制教学(覆盖面更广),私信提示被抑制。
# **私信里新建游戏不提示**(玩家已在私信会话内)。
# ─────────────────────────────────────────────────────────────────────────
_DM_LIMITED_GAMES: frozenset = frozenset({
    '谁是牛头王', 'wordle', '蓄攻防', '情书',
    '阿瓦隆', 'HP杀', '一夜动物杀', '末日之眼', '杀手',
    '无限牌', '十全牌', '数点', '圣约召唤',
    '漫漫长夜', '法则迷宫',
    '十七步', '同步麻将', '德州波卡', '幸运波卡',
})

_pending_dm_warn_keys: set[str] = set()

# 不计分对局的补记 —— 引擎不把它们写进 lgtbot.db,今日统计因此看不到。
# 「不记录」的另两种原因(单机局 / 未连库)不属于不计分,marker 取得足够窄把它们排除在外。
_UNRANKED_MARKER = '游戏结果不记录：因为该游戏为非正式游戏'
# 原因与参与者只在结算正文里,游戏名反过来只在 cb_match_event 拿得到 —— 在这里寄存,由紧随其后的那条结算文本取走。
_pending_unranked: dict[str, str] = {}

# 白名单模式(正式环境主动私信被拒,发出去会失败)下的受限警告
_DM_WARNING_TEXT_LEGACY = (
    '## ⚠️ 主动私信受限\n'
    '此游戏存在**主动私信**，会受到协议限制发送失败。\n'
    '请在游戏中**私信机器人**发送“赛况”来短暂激活私信'
)

# 全员直推(sandbox_dm_users: ['all'])模式
_DM_WARNING_TEXT_ALL = (
    '## 💬 主动私信提醒\n'
    '此游戏存在**主动私信**。请在机器人头像→权限设置开启**主动消息**权限'
)

# ──────── per-target 串行化:发到同一 target 的消息按 cb 调用顺序送达 QQ ────────
# cb_send_text/image_message 投递到 asyncio loop 立即返回;per-target asyncio.Lock 保证发到同一 target
# 的消息按 cb 调用顺序送达 QQ(asyncio FIFO + Lock 串行)。
_send_locks: dict[str, asyncio.Lock] = {}


def _get_send_lock(key: str) -> asyncio.Lock:
    """懒创建 per-target Lock。只能从 asyncio loop 调(单线程,dict get/setdefault 安全)。"""
    lock = _send_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _send_locks[key] = lock
    return lock

# 教学文案按场景给出真实限制(官方「被动消息」表:群 5 条/5 分钟,单聊 4 条/60 分钟)
_REFRESH_TIP_BASE_GROUP = (
    '## ⚠️ 消息回复限制\n'
    '机器人每条消息**最多回复5次**，且**5分钟**后失效。\n'
    '🔄 ***请及时点击刷新按钮***，否则将**影响消息接收和游戏进程**。'
)
_REFRESH_TIP_BASE_DM = (
    '## ⚠️ 私信回复限制\n'
    '机器人每条消息**最多回复4次**，且**60分钟**后失效。\n'
    '🔄 ***请及时点击刷新按钮***，否则将**影响消息接收和游戏进程**。'
)

# 全量申请段 —— 只在群聊里拼到末尾,私信里没有「群号」概念
_REFRESH_TIP_GROUP_TAIL = (
    '\n'
    '\n'
    '> 💡 群主授权**主动消息权限**后可规避此限制，点击下方按钮或发送“全量申请”，然后按照提示进行操作'
)


async def _send_refresh_tip(target_id: str, is_uid: bool) -> None:
    """走标准 `_send_text_quota_managed` 通道发出教学提示。

    真正的刷新按钮由 ``_send_text_quota_managed`` 按用量自动挂载;教学消息本身视场景另带「全量申请」按钮。

    走 per-target Lock 排队 —— 跟 ``_serialized_text_send`` 共用同一把锁,
    保证教学提示永远在建房公告之后到达 QQ。

    分支:
      · 私信(``is_uid=True``):DM 版 BASE(4 条/60 分钟),无附加按钮。
      · 群聊(``is_uid=False``):群版 BASE(5 条/5 分钟)+ GROUP_TAIL 段,底部挂一行「全量申请」按钮。
    """
    if is_uid:
        msg = _REFRESH_TIP_BASE_DM
        extra = None
    else:
        msg = _REFRESH_TIP_BASE_GROUP + _REFRESH_TIP_GROUP_TAIL
        extra = buttons.build_full_volume_apply_button()
    key = helpers.target_key(target_id, is_uid)
    try:
        async with _get_send_lock(key):
            page_logs.log_outgoing(target_id, is_uid, msg)
            await _send_text_quota_managed(target_id, is_uid, msg, extra)
    except Exception as e:
        log.debug(f'消息回复限制说明发送失败 ({target_id}): {e}')


def _schedule_refresh_tip(target_id: str, is_uid: bool) -> None:
    """把 `_send_refresh_tip` 投到 asyncio loop,fire-and-forget(返回的 Future 故意不 await)。"""
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

    由 ``_serialized_text_send`` / ``_serialized_mixed_send`` 在 per-target Lock
    持有期间、本条已发完之后调用。教学提示走 ``_schedule_refresh_tip`` 投到 asyncio loop,
    内部再次抢同一把 Lock —— 当前 send task 释放锁后,教学提示 task 自然排到下一位。

    能主动推送的群(判据是 ``can_push_group``,不看是否全量)与直推私信用户不依赖刷新按钮,
    讲刷新按钮的教学只会误导 —— 只清掉标记,不发送。
    """
    if key not in _pending_tip_keys:
        return
    _pending_tip_keys.discard(key)
    if (not is_uid) and helpers.can_push_group(target_id):
        log.debug(f'主动群 {target_id} 跳过刷新按钮使用说明')
        return
    if _is_sandbox_dm(target_id, is_uid):
        log.debug(f'直推私信用户 {target_id} 跳过刷新按钮使用说明')
        return
    _schedule_refresh_tip(target_id, is_uid)


# ─────────── 「带开局私信」游戏限制提示 ──────────────────────────────────
# 结构跟上面 _consume_pending_tip / _schedule_refresh_tip 完全对称。

async def _send_dm_warning(target_id: str, is_uid: bool) -> None:
    """走标准 ``_send_text_quota_managed`` 通道发出「主动私信」提示。

    文案随模式切换:全员直推(``DM_PUSH_ALL``)用提醒版(加好友 + 开权限即可收到),
    白名单模式用受限警告版。两版底部都挂「💫 添加好友」link 按钮。

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
    """把 `_send_dm_warning` 投到 asyncio loop,fire-and-forget。"""
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

    调用时机同 ``_consume_pending_tip``。
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

      ``announce``       刷新 ``state.current_game[key]``;房间仍在
                         等待中时,再挂一组与 ``join_leave`` 相同的按钮。
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
                         私信对局全员 LEFT 后的解散广播私发不到任何人,这条可送达的
                         中途退出就是对局对该目标结束的最后信号;群聊对局仍在继续,
                         不动任何状态。
      ``game_over``      游戏自然结束的结算广播 —— 挂「📊 查看战绩 + 🔄 重开一局」。
                         结算广播无 brief,重开按钮的游戏名从 ``current_game`` 回查,
                         取完即清(对局已随结算释放)。
      ``game_over_unrecorded``  同上,但结算带「游戏结果不记录」(单机 / 非正式局 /
                         未连接数据库) —— 本局没进战绩,不挂「查看战绩」;
                         若游戏名也未知则整组不挂。
      ``game_started``   引擎 Match::GameStart 成功后的 BoardcastAtAll —— 挂
                         「🎮 游戏帮助」(广播本身在教玩家发「帮助」),并做
                         进行中对局跟踪(active_matches)。
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
    if kind == 'game_started':
        # 游戏名:多人局 current_game 已由 new_game 的 brief 写入,优先用;单机局没有 new_game、
        # game_started 又无 brief,回退到 dispatcher 从「/新游戏 X」命令抓下的 pending 名。pop 无论命中与否都清掉 pending。
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
        state.waiting_rooms.pop(key, None)     # 开局 = 离开「等待中」
    elif kind in ('new_game', 'join_leave'):
        # 等待中的房间(已建房、未开局)。join_leave 也写:建房广播偶尔拿不到 brief 时,后续加入/退出广播能把游戏名补上;since 保留首次建房时刻。
        prev = state.waiting_rooms.get(key) or {}
        state.waiting_rooms[key] = {
            'target_id': target_id,
            'is_uid': is_uid,
            'game': game_name or prev.get('game') or state.current_game.get(key, ''),
            'since': prev.get('since') or time.time(),
        }
    elif kind in ('game_over', 'game_over_unrecorded', 'all_left', 'terminate') \
            or (kind == 'mid_quit' and is_uid):
        state.active_matches.pop(key, None)
        state.waiting_rooms.pop(key, None)

    # 按钮挂载 —— new_game / join_leave 都挂同样一组:
    #   · 群聊:  「加入 / 退出」+ 「📖《X》规则」 两行
    #   · 私聊:  仅「📖《X》规则」一行(DM 里 /加入 /退出 无意义)
    # announce 也挂同一组(「设置成功」的回执带 brief,是常见的报名入口),但只限**等待中的房间**:
    # 对局已开始时挂「加入 / 退出」只会点出一个错误回执。
    if kind in ('new_game', 'join_leave') or (
            kind == 'announce' and key in state.waiting_rooms):
        btns = buttons.build_game_action_buttons(
            state.current_game.get(key),
            include_rule=True,
            include_join_leave=not is_uid,
        )
        if btns:
            state.pending_buttons[key] = btns
        if kind == 'new_game':
            # 「消息回复限制」教学(见 _pending_tip_keys 段注释);是否真发由 _consume_pending_tip 按目标过滤。
            _pending_tip_keys.add(key)
            # 「主动私信」提示只在回复限制教学**不会**发送的群里标记 —— 两条都跟在建房公告后太吵。
            if (not is_uid and game_name and game_name in _DM_LIMITED_GAMES
                    and helpers.can_push_group(target_id)):
                _pending_dm_warn_keys.add(key)
    elif kind == 'all_left':
        state.pending_buttons[key] = buttons.build_dissolve_buttons()
    elif kind in ('game_over', 'game_over_unrecorded'):
        # 结算广播不带 brief,重开按钮的游戏名从 current_game 回查;pop 取完即清 ——
        # 残留会让之后的按钮回查到已结束的游戏。
        game = state.current_game.pop(key, None)
        if kind == 'game_over_unrecorded':
            _pending_unranked[key] = game or ''
        btns = buttons.build_game_over_buttons(
            game, include_record=(kind == 'game_over'),
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
    elif kind == 'game_started':
        state.pending_buttons[key] = buttons.build_game_help_buttons()


def cb_get_user_name(uid: str) -> str:
    """C++ → Python：返回用户昵称(主框架 data.db users 表;未命中返回 uid 兜底)

    ``userinfo`` 查昵称同步且线程安全(缓存命中零 I/O;未命中走框架
    log_service 的独立只读连接),可从引擎工作线程直调。昵称经
    ``helpers.sanitize_md_name`` 按 markdown 语境转义。

    非 markdown 出站路径(媒体兜底 msg_type=7 / WebUI 消息日志)在各自出口
    用 ``helpers.strip_md_escapes`` 还原,不会露出反斜杠。已知残留:引擎自渲
    的对局图片(HTML)里带特殊字符的昵称会显示 ``\\`` 前缀。
    """
    return helpers.sanitize_md_name(userinfo.display_name(uid) or uid)


def cb_get_user_avatar_url(uid: str) -> str:
    """C++ → Python：返回头像 URL(按绑定 bot 的 appid 即时推导,不落库)

    QQ 官方头像直链仅由 appid + openid 决定,推导即最新 —— 换绑 bot 后也
    天然正确。无绑定 bot 时返回 '',C++ 端 DownloadUserAvatar 会跳过下载。
    """
    return userinfo.avatar_url(uid)


# ──────── 文本发送 ────────────────────────────────────────────────────────

# ──────── 代理身份回执的 @ 改写(%中断 群管代理用)─────────────────────────
# dispatcher 的 %中断 受限代理会把发给引擎的 uid 换成已配置的引擎管理员,于是引擎回执里的
# ``At(uid)`` 变成 <@引擎管理员>,而不是真正点了指令的那位群管。
#
# 这里给发送路径挂一个**一次性、限时、限 target** 的 mention 改写:dispatcher 代理派发前登记
# (target_key → 引擎管理员uid → 真实操作者uid),引擎的下一条回执命中即改写并立即注销。
# "一次性 + 5s 过期"是为了不误伤后续真的要 @ 该管理员的消息(例如该管理员本人正在这个群里玩游戏)。
_MENTION_REWRITE_TTL_S = 5.0
# key → (from_uid, to_uid, expires_at)。必须挂持久 dict:登记发生在热重载后的**新** dispatcher,
# 消费却在引擎复用时 C++ 仍持有的**旧** callbacks 模块里,模块级 dict 会让登记永远不被旧回调看到。
_mention_rewrites: dict = boot._get_persistent()['mention_rewrites']


def register_mention_rewrite(key: str, from_uid: str, to_uid: str) -> None:
    """登记一次性 @ 改写(供 dispatcher 的代理指令调用)。"""
    if not (key and from_uid and to_uid) or from_uid == to_uid:
        return
    _mention_rewrites[key] = (from_uid, to_uid,
                              time.monotonic() + _MENTION_REWRITE_TTL_S)


def _apply_mention_rewrite(key: str, msg: str) -> str:
    """若本 key 有未过期的改写登记且 msg 里确实含该 mention,替换并注销。"""
    ent = _mention_rewrites.get(key)
    if ent is None:
        return msg
    from_uid, to_uid, expires = ent
    if time.monotonic() > expires:
        _mention_rewrites.pop(key, None)
        return msg
    token = f'<@{from_uid}>'
    if token not in msg:
        return msg                      # 不是目标回执,留给下一条(直到过期)
    _mention_rewrites.pop(key, None)     # 一次性:命中即注销
    log.debug(f'[%中断] 回执 @ 改写: {from_uid} → {to_uid}')
    return msg.replace(token, f'<@{to_uid}>')


# 中断投票广播的识别串 —— 上游 match.cc::UserInterrupt 的 Boardcast:
#   「有玩家确定中断比赛，目前 N 人尚未确定中断，所有玩家可通过「/中断」…」
# 取「尚未确定中断」这段:N 是变量,前半段「确定 / 取消」也会变(取消中断走同一条广播),中间这几个字在两种情形下都在,且全库仅此一处出现。
# 这条判定要结合发起人身份,所以放在 Python 侧而不是 bridge 的 ClassifyMatchEvent。
_INTERRUPT_VOTE_MARKER = '尚未确定中断'


def _force_interrupt_buttons_for(key: str, msg: str):
    """群管发过 ``/中断`` 且本条正是中断投票广播 → 返回「强制中断游戏」按钮组。

    其余情况返回 None(不挂任何按钮),包括:普通玩家发起的中断投票、群管发起但本条不是那条广播(如「确定中断成功」回执)、标记已过期。
    标记一次性:命中即注销,免得同一个群里后续别人的中断投票也蹭到按钮。
    """
    expires = state.force_interrupt_hints.get(key)
    if not expires:
        return None
    if time.time() > expires:
        state.force_interrupt_hints.pop(key, None)
        return None
    if _INTERRUPT_VOTE_MARKER not in msg:
        return None                      # 不是目标广播,留给下一条(直到过期)
    state.force_interrupt_hints.pop(key, None)
    return buttons.build_force_interrupt_buttons()


def _record_unranked(key: str, target_id: str, is_uid: bool, msg: str) -> None:
    """把不计分对局补记进 metrics 的临时账本(见 _pending_unranked 段注释)。

    pop 无条件执行:寄存项只对紧随其后的这一条结算文本有效,留着会串到下一局。
    """
    game = _pending_unranked.pop(key, None)
    if game is None or _UNRANKED_MARKER not in msg:
        return
    metrics.record_unranked_match(game, helpers.mentioned_ids(msg),
                                  '' if is_uid else target_id)


def cb_send_text_message(target_id: str, is_uid: bool, msg: str):
    """C++ → Python：发送文本消息（fire-and-forget,不阻塞 C++ 调用线程）

    投递到 asyncio loop 后立即返回:回调期间引擎持有 ``Match.mutex_``,在这里阻塞会让
    后续指令与 OnGameOver 抢锁,写已关闭的管道而 SIGSEGV。per-target Lock 保证发到
    同一 target 的消息按 cb 调用顺序送达 QQ。

    本条回复要附的按钮（若有）已由 bridge 先调用 cb_match_event 写进
    state.pending_buttons[key]——同一次 HandleMessages 内顺序调用,
    GIL 保护下读写安全。这里 pop 出来跟着 send task 走。
    """
    key = helpers.target_key(target_id, is_uid)
    _record_unranked(key, target_id, is_uid, msg)
    extra_buttons = state.pending_buttons.pop(key, None)
    if not extra_buttons:
        # 群管发起的中断投票 → 给「还差 N 人」那条广播挂「强制中断游戏」;
        # 只在没有其他按钮时挂,不抢 cb_match_event 排好的按钮组。
        extra_buttons = _force_interrupt_buttons_for(key, msg)
    # 代理指令(%中断)的回执把 @引擎管理员 改回 @真实操作者;无登记时原样返回
    msg = _apply_mention_rewrite(key, msg)
    # 日志是纯文本展示语境,还原昵称的 md 转义后再记录;实际发送仍用带转义的 msg
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
      ② ``_consume_pending_tip`` / ``_consume_pending_dm_warn``  如本帧标了 new_game,
         调度提示 task —— 它也走同一把 Lock,会自动排在本条之后。
    """
    async with _get_send_lock(key):
        await _send_text_quota_managed(target_id, is_uid, msg, extra_buttons)
        _consume_pending_tip(key, target_id, is_uid)
        _consume_pending_dm_warn(key, target_id, is_uid)


def _drop_scope(is_uid: bool) -> str:
    """丢弃日志里的场景词 —— 私信 / 群聊各自可 grep。"""
    return '私信' if is_uid else '群聊'


def _no_ref_reason(key: str) -> str:
    """「无有效引用」的丢弃说明,带上该场景的 TTL(群 5 分钟 / 私信 60 分钟)。"""
    return f'{key} 无有效消息ID（未登记或已超 {quota.ref_ttl(key) / 60:.0f} 分钟）'


# 被动引用已失效:QQ 不再认这条 msg_id / event_id → 移出引用池,换下一条重发。
_DEAD_REF_CODES = frozenset({
    40034005,   # 回复消息 msg_id 已过期
    304103,     # 消息 ID 已过期,不能回复
    40034024,   # msg_id 无效或越权
    40034025,   # event_id 无效
    40034026,   # event_id 已过期
    40034027,   # 该事件不支持回复消息
    40034128,   # 被动回复时间或次数超限
})
_DEAD_REF_RETRIES = 3

# 40034100 = 主动消息超过频控(官方:单群 / 单个好友 20 条/分钟,bot 群消息总量 60 条/分钟)。
# 文档未说明按滑动 60 秒还是自然分钟计;退避累计略超 60 秒,两种算法下都至少会等到一次名额空出来。
_RATE_LIMIT_CODE = 40034100
_RATE_LIMIT_BACKOFF = (5.0, 10.0, 15.0, 15.0, 20.0)


def _send_result(ret) -> tuple[bool, object]:
    """``send_to_*`` 的返回值 ``(ok, data, payload)`` → ``(ok, code)``。

    只有明确的 ``ok=False`` 才算失败(mock / 未知形状按成功,同 log_attribution._note_push_result)。
    """
    try:
        if ret[0] is False:
            data = ret[1]
            code = (data.get('code') or data.get('err_code')) if isinstance(data, dict) else None
            try:
                return False, int(code)
            except (TypeError, ValueError):
                return False, code
    except Exception:
        pass
    return True, None


async def _pick_route(key: str, target_id: str, is_uid: bool, what: str, *, retry: bool = False):
    """选这条消息的发送通道:被动引用 / 主动消息 / 丢弃。

    返回 ``(consumed, is_active_push)``:``consumed`` 是 ``quota.try_consume_ref`` 的四元组,
    ``None`` 表示走主动消息;整条丢弃时返回 ``None``(日志已记)。

    主动直推资格(可推送群 / 沙箱私信)不代表跳过被动配额 —— 引用池还有次数照常先走被动;
    次数用完才直接主动消息、不挂刷新按钮。今日主动消息额度用满的目标**失去该资格**,
    退回刷新按钮机制(见 _active_push_allowed;跨天自动恢复)。

    引用池取不到次数时的三种去向(``has_valid_ref`` 区分前两者):
      · 有主动直推资格          → 直接主动消息
      · 无有效引用(未登记 / 超 TTL)→ **直接丢弃**(等刷新与主动消息都是死路)
      · TTL 内次数用完          → 阻塞等刷新 ≤15s,超时再看主动额度

    ``retry=True``(上次发送被拒后重选)不再重复记配额耗尽。
    """
    # 直推私信(all 模式全员 / 白名单沙箱用户):逻辑与全量群完全一致。
    is_sandbox_dm = _is_sandbox_dm(target_id, is_uid)
    # 群的主动推送资格:QQ 后台开了 allow_proactive_msg 的群(与全量消息权限分别开通,见 helpers.can_push_group)。
    # 只认落实过的事实,不看框架 non_at_message.* 配置 —— 可能真实权限不同步,会让没权限的群也走主动消息(必拒)。
    is_full = (not is_uid) and helpers.can_push_group(target_id)
    is_active_push = (is_full or is_sandbox_dm) and _active_push_allowed(target_id, is_uid)

    consumed = quota.try_consume_ref(key)
    if consumed is not None:
        return consumed, is_active_push
    # 指标只计「真耗尽」:TTL 内引用的被动条数真用完(无引用 / 已过期不算配额压力),
    # 且目标**无主动直推资格**(有资格的配额满后无缝转主动消息,没有实际影响)。
    had_valid_ref = quota.has_valid_ref(key)
    if had_valid_ref and not is_active_push and not retry:
        metrics.record_quota_exhausted()
    if is_active_push:
        tag = '私信直推' if is_sandbox_dm else '全量直推'
        log.info(f'⚡ [{tag}] {key} 配额已满，走主动消息: {what}')
        return None, True
    if not had_valid_ref:
        # **无有效引用**(从未登记 / 已过 TTL)→ 直接丢弃,不白等、不白烧一次必失败的调用。
        log.info(f'🗑️ [{_drop_scope(is_uid)}丢弃] {_no_ref_reason(key)}，丢弃: {what}')
        return None
    # 群聊配额满 / 普通私信配额满(TTL 内仍有引用) → 阻塞等待刷新,不预先尝试发送(直接发也会被 QQ 拒)。
    wait_start = time.monotonic()
    q = quota.ref_quota(key)
    log.info(f'⏳ [配额已满] {key} 已用 {q}/{q}，'
             f'阻塞等待刷新按钮 ≤{quota.REFRESH_WAIT_TIMEOUT:.0f}s | 待发: {what}')
    consumed = await quota.wait_and_consume(key, quota.REFRESH_WAIT_TIMEOUT)
    elapsed = time.monotonic() - wait_start
    if consumed is not None:
        log.info(f'✅ [配额已刷新] {key} 等 {elapsed:.1f}s 后续命成功，重发: {what}')
        return consumed, False
    metrics.record_quota_wait_timeout()
    # 等待超时 → 改走主动消息(无 msg_id/event_id),bot 若在该群/用户上有主动 quota 还能落地。
    # 今日主动额度已用满时不强发:QQ 必拒,发了只是白烧一次调用并让日志误报成功。
    if not _active_push_allowed(target_id, is_uid):
        log.warning(f'🚫 [主动额度已满] {key} 经 {elapsed:.1f}s 无刷新，'
                    f'且今日主动消息已达上限 {ACTIVE_PUSH_DAILY_LIMIT}，丢弃: {what}')
        return None
    log.warning(f'⏰ [超时强发] {key} 经 {elapsed:.1f}s 无刷新，尝试发送主动消息: {what}')
    return None, False


async def _deliver(target_id: str, is_uid: bool, what: str, send) -> None:
    """选通道 + 发送 + 失败处理,文本与图片共用。

    ``send(sender, kwargs, used, is_active_push)`` 发一次并返回 ``(ok, code)``(``used`` 见 quota.try_consume_ref)。
    失败时:
      · 被动引用已失效(``_DEAD_REF_CODES``)→ 移出引用池,重新选通道(下一条引用 / 主动 / 丢弃)
      · 主动直推撞频控(40034100)→ 按 ``_RATE_LIMIT_BACKOFF`` 退避重发,等待中来了新引用就改走被动
      · 其他错误照旧不重试(框架已记错误日志)
    调用方持有 per-target Lock,退避期间同一目标的后续消息排在后面,顺序不乱。
    """
    key = helpers.target_key(target_id, is_uid)
    route = await _pick_route(key, target_id, is_uid, what)
    dead = waits = 0
    while route is not None:
        consumed, is_active_push = route
        if consumed is not None:
            ref_type, ref_value, used, ref_appid = consumed
            sender, kwargs = helpers.get_sender(ref_appid), {ref_type: ref_value}
        else:
            # 主动路径:无 ref / 无 appid,kwargs 空
            sender, kwargs, used = helpers.get_sender(''), {}, 0
        if sender is None:
            log.warning(f'无可用 sender，丢弃 → {target_id}: {what}')
            return

        ok, code = await send(sender, kwargs, used, is_active_push)
        if ok:
            if consumed is None:
                # 指标:无 ref 即主动消息(全量直推 / 沙箱直推 / 超时强发),按日分桶计数;被拒的不算
                metrics.record_active_push(target_id, is_uid)
            if waits:
                metrics.record_rate_limit(dropped=False)
                log.info(f'✅ [频控补发] {key} 退避 {waits} 次后送达: {what}')
            return
        if consumed is not None and code in _DEAD_REF_CODES and dead < _DEAD_REF_RETRIES:
            dead += 1
            quota.drop_ref(key, ref_value)
            log.info(f'♻️ [引用失效] {key} 的 {ref_type} 被拒({code})，换下一条: {what}')
            route = await _pick_route(key, target_id, is_uid, what, retry=True)
            continue
        if consumed is None and is_active_push and code == _RATE_LIMIT_CODE:
            if waits >= len(_RATE_LIMIT_BACKOFF):
                metrics.record_rate_limit(dropped=True)
                log.warning(f'🚫 [频控丢弃] {key} 退避 {waits} 次仍超频控，丢弃: {what}')
                return
            # 往上抖 0~20%:bot 总量撞线时各群别在同一时刻一起重发
            delay = _RATE_LIMIT_BACKOFF[waits] * (1 + random.random() * 0.2)
            waits += 1
            log.warning(f'🐢 [频控退避] {key} 主动消息超频控，{delay:.0f}s 后第 {waits} 次重发: {what}')
            fresh = await quota.wait_and_consume(key, delay)
            route = ((fresh, True) if fresh is not None
                     else await _pick_route(key, target_id, is_uid, what, retry=True))
            continue
        return


async def _send_text_quota_managed(target_id, is_uid, msg, extra_buttons):
    """文本发送核心:自动追加刷新按钮,选通道与失败重发见 ``_deliver``。

    全量群 / 沙箱私信(主动直推)整个生命周期不追加 ``build_refresh_button``:
    它们次数用完直接走主动消息,不被被动回复条数限制,这个教学按钮没有意义。
    """
    key = helpers.target_key(target_id, is_uid)

    async def _send(sender, kwargs, used, is_active_push):
        # 倒数第 2 条起追加刷新按钮(群 4/5 条,私信 3/4 条);最后一条用「⚠️ 最终刷新」。
        btns = list(extra_buttons) if extra_buttons else []
        if not is_active_push and used >= quota.refresh_threshold(key):
            is_last = (used >= quota.ref_quota(key))
            btns.append(quota.build_refresh_button(is_last=is_last))
            tag = '⚠️' if is_last else '🔄'
            log.info(f'📊 [配额追踪] {key} 已用 {used}/{quota.ref_quota(key)} → {tag}')
        try:
            with log_attribution.mark_outbound():
                if is_uid:
                    ret = await sender.send_to_user(target_id, msg, buttons=btns or None, **kwargs)
                else:
                    ret = await sender.send_to_group(target_id, msg, buttons=btns or None, **kwargs)
        except Exception as e:
            log.warning(f'发送文本失败 ({target_id}): {e}')
            return False, None
        return _send_result(ret)

    await _deliver(target_id, is_uid, repr((msg or '')[:30].replace('\n', ' ')), _send)


# ──────── 图片发送 ────────────────────────────────────────────────────────

# ──────── 图文混排(还原引擎排版) ─────────────────────────────────────────
# 桥接层在排版串里用 \x01IMG<i>\x01 占位符标出每张图片在原文里的**位置**(见 LGTBot_ElainaBot.cc::HandleMessages),
# 图文排版才能原样送到 QQ。引擎给玩家看的文案不含控制字符,不存在与正文冲突的可能。
_IMG_PLACEHOLDER_RE = re.compile('\x01IMG(\\d+)\x01')


def _split_layout(content: str, n_images: int) -> list[tuple[str, object]]:
    """把带占位符的排版串拆成有序段:``('text', str)`` / ``('image', idx)``。

    占位符一个都没有时退化为「文字在前,图片依次在后」。占位符没覆盖到的图片补在末尾,
    保证任何情况下都不丢图。
    """
    content = content or ''
    segs: list[tuple[str, object]] = []
    seen: set[int] = set()
    pos = 0
    for m in _IMG_PLACEHOLDER_RE.finditer(content):
        idx = int(m.group(1))
        if m.start() > pos:
            segs.append(('text', content[pos:m.start()]))
        pos = m.end()
        if 0 <= idx < n_images and idx not in seen:
            seen.add(idx)
            segs.append(('image', idx))
    if pos < len(content):
        segs.append(('text', content[pos:]))
    segs.extend(('image', i) for i in range(n_images) if i not in seen)
    return segs


def _layout_plain_text(segs) -> str:
    """排版段里的纯文字部分(日志展示 / 媒体兜底的 caption 用)。"""
    return ''.join(v for kind, v in segs if kind == 'text')


def _build_layout_markdown(segs, urls: dict, sizes: dict) -> str:
    """按段序拼 markdown:文字原样保留,图片转 ``![image #Wpx #Hpx](url)``。

    段间统一用空行分隔 —— QQ markdown 里图片要独占段落才按块渲染。文字段自身
    首尾的换行先剥掉,避免相邻段之间出现连续空行。
    """
    parts = []
    for kind, val in segs:
        if kind == 'text':
            text = val.strip('\n')
            if text.strip():
                parts.append(text)
        else:
            width, height = sizes[val]
            parts.append(f'![image #{width}px #{height}px]({urls[val]})')
    return '\n\n'.join(parts)


def _read_rendered_image(image_path: str) -> bytes | None:
    """读一张引擎渲染出来的图片;文件未落盘 / 读失败返回 None。

    LGTBot 通过 popen 异步调用 markdown2image 生成图片,存在小概率回调到达时
    文件还没落盘,这里短暂轮询等待最多 2s。
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
        return None
    try:
        with open(image_path, 'rb') as f:
            return f.read()
    except Exception as e:
        log.warning(f'读取图片失败: {e}')
        return None


def cb_send_image_message(target_id: str, is_uid: bool, image_paths, content: str = ''):
    """C++ → Python：发送图文消息（fire-and-forget,理由同 ``cb_send_text_message``）

    ``image_paths`` 是本次 flush 的**全部**图片路径(list;传 str 时按单图处理,兼容未重新编译的桥接层)。
    ``content`` 是带 ``\\x01IMG<i>\\x01`` 占位符的排版串,占位符标出每张图在原文里的位置。

    图片读完后投到 asyncio loop 串行发送,本函数立即返回让 C++ read thread 释放 Match.mutex_。
    读不出来的图片直接从排版里剔除(其余照发);一张都没读到但有文字时退化成纯文本消息,不让文案跟着图片一起丢。
    """
    paths = [image_paths] if isinstance(image_paths, str) else [str(p) for p in image_paths]
    images = {}      # 原始索引 → (data, filename);读失败的索引缺席
    for i, path in enumerate(paths):
        data = _read_rendered_image(path)
        if data is not None:
            images[i] = (data, os.path.basename(path) or 'lgtbot.png')

    segs = [s for s in _split_layout(content, len(paths))
            if s[0] != 'image' or s[1] in images]
    plain = _layout_plain_text(segs)
    if not images and not plain.strip():
        return

    # 本条要附的按钮(cb_match_event 先写进 pending_buttons)跟着走
    # markdown 通道能挂按钮,媒体兜底不能,兜底时 _send_mixed_message 会把它还回去。
    key = helpers.target_key(target_id, is_uid)
    extra_buttons = state.pending_buttons.pop(key, None)

    # 日志展示用 humanize + 去转义版（纯文本更可读），实际发送时再按通道决定
    page_logs.log_outgoing(
        target_id, is_uid,
        helpers.strip_md_escapes(helpers.humanize_mentions(plain)), image=bool(images),
    )

    loop = state.event_loop
    if loop is None or loop.is_closed():
        log.warning('事件循环不可用，丢弃图片消息')
        return
    try:
        asyncio.run_coroutine_threadsafe(
            _serialized_mixed_send(key, target_id, is_uid, segs, images, plain, extra_buttons),
            loop)
    except Exception as e:
        log.warning(f'调度图片发送失败: {e}')


async def _serialized_mixed_send(key: str, target_id: str, is_uid: bool,
                                 segs, images: dict, plain: str, extra_buttons) -> None:
    """串行化的图文发送 —— 与 ``_serialized_text_send`` 共享 per-target Lock。"""
    async with _get_send_lock(key):
        await _send_mixed_message(target_id, is_uid, segs, images, plain, extra_buttons)
        if plain:
            _consume_pending_tip(key, target_id, is_uid)
            _consume_pending_dm_warn(key, target_id, is_uid)


async def _send_mixed_message(target_id: str, is_uid: bool, segs, images: dict,
                              plain: str, extra_buttons) -> None:
    """图文混排发送。

    通道 A(全部图片上传成功):拼一条 markdown,文字与图片**按引擎原顺序**内联,
    整个 flush 只发一条消息。走 ``_send_text_quota_managed``:
    markdown 与文本在框架侧是同一种 msg_type,配额 / 主动直推 / 刷新按钮逻辑完全复用。

    通道 B(任一图片上传失败):退回媒体消息(msg_type=7)。它一条只能带一个媒体、
    content 也不解析 markdown,排版无法还原 —— 首图带全部文字,其余图单发。
    按钮挂不上媒体消息,还给 pending_buttons 让下一条文本带。
    """
    idxs = [i for kind, i in segs if kind == 'image']
    user_id_for_cos = target_id if is_uid else ''
    urls: dict = {}
    if idxs:
        results = await asyncio.gather(*[
            uploader.upload_image(images[i][0], images[i][1], user_id=user_id_for_cos,
                                  target_id=target_id, target_is_uid=is_uid)
            for i in idxs
        ])
        urls = dict(zip(idxs, results))

    if all(urls.get(i) for i in idxs):
        sizes = {i: uploader.get_image_size(images[i][0]) for i in idxs}
        await _send_text_quota_managed(target_id, is_uid,
                                       _build_layout_markdown(segs, urls, sizes),
                                       extra_buttons)
        return

    if extra_buttons:
        state.pending_buttons.setdefault(helpers.target_key(target_id, is_uid), extra_buttons)
    for n, i in enumerate(idxs):
        data, filename = images[i]
        # pre_url 把已知结果透传下去:成功的图不再重传,失败的('')直接走媒体
        await _send_image_quota_managed(target_id, is_uid, data,
                                        plain if n == 0 else '', filename,
                                        pre_url=urls.get(i) or '')


async def _send_image_quota_managed(target_id, is_uid, data, raw_content, filename,
                                    *, pre_url: str | None = None):
    """图片发送核心：优先图床+markdown，失败回退 media;选通道与失败重发见 ``_deliver``

    发送通道二选一：
      A. 图床 markdown：通过 image_hosting 上传图片得到 URL，用 markdown
         `![](url)` 内嵌；保留 `<@openid>` 原生 mention，可挂刷新按钮
      B. 媒体兜底：图床未启用 / 上传失败时走 msg_type=7 路径（content
         字段需 humanize mentions，无法挂按钮）

    ``pre_url`` 是上游(``_send_mixed_message`` 的媒体兜底分支)已经拿到的上传结果:
    非空 = 直接用该 URL,``''`` = 已知上传失败、跳过重传直接走媒体,``None``(默认)= 本函数自己上传。
    """
    # 上传只做一次:换引用重发、频控退避重发都复用同一个 URL / file_info
    up = {'url': pre_url, 'media': None}

    async def _send(sender, kwargs, used, is_active_push):
        if up['url'] is None:
            # target 一并透传:qq_file 图床用当前消息目标作上传作用域(其余图床忽略)
            up['url'] = await uploader.upload_image(
                data, filename, user_id=target_id if is_uid else '',
                target_id=target_id, target_is_uid=is_uid) or ''
        if up['url']:
            res = await _send_markdown_image(sender, target_id, is_uid, kwargs, raw_content,
                                             up['url'], data, used, is_full=is_active_push)
            if res is not None:
                return res
            # markdown 发送抛异常（极少见）→ 落回 media
        return await _send_media_fallback(sender, target_id, is_uid, kwargs, raw_content, data, up)

    await _deliver(target_id, is_uid, '[图片]', _send)


async def _send_markdown_image(sender, target_id, is_uid, kwargs, raw_content,
                               image_url, data, used, *, is_full: bool = False):
    """构造 markdown 文本 + 图片 + 按钮，调 send_to_*。返回 ``(ok, code)``,发送抛异常时返回 None(调用方退回媒体消息)。

    ``kwargs`` 为空即主动消息(全量群 / 沙箱私信 / 配额耗尽超时路径),不带 msg_id/event_id。
    ``is_full=True``(调用方传 is_active_push,即全量群或沙箱私信)时跳过刷新按钮追加。
    """
    width, height = uploader.get_image_size(data)
    parts = []
    if raw_content:
        parts.append(raw_content)
    parts.append(f'![image #{width}px #{height}px]({image_url})')
    md = '\n\n'.join(parts)

    # markdown 通道支持挂按钮（不像 msg_type=7);全量群从不挂刷新按钮。
    # 阈值按场景取(群 4/5 条,私信 3/4 条),同 _send_text_quota_managed。
    btns: list = []
    key = helpers.target_key(target_id, is_uid)
    if not is_full and used >= quota.refresh_threshold(key):
        is_last = (used >= quota.ref_quota(key))
        btns.append(quota.build_refresh_button(is_last=is_last))
    btns_arg = btns if btns else None

    try:
        with log_attribution.mark_outbound():
            if is_uid:
                ret = await sender.send_to_user(target_id, md, buttons=btns_arg, **kwargs)
            else:
                ret = await sender.send_to_group(target_id, md, buttons=btns_arg, **kwargs)
    except Exception as e:
        log.warning(f'markdown 图片发送失败 ({target_id}): {e}, 回退到媒体消息')
        return None
    return _send_result(ret)


async def _send_media_fallback(sender, target_id, is_uid, kwargs, raw_content, data, up):
    """msg_type=7 媒体消息兜底：上传 file_info → send_to_* with media,返回 ``(ok, code)``。
    media 不解析 <@openid>，content 这里要先 humanize 成可读 @昵称。

    ``kwargs`` 为空即主动消息。``up['media']`` 缓存 ``(sender, file_info)``:file_info 归上传它的 bot 所有,
    换了 sender(被动引用的 appid 与主动路径的不同)才重新上传。
    """
    from core.message.media import upload_media_bytes

    if not up['media'] or up['media'][0] is not sender:
        prefix = 'users' if is_uid else 'groups'
        try:
            file_info = await upload_media_bytes(sender, data, 1, f"/v2/{prefix}/{target_id}/files")
        except Exception as e:
            log.warning(f'图片上传异常: {e}')
            return False, None
        if not file_info:
            log.warning(f'图片上传失败 → {target_id}')
            return False, None
        up['media'] = (sender, file_info)

    # msg_type=7 的 content 是纯文本(QQ 不按 markdown 解析):humanize 提及后
    # 再把源头(cb_get_user_name)给昵称加的 md 转义还原,避免露出反斜杠
    rendered_content = helpers.strip_md_escapes(helpers.humanize_mentions(raw_content))
    media_dict = {'file_info': up['media'][1]}
    try:
        with log_attribution.mark_outbound():
            if is_uid:
                ret = await sender.send_to_user(target_id, rendered_content, media=media_dict, **kwargs)
            else:
                ret = await sender.send_to_group(target_id, rendered_content, media=media_dict, **kwargs)
    except Exception as e:
        log.warning(f'发送图片失败 ({target_id}): {e}')
        return False, None
    return _send_result(ret)
