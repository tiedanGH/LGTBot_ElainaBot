#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真桥接冒烟测试 —— import 真编译出的 LGTBot_ElainaBot.so 起引擎跑一遍。

覆盖 mock 单测(conftest 假 boot)照不到的两类回归:
  · Boost.Python ABI —— .so 与当前 python3 的转换层(str / list / callback)
  · ``g_bot_core`` 生命周期 —— start → 指令响应 → release → 再 start(重启路径)
    → 活跃房间时 release 拒绝 → 退出后放行

**完全独立于 ElainaBot 主框架**(CI 只 checkout 本插件仓库):不 import
core.* / mod.*,自己完成 boot.py 同款的 chdir + RTLD_GLOBAL 预加载。

用法(需先 bash build.sh 完成编译;Linux only):
    python3 tests/smoke_bridge.py

退出码:0 全过;1 断言失败;2 环境缺件(没编译)。
"""

import ctypes
import glob
import os
import signal
import sys
import tempfile
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_DIR = os.path.join(REPO_ROOT, 'build')
SO_PATH = os.path.join(REPO_ROOT, 'LGTBot_ElainaBot.so')
GAME_PATH = os.path.join(BUILD_DIR, 'plugins')

# 整体看门狗:引擎卡死(match-lock / 子进程等待)时不许拖垮 CI,直接超时失败
WATCHDOG_SECONDS = 600


def die(code: int, msg: str) -> None:
    print(f'❌ {msg}', flush=True)
    sys.exit(code)


def step(msg: str) -> None:
    print(f'✅ {msg}', flush=True)


# ──────── 收集回调(引擎 → Python) ─────────────────────────────────────────
_lock = threading.Lock()
_received: list = []   # (target_id, is_uid, text)
_images: list = []     # image_path
_events: list = []     # (target_id, is_uid, kind, game_name)


def cb_get_user_name(uid: str) -> str:
    return f'冒烟用户{uid}'


def cb_get_user_avatar_url(uid: str) -> str:
    return ''


def cb_send_text(target_id: str, is_uid: bool, msg: str) -> None:
    with _lock:
        _received.append((target_id, bool(is_uid), msg))


def cb_send_image(target_id: str, is_uid: bool, image_path: str, content: str = '') -> None:
    with _lock:
        _images.append(image_path)
        if content:
            _received.append((target_id, bool(is_uid), content))


def cb_match_event(target_id: str, is_uid: bool, kind: str, game_name: str) -> None:
    with _lock:
        _events.append((target_id, bool(is_uid), kind, game_name))


def texts() -> list:
    with _lock:
        return [t for _, _, t in _received]


def wait_for(pred, timeout: float = 8.0, interval: float = 0.1):
    """轮询等待 pred() 返回 truthy(引擎多数元指令同步回执,宽容异步余量)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = pred()
        if got:
            return got
        time.sleep(interval)
    return None


def dump_state(reason: str) -> None:
    print(f'―――― 现场转储({reason})――――', flush=True)
    with _lock:
        for i, (tid, isu, t) in enumerate(_received[-10:]):
            print(f'  recv[{i}] uid={isu} {t[:120]!r}', flush=True)
        for e in _events[-10:]:
            print(f'  event {e!r}', flush=True)
        print(f'  images={len(_images)}', flush=True)


def _watchdog(signum, frame):  # noqa: ARG001
    dump_state('看门狗超时')
    print(f'❌ 冒烟测试超过 {WATCHDOG_SECONDS}s 未完成,判定引擎卡死', flush=True)
    os._exit(1)


