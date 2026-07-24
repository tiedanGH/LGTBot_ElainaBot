#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
「仪表盘」标签 —— 集中展示版本/机器人绑定/缓存，提供一键更新与缓存清理。

★ 安全准则 ★
  · **没有任何自动清理 / 自动更新 / 后台定时任务**。所有破坏性动作
    (清缓存 / git pull / git submodule update / 删 build/ 等)都通过
    ``render_*`` 端点暴露，而端点**必须用户在 Web UI 上点按钮 + 至少
    一次 confirm 弹窗触发**;后端不主动调度它们，也不在任何定时器、
    @on_load / @on_unload 钩子或框架事件回调里调用。
  · ``_clear_dir`` / ``_clear_dir_keep_recent`` 在真正动手删之前 ``log.info``
    一条 audit 日志(包含路径 + 删除项数),方便事后排查「为什么文件没了」。
  · 引擎自身(LGTBot C++)可能在启动时清理过期渲染图，**那是 lgtbot 子模块
    的行为**,不归本模块管;若服务器上 ``data/engine/images/gen`` 出现意外
    丢失，先看 ``data/build/build.log`` 是否有 ``--clean`` / 编译 trace,
    再看主框架 plugin 日志是否记到了本文件 audit 行(没有 = 不是我们清的)。

Python 侧职责:
  · ``TAB_HTML`` / ``TAB_JS`` 从 ``templates/dashboard/`` 加载
  · ``get_data()`` 返回面板数据(版本、机器人列表、缓存尺寸、引擎配置内容、
    config 文件绝对路径 —— 保存走主框架 ``/api/config-file/save`` 端点)
  · ``render_check_update`` / ``render_do_update`` / ``render_clear_*``
    供 ``webui/main.py`` 注册为隐藏 action 端点(参考 RESTART_KEY 套路),
    每个端点返回 ``<pre id="result">JSON</pre>`` 单片段供 JS 解析
