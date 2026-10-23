#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""游戏开关 —— 引擎照常全量编译,面板里停用的游戏启动时不加载,运行中拦下它们的开局指令。

引擎把 game_path 下的每个子目录都当成游戏加载,没有名单入口。每次 ``start()`` 之前,
``prepare_game_path`` 把启用的游戏拼成 ``<BUILD_DIR>/plugins_enabled/`` 交给引擎:
  · 引擎只认真实目录,指向目录的符号链接会被跳过 —— 每个游戏建一层真实目录,里面逐项用符号链接指回原目录
  · 引擎按「game_path/模块名/…」取图标、资源和 runner 加载的 libgame.so,目录名沿用原名
  · 桥接层从 game_path 反推 config_runner(上一级目录)与崩溃转储目录(找 ``/build/plugins``),
    所以视图目录与 ``plugins/`` 同级、名字以 plugins 开头

停用名单 ``data/games_disabled.json`` 存 ``{模块名: 显示名}``:引擎按显示名找游戏,运行中拦截比对的就是它。
文件还不存在时按 ``DEFAULT_DISABLED`` 生效,首次启动引擎时写成文件,之后以文件为准。
显示名从 libgame.so 导出的 ``GetGameInfo`` 读(extern "C" 的纯 C 结构),放在子进程里跑,游戏库的静态初始化不进本进程。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from core.base.logger import get_logger, PLUGIN
from . import boot

log = get_logger(PLUGIN, 'LGTBot')

DISABLED_PATH = os.path.join(boot.DATA_DIR, 'games_disabled.json')
VIEW_DIR = os.path.join(boot.BUILD_DIR, 'plugins_enabled')

# 持久 dict 里记本次引擎启动时实际停用的模块,面板据此标出「重启后生效」
_APPLIED_KEY = 'games_applied_disabled'

# 默认停用的游戏
DEFAULT_DISABLED: dict[str, str] = {
    'bidding_poker':    '投标波卡',
    'escape_building':  '逃脱大楼',
    'holdem_poker':     '德州波卡',
    'jewish_chess':     '犹太人棋',
    'lucky_poker':      '幸运波卡',
    'mahjong_17_steps': '十七步',
    'poker_squares':    '扑克矩阵',
    'sync_mahjong':     '同步麻将',
    'wordle':           'wordle',
}

_PROBE_TIMEOUT = 30

# argv[1:] = 各 libgame.so 路径;每读完一个输出一条 \x1e 开头的 JSON,游戏库自己往 stdout 打的东西不影响解析
_PROBE = r'''
import ctypes, json, os, sys

class Props(ctypes.Structure):
    _fields_ = [('name', ctypes.c_char_p), ('developer', ctypes.c_char_p),
                ('description', ctypes.c_char_p), ('shuffled_player_id', ctypes.c_bool)]

class Achievements(ctypes.Structure):
    _fields_ = [('data', ctypes.c_void_p), ('size', ctypes.c_uint32)]

class GenericOptions(ctypes.Structure):
    _fields_ = [('bench_computers', ctypes.c_uint32), ('is_formal', ctypes.c_bool)]

class Info(ctypes.Structure):
    # 与引擎的 GameInfo 同布局;末尾多留一段,上游往后加字段也写不出界
    _fields_ = [('props', ctypes.POINTER(Props)), ('module', ctypes.c_char_p), ('rule', ctypes.c_char_p),
                ('achievements', Achievements), ('options', GenericOptions), ('reserved', ctypes.c_char * 256)]

def text(b):
    return b.decode('utf-8', 'replace') if b else ''

for lib in sys.argv[1:]:
    try:
        info = Info()
        ctypes.CDLL(lib, mode=os.RTLD_NOW).GetGameInfo(ctypes.byref(info))
        p = info.props.contents
        rec = {'name': text(p.name), 'developer': text(p.developer), 'description': text(p.description)}
    except Exception as e:
        rec = {'error': str(e)}
    sys.stdout.write('\x1e' + json.dumps(rec) + '\n')
    sys.stdout.flush()
'''


# ──────── 停用名单 ────────────────────────────────────────────────────────

