#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
「引擎编译」标签 —— 在 WebUI 内调度 bash build.sh,日志实时回显。

★ 安全准则 ★
  · **没有任何自动编译 / 自动清理 / 后台调度**。``bash build.sh`` /
    ``--clean`` / ``rm -rf build/`` 这些破坏性动作全部通过 ``render_*``
    端点暴露，而端点**必须用户在仪表盘点按钮 + confirm 触发**;
    后端不在任何 @on_load、定时器或框架钩子里主动调用。
  · ``_start_build`` / ``_kill_build`` / ``render_build_remove`` 在动手
    之前都 ``log.info`` 一条 audit 日志，记录命令 + 路径。
  · 上报「文件自动消失」时，先到主框架 plugin 日志找 ``[build-*]`` /
    ``[cache-clear]`` 这些 audit 行:没有 = 不是本插件做的(可能是
    LGTBot C++ 引擎自身、外部脚本、或 git pull 触发的文件变化)。

进程模型:
  · 子进程用 ``subprocess.Popen(..., start_new_session=True)`` 独立 session,
    父进程(整个 ElainaBot 框架)退出 / 热重载都不会牵连编译进程。重新打开
    Web UI 时，从 ``data/build/state.json`` 取 PID,``os.kill(pid, 0)`` 检查
    是否还活着 —— 仍跑就接着展示日志，跑完就显示空闲。
  · stdout / stderr 重定向到 ``data/build/build.log``;若系统装了 util-linux
    ``script`` 命令，用 ``script -qfec '<cmd>' /dev/null`` 包一层伪 tty,
    让 cmake / gcc / clang 输出彩色 ANSI escape 序列(直接重定向 stdout 时
    多数工具默认关闭颜色)。fallback 到无伪 tty(日志仍可读，只是没颜色)。
  · 自身的 ``_ansi_to_segments`` 把日志里的 ``\\x1b[...m`` escape 解析成结构化段
   ``[{t,b,c}]``(文本 / 粗体 / 前景色,支持 30-37 / 90-97),
    JSON 交给前端直接建 DOM 节点 —— 日志内容全程不以 HTML 字符串形态传输;其他 control char 全剥掉。
  · 终止编译:``os.killpg(os.getpgid(pid), SIGTERM)`` —— 整个 session 一起死，
    包括 build.sh fork 的 cmake / make / g++。2 秒不响应升级 SIGKILL。

命令注入防护(关键!):
  · 全部 subprocess.Popen 调用用 **list 形式**,不传 shell=True。
  · 「编译指定目标」的 target 名称走两道闸:
    1. 前端 confirm 前 regex 校验 ``^[A-Za-z_][A-Za-z0-9_\\-]{0,62}$``
    2. 后端 ``_validate_target_name`` 再校验同样规则;前端绕过(改 JS)也无法
       让特殊字符进入 argv
  · target 名通过 framework ``/api/config-file/save`` POST 写到
    ``data/build/build_target_input.json`` 临时文件，后端读 JSON 取出来再过
    校验。``json.loads`` 本身就杜绝任何 shell 转义可能(里面是字符串字面量，
    不是 shell 字符串)。
  · 交给 shell(``script`` / ``bash -c``)的命令串是**常量 wrapper**(见
    ``_BUILD_WRAPPER``);真正要跑的 argv 经环境变量 ``LGTBOT_ARGV_NL`` 作为
    数据传入，wrapper 用 ``mapfile`` 读回 bash 数组按元素执行 —— shell 从不对
    argv 元素分词，target 名无论如何都到不了命令行 tokenize 阶段。

状态文件:
  · ``data/build/state.json``  {pid, start_time, cmd_display, cmd_argv,
                                started_iso, finished, running}
  · ``data/build/build.log``   编译输出(每次启动覆盖)
  · ``data/build/build_target_input.json``  自定义 target 参数(JS POST 写入)

故意把 build/ 子目录放在 ``data/`` 根下而非 ``data/engine/`` —— 编译产物属
于「插件级」资源，与引擎自身数据分离，语义清晰。但 framework 配置入口
非递归扫 data/(已有 webui 文档说明),子目录天然不可见，不会污染配置列表。
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from datetime import datetime

from core.base.logger import get_logger, PLUGIN
from .. import audit, boot

log = get_logger(PLUGIN, 'LGTBot')

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')


def _load(name: str) -> str:
    with open(os.path.join(_TEMPLATE_DIR, name), 'r', encoding='utf-8') as f:
        return f.read()


TAB_HTML = _load('build/build.html')
TAB_CSS = _load('build/build.css')
TAB_JS = _load('build/build.js')


# ─────────────────────────────────────────────────────────────────────────
# 路径常量 —— data/build/ 子目录集中所有编译相关运行时数据
# ─────────────────────────────────────────────────────────────────────────
BUILD_DATA_DIR = os.path.join(boot.DATA_DIR, 'build')
STATE_PATH     = os.path.join(BUILD_DATA_DIR, 'state.json')
LOG_PATH       = os.path.join(BUILD_DATA_DIR, 'build.log')
PARAMS_PATH    = os.path.join(BUILD_DATA_DIR, 'build_target_input.json')
# 子进程 wrapper shell 跑完会把退出码 printf 到这里;get_build_state() 在
# 探测到 PID 已死时读它,把 returncode 落进 state.json(用于 UI 展示
# 「编译成功 / 编译失败」)
STATUS_PATH    = os.path.join(BUILD_DATA_DIR, 'last_exit_status')

os.makedirs(BUILD_DATA_DIR, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────
# 目标名白名单 ——
# C++/CMake target 名通常是 [A-Za-z_][A-Za-z0-9_]+,可能含 - 或 .,
# 例如 ``markdown2image`` / ``LGTBot_ElainaBot`` / ``numcomb``。
# 限制最大 63 字符,严禁任何 shell metachar(空格 ; | & $ \\ ` ( ) < > " ' 等)。
# ─────────────────────────────────────────────────────────────────────────
_TARGET_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_\-]{0,62}$')


def _validate_target_name(name: str) -> bool:
    """目标名严格白名单。后端最后一道闸，前端绕过也卡得住。"""
    return bool(name) and bool(_TARGET_RE.match(name))


# ─────────────────────────────────────────────────────────────────────────
# 状态文件读写
# ─────────────────────────────────────────────────────────────────────────

def _read_state() -> dict:
    """读 state.json;不存在 / 损坏返回 ``{}``。"""
    try:
        with open(STATE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning(f'读 build state 失败：{e}')
        return {}


def _write_state(d: dict) -> None:
    try:
        with open(STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f'写 build state 失败：{e}')


def _is_alive(pid) -> bool:
    """``os.kill(pid, 0)`` 探测进程是否存活;0/None 直接 False。"""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, OverflowError, ValueError):
        return False
    except PermissionError:
        # 进程存在但属于别的 uid。理论上不会(我们启动的);保守视为存活。
        return True
    except OSError:
        return False


def _log_size() -> int:
    try:
        return os.path.getsize(LOG_PATH) if os.path.isfile(LOG_PATH) else 0
    except OSError:
        return 0


def _read_exit_status() -> int | None:
    """读 STATUS_PATH 文件取 shell wrapper 写入的退出码;缺失 / 损坏返回 None。"""
    try:
        with open(STATUS_PATH, 'r', encoding='utf-8') as f:
            txt = f.read().strip()
        if txt.lstrip('-').isdigit():
            return int(txt)
    except (FileNotFoundError, OSError, ValueError):
        pass
    return None


def _compute_elapsed(state: dict) -> int | None:
    """从 ``start_time`` 和 ``finished_time`` 计算秒级 elapsed;
    任一缺失返回 None(meta 瞬时任务不展示用时)。
    """
    st = state.get('start_time')
    ft = state.get('finished_time')
    if not isinstance(st, (int, float)) or not isinstance(ft, (int, float)):
        return None
    return max(0, int(ft - st))


def get_build_state() -> dict:
    """对外:返回完整状态 dict。

    若 state 标记 running 但 PID 已死，补全 finished + returncode(读
    STATUS_PATH 取 wrapper 写入的退出码，可能 None —— 被 SIGKILL 等情况)
    + finished_time + elapsed_sec(用于 UI「上次任务  用时 X 分 Y 秒」)。

    ``kind`` 字段决定 UI badge 文案:
      · ``'build'`` —— 编译类(完整 / 增量 / 桥接层 / 自定义 / 清理重编),
                       结束后按 returncode 显示 ✅ 编译成功 / ❌ 编译失败
      · ``'meta'``  —— 非编译类(列出目标 / 删 build/),仅显示「已完成」
    """
    state = _read_state()
    pid = state.get('pid')
    running = _is_alive(pid) if pid else False
    if state and not running and not state.get('finished'):
        rc = _read_exit_status()
        state['finished'] = True
        state['running'] = False
        if rc is not None:
            state['returncode'] = rc
        # 完成时间 + 耗时 —— 在 finalize 这一刻记录,跨重启读 state 仍能展示
        state['finished_time'] = time.time()
        elapsed = _compute_elapsed(state)
        if elapsed is not None:
            state['elapsed_sec'] = elapsed
        _write_state(state)
    return {
        'running': running,
        'pid': pid if running else None,
        'cmd_display': state.get('cmd_display', ''),
        'cmd_argv': state.get('cmd_argv', []),
        'kind': state.get('kind', 'build'),
        'started_iso': state.get('started_iso', ''),
        'finished': state.get('finished', False),
        'returncode': state.get('returncode'),
        'elapsed_sec': state.get('elapsed_sec'),
        'log_size': _log_size(),
    }


# ─────────────────────────────────────────────────────────────────────────
# 启动 / 终止 编译
# ─────────────────────────────────────────────────────────────────────────

# 固定 wrapper 脚本(模块常量,不含任何用户/动态数据)——
# 真正要跑的构建命令通过环境变量 LGTBOT_ARGV_NL(换行分隔)传入,``mapfile`` 读回 bash 数组后 ``"${_cmd[@]}"`` 逐元素执行:
# shell **不对数组元素再做分词 / 通配 / 展开**,任何字符都安全。退出码写 LGTBOT_STATUS_FILE,并向 LGTBOT_LOG_FILE 追加 ``[exit:N]``。
#
# 关键安全设计:交给 shell 解释的命令串是**常量**,用户可控的 target 只作为"数据"经 env 传入,永远不进入命令行 tokenize 阶段
# 从根上杜绝命令注入(target 另有 _validate_target_name 白名单,双保险)。argv 元素均无换行(全是硬编码常量 + 白名单 target),换行分隔安全。
_BUILD_WRAPPER = (
    'mapfile -t _cmd <<< "$LGTBOT_ARGV_NL"; '
    '"${_cmd[@]}"; status=$?; '
    'printf "%s" "$status" > "$LGTBOT_STATUS_FILE"; '
    'printf "\\n[exit:%s]\\n" "$status" >> "$LGTBOT_LOG_FILE"; '
    'exit "$status"'
)


def _wrap_for_subprocess() -> list:
    """最终给 Popen 的 argv(list 形式，绝不 shell=True)。

    返回值只含**字面量**:要跑的构建命令由 ``_start_build`` 经 env 传入,不拼
    进这里的命令行。优先 util-linux ``script`` 提供伪 tty 让 cmake / gcc 保留
    彩色 ANSI;缺失回退纯 bash(无色但仍工作)。两条路径都在 bash 下执行
    ``_BUILD_WRAPPER``,末尾把退出码写 STATUS_PATH 供 get_build_state() 读取。
    """
    script_bin = shutil.which('script')
    if script_bin:
        # script -c 的命令串同样是常量:让它在 bash 下跑 env 里的 wrapper。
        return [script_bin, '-qfec', 'bash -c "$LGTBOT_WRAP"', '/dev/null']
    return ['bash', '-c', _BUILD_WRAPPER]


def _start_build(argv: list, display: str, kind: str = 'build') -> dict:
    """启动一个编译子进程。

    Args:
      argv:要执行的构建命令 list,例如 ``['bash', 'build.sh', '-i']``。经
           env(``LGTBOT_ARGV_NL``)作为数据传给常量 wrapper 执行(见
           ``_BUILD_WRAPPER``),不拼进命令行 —— 无注入风险。
      display:UI 上显示的任务名，例如 ``'增量编译桥接层'``。
      kind:``'build'`` 编译类(显示成功/失败)或 ``'meta'`` 非编译类
           (列目标 / 删 build/,只显示「已完成」)。

    成功返回 ``{success: True, message, pid}``,失败 ``{success: False, message}``。
    """
    state = get_build_state()
    if state['running']:
        return {
            'success': False,
            'message': f'已有编译在进行中 ({state["cmd_display"]}， PID {state["pid"]})，'
                       f'请先「终止编译」再启动新任务',
        }

    # 清掉上次的退出码文件 —— 必须先清,不然 get_build_state 探测到 PID 已死
    # 时会立刻把旧的 returncode 读进新 state(看起来像新任务一启动就失败)
    try:
        if os.path.isfile(STATUS_PATH):
            os.remove(STATUS_PATH)
    except OSError:
        pass

    # 清空旧日志,写入命令头
    try:
        with open(LOG_PATH, 'w', encoding='utf-8') as f:
            f.write(f'[{datetime.now().isoformat(timespec="seconds")}] '
                    f'$ {" ".join(shlex.quote(a) for a in argv)}\n')
    except Exception as e:
        return {'success': False, 'message': f'无法创建日志文件：{e}'}

    actual_cmd = _wrap_for_subprocess()
    log_f = open(LOG_PATH, 'ab', buffering=0)  # binary append, unbuffered

    # 强制彩色输出环境变量(对支持的工具生效)
    env = os.environ.copy()
    env.update({
        'CMAKE_COLOR_DIAGNOSTICS': 'ON',
        'CLICOLOR_FORCE': '1',
        'FORCE_COLOR': '1',
        'TERM': 'xterm-256color',
    })
    # 要执行的构建命令 + 退出码/日志文件路径,全部经 env 作为**数据**传给常量 wrapper(见 _BUILD_WRAPPER),不进入任何命令行 tokenize。
    env.update({
        'LGTBOT_ARGV_NL': '\n'.join(argv),
        'LGTBOT_STATUS_FILE': STATUS_PATH,
        'LGTBOT_LOG_FILE': LOG_PATH,
        'LGTBOT_WRAP': _BUILD_WRAPPER,
    })

    try:
        proc = subprocess.Popen(
            actual_cmd,
            cwd=boot.PLUGIN_DIR,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,   # 独立 session/process group,父进程死了不被牵连
            env=env,
            close_fds=True,
        )
    except FileNotFoundError as e:
        log_f.close()
        return {'success': False, 'message': f'命令未找到：{e}'}
    except Exception as e:
        log_f.close()
        return {'success': False, 'message': f'启动失败：{e}'}
    finally:
        log_f.close()  # 子进程已 dup fd,父进程不需要持有

    _write_state({
        'pid': proc.pid,
        'start_time': time.time(),
        'cmd_display': display,
        'cmd_argv': argv,   # 显示用,不含 script 包装(用户看了易懂)
        'kind': kind,
        'started_iso': datetime.now().isoformat(timespec='seconds'),
        'finished': False,
        'running': True,
        'returncode': None,
    })

    log.info(f'[build] 已启动：{display} (PID {proc.pid}， kind={kind})')
    # 只记「已启动」—— 编译结果由 build 状态页(state.json + 日志)负责展示
    audit.record('build', display, f'已启动 (PID {proc.pid})')
    return {'success': True, 'message': f'已启动：{display}', 'pid': proc.pid}


def _kill_build() -> dict:
    """对当前 build 进程发 SIGTERM(2s 不响应升级 SIGKILL)。"""
    state = get_build_state()
    if not state['running']:
        return {'success': False, 'message': '当前没有编译在进行'}
    pid = state['pid']
    try:
        pgid = os.getpgid(int(pid))
    except (ProcessLookupError, PermissionError, OSError) as e:
        audit.record('build', '终止编译', f'获取进程组失败: {e}', ok=False)
        return {'success': False, 'message': f'获取进程组失败：{e}'}

    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError) as e:
        audit.record('build', '终止编译', f'SIGTERM 失败: {e}', ok=False)
        return {'success': False, 'message': f'SIGTERM 失败：{e}'}

    # 最多等 2s 让进程优雅退出;否则 SIGKILL
    for _ in range(20):
        time.sleep(0.1)
        if not _is_alive(pid):
            break
    else:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except Exception:
            pass

    # 写 finished + 完成时间 + 耗时(用户手动终止也算「用时 N 秒」展示)
    s = _read_state()
    s['finished'] = True
    s['running'] = False
    s['finished_time'] = time.time()
    elapsed = _compute_elapsed(s)
    if elapsed is not None:
        s['elapsed_sec'] = elapsed
    _write_state(s)

    # 在日志末尾追加一条提示,方便用户对应得上
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(f'\n[{datetime.now().isoformat(timespec="seconds")}] '
                    f'⛔ 用户主动终止 (PID {pid})\n')
    except Exception:
        pass

    audit.record('build', '终止编译', f'PID {pid}')
    return {'success': True, 'message': f'已终止编译进程 (PID {pid})'}