"""

from __future__ import annotations

import asyncio
import glob
import html as _html
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

from aiohttp import web

from core.base.logger import get_logger, PLUGIN
from .. import audit, boot, helpers, prebuilt, state, userdb
from .. import config as _config

log = get_logger(PLUGIN, 'LGTBot')

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')


def _load(name: str) -> str:
    with open(os.path.join(_TEMPLATE_DIR, name), 'r', encoding='utf-8') as f:
        return f.read()


TAB_HTML = _load('dashboard/dashboard.html')
TAB_CSS = _load('dashboard/dashboard.css')
TAB_JS = _load('dashboard/dashboard.js')


# ─────────────────────────────────────────────────────────────────────────
# 运行环境自检 —— 运行时依赖(引擎运行必须)+ 编译依赖(本地编译才需,预编译模式可免)。
# 构建来源 mode 仍取 ``prebuilt.mode_info()``。
# ``get_data()`` 把结果塞进 dashboard-data,前端 dashboard.js 的 dashRenderSelfCheck 渲染成检查项网格。
# 编译依赖列表覆盖 build.sh 依赖检查 + apt 清单 + CI(cmake.yml)所装。
# ─────────────────────────────────────────────────────────────────────────

_LIB_DIRS = (
    '/usr/lib', '/usr/lib64', '/usr/local/lib', '/usr/local/lib64',
    '/usr/lib/x86_64-linux-gnu',
)
_ldconfig_cache: str | None = None


def _ldconfig_text() -> str:
    """``ldconfig -p`` 输出,进程内缓存一次(自检可能多次调用)。"""
    global _ldconfig_cache
    if _ldconfig_cache is None:
        try:
            out = subprocess.run(['ldconfig', '-p'], capture_output=True, text=True, timeout=5)
            _ldconfig_cache = out.stdout or ''
        except Exception:
            _ldconfig_cache = ''
    return _ldconfig_cache


def _lib_present(token: str) -> bool:
    """共享库是否存在:先查 ldconfig 缓存,再扫常见 lib 目录(缓存可能过期)。"""
    if token in _ldconfig_text():
        return True
    for d in _LIB_DIRS:
        if glob.glob(os.path.join(d, token + '*.so*')):
            return True
    return False


def _header_present(*paths: str) -> bool:
    """开发头是否存在。除给定路径外,对 ``/usr/include/<x>`` 额外探 multiarch 目录 ``/usr/include/<triplet>/<x>``
    (Debian/Ubuntu 把 curl.h 等放这里,如 ``/usr/include/x86_64-linux-gnu/curl/curl.h``)"""
    for p in paths:
        if os.path.isfile(p):
            return True
        prefix = '/usr/include/'
        if p.startswith(prefix) and glob.glob(f'{prefix}*-linux-gnu/{p[len(prefix):]}'):
            return True
    return False


def _python_dev_present() -> bool:
    try:
        import sysconfig
        inc = sysconfig.get_path('include')
        return bool(inc) and os.path.isfile(os.path.join(inc, 'Python.h'))
    except Exception:
        return False


# ldd 结果缓存:path → (mtime, missing_set)。产物 mtime 不变就不重跑 subprocess。
_ldd_cache: dict = {}


def _run_ldd(path: str):
    """对单个 ELF 跑 ``ldd``,返回其中 ``=> not found`` 的 soname 集合;ldd 不可用 /
    执行失败返回 ``None``(调用方回退静态检查)。结果按 mtime 缓存。"""
    try:
        mtime = os.path.getmtime(path)
        hit = _ldd_cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        out = subprocess.run(['ldd', path], capture_output=True, text=True, timeout=10)
        if not out.stdout:
            return None
        missing = {line.strip().split('=>')[0].strip()
                   for line in out.stdout.splitlines() if 'not found' in line}
        _ldd_cache[path] = (mtime, missing)
        return missing
    except Exception:
        return None


# 已知运行时硬依赖 —— 逐项在面板列出(面板友好名, soname 前缀, 备注)。
# 判定:ldd 在场时看实测缺失集是否有该前缀的 soname(精确 / 带版本);否则回退 ldconfig/lib 目录静态判定。
_RUNTIME_LIBS = (
    ('Boost.Python 运行时', 'libboost_python', ''),
    ('libcurl 运行时',      'libcurl',         ''),
    ('SQLite3 运行时',      'libsqlite3',      'slim 镜像通常自带'),
    ('Protobuf 运行时',     'libprotobuf',     ''),
)
_RUNTIME_PREFIXES = tuple(t for _, t, _ in _RUNTIME_LIBS)


def self_check() -> dict:
    """收集运行时 + 编译依赖自检数据。

    runtime = 引擎「跑起来」的硬依赖(缺失报红、计入严重异常):桥接层 .so / 引擎共享库 /
    Python 版本 + **逐项列出**的系统库(Boost.Python、libcurl、SQLite3、Protobuf)。系统库
    判定优先用 ldd 实测:对真实产物(桥接 .so、build/lib*.so、runner)跑 ``ldd``,某库若出现
    在 ``not found`` 集里即判缺(soname 版本精确、自动适应 --glog 等条件构建,不会像 ldconfig
    静态子串那样误报;实测 Boost.System 是 header-only、glog 默认 OFF、gflags 被 --as-needed
    丢弃 —— 都**不是**运行时依赖,不列)。ldd 还会额外揪出清单外的缺失外部库。产物 / ldd
    不可用时回退 ldconfig/lib 目录静态判定。

    warn 级(``warn: True``,前端黄点、计入警告计数、不算严重异常):Qt 图片渲染 ——
    markdown2image 需要 Qt5 WebKit 或 Qt6 WebEngine **其一**(二选一,detail 注明当前后端),
    缺失引擎仍可启动、只是图片渲染不可用。

    compile = 仅本地从源码编译时才需要(标 ``optional_prebuilt=True``,灰显、不计异常)。
    返回 ``{runtime:[...], compile:[...], mode:{...}}``。"""
    active = boot.BUILD_DIR
    engine_libs = glob.glob(os.path.join(active, 'lib*.so')) if os.path.isdir(active) else []
    engine_root = getattr(boot, 'ENGINE_ROOT', boot.PLUGIN_DIR)

    runtime = [
        {'name': '桥接层扩展 LGTBot_ElainaBot.so',
         'ok': bool(boot.LGTBOT_AVAILABLE),
         'detail': '已加载' if boot.LGTBOT_AVAILABLE else (boot.IMPORT_ERROR or '未加载')},
        {'name': '引擎共享库 (build/lib*.so)',
         'ok': len(engine_libs) > 0,
         'detail': f'{len(engine_libs)} 个' if engine_libs else '缺失,需下载预编译包或本地编译'},
        {'name': 'Python 版本',
         'ok': sys.version_info[:2] >= (3, 11),
         'detail': f'{prebuilt.local_python_tag()}(框架要求 3.11+)'},
    ]

    # ── 系统库运行时依赖:逐项列出;ldd 在场以链接器真相判定,否则静态回退 ────────
    bridge_so = os.path.join(engine_root, 'LGTBot_ElainaBot.so')
    runners = [os.path.join(active, n) for n in ('config_runner', 'match_game_runner')]
    targets = [p for p in [bridge_so, *engine_libs, *runners] if os.path.isfile(p)]
    # 引擎自有库(相互 NEEDED,由 boot 预加载 / LD_LIBRARY_PATH 解析,ldd 裸跑必然 not found)。
    # 用 lib*.so* 通配:NEEDED 记录的是带版本 soname(如 libmd4c-html.so.0),lib*.so 收不到
    own_libs = ({os.path.basename(p)
                 for p in glob.glob(os.path.join(active, 'lib*.so*'))}
                | {'LGTBot_ElainaBot.so'})

    # ldd 实测缺失的外部 soname 集;None = 没跑 ldd(回退 ldconfig 静态判定)
    missing_ext = None
    if targets and shutil.which('ldd'):
        results = [_run_ldd(p) for p in targets]
        if all(r is not None for r in results):
            missing_ext = set()
            for r in results:
                missing_ext |= {s for s in r if s not in own_libs}

    for label, token, note in _RUNTIME_LIBS:
        if missing_ext is not None:
            miss = sorted(s for s in missing_ext if s.startswith(token))
            ok = not miss
            detail = ('已检测到(ldd)' if ok
                      else f'缺 {miss[0]},需安装运行时库(见 DEPLOY)')
        else:
            ok = _lib_present(token)
            detail = ('已检测到' if ok else '未检测到,需安装运行时库(见 DEPLOY)') \
                     + (f'({note})' if note else '')
        runtime.append({'name': label, 'ok': ok, 'detail': detail})

    # ldd 揪出的、不在已知清单里的其他缺失外部库(catch-all;Qt 由下方 warn 项单独管)
    if missing_ext:
        for so in sorted(missing_ext):
            if not so.startswith(_RUNTIME_PREFIXES) and not so.startswith('libQt'):
                runtime.append({'name': f'系统库 {so}', 'ok': False,
                                'detail': f'缺 {so},需安装运行时库(见 DEPLOY)'})

    # ── Qt 图片渲染(warn 级:二选一,缺失黄点计警告,引擎仍可启动) ─────────────
    # markdown2image 出图后端:Qt5 走 WebKit、Qt6 走 WebEngine,**二选一**即可。
    # 用 _lib_present 区分并命名当前后端(与旧版一致);缺失只影响图片渲染。
    qt5 = _lib_present('libQt5WebKit')          # 匹配 libQt5WebKit{,Widgets}*.so
    qt6 = _lib_present('libQt6WebEngineCore')
    qt_ok = qt5 or qt6
    if qt5:
        qt_detail = '已检测到 Qt5 WebKit'
    elif qt6:
        qt_detail = '已检测到 Qt6 WebEngine'
    else:
        qt_detail = '未检测到 Qt5 WebKit / Qt6 WebEngine(二选一),缺失仅影响图片渲染,引擎仍可启动'
    runtime.append({'name': '图片渲染 Qt (markdown2image)', 'ok': qt_ok,
                    'warn': True, 'detail': qt_detail})

    which = shutil.which

    def _dep(name: str, ok: bool, detail: str = '') -> dict:
        return {'name': name, 'ok': bool(ok),
                'detail': detail or ('已检测到' if ok else '未检测到'),
                'optional_prebuilt': True}

    cxx = which('g++') or which('clang++')
    compile_deps = [
        _dep('CMake', which('cmake'), which('cmake') or '未安装'),
        _dep('C++ 编译器 (g++/clang++)', cxx, cxx or '未安装'),
        _dep('Boost.Python 开发 (libboost-python-dev)', _lib_present('libboost_python')),
        _dep('gflags 开发 (libgflags-dev)', _lib_present('libgflags')),
        _dep('libcurl 开发头', _header_present('/usr/include/curl/curl.h')),
        _dep('Python 开发头 (python3-dev)', _python_dev_present(),
             'Python.h ' + ('存在' if _python_dev_present() else '缺失')),
        _dep('SQLite3 开发头', _header_present('/usr/include/sqlite3.h')),
        _dep('Protobuf 编译器 (protoc)', which('protoc'), which('protoc') or '未安装'),
    ]
    return {'runtime': runtime, 'compile': compile_deps, 'mode': prebuilt.mode_info()}


# ─────────────────────────────────────────────────────────────────────────
# 元数据:版本号 / GitHub 仓库 URL —— 从 main.py 顶层 __plugin_meta__ 读取,
# 避免硬编码两处。
#
# 关键:framework 大型插件入口的 sys.modules 名是 ``plugins.LGTBot_ElainaBot``
# (见 core/plugin/_loader.py::_import_plugin 行 246 的
# ``mod_name = f'plugins.{name}'``),**不是** ``plugins.LGTBot_ElainaBot.main``。
# 早期版本误用后者导致查不到 → "无法解析 GitHub 仓库地址"。
#
# 三层 fallback 保证稳健:
#   1. sys.modules['plugins.LGTBot_ElainaBot']           (大型插件 entry)
#   2. sys.modules['plugins.LGTBot_ElainaBot.main']      (multi-file 加载备选)
#   3. ast.parse(main.py) → 取顶层 __plugin_meta__ 字面量(永不失败的兜底)
# ─────────────────────────────────────────────────────────────────────────

def _get_plugin_meta() -> dict:
    """获取 main.py 顶层 ``__plugin_meta__``;三层 fallback 保证拿得到。"""
    # 路径 1:大型插件入口模块名
    for mod_name in ('plugins.LGTBot_ElainaBot', 'plugins.LGTBot_ElainaBot.main'):
        m = sys.modules.get(mod_name)
        if m is None:
            continue
        meta = getattr(m, '__plugin_meta__', None)
        if isinstance(meta, dict) and meta:
            return meta
    # 路径 2:扫 sys.modules 找带 __plugin_meta__ 且名字匹配本插件的
    for name, mod in list(sys.modules.items()):
        if 'LGTBot_ElainaBot' not in name:
            continue
        meta = getattr(mod, '__plugin_meta__', None)
        if isinstance(meta, dict) and meta.get('name') == 'LGTBot 机器人':
            return meta
    # 路径 3:AST 直接读文件取字面量
    try:
        import ast
        main_py = os.path.join(boot.PLUGIN_DIR, 'main.py')
        with open(main_py, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read())
        for node in tree.body:
            if (isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == '__plugin_meta__'
                    and isinstance(node.value, ast.Dict)):
                return ast.literal_eval(node.value) or {}
    except Exception as e:
        log.debug(f'AST 解析 main.py 失败: {e}')
    return {}


def _parse_github_owner_repo(url: str) -> tuple[str, str]:
    """从 ``https://github.com/owner/repo`` 抽 ``(owner, repo)``;失败返回 ``('', '')``。"""
    if not url:
        return '', ''
    try:
        s = url.rstrip('/')
        if s.endswith('.git'):
            s = s[:-4]
        parts = s.split('/')
        # 主机名必须 **精确等于** github.com(或 www 变体)
        if len(parts) >= 5 and parts[2].lower() in ('github.com', 'www.github.com'):
            return parts[3], parts[4]
    except Exception:
        pass
    return '', ''


# ─────────────────────────────────────────────────────────────────────────
# 子模块(lgtbot/)状态检测 —— 读 .gitmodules 取 url/branch,然后:
#   · 路径不存在 → status='missing'
#   · 路径存在但无 .git → status='empty'(子模块未 init)
#   · 否则 → status='ok',rev-parse HEAD 取本地 commit
#
# 上游最新 commit 通过 GitHub commits API 取。
# ─────────────────────────────────────────────────────────────────────────