def load_disabled() -> dict[str, str]:
    """读停用名单 ``{模块名: 显示名}``;文件还不存在时返回默认名单,损坏返回空。纯模块名列表也认,显示名留空。"""
    try:
        with open(DISABLED_PATH, encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return dict(DEFAULT_DISABLED)
    except (OSError, ValueError):
        return {}
    if isinstance(data, list):
        return {m: '' for m in data if isinstance(m, str) and m}
    if isinstance(data, dict):
        return {k: v if isinstance(v, str) else '' for k, v in data.items() if isinstance(k, str) and k}
    return {}


def save_disabled(mapping: dict[str, str]) -> None:
    os.makedirs(os.path.dirname(DISABLED_PATH), exist_ok=True)
    tmp = DISABLED_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(dict(sorted(mapping.items())), f, ensure_ascii=False, indent=2)
    os.replace(tmp, DISABLED_PATH)


_names_cache: tuple = (None, frozenset())


def disabled_names() -> frozenset[str]:
    """停用游戏的显示名集合;按文件 mtime 缓存,面板保存后下一条消息就生效。"""
    global _names_cache
    try:
        st = os.stat(DISABLED_PATH)
    except OSError:
        return frozenset(n for n in load_disabled().values() if n)
    key = (st.st_mtime_ns, st.st_size)
    if _names_cache[0] != key:
        _names_cache = (key, frozenset(n for n in load_disabled().values() if n))
    return _names_cache[1]


# ──────── 游戏列表 ────────────────────────────────────────────────────────

def _modules(game_dir: str) -> list[str]:
    """``game_dir`` 下带 libgame.so 的子目录名,即引擎的模块名。"""
    try:
        names = os.listdir(game_dir)
    except OSError:
        return []
    return sorted(n for n in names if os.path.isfile(os.path.join(game_dir, n, 'libgame.so')))


def _probe(libs: list[str]) -> list[dict]:
    """在子进程里逐个读 ``GetGameInfo``,与 ``libs`` 一一对应;子进程没读到的记成错误。"""
    if not libs:
        return []
    env = dict(os.environ)
    # 个别游戏库依赖 build/ 里的共享库(libtinyexpr 等)
    env['LD_LIBRARY_PATH'] = os.pathsep.join(p for p in (boot.BUILD_DIR, env.get('LD_LIBRARY_PATH', '')) if p)
    out = ''
    try:
        r = subprocess.run([sys.executable, '-I', '-c', _PROBE, *libs], capture_output=True,
                           text=True, encoding='utf-8', errors='replace', env=env, timeout=_PROBE_TIMEOUT)
        out = r.stdout
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode('utf-8', 'replace') if isinstance(e.stdout, bytes) else (e.stdout or '')
    except OSError as e:
        log.warning(f'[游戏开关] 读取游戏信息失败: {e}')
    recs = []
    for chunk in out.split('\x1e')[1:]:
        try:
            recs.append(json.loads(chunk.split('\n', 1)[0]))
        except ValueError:
            recs.append({'error': '游戏信息解析失败'})
    recs = recs[:len(libs)]
    return recs + [{'error': '读取游戏信息的子进程提前退出'}] * (len(libs) - len(recs))


_info_cache: dict = {'key': None, 'games': []}


def list_games() -> list[dict]:
    """当前构建来源下的全部游戏 ``[{module, name, developer, description, error}]``,按模块名排序。

    ``name`` 读不到时为空串。按各 libgame.so 的 mtime / 大小缓存,编译或换包后自动重读。要起子进程。"""
    game_dir = boot.GAME_PATH
    modules = _modules(game_dir)
    libs = [os.path.join(game_dir, m, 'libgame.so') for m in modules]
    key = []
    for m, lib in zip(modules, libs):
        try:
            st = os.stat(lib)
            key.append((m, st.st_mtime_ns, st.st_size))
        except OSError:
            key.append((m, 0, 0))
    key = (game_dir, tuple(key))
    if _info_cache['key'] == key:
        return _info_cache['games']
    games = [{'module': m, 'name': rec.get('name', ''), 'developer': rec.get('developer', ''),
              'description': rec.get('description', ''), 'error': rec.get('error', '')}
             for m, rec in zip(modules, _probe(libs))]
    _info_cache.update(key=key, games=games)
    return games


# ──────── 启动时过滤 ──────────────────────────────────────────────────────

def _build_view(src: str, enabled: list[str]) -> None:
    """在临时目录拼好视图再整体换入,换不进去就保持没有视图的状态。"""
    tmp, old = VIEW_DIR + '.tmp', VIEW_DIR + '.old'
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        for m in enabled:
            game = os.path.join(tmp, m)
            os.mkdir(game)
            for entry in os.listdir(os.path.join(src, m)):
                os.symlink(os.path.join(src, m, entry), os.path.join(game, entry))
        shutil.rmtree(old, ignore_errors=True)
        if os.path.lexists(VIEW_DIR):
            os.rename(VIEW_DIR, old)
        os.rename(tmp, VIEW_DIR)
    finally:
        # rmtree 不跟随目录里的符号链接,删视图不会波及原游戏目录
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)