# ─────────────────────────────────────────────────────────────────────────
# 日志读取 + ANSI 解析成结构化段
# ─────────────────────────────────────────────────────────────────────────

# CSI 序列 \x1b[<params>m;我们只关心 m 结尾的 SGR(颜色 / 粗体)
_SGR_RE = re.compile(r'\x1b\[([0-9;]*)m')
# 其它 escape(光标移动 / 清屏等)整段剥掉,UI 不需要
_OTHER_CSI_RE = re.compile(r'\x1b\[[0-9;?]*[ABCDEFGHJKLSTfilmnpsu]')
_OTHER_ESC_RE = re.compile(r'\x1b[=>()]?[0-9A-Za-z]')
# 删 NULL / BEL / 表单进给等
_CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')

# SGR 颜色码 → CSS color(只覆盖 30-37 / 90-97 前景色;背景色 40-47 / 100-107 留作扩展)
_SGR_FG = {
    30: '#3b3b3b', 31: '#d33', 32: '#3a3', 33: '#c80',
    34: '#36c', 35: '#a3a', 36: '#1aa', 37: '#aaa',
    90: '#888', 91: '#f88', 92: '#8d8', 93: '#dd6',
    94: '#88f', 95: '#f8f', 96: '#6dd', 97: '#eee',
}