# 给 git 命令注入的 SSH→HTTPS url 改写 ——
# lgtbot 上游 .gitmodules 把 7 个嵌套子模块都登记成 ``git@github.com:...`` 形式,
# 市场用户没 SSH key 就会卡住。下面这两条 ``-c url.<https>.insteadOf=<ssh>`` 在**本次
# git 子进程内临时生效**,不污染用户 ``~/.gitconfig`` 也不动 .gitmodules 文件,
# 递归 init 时同样会作用到嵌套子模块。同时覆盖两种 ssh 写法:
#   · ``git@github.com:owner/repo.git``        ← scp-like 短写法
#   · ``ssh://git@github.com/owner/repo.git``  ← 完整 ssh:// 形式
_GITHUB_SSH_TO_HTTPS_ARGS = [
    '-c', 'url.https://github.com/.insteadOf=git@github.com:',
    '-c', 'url.https://github.com/.insteadOf=ssh://git@github.com/',
]


# .gitmodules 唯一一项的 fallback —— 文件丢失或读取失败时仍按已知配置工作
_SUBMODULE_FALLBACK = {
    'path':   'lgtbot',
    'url':    'https://github.com/Slontia/lgtbot.git',
    'branch': 'master',
}


def _parse_gitmodules() -> dict:
    """解析 ``<plugin_dir>/.gitmodules``,返回 ``{path, url, branch}`` dict。

    文件不存在或解析失败时返回 ``_SUBMODULE_FALLBACK`` 兜底值，让 UI 仍能展示
    上游链接和默认分支。该插件只配置了一个子模块(lgtbot),不实现多 submodule
    支持以保持代码简洁;后续若加更多子模块再扩展为 list。
    """
    info = dict(_SUBMODULE_FALLBACK)
    gm = os.path.join(boot.PLUGIN_DIR, '.gitmodules')
    if not os.path.isfile(gm):
        return info
    try:
        with open(gm, 'r', encoding='utf-8') as f:
            for raw in f:
                line = raw.strip()
                if '=' not in line or line.startswith('#'):
                    continue
                k, _, v = line.partition('=')
                k, v = k.strip(), v.strip()
                if k in ('path', 'url', 'branch'):
                    info[k] = v
    except Exception as e:
        log.debug(f'读取 .gitmodules 失败: {e}')
    return info


def _local_submodule_commit(sub_path: str) -> tuple[str, str]:
    """``git -C <plugin_dir>/<sub_path> rev-parse HEAD`` 取本地 commit。

    返回 ``(short_sha, full_sha)``;失败返回 ``('', '')``。
    """
    sub_abs = os.path.join(boot.PLUGIN_DIR, sub_path)
    try:
        proc = subprocess.run(
            ['git', '-C', sub_abs, 'rev-parse', 'HEAD'],
            capture_output=True, text=True, timeout=5.0,
        )
        if proc.returncode != 0:
            return '', ''
        full = (proc.stdout or '').strip()
        return (full[:7], full) if full else ('', '')
    except Exception as e:
        log.debug(f'rev-parse {sub_path} HEAD 失败：{e}')
        return '', ''