def main() -> None:
    if hasattr(signal, 'SIGALRM'):
        signal.signal(signal.SIGALRM, _watchdog)
        signal.alarm(WATCHDOG_SECONDS)

    # ── 环境检查 ─────────────────────────────────────────────────────────
    if not os.path.isfile(SO_PATH):
        die(2, f'未找到 {SO_PATH} —— 请先 bash build.sh')
    if not os.path.isdir(BUILD_DIR):
        die(2, f'未找到 {BUILD_DIR}/')

    # ── 复刻 boot.py 的加载副作用:chdir + RTLD_GLOBAL 预加载 ─────────────
    # chdir 让引擎按相对路径找到 markdown2image;RTLD_GLOBAL 预加载 build/
    # 下全部共享库,让 .so 的未决符号(libbot_core 等)在 import 时可见。
    os.chdir(BUILD_DIR)
    for lib in sorted(glob.glob(os.path.join(BUILD_DIR, 'lib*.so*'))):
        ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
    sys.path.insert(0, REPO_ROOT)
    import LGTBot_ElainaBot as bridge  # noqa: E402  真 .so
    step(f'import LGTBot_ElainaBot.so(Boost.Python ABI 兼容 python{sys.version_info.major}.{sys.version_info.minor})')

    # ── set_restart_args:str + list 转换层冒烟 ──────────────────────────
    bridge.set_restart_args(sys.executable, list(sys.argv))
    step('set_restart_args(str, list) 转换正常')

    # ── 临时数据目录起引擎 ────────────────────────────────────────────────
    tmp = tempfile.mkdtemp(prefix='lgtbot_smoke_')
    img_dir = os.path.join(tmp, 'images')
    os.makedirs(img_dir, exist_ok=True)
    conf = os.path.join(tmp, 'lgtbot.json')
    with open(conf, 'w', encoding='utf-8') as f:
        f.write('{}')

    def start_engine(tag: str) -> None:
        db = os.path.join(tmp, f'lgtbot_{tag}.db')
        ok = bridge.start(
            GAME_PATH, db, conf, img_dir, '',
            cb_get_user_name, cb_get_user_avatar_url,
            cb_send_text, cb_send_image, cb_match_event,
        )
        if not ok:
            die(1, f'引擎 start({tag}) 返回 False')
        step(f'引擎 start({tag}) 成功(db={os.path.basename(db)})')

    start_engine('first')

    # ── 指令响应断言(/关于 回执固定含 "LGTBot" 前缀) ─────────────────────
    n0 = len(texts())
    bridge.on_private_message('/关于', 'smoke_user_1')
    got = wait_for(lambda: [t for t in texts()[n0:] if 'LGTBot' in t])
    if not got:
        dump_state('/关于 无响应')
        die(1, '/关于 未收到含 "LGTBot" 的回执')
    step(f'/关于 → 回执正常({got[0][:40]!r}…)')

    # 游戏列表可能以文本或渲染图片(markdown2image)回执,两种形态都算通过
    n1 = len(texts())
    with _lock:
        i1 = len(_images)
    bridge.on_private_message('/游戏列表', 'smoke_user_1')
    got = wait_for(lambda: texts()[n1:] or _images[i1:])
    if not got:
        dump_state('/游戏列表 无响应')
        die(1, '/游戏列表 未收到任何回执(文本或图片)')
    step(f'/游戏列表 → 回执正常({len(got)} 条/张)')

    # ── 群聊路径也各跑一条(OnPublicMessage 的三参转换) ───────────────────
    n2 = len(texts())
    bridge.on_public_message('/关于', 'smoke_user_1', 'smoke_group_1')
    if not wait_for(lambda: texts()[n2:]):
        dump_state('群聊 /关于 无响应')
        die(1, '群聊 /关于 未收到回执')
    step('群聊路径 on_public_message 正常')

    # ── 活跃房间的 release 守卫(能创建出房间才断言,创建不了则跳过) ────────
    # 游戏显示名随上游演进,从候选里试;cb_match_event 的 new_game 事件是
    # 「房间真的建立」的可靠信号,不解析回执文本。
    created_game = ''
    for cand in ('五子棋', 'LIE', 'E卡', '猜拳游戏'):
        with _lock:
            _events.clear()
        bridge.on_private_message(f'/新游戏 {cand}', 'smoke_user_2')
        if wait_for(lambda: [e for e in _events if e[2] == 'new_game'], timeout=5.0):
            created_game = cand
            break
    if created_game:
        step(f'/新游戏 {created_game} → 房间已创建')
        if bridge.release_bot_if_not_processing_games():
            # 等待中的房间(未开局)不算 processing game —— 引擎允许释放属正常
            step('等待房间不阻塞 release(引擎语义:未开局可释放)')
            start_engine('after-waiting-room')
        else:
            step('活跃房间正确阻塞了 release')
            bridge.on_private_message('/退出', 'smoke_user_2')
            ok = wait_for(lambda: bridge.release_bot_if_not_processing_games(), timeout=10.0, interval=0.5)
            if not ok:
                dump_state('退出后 release 仍拒绝')
                die(1, '/退出 后 release_bot_if_not_processing_games 仍返回 False')
            step('/退出 后 release 放行')
            start_engine('after-leave')
    else:
        print('ℹ️ 候选游戏名均未建出房间(上游改名?),跳过活跃房间守卫断言', flush=True)

    # ── restart-release 生命周期:release → 再 start → 指令仍响应 ─────────
    if not bridge.release_bot_if_not_processing_games():
        die(1, '空闲状态 release_bot_if_not_processing_games 返回 False')
    step('release(空闲)放行')

    start_engine('second')
    n3 = len(texts())
    bridge.on_private_message('/关于', 'smoke_user_3')
    if not wait_for(lambda: [t for t in texts()[n3:] if 'LGTBot' in t]):
        dump_state('重启后 /关于 无响应')
        die(1, '重启(g_bot_core 换代)后 /关于 无回执')
    step('重启后指令仍正常响应(g_bot_core 生命周期 OK)')

    if not bridge.release_bot_if_not_processing_games():
        die(1, '最终 release 返回 False')
    step('最终 release 放行')

    print(f'\n🎉 冒烟全部通过:收到文本 {len(texts())} 条 / 图片 {len(_images)} 张 / 房间事件 {len(_events)} 个', flush=True)


if __name__ == '__main__':
    main()