def _ansi_to_segments(text: str) -> list:
    """ANSI escape → 结构化段列表(只支持 SGR 前景色 + 粗体;其他 escape 剥掉)。

    返回 ``[{'t': 文本, 'b': 1(粗体,可选), 'c': '#rgb'(前景色,可选)}, ...]``,
    经 JSON 原样交给前端,由 JS 用 createTextNode / CSSOM 直接建 DOM 节点。
    日志内容从头到尾不出现 HTML 字符串,前端也就没有任何 HTML 解析环节。
    """
    if not text:
        return []
    # 先把 \r 转 \n(script 命令可能产生 CR/LF 混乱)
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    segments: list = []
    bold = False
    color = ''

    def emit(chunk: str) -> None:
        # 清掉其它 CSI / 控制字符;空块不产生段
        chunk = _OTHER_CSI_RE.sub('', chunk)
        chunk = _OTHER_ESC_RE.sub('', chunk)
        chunk = _CTRL_RE.sub('', chunk)
        if not chunk:
            return
        seg: dict = {'t': chunk}
        if bold:
            seg['b'] = 1
        if color:
            seg['c'] = color
        segments.append(seg)

    pos = 0
    for m in _SGR_RE.finditer(text):
        emit(text[pos:m.start()])
        # SGR 状态机:0 复位 / 1 粗体 / 30-37+90-97 前景色。新前景色替换旧值
        # (而非嵌套),与 ANSI 语义一致。
        for code_str in (m.group(1) or '0').split(';'):
            try:
                code = int(code_str)
            except ValueError:
                continue
            if code == 0:
                bold = False
                color = ''
            elif code == 1:
                bold = True
            elif code in _SGR_FG:
                color = _SGR_FG[code]
        pos = m.end()

    emit(text[pos:])
    return segments