def _query_upstream_commit(owner: str, repo: str, branch: str) -> tuple[str, str, str]:
    """GET GitHub ``/repos/{owner}/{repo}/commits/{branch}`` 取最新 commit。

    返回 ``(short_sha, full_sha, error_message)``;失败时前两项空，error 含原因。
    """
    if not owner or not repo:
        return '', '', '上游仓库地址未配置'
    api_url = f'https://api.github.com/repos/{owner}/{repo}/commits/{branch or "HEAD"}'
    try:
        req = urllib.request.Request(
            api_url,
            headers={
                'User-Agent': 'LGTBot-Dashboard',
                'Accept': 'application/vnd.github+json',
            },
        )
        with urllib.request.urlopen(req, timeout=8.0) as r:
            data = json.loads(r.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as e:
        return '', '', f'GitHub HTTP {e.code} (可能触发匿名 60 次每小时限流)'
    except Exception as e:
        return '', '', f'网络错误：{e}'
    full = data.get('sha') or ''
    if not full:
        return '', '', '响应缺少 sha 字段'
    return full[:7], full, ''


def _get_submodule_info(query_remote: bool = False) -> dict:
    """汇总子模块状态。``query_remote=True`` 会调用 GitHub API 取远端 commit;
    ``False`` 只取本地状态，适合 ``get_data()`` 首次渲染(避免拖慢页面)。

    status 取值(子模块自身):
      · ``ok``       —— 子模块已初始化，本地 HEAD 可读
      · ``missing``  —— 文件夹不存在
      · ``empty``    —— 文件夹存在但内部为空 / 无 .git(未 init)

    repo_status 取值(**插件目录自身**,与子模块独立):
      · ``ok``     —— ``<plugin_dir>/.git`` 存在，可正常 git pull / submodule update
      · ``no_git`` —— 插件市场 zip 下载场景，根目录无 .git;前端据此把桥接层
                     行的「更新桥接层」按钮文案换成「初始化为 git 仓库」
    """
    cfg = _parse_gitmodules()
    sub_path = cfg['path']
    upstream_url = cfg['url']
    branch = cfg['branch']
    owner, repo = _parse_github_owner_repo(upstream_url)

    info = {
        'path': sub_path,
        'upstream_url': upstream_url,
        'upstream_owner': owner,
        'upstream_repo': repo,
        'branch': branch,
        'status': 'ok',
        # 插件目录本身的 git 状态 —— 与子模块状态独立,市场用户解压 zip 后是 'no_git'
        'repo_status': ('ok' if os.path.isdir(os.path.join(boot.PLUGIN_DIR, '.git'))
                        else 'no_git'),
        'local_commit': '',
        'local_commit_full': '',
        'remote_commit': '',
        'remote_commit_full': '',
        'has_update': False,
        'error': '',
    }

    sub_abs = os.path.join(boot.PLUGIN_DIR, sub_path)
    if not os.path.isdir(sub_abs):
        info['status'] = 'missing'
    else:
        # 目录是否为「子模块已 init」的标志:lgtbot/.git 存在(子模块下是文件,
        # 指向父仓库 .git/modules/lgtbot;非子模块独立 clone 则是目录)
        if not os.path.exists(os.path.join(sub_abs, '.git')):
            # 目录有内容但没 .git —— 可能是 git submodule deinit 后空架子
            try:
                entries = list(os.scandir(sub_abs))
            except OSError:
                entries = []
            info['status'] = 'empty' if not entries else 'empty'
        else:
            short, full = _local_submodule_commit(sub_path)
            info['local_commit'] = short
            info['local_commit_full'] = full

    if query_remote:
        # 远端 commit 可在任何 status 下查询(即便子模块未 init,UI 也能展示「上游
        # 最新是 abc1234,你需要初始化子模块」)
        short, full, err = _query_upstream_commit(owner, repo, branch)
        info['remote_commit'] = short
        info['remote_commit_full'] = full
        if err:
            info['error'] = err
        elif info['status'] == 'ok' and full and info['local_commit_full']:
            info['has_update'] = (full != info['local_commit_full'])
        elif info['status'] != 'ok':
            # 未 init / missing 都视为「有更新」—— UI 会把按钮文案切成「初始化子模块」
            info['has_update'] = True

    return info


# ─────────────────────────────────────────────────────────────────────────
# 语义版本号比较 —— 接受 'v1.5.0' / '1.5.0' / 'v1.5.0-beta.1' 等形式
# ─────────────────────────────────────────────────────────────────────────

def _semver_tuple(v: str) -> tuple:
    """规范化版本号为可比较元组。

    返回 ``(major, minor, patch, pre_tuple)``:
      · 无 pre-release 时 ``pre_tuple = ('~',)`` —— ``'~'`` 大于任何字母，
        让「正式版」永远大于带 pre-release 的版本(语义化版本规范)
      · 解析失败的段 → 0,无法成为 winner
    """
    if not v:
        return 0, 0, 0, ('zzz',)
    s = v.lstrip('vV').strip()
    main, _, pre = s.partition('-')
    parts = main.split('.')
    nums: list[int] = []
    for p in parts[:3]:
        try:
            nums.append(int(p))
        except Exception:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    pre_tuple: tuple = ()
    if pre:
        for p in pre.split('.'):
            try:
                pre_tuple += (int(p),)
            except Exception:
                pre_tuple += (p,)
    else:
        pre_tuple = ('~',)
    return nums[0], nums[1], nums[2], pre_tuple


def _pick_latest_semver(names: list) -> str:
    valid = [n for n in (names or []) if n]
    return max(valid, key=_semver_tuple) if valid else ''


def _semver_gt(a: str, b: str) -> bool:
    return _semver_tuple(a) > _semver_tuple(b)


# ─────────────────────────────────────────────────────────────────────────
# 缓存目录尺寸 —— os.walk 递归累加,避免 du 子进程开销
# ─────────────────────────────────────────────────────────────────────────

def _dir_size_and_count(path: str) -> dict:
    """返回 ``{'bytes', 'count', 'exists'}``;目录不存在时全为 0 + ``exists=False``。"""
    if not os.path.isdir(path):
        return {'bytes': 0, 'count': 0, 'exists': False}
    total_bytes = 0
    total_count = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for fname in files:
            try:
                total_bytes += os.path.getsize(os.path.join(root, fname))
                total_count += 1
            except OSError:
                pass
    return {'bytes': total_bytes, 'count': total_count, 'exists': True}


# cache key → 实际目录名映射。赛况缓存目录引擎用复数 ``matches``,且内部是
# ``<match_id>_<game>/`` 子目录嵌套 PNG;头像 / 图片是扁平结构。「保留 7 天」
# 的 mtime 比较仍按直接子项(matches/ 下就是子目录,avatar / gen 下就是文件)
# —— _clear_dir_keep_recent 对两种结构都成立。
_CACHE_DIRNAMES = {
    'avatar': 'avatar',
    'gen':    'gen',
    'match':  'matches',
}


def _cache_sizes() -> dict:
    return {
        key: _dir_size_and_count(_cache_dir(key))
        for key in _CACHE_DIRNAMES
    }


# ─────────────────────────────────────────────────────────────────────────
# 数据入口 —— 每次页面渲染调用一次
# (注:插件配置 / 引擎配置 / 更新公告 / 疑难解答 的编辑器全部搬迁到「⚙️ 配置
# 管理」tab,见 mod/webui/page_config.py,本文件不再 read lgtbot.json / yaml)
# ─────────────────────────────────────────────────────────────────────────

def _group_remarks() -> dict:
    """读主框架 ``data/group_remarks.json``(由框架 Web 面板维护的群备注名)。

    格式 ``{gid: {"name": .., "qq": ..}}``,兼容旧版纯字符串值。文件缺失 / 解析
    失败返回空 dict(降级到 openid 展示)。项目根 = 插件目录的上上级。
    """
    path = os.path.join(os.path.dirname(os.path.dirname(boot.PLUGIN_DIR)),
                        'data', 'group_remarks.json')
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.debug(f'读取 group_remarks.json 失败: {e}')
        return {}


def _remark_name(val) -> str:
    """从 group_remarks 的单条值取备注名(兼容 dict / 旧版纯字符串)。"""
    if isinstance(val, dict):
        return val.get('name', '') or ''
    return str(val) if val else ''


def _active_matches_view() -> list:
    """把 ``state.active_matches`` 整理成仪表盘可渲染的进行中对局列表(最近开始的在前)。

    展示名:私聊局用 userdb 缓存的昵称;群局优先用主框架的群备注名。两者查不到时前端回退显示 openid。
    ``game`` 为空(如单机局开局广播无 brief且此前无 new_game)时交给前端显示「未知游戏」。
    ``since`` 为开局时刻的 epoch 秒,时长由前端计算。
    """
    matches = list(state.active_matches.values())
    # 仅在存在群局时才读备注文件,省掉纯私聊场景的磁盘 IO
    remarks = _group_remarks() if any(not m.get('is_uid') for m in matches) else {}
    out = []
    for rec in matches:
        is_uid = bool(rec.get('is_uid'))
        tid = str(rec.get('target_id', ''))
        name = (userdb.get_name(tid) or '') if is_uid else _remark_name(remarks.get(tid))
        out.append({
            'is_uid': is_uid,
            'id': tid,
            'name': name,
            'game': rec.get('game', '') or '',
            'since': rec.get('since', 0) or 0,
        })
    out.sort(key=lambda m: m['since'], reverse=True)
    return out


def get_data() -> str:
    """返回可嵌入 ``<script id="dashboard-data">`` 的 JSON 字符串。

    ``submodule`` 字段只包含本地状态(``status`` / ``local_commit``);上游
    commit 留给「检查更新」按钮去查，避免每次页面渲染都打 GitHub API。
    """
    meta = _get_plugin_meta()
    payload = {
        'version': meta.get('version', ''),
        'github_url': meta.get('github', ''),
        'engine_running': bool(boot.is_engine_running()),
        'matches': _active_matches_view(),
        'bots': helpers.list_framework_bots(),
        'bound_appid': helpers.get_bound_appid(),
        'bind_configured': state.bind_bot_appid or '',
        'update_hint': _get_update_hint(),
        'bridge': _bridge_repo_link(),
        'submodule': _get_submodule_info(query_remote=False),
        'cache': _cache_sizes(),
        'self_check': self_check(),
    }
    data_json = json.dumps(payload, ensure_ascii=False, default=str)
    return data_json.replace('</script>', '<\\/script>')


# ─────────────────────────────────────────────────────────────────────────
# Action 端点 —— 由 webui/main.py 用 _LazyHtmlDict 注册为隐藏 _registry key,
# JS 侧用 fetch(apiUrl(KEY)) 触发,统一解析 <pre id="result">JSON</pre>
# ─────────────────────────────────────────────────────────────────────────

def _fragment(payload: dict) -> str:
    """把 ``payload`` 包成 ``<pre id="result">…</pre>``。

    JS 统一 DOMParser → ``#result.textContent`` → ``JSON.parse``。
    ``html.escape`` 防 payload 含 ``< > &`` 时破坏外层 HTML 结构。
    """
    body = json.dumps(payload, ensure_ascii=False, default=str)
    return f'<pre id="result">{_html.escape(body)}</pre>'


def render_matches() -> str:
    """只读轻量端点:只返回进行中对局列表,供前端每几秒实时轮询。

    刻意**不**复用 get_data() —— 那会顺带跑缓存目录 os.walk / 机器人列表等较重逻辑,
    不适合高频轮询;这里只读内存里的 state.active_matches(+ 群备注 / 昵称查表),开销极小。"""
    return _fragment({'matches': _active_matches_view()})


def _bridge_repo_link() -> dict:
    """桥接层(本插件)自身的 GitHub 仓库跳转链接字段。

    与 lgtbot 子模块的 ``upstream_*`` 同款,只是语义是"本仓库"而非"上游",
    故用 ``repo_*`` 命名。owner/repo 解析失败时字段为空,前端不渲染链接。
    """
    url = _get_plugin_meta().get('github', '') or ''
    owner, repo = _parse_github_owner_repo(url)
    return {'repo_url': url, 'repo_owner': owner, 'repo_name': repo}


def _bridge_check_payload() -> dict:
    """对 GitHub tags 做一次查询，返回桥接层(本插件)的版本对比 dict。"""
    meta = _get_plugin_meta()
    local_ver = meta.get('version', '') or ''
    owner, repo = _parse_github_owner_repo(meta.get('github', '') or '')
    # 仓库跳转链接字段并入每个返回分支,前端「版本与更新」区域各状态都能显示
    link = _bridge_repo_link()
    if not owner or not repo:
        return {
            'success': False,
            'local_version': local_ver,
            'remote_version': '',
            'has_update': False,
            'error': '无法从 __plugin_meta__ 解析 GitHub 仓库地址',
            **link,
        }
    api_url = f'https://api.github.com/repos/{owner}/{repo}/tags?per_page=30'
    try:
        req = urllib.request.Request(
            api_url,
            headers={
                'User-Agent': 'LGTBot-Dashboard',
                'Accept': 'application/vnd.github+json',
            },
        )
        with urllib.request.urlopen(req, timeout=8.0) as r:
            tags = json.loads(r.read().decode('utf-8') or '[]')
    except urllib.error.HTTPError as e:
        return {
            'success': False,
            'local_version': local_ver,
            'remote_version': '',
            'has_update': False,
            'error': f'GitHub HTTP {e.code}(可能触发匿名 60 次每小时限流)',
            **link,
        }
    except Exception as e:
        return {
            'success': False,
            'local_version': local_ver,
            'remote_version': '',
            'has_update': False,
            'error': f'网络错误：{e}',
            **link,
        }
    names = [t.get('name', '') for t in tags if isinstance(t, dict)]
    latest = _pick_latest_semver(names)
    has_update = bool(latest) and _semver_gt(latest, local_ver)
    return {
        'success': True,
        'local_version': local_ver,
        'remote_version': latest,
        'has_update': has_update,
        'tag_count': len(names),
        'error': '',
        **link,
    }


# ─────────────────────────────────────────────────────────────────────────
# 启动自检(仅桥接层)+ 最新 Release 信息
# ─────────────────────────────────────────────────────────────────────────

_STARTUP_CHECK_KEY = 'startup_update_check'
# 热重载风暴保护:近期(含手动检查)已查过就沿用缓存,避免匿名 GitHub API
# 60 次/小时限流被频繁 reload 烧光
_STARTUP_CHECK_MIN_INTERVAL = 600.0


def _get_update_hint() -> dict:
    """从持久缓存取启动自检结果,供 get_data 渲染新版本标记。"""
    cached = boot._get_persistent().get(_STARTUP_CHECK_KEY) or {}
    bridge = cached.get('bridge') or {}
    if bridge.get('success') and bridge.get('has_update'):
        return {'has_update': True, 'remote_version': bridge.get('remote_version', '')}
    return {'has_update': False, 'remote_version': ''}


def schedule_startup_update_check() -> None:
    """@on_load 调用:后台异步检查一次桥接层是否有新版本。

    只提示不动手 —— 不查上游子模块、不自动更新、不触发完整「检查更新」流程;
    结果落持久缓存,仪表盘「版本与更新」区据此渲染标记。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_startup_update_check())


async def _startup_update_check() -> None:
    await asyncio.sleep(3.0)          # 避开启动 / 热重载抖动
    p = boot._get_persistent()
    cached = p.get(_STARTUP_CHECK_KEY) or {}
    now = time.time()
    if now - float(cached.get('ts', 0) or 0) < _STARTUP_CHECK_MIN_INTERVAL:
        return
    loop = asyncio.get_running_loop()
    try:
        bridge = await loop.run_in_executor(None, _bridge_check_payload)
    except Exception as e:
        log.debug(f'启动自检桥接层更新失败: {e}')
        return
    p[_STARTUP_CHECK_KEY] = {'ts': now, 'bridge': bridge}
    if bridge.get('success') and bridge.get('has_update'):
        log.info(f'✨ 检测到桥接层新版本: v{bridge.get("local_version")} → '
                 f'{bridge.get("remote_version")} (插件仪表盘可一键更新)')


def _fetch_releases() -> dict:
    """GET ``/releases`` → ``{releases: [...], error: ''}``。

    ``releases`` 是**比本地版本新**的已发布版本(新→旧,各含 markdown 正文),
    让用户跨多个版本升级时能分别查看每个版本的说明(如 2.2.x → 2.4.0 会同时列出 2.3.0 与 2.4.0)。
    本项目约定只在大版本(2.3 / 2.4 …)发 release,补丁版(2.4.1)不发 —— 故按「版本号 > 本地」过滤 GitHub 上真实发布的 release,
    补丁版天然不产生新卡片。若没有比本地更新的(已最新 / 仅落后无 release 的补丁),退化为只含最新一个 release(与旧行为一致)。

    仓库无 release / 网络失败时 ``releases`` 为空,前端静默不渲染折叠卡。
    """
    owner, repo = _parse_github_owner_repo(_get_plugin_meta().get('github', ''))
    if not owner:
        return {'error': 'no-repo', 'releases': []}
    local_ver = _get_plugin_meta().get('version', '') or ''
    api_url = f'https://api.github.com/repos/{owner}/{repo}/releases?per_page=30'
    try:
        req = urllib.request.Request(api_url, headers={
            'User-Agent': 'LGTBot-Dashboard',
            'Accept': 'application/vnd.github+json',
        })
        with urllib.request.urlopen(req, timeout=8.0) as r:
            data = json.loads(r.read().decode('utf-8') or '[]')
    except Exception as e:
        return {'error': str(e), 'releases': []}
    if not isinstance(data, list):
        return {'error': 'bad-payload', 'releases': []}
    rels = []
    for rel in data:
        # 跳过 draft 与 prerelease —— 后者含 CI 滚动的 `prebuilt` 预发布包,
        # 它不是版本发布,不该出现在「新版本说明」卡里(其 tag 也非 semver)。
        if not isinstance(rel, dict) or rel.get('draft') or rel.get('prerelease'):
            continue
        rels.append({
            'tag_name': rel.get('tag_name', ''),
            'name': rel.get('name', ''),
            'body': rel.get('body', '') or '',
            'published_at': rel.get('published_at', ''),
            'html_url': rel.get('html_url', ''),
        })
    if not rels:
        return {'error': 'no-release', 'releases': []}
    # 不完全信任 API 顺序,按语义化版本降序(新→旧)自排
    rels.sort(key=lambda r: _semver_tuple(r.get('tag_name', '')), reverse=True)
    newer = [r for r in rels if _semver_gt(r.get('tag_name', ''), local_ver)]
    # 有比本地新的 → 全列(可能多个);否则(已最新)→ 只给最新一个
    return {'releases': newer if newer else rels[:1], 'error': ''}


def render_check_update() -> str:
    """同时检查桥接层(本插件)与 lgtbot 子模块(上游 commit)两边的更新。

    返回 ``{success, bridge, submodule, release}``:
      · ``bridge``    —— 本插件 __plugin_meta__.version vs GitHub tags
      · ``submodule`` —— 子模块本地 HEAD vs 上游 main/master HEAD,含 status
                        (ok / missing / empty),供 UI 决定按钮文案
      · ``release``   —— ``{releases: [...]}``:比本地新的 release 列表
                        (跨多个大版本升级时逐个可展开);已最新则退化为最新一个。
                        markdown 正文由前端渲染成折叠卡
    任一侧失败不影响另一侧，success 反映「两侧都没致命错误」(子模块网络失败
    会被 UI 单独展示);整体 success 仅当桥接层成功时为 True。
    """
    bridge = _bridge_check_payload()
    submodule = _get_submodule_info(query_remote=True)
    release = _fetch_releases()
    # 手动检查的结果同步进启动自检缓存 (更新完成后再点一次检查)
    boot._get_persistent()[_STARTUP_CHECK_KEY] = {'ts': time.time(), 'bridge': bridge}
    return _fragment({
        'success': bool(bridge.get('success')),
        'bridge': bridge,
        'submodule': submodule,
        'release': release,
    })


def _git_output_summary(proc, success: bool) -> str:
    """取 git 输出首行作审计详情:成功优先 stdout,失败优先 stderr。"""
    text = ((proc.stdout if success else proc.stderr) or
            (proc.stderr if success else proc.stdout) or '').strip()
    return text.splitlines()[0][:120] if text else f'returncode={proc.returncode}'


def render_do_update() -> str:
    """更新桥接层 —— 在 ``boot.PLUGIN_DIR`` 执行 ``git pull --ff-only``。

    同步阻塞 web worker 数秒(直到 git 完成);考虑到该操作极少触发，且
    ``--ff-only`` 模式下 git 不会进入交互，可接受。失败不会破坏工作区。
    """
    plugin_dir = boot.PLUGIN_DIR
    if not os.path.isdir(os.path.join(plugin_dir, '.git')):
        return _fragment({
            'success': False,
            'message': ('插件目录不是 git 仓库 (.git/ 不存在)。'
                        '若是从插件市场下载安装，请先点击「📥 初始化为 git 仓库」'
                        '把当前目录建成 git 工作树，再进行更新。'),
        })
    try:
        # 显式带 ``origin main`` 而非裸 ``git pull --ff-only``:
        # 裸 pull 会报「There is no tracking information for the current branch」。
        # 显式参数同时也保证手动 clone 的开发者无论当前在哪个分支都从上游 main 拉。
        proc = subprocess.run(
            ['git', '-C', plugin_dir, 'pull', '--ff-only', 'origin', 'main'],
            capture_output=True,
            text=True,
            timeout=60.0,
        )
    except subprocess.TimeoutExpired:
        audit.record('update', '更新桥接层 (git pull)', 'git pull 超时 (60s)', ok=False)
        return _fragment({'success': False, 'message': 'git pull 超时 (超过 60 秒)'})
    except FileNotFoundError:
        return _fragment({
            'success': False,
            'message': '未找到 git 命令，请确认系统已安装 git',
        })
    except Exception as e:
        audit.record('update', '更新桥接层 (git pull)', f'异常: {e}', ok=False)
        return _fragment({'success': False, 'message': f'git pull 异常：{e}'})

    success = proc.returncode == 0
    audit.record('update', '更新桥接层 (git pull)',
                 _git_output_summary(proc, success), ok=success)
    msg = ('✅ 桥接层已更新，请重启 LGTBot 引擎或重启整个进程以加载新版本'
           if success else '❌ git pull 失败，请尝试手动更新。详见 stderr')
    return _fragment({
        'success': success,
        'returncode': proc.returncode,
        'stdout': (proc.stdout or '').strip(),
        'stderr': (proc.stderr or '').strip(),
        'message': msg,
    })


def render_do_update_force() -> str:
    """强制更新桥接层 —— ``git fetch origin`` + ``git reset --hard origin/main``。

    用于普通 `git pull` 因工作区有本地修改报「would be overwritten by merge」
    时的兜底。**会丢弃所有已 tracked 文件的本地改动**(被 .gitignore 排除的
    ``data/`` / ``build/`` / ``lgtbot/`` 等运行时目录不受影响)。前端在普通
    更新失败后弹一个按钮调本端点，带 danger 级双 confirm 警示用户。
    """
    plugin_dir = boot.PLUGIN_DIR
    if not os.path.isdir(os.path.join(plugin_dir, '.git')):
        return _fragment({
            'success': False,
            'message': ('插件目录不是 git 仓库 (.git/ 不存在)。'
                        '请先点击「📥 初始化为 git 仓库」。'),
        })

    stages: list = []

    def run_stage(label: str, cmd: list, timeout: float = 60.0) -> bool:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            stages.append((label, -1, '', f'{label} 超时 ({timeout:.0f}s)'))
            return False
        except FileNotFoundError:
            stages.append((label, -1, '', '未找到 git 命令'))
            return False
        except Exception as e:
            stages.append((label, -1, '', f'{label} 异常: {e}'))
            return False
        stages.append((label, p.returncode,
                       (p.stdout or '').strip(), (p.stderr or '').strip()))
        return p.returncode == 0

    # 1. fetch 拉远端最新 ref(包括 origin/main),浅 fetch 防大体量
    if not run_stage('git fetch origin',
                     ['git', '-C', plugin_dir, 'fetch', 'origin', '--depth', '50'],
                     timeout=60.0):
        return _fragment({
            'success': False,
            'stages': stages,
            'message': '❌ git fetch 失败，网络或仓库配置问题，详见 stages',
        })

    # 2. reset --hard 覆盖工作区到 origin/main(丢弃所有本地修改)
    if not run_stage('git reset --hard origin/main',
                     ['git', '-C', plugin_dir, 'reset', '--hard', 'origin/main'],
                     timeout=30.0):
        audit.record('update', '强制更新桥接层 (reset --hard)',
                     'git reset --hard 失败', ok=False)
        return _fragment({
            'success': False,
            'stages': stages,
            'message': '❌ git reset --hard 失败，详见 stages',
        })

    log.warning(f'[do-update-force] 强制更新完成 plugin_dir={plugin_dir} '
                f'(本地未提交修改已丢弃)')
    audit.record('update', '强制更新桥接层 (reset --hard)',
                 '已对齐 origin/main,本地未提交修改已丢弃')
    return _fragment({
        'success': True,
        'stages': stages,
        'message': ('✅ 桥接层已强制更新到 origin/main。'
                    '本地未提交的修改已丢弃。请重启 LGTBot 引擎或整进程以加载新版本。'),
    })


def render_update_submodule() -> str:
    """更新或初始化 lgtbot 子模块 ——
    ``git -C <plugin_dir> submodule update --init --recursive --force <path>``。

    同一条命令兼任「初始化」(子模块文件夹不存在 / 空)和「更新」(把本地子模块
    HEAD 强制对齐到父仓库 gitlink 记录的 commit)。``--force`` 抹掉子模块内
    任何本地修改，这正是用户需要的「彻底回到上游版本」语义。

    注意:本命令把子模块对齐到**父仓库 gitlink**,不会拉「上游 main 最新
    commit」。要真正吃到上游最新代码，需要桥接层先 git pull(父仓库 gitlink
    更新),再跑本命令。该工作流由 UI 引导。
    """
    plugin_dir = boot.PLUGIN_DIR
    sub_cfg = _parse_gitmodules()
    sub_path = sub_cfg['path']

    if not os.path.isdir(os.path.join(plugin_dir, '.git')):
        return _fragment({
            'success': False,
            'message': ('插件目录不是 git 仓库 (.git/ 不存在)。'
                        '若是从插件市场下载安装，请先点击「📥 初始化为 git 仓库」'
                        '把当前目录建成 git 工作树，再初始化子模块。'),
        })

    # ``_GITHUB_SSH_TO_HTTPS_ARGS`` 必须在子命令(``submodule``)之前传给 git ——
    # ``git -c <k>=<v> <subcmd>`` 是 git 的标准用法,会递归到嵌套子模块。lgtbot
    # 上游 .gitmodules 全是 SSH,没这个改写市场用户绝对拉不到。
    cmd = ['git', '-C', plugin_dir, *_GITHUB_SSH_TO_HTTPS_ARGS,
           'submodule', 'update',
           '--init', '--recursive', '--force', sub_path]
    try:
        # git submodule update 第一次 init 时要克隆整个 lgtbot 仓库,大概 30~60s
        # (含 50+ 游戏插件子目录),网络慢可能更久 —— timeout 给 300s
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300.0,
        )
    except subprocess.TimeoutExpired:
        # 超时时克隆可能已进行到一半 —— 真变更,记 ok=False
        audit.record('update', '更新 lgtbot 子模块',
                     '超时 (300s),可能留下半克隆', ok=False)
        return _fragment({
            'success': False,
            'command': ' '.join(cmd),
            'message': 'git submodule update 超时 (超过 5 分钟)，可能是网络问题',
        })
    except FileNotFoundError:
        return _fragment({
            'success': False,
            'command': ' '.join(cmd),
            'message': '未找到 git 命令，请确认系统已安装 git',
        })
    except Exception as e:
        return _fragment({
            'success': False,
            'command': ' '.join(cmd),
            'message': f'git submodule update 异常：{e}',
        })

    success = proc.returncode == 0
    audit.record('update', '更新 lgtbot 子模块',
                 _git_output_summary(proc, success), ok=success)
    msg = ('✅ 子模块已更新，如桥接层 C++ 部分有变化需要重新 bash build.sh'
           if success else '❌ git submodule update 失败，详见 stderr')
    return _fragment({
        'success': success,
        'command': ' '.join(cmd),
        'returncode': proc.returncode,
        'stdout': (proc.stdout or '').strip(),
        'stderr': (proc.stderr or '').strip(),
        'message': msg,
    })


# ─────────────────────────────────────────────────────────────────────────
# 把当前插件目录初始化为 git 工作仓库 —— 给「从插件市场下载」的用户用
#
# 主框架插件市场(``web/tools/_market/install.py``)在解压 zip 时显式过滤掉
# ``/.git/`` 子目录,所以市场用户拿到的目录没有任何 git 元信息,既不能
# ``git pull`` 更新桥接层,也跑不了 ``git submodule update --init`` 把 lgtbot
# 拉下来。本函数原地建仓:``git init`` + ``remote add`` + 浅 fetch + ``reset
# --mixed v<version>``,**保留工作区所有文件不动**(用户的 data/、build/、
# lgtbot/ 都不会被擦)。完成后 git status 会显示大量 M(zip 解压版与 tag
# 内容可能字节级有差异),这是预期 —— 后续 ``git pull --ff-only`` 仍能 ff
# 那些 unmodified 的文件,modified 文件保留。
# ─────────────────────────────────────────────────────────────────────────

def _git_run(cmd: list, timeout: float = 60.0):
    """跑一条 git 命令，返回 ``CompletedProcess``。捕获常见异常包成同形态对象，
    让 ``render_init_repo`` 单一返回路径处理 ok / 各类错误。
    """
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def render_init_repo() -> str:
    """把 ``boot.PLUGIN_DIR`` 原地变成与上游同步的 git 仓库。

    步骤:
      1. ``git --version`` 探测 —— 没装 git 直接报友好错误
      2. 若 ``.git/`` 已存在 —— 报「已初始化」,防止重复点击
      3. ``git init -b main`` (``-b main`` 避免老版本 git 默认 master,
         省一次 rename)
      4. ``git remote add origin <__plugin_meta__['github']>``
         —— 从插件元数据读 URL,不硬编码
      5. ``git fetch origin --tags --depth 50``
         —— 浅 fetch 拿 tag 和最近 50 个 commit,足够后续 ``git pull``
      6. ``git reset --mixed v<version>`` —— 把 HEAD 和 index 指向当前版本对应
         的 tag;若 tag 不存在(dev 版),fallback 到 ``origin/main``。``--mixed``
         **只动 index 不动工作区**,所以用户的 data / build / lgtbot 全保留。
    """
    plugin_dir = boot.PLUGIN_DIR

    # 1. git 客户端探测(git_missing 让前端弹「改用下载更新 / 插件市场」引导)
    try:
        proc = _git_run(['git', '--version'], timeout=5.0)
    except FileNotFoundError:
        return _fragment({
            'success': False,
            'git_missing': True,
            'message': '未找到 git 命令，请先安装 git 客户端再使用本功能',
        })
    except Exception as e:
        return _fragment({'success': False, 'message': f'探测 git 异常：{e}'})
    if proc.returncode != 0:
        return _fragment({
            'success': False,
            'message': f'git --version 失败 (rc={proc.returncode}): '
                       f'{(proc.stderr or proc.stdout or "").strip()}',
        })

    # 2. 已是仓库 → 拒绝重复操作
    if os.path.isdir(os.path.join(plugin_dir, '.git')):
        return _fragment({
            'success': False,
            'message': '插件目录已经是 git 仓库 (.git/ 已存在)，无需重复初始化。'
                       '如需更新请用「⬇ 更新桥接层」按钮。',
        })

    # 3. 读元数据拿 github URL + 当前版本
    meta = _get_plugin_meta()
    github_url = (meta.get('github') or '').strip()
    version = (meta.get('version') or '').strip()
    if not github_url:
        return _fragment({
            'success': False,
            'message': '__plugin_meta__ 中未配置 github 字段，无法确定 remote',
        })
    # GitHub URL 不带 .git 后缀也能 clone,但加上更标准
    remote_url = github_url if github_url.endswith('.git') else github_url + '.git'
    target_tag = f'v{version}' if version else ''

    # 4. git init -b main → remote add → fetch → reset
    stages: list = []   # [(label, returncode, stdout, stderr)] 失败时全部回传给前端

    def run_stage(label: str, cmd: list, timeout: float = 60.0) -> bool:
        try:
            p = _git_run(cmd, timeout=timeout)
        except subprocess.TimeoutExpired:
            stages.append((label, -1, '', f'{label} 超时 ({timeout:.0f}s)'))
            return False
        except Exception as e:
            stages.append((label, -1, '', f'{label} 异常: {e}'))
            return False
        stages.append((label, p.returncode,
                       (p.stdout or '').strip(), (p.stderr or '').strip()))
        return p.returncode == 0

    if not run_stage('git init',
                     ['git', '-C', plugin_dir, 'init', '-b', 'main'], timeout=10.0):
        audit.record('update', '初始化 git 仓库', '失败于 git init', ok=False)
        return _fragment({
            'success': False,
            'stages': stages,
            'message': '❌ git init 失败，详见 stages 输出',
        })
    if not run_stage('git remote add',
                     ['git', '-C', plugin_dir, 'remote', 'add', 'origin', remote_url],
                     timeout=10.0):
        audit.record('update', '初始化 git 仓库', '失败于 git remote add', ok=False)
        return _fragment({
            'success': False,
            'stages': stages,
            'message': '❌ git remote add 失败，详见 stages 输出',
        })
    if not run_stage('git fetch --tags --depth 50',
                     ['git', '-C', plugin_dir, 'fetch', 'origin',
                      '--tags', '--depth', '50'], timeout=120.0):
        audit.record('update', '初始化 git 仓库', '失败于 git fetch', ok=False)
        return _fragment({
            'success': False,
            'stages': stages,
            'message': '❌ git fetch 失败 (网络问题 / 仓库不可达?)，详见 stages',
        })

    # 5. reset --mixed 到当前版本 tag;失败 fallback 到 origin/main
    fallback_to_main = False
    version_tag_used = ''
    if target_tag and run_stage(f'git reset --mixed {target_tag}',
                                ['git', '-C', plugin_dir, 'reset', '--mixed',
                                 target_tag], timeout=30.0):
        version_tag_used = target_tag
    else:
        # tag 不存在 / reset 失败 → 退到 origin/main
        if target_tag:
            stages.append((f'tag {target_tag} 不可用',
                           -1, '', 'fallback 到 origin/main'))
        if not run_stage('git reset --mixed origin/main',
                         ['git', '-C', plugin_dir, 'reset', '--mixed',
                          'origin/main'], timeout=30.0):
            audit.record('update', '初始化 git 仓库', '失败于 git reset', ok=False)
            return _fragment({
                'success': False,
                'stages': stages,
                'message': '❌ git reset 失败，详见 stages 输出',
            })
        fallback_to_main = True

    # 6. 把 main 分支的 upstream 设为 origin/main
    # 此步失败不算致命(``render_do_update`` 已显式带 ``origin main`` 兜底),
    # 只记一条 stage 让用户看到。
    run_stage('git branch -u origin/main',
              ['git', '-C', plugin_dir, 'branch',
               '--set-upstream-to=origin/main', 'main'], timeout=10.0)

    log.info(f'[init-repo] 完成 plugin_dir={plugin_dir} '
             f'tag={version_tag_used or "(fallback origin/main)"}')
    audit.record('update', '初始化 git 仓库',
                 f'HEAD={version_tag_used or "origin/main"}')
    return _fragment({
        'success': True,
        'stages': stages,
        'version_tag_used': version_tag_used,
        'fallback_to_main': fallback_to_main,
        'remote_url': remote_url,
        'message': (f'✅ 已把插件目录初始化为 git 仓库 '
                    f'(HEAD = {version_tag_used or "origin/main"})。'
                    f'工作区文件全部保留，可继续使用「⬇ 更新桥接层」'
                    f'和「⬇ 初始化子模块」按钮。'),
    })


# ─────────────────────────────────────────────────────────────────────────
# 免 git 下载更新 —— no_git(插件市场安装)场景直接下 GitHub 源码 zip 覆盖
# ─────────────────────────────────────────────────────────────────────────

# 覆盖更新绝不触碰的顶层目录:运行时数据 / 编译产物 / 子模块
# (archive zip 不含 子模块内容,覆盖会清掉本地 lgtbot)/ git 元数据 / 崩溃转储
_UPDATE_PROTECTED = ('data', 'build', 'build_prebuilt', 'lgtbot',
                     '.git', 'LGTBot_CRASH_DUMPS')


def _apply_source_zip(content: bytes) -> dict:
    """把 GitHub 源码 archive zip 覆盖解压到插件目录,返回 ``{'files': n}``。

    参考主框架插件市场 ``_extract_zip_subset`` 的处理:自动剥离 archive 的
    根目录前缀(``repo-2.5.0/``)、跳过 ``__pycache__``、防路径穿越
    (realpath 必须落在插件目录内)。只**覆盖 / 新增**,不删除任何本地文件;
    ``_UPDATE_PROTECTED`` 顶层目录整棵跳过。"""
    plugin_dir = os.path.realpath(boot.PLUGIN_DIR)
    files = 0
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        flist = zf.namelist()
        roots = {f.split('/')[0] for f in flist if '/' in f}
        prefix = (roots.pop() + '/') if len(roots) == 1 else ''
        for fp in flist:
            if fp.endswith('/'):
                continue
            rel = fp[len(prefix):] if fp.startswith(prefix) else fp
            if not rel or '__pycache__' in rel:
                continue
            if rel.split('/')[0] in _UPDATE_PROTECTED:
                continue
            dest = os.path.realpath(os.path.join(plugin_dir, rel))
            if not dest.startswith(plugin_dir + os.sep):
                log.warning(f'[download-update] 跳过越界成员(疑似路径穿越): {fp!r}')
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(fp) as src, open(dest, 'wb') as dst:
                shutil.copyfileobj(src, dst)
            files += 1
    if files == 0:
        raise RuntimeError('zip 内没有可解压的文件')
    return {'files': files}


def render_download_update() -> str:
    """免 git 覆盖更新:下载最新 release 的源码 zip 直接覆盖插件目录。

    服务给 no_git(插件市场安装、无 git 环境)场景 —— 与「⬇ 更新桥接层」 (git pull)互为替代。
    服务端**重新查一次**最新版本(不信前端传参),已是最新则拒绝。zip 走 prebuilt 的镜像顺序逐个尝试
    (源码包仅几 MB,步下载可接受,前端按钮转「下载中…」)。"""
    check = _bridge_check_payload()
    if not check.get('success'):
        return _fragment({'success': False,
                          'message': f'检查更新失败：{check.get("error", "未知错误")}'})
    if not check.get('has_update'):
        return _fragment({'success': False, 'message': '已是最新版本，无需下载更新'})
    remote = (check.get('remote_version') or '').strip()
    tag = remote if remote.startswith(('v', 'V')) else f'v{remote}'
    owner, repo = _parse_github_owner_repo(_get_plugin_meta().get('github', '') or '')
    url = f'https://github.com/{owner}/{repo}/archive/refs/tags/{tag}.zip'

    content, last_err = None, None
    for mirror in prebuilt._download_mirror_order(None):
        try:
            req = urllib.request.Request(prebuilt._build_mirror_url(mirror, url),
                                         headers={'User-Agent': 'LGTBot-Dashboard'})
            with urllib.request.urlopen(req, timeout=60.0) as r:
                content = r.read()
            break
        except Exception as e:
            last_err = e
    if content is None:
        audit.record('update', '下载更新 (源码 zip)', f'{tag} 下载失败: {last_err}', ok=False)
        return _fragment({'success': False, 'message': f'下载失败(所有镜像均不可用): {last_err}'})

    try:
        result = _apply_source_zip(content)
    except Exception as e:
        audit.record('update', '下载更新 (源码 zip)', f'{tag} 解压覆盖失败: {e}', ok=False)
        return _fragment({'success': False, 'message': f'解压覆盖失败: {e}'})

    log.info(f'[download-update] 已覆盖更新到 {tag}({result["files"]} 个文件)')
    audit.record('update', '下载更新 (源码 zip)', f'{tag}，覆盖 {result["files"]} 个文件')
    return _fragment({
        'success': True,
        'version': remote,
        'files': result['files'],
        'message': (f'✅ 已下载 {tag} 并覆盖更新（{result["files"]} 个文件；'
                    f'data / build / lgtbot 等均未触碰，未删除任何本地文件）。'
                    f'重启 LGTBot 或整个进程后生效。'),
    })


# ─────────────────────────────────────────────────────────────────────────
# 缓存清理
# ─────────────────────────────────────────────────────────────────────────

def _clear_dir(path: str) -> tuple[bool, str, int]:
    """递归删除 ``path`` 下全部直接子项，保留 ``path`` 本身;返回 ``(ok, msg, n)``.

    Audit:本函数被调用前必然走过 UI 的两次 confirm(见 dashboard.js 的
    ``DASH_CLEAR_PROMPTS``)。这里 ``log.info`` 一条 audit 日志便于事后排查
    「是谁、什么时候清了哪个目录」。
    """
    if not os.path.isdir(path):
        log.info(f'[cache-clear] 跳过(目录不存在):{path}')
        return True, '目录不存在，无需清理', 0
    log.info(f'[cache-clear] 开始清理:{path}')
    removed = 0
    errs: list = []
    try:
        for entry in os.scandir(path):
            try:
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path, ignore_errors=False)
                else:
                    os.remove(entry.path)
                removed += 1
            except Exception as e:
                errs.append(f'{entry.name}: {e}')
    except Exception as e:
        log.warning(f'[cache-clear] 扫描失败: {path}:{e}')
        return False, f'扫描目录失败：{e}', removed
    if errs:
        log.warning(f'[cache-clear] 完成但有 {len(errs)} 项失败: {path} (删除 {removed} 项)')
        return False, ';'.join(errs[:5]), removed
    log.info(f'[cache-clear] 完成: {path} (删除 {removed} 项)')
    return True, '清理完成', removed


def _clear_dir_keep_recent(path: str, days: int = 7) -> tuple[bool, str, int]:
    """删除 ``path`` 下 mtime 早于 ``days`` 天的直接子项(对「每对局一目录」结构有意义)。

    Audit:同 ``_clear_dir``,执行前后 INFO 日志。
    """
    if not os.path.isdir(path):
        log.info(f'[cache-clear] 跳过 (目录不存在):{path}')
        return True, '目录不存在，无需清理', 0
    log.info(f'[cache-clear] 开始保留近 {days} 天: {path}')
    cutoff = time.time() - days * 86400
    removed = 0
    errs: list = []
    try:
        for entry in os.scandir(path):
            try:
                st = entry.stat(follow_symlinks=False)
                if st.st_mtime >= cutoff:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path, ignore_errors=False)
                else:
                    os.remove(entry.path)
                removed += 1
            except Exception as e:
                errs.append(f'{entry.name}:{e}')
    except Exception as e:
        log.warning(f'[cache-clear] 扫描失败: {path}:{e}')
        return False, f'扫描目录失败：{e}', removed
    if errs:
        log.warning(f'[cache-clear] 完成但有 {len(errs)} 项失败: {path} (保留近 {days} 天，删除 {removed} 项)')
        return False, ';'.join(errs[:5]), removed
    log.info(f'[cache-clear] 完成保留近 {days} 天: {path} (删除 {removed} 项)')
    return True, f'已保留近 {days} 天数据', removed


def _cache_dir(name: str) -> str:
    """按 cache key 取实际目录路径(键 'match' → 实际目录 'matches' / 复数)。"""
    return os.path.join(boot.IMG_PATH, _CACHE_DIRNAMES.get(name, name))


def _audit_clear(action: str, ok: bool, msg: str, n: int) -> None:
    """清缓存审计:目录不存在(n=0)也照记 —— 用户确实按下了清理按钮。"""
    audit.record('cache', action, f'删除 {n} 项' + ('' if ok else f'; {msg}'), ok=ok)


def render_clear_avatar() -> str:
    ok, msg, n = _clear_dir(_cache_dir('avatar'))
    _audit_clear('清理头像缓存 (全部)', ok, msg, n)
    return _fragment({'success': ok, 'message': msg, 'removed': n})


def render_clear_avatar_7d() -> str:
    ok, msg, n = _clear_dir_keep_recent(_cache_dir('avatar'), days=7)
    _audit_clear('清理头像缓存 (保留7天)', ok, msg, n)
    return _fragment({'success': ok, 'message': msg, 'removed': n})


def render_clear_gen() -> str:
    ok, msg, n = _clear_dir(_cache_dir('gen'))
    _audit_clear('清理图片缓存 (全部)', ok, msg, n)
    return _fragment({'success': ok, 'message': msg, 'removed': n})


def render_clear_gen_7d() -> str:
    ok, msg, n = _clear_dir_keep_recent(_cache_dir('gen'), days=7)
    _audit_clear('清理图片缓存 (保留7天)', ok, msg, n)
    return _fragment({'success': ok, 'message': msg, 'removed': n})


def render_clear_match_all() -> str:
    ok, msg, n = _clear_dir(_cache_dir('match'))
    _audit_clear('清理赛况缓存 (全部)', ok, msg, n)
    return _fragment({'success': ok, 'message': msg, 'removed': n})


def render_clear_match_7d() -> str:
    ok, msg, n = _clear_dir_keep_recent(_cache_dir('match'), days=7)
    _audit_clear('清理赛况缓存 (保留7天)', ok, msg, n)
    return _fragment({'success': ok, 'message': msg, 'removed': n})


# ─────────────────────────────────────────────────────────────────────────
# 机器人绑定 —— register_route 真路由(要接 ?appid= 参数,不走 fragment 协议)
# ─────────────────────────────────────────────────────────────────────────

async def bind_bot_handler(request: 'web.Request') -> 'web.Response':
    """``GET /api/ext/lgtbot/bind-bot?appid=<appid>`` —— 面板换绑机器人。

    校验 appid 必须在框架 bot.yaml 配置列表内,然后由
    ``config.persist_bind_bot_appid`` 写回 config.yaml(行级替换保注释)并
    即时应用:更新 ``state.bind_bot_appid`` + 从新绑定 bot 的 data.db 重载
    全量群集合。绑定切换后,其他 bot 的事件立刻被 dispatcher 静默忽略。
    """
    appid = (request.query.get('appid') or '').strip()
    valid = {b['appid'] for b in helpers.list_framework_bots()}
    if not appid or appid not in valid:
        return web.json_response(
            {'success': False, 'message': f'appid 无效或不在框架 bot 列表中: {appid!r}'},
            status=400,
        )
    ok, msg = _config.persist_bind_bot_appid(appid)
    audit.record('bind', '换绑机器人',
                 f'→ {appid}' + ('' if ok else f'; {msg}'), ok=ok)
    return web.json_response({
        'success': ok,
        'message': msg,
        'bound_appid': helpers.get_bound_appid(),
    })