def prepare_game_path() -> str:
    """引擎 ``start()`` 之前调用:返回要交给引擎的 game_path,并记下这次实际停用的模块。

    名单文件不存在时写入默认名单。名单为空就原样返回 ``plugins/``。全部停用,或视图建不出来(文件系统不支持符号链接等)时告警并全部加载。
    只能在真正调 ``start()`` 前重建:复用运行中的引擎时,它还在引用视图里的路径。"""
    if not os.path.exists(DISABLED_PATH):
        try:
            save_disabled(DEFAULT_DISABLED)
            log.info(f'🎮 [游戏开关] 已写入默认停用名单({len(DEFAULT_DISABLED)} 个游戏)')
        except OSError as e:
            log.warning(f'[游戏开关] 写入默认停用名单失败: {e}')
    src = boot.GAME_PATH
    modules = _modules(src)
    disabled = sorted(set(load_disabled()) & set(modules))
    applied: list[str] = []
    path = src
    if disabled and len(disabled) == len(modules):
        log.warning('[游戏开关] 名单停用了全部游戏,本次忽略名单、全部加载')
    elif disabled:
        try:
            _build_view(src, [m for m in modules if m not in disabled])
            path, applied = VIEW_DIR, disabled
        except OSError as e:
            log.warning(f'[游戏开关] 无法创建视图目录({e}),本次全部加载;开局拦截照常生效')
    if not applied:
        shutil.rmtree(VIEW_DIR, ignore_errors=True)
    boot._get_persistent()[_APPLIED_KEY] = applied
    if applied:
        log.info(f'🎮 [游戏开关] 本次不加载 {len(applied)} 个游戏: {", ".join(applied)}')
    return path


def applied_disabled() -> list[str] | None:
    """运行中的引擎启动时实际停用的模块;引擎没在跑返回 ``None``。"""
    if not boot.is_engine_running():
        return None
    return list(boot._get_persistent().get(_APPLIED_KEY) or [])


# ──────── 面板 ────────────────────────────────────────────────────────────

def panel_state() -> dict:
    """「游戏开关」卡片数据:全部游戏、已保存的停用名单(只列当前构建里有的)、本次启动实际停用的名单。"""
    games = list_games()
    known = {g['module'] for g in games}
    return {
        'games': games,
        'disabled': sorted(m for m in load_disabled() if m in known),
        'applied': applied_disabled(),
    }


def save_selection(disabled: list[str]) -> tuple[bool, str, dict]:
    """面板保存:校验后写名单,返回 ``(成功, 提示, {'disabled': [新停用], 'enabled': [恢复启用]})``。

    当前构建里没有的模块:名单里原有的照旧保留(换编译来源后可能又出现),新提交的忽略。"""
    games = {g['module']: g for g in list_games()}
    old = load_disabled()
    want = {m for m in disabled if m in games}
    if games and len(want) == len(games):
        return False, '至少保留一个启用的游戏', {}
    new = {m: n for m, n in old.items() if m not in games}
    new.update({m: games[m]['name'] for m in want})
    changes = {'disabled': sorted(want - set(old)),
               'enabled': sorted(m for m in old if m in games and m not in want)}
    if new == old:
        return True, '停用名单没有变化', changes
    save_disabled(new)
    return True, '已保存：开局拦截立即生效，游戏列表在重启 LGTBot 后更新', changes