def _read_log_tail(max_bytes: int = 64 * 1024) -> str:
    """读日志末尾 ``max_bytes`` 字节(decode utf-8,容错)。"""
    if not os.path.isfile(LOG_PATH):
        return ''
    try:
        size = os.path.getsize(LOG_PATH)
        offset = max(0, size - max_bytes)
        with open(LOG_PATH, 'rb') as f:
            f.seek(offset)
            data = f.read()
        return data.decode('utf-8', errors='replace')
    except Exception as e:
        return f'[读取日志失败：{e}]\n'


# ─────────────────────────────────────────────────────────────────────────
# 数据入口 + 9 个 action 端点
# ─────────────────────────────────────────────────────────────────────────

def _read_last_custom_target() -> str:
    """读 PARAMS_PATH 取上一次「编译指定目标」的 target,经过白名单校验后返回;
    任一环节失败返回空串(等价于"无历史值"——前端 prompt 不预填)。

    这是 prompt 预填功能的数据源 —— 用户连续编译同一个 target 时不用重复
    输入。文件由 buildCustom 的 framework /api/config-file/save POST 路径
    写入，无需额外的持久化通道。
    """
    try:
        with open(PARAMS_PATH, 'r', encoding='utf-8') as f:
            params = json.load(f) or {}
        candidate = (params.get('target') or '').strip()
        if _validate_target_name(candidate):
            return candidate
    except Exception:
        pass
    return ''


def get_data() -> str:
    """页面渲染时填入 ``<script id="build-data">``。"""
    state = get_build_state()
    log_text = _read_log_tail(64 * 1024)
    payload = {
        'state': state,
        'log_segments': _ansi_to_segments(log_text),
        'log_size': _log_size(),
        'params_path': os.path.abspath(PARAMS_PATH),
        'last_custom_target': _read_last_custom_target(),
        'plugin_dir': boot.PLUGIN_DIR,
        'build_sh_exists': os.path.isfile(os.path.join(boot.PLUGIN_DIR, 'build.sh')),
        'build_dir_exists': os.path.isdir(os.path.join(boot.PLUGIN_DIR, 'build')),
        'has_script_cmd': bool(shutil.which('script')),
    }
    data_json = json.dumps(payload, ensure_ascii=False, default=str)
    return data_json.replace('</script>', '<\\/script>')


def _fragment(payload: dict) -> str:
    body = json.dumps(payload, ensure_ascii=False, default=str)
    return f'<pre id="result">{_html.escape(body)}</pre>'


def _require_build_dir() -> tuple[bool, str]:
    """增量编译前置:build/ 必须已存在"""
    if not os.path.isdir(os.path.join(boot.PLUGIN_DIR, 'build')):
        return False, '增量编译要求 build/ 目录已存在，请先「完整编译」'
    return True, ''


def _require_build_sh() -> tuple[bool, str]:
    if not os.path.isfile(os.path.join(boot.PLUGIN_DIR, 'build.sh')):
        return False, 'build.sh 不存在，无法启动编译'
    return True, ''


def render_build_full() -> str:
    """🚀 完整编译 —— bash build.sh"""
    ok, msg = _require_build_sh()
    if not ok:
        return _fragment({'success': False, 'message': msg})
    return _fragment(_start_build(['bash', 'build.sh'], '完整编译'))


def render_build_incr() -> str:
    """⚡ 增量编译 —— bash build.sh -i"""
    ok, msg = _require_build_sh()
    if not ok:
        return _fragment({'success': False, 'message': msg})
    ok, msg = _require_build_dir()
    if not ok:
        return _fragment({'success': False, 'message': msg})
    return _fragment(_start_build(['bash', 'build.sh', '-i'], '增量编译'))


def render_build_bridge() -> str:
    """🔌 增量编译桥接层 —— bash build.sh -i -t LGTBot_ElainaBot"""
    ok, msg = _require_build_sh()
    if not ok:
        return _fragment({'success': False, 'message': msg})
    ok, msg = _require_build_dir()
    if not ok:
        return _fragment({'success': False, 'message': msg})
    return _fragment(_start_build(
        ['bash', 'build.sh', '-i', '-t', 'LGTBot_ElainaBot'],
        '增量编译桥接层'))


def render_build_list() -> str:
    """📋 列出可编译目标 —— bash build.sh --list-targets

    kind='meta':非编译任务，UI 只显示「已完成」灰色 badge,不展示
    成功/失败(因为「列出」无所谓成功失败，只看日志输出即可)。
    """
    ok, msg = _require_build_sh()
    if not ok:
        return _fragment({'success': False, 'message': msg})
    return _fragment(_start_build(
        ['bash', 'build.sh', '--list-targets'], '列出可编译目标', kind='meta'))


def render_build_custom() -> str:
    """🎯 编译指定目标 —— 从 PARAMS_PATH 读 target,严格白名单后启动。

    PARAMS_PATH 由 JS 通过 framework ``/api/config-file/save`` POST 写入，
    内容形如 ``{"target": "numcomb"}``。后端读 JSON 后再次校验，**不信任前端**。
    """
    ok, msg = _require_build_sh()
    if not ok:
        return _fragment({'success': False, 'message': msg})
    ok, msg = _require_build_dir()
    if not ok:
        return _fragment({'success': False, 'message': msg})
    try:
        with open(PARAMS_PATH, 'r', encoding='utf-8') as f:
            params = json.load(f)
    except FileNotFoundError:
        return _fragment({'success': False, 'message': '目标参数文件不存在，请重新点击「编译指定目标」'})
    except Exception as e:
        return _fragment({'success': False, 'message': f'读取目标参数失败：{e}'})
    target = (params.get('target') or '').strip()
    if not _validate_target_name(target):
        return _fragment({
            'success': False,
            'message': f'目标名称非法：{target!r} (只允许字母数字下划线连字符，'
                       f'长度 1-63，必须以字母或下划线开头)',
        })
    return _fragment(_start_build(
        ['bash', 'build.sh', '-i', '-t', target],
        f'增量编译目标 {target}'))


def render_build_kill() -> str:
    """🛑 终止当前编译进程"""
    return _fragment(_kill_build())


def render_build_clean() -> str:
    """🧹 清理重编 —— bash build.sh --clean(删 build/ 后重新完整编译)"""
    ok, msg = _require_build_sh()
    if not ok:
        return _fragment({'success': False, 'message': msg})
    return _fragment(_start_build(
        ['bash', 'build.sh', '--clean'],
        '清理重编 (--clean)'))


def _write_meta_done(display: str, argv: list, returncode: int) -> None:
    """记录一次「同步完成」的 meta 任务到 state.json,让 UI 显示「已完成」灰 badge。

    用于 render_build_remove 这种不启动子进程、瞬时完成的动作。
    """
    now = datetime.now().isoformat(timespec='seconds')
    _write_state({
        'pid': None,
        'start_time': time.time(),
        'cmd_display': display,
        'cmd_argv': argv,
        'kind': 'meta',
        'started_iso': now,
        'finished': True,
        'running': False,
        'returncode': returncode,
    })


def render_build_remove() -> str:
    """🗑 删除 build/ 目录 —— 同步 ``shutil.rmtree``,不启动子进程。

    Python 直接调用 rmtree(不走 subprocess),避免任何 shell 介入。
    完成后写 state.json(``kind='meta'``)让 UI 标记「已完成」。

    Audit:本函数只能由用户在仪表盘点「🗑 删除 build/ 目录」按钮 + 双次
    confirm 后触发(见 build.js::buildRemove);后端不主动调用。
    rmtree 前后各 ``log.info`` 一条 audit 日志便于事后排查。
    """
    state = get_build_state()
    if state['running']:
        return _fragment({
            'success': False,
            'message': '编译进行中，无法删除 build/(请先「终止编译」)',
        })
    build_dir = os.path.join(boot.PLUGIN_DIR, 'build')
    display_argv = ['rm', '-rf', 'build']  # 仅展示用,实际是 Python rmtree
    if not os.path.isdir(build_dir):
        log.info(f'[build-remove] 跳过(目录不存在):{build_dir}')
        _write_meta_done('删除 build/ 目录 (目录原本不存在)', display_argv, 0)
        return _fragment({'success': True, 'message': 'build/ 目录不存在，无需删除', 'removed': False})
    log.info(f'[build-remove] 开始删除:{build_dir}')
    try:
        shutil.rmtree(build_dir)
    except Exception as e:
        log.warning(f'[build-remove] 失败:{build_dir}:{e}')
        _write_meta_done('删除 build/ 目录', display_argv, 1)
        audit.record('build', '删除 build/ 目录', str(e), ok=False)
        return _fragment({'success': False, 'message': f'删除失败：{e}'})
    log.info(f'[build-remove] 完成:{build_dir}')
    audit.record('build', '删除 build/ 目录')
    # 顺便在日志末尾留一条 audit 记录(若 build.log 不存在,append 模式会创建)
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(f'\n[{datetime.now().isoformat(timespec="seconds")}] '
                    f'🗑 已删除 build/ 目录\n')
    except Exception:
        pass
    _write_meta_done('删除 build/ 目录', display_argv, 0)
    return _fragment({'success': True, 'message': '✅ build/ 目录已删除', 'removed': True})


def render_build_log() -> str:
    """轮询用:返回当前状态 + 日志末尾 64KB。

    前端的 polling 不是真增量(provider 无参拿不到 since-byte),而是「全量
    末尾切片」—— 每次都返回最后 64KB,JS 自己 diff 决定是否替换。日志很长
    时只看末尾即可，看更老内容请打开 data/build/build.log 文件。
    """
    state = get_build_state()
    log_text = _read_log_tail(64 * 1024)
    return _fragment({
        'state': state,
        'log_segments': _ansi_to_segments(log_text),
        'log_size': _log_size(),
    })
