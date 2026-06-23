#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
「仪表盘」标签 —— 集中展示版本/统计/引擎配置/缓存，提供一键更新与缓存清理。

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
  · ``get_data()`` 返回所有面板数据(版本、统计、缓存尺寸、引擎配置内容、
    config 文件绝对路径 —— 保存走主框架 ``/api/config-file/save`` 端点)
  · ``render_check_update`` / ``render_do_update`` / ``render_clear_*``
    供 ``webui/main.py`` 注册为隐藏 action 端点(参考 RESTART_KEY 套路),
    每个端点返回 ``<pre id="result">JSON</pre>`` 单片段供 JS 解析

读取 ``lgtbot.db`` 时严格只读(``sqlite3 URI mode=ro``):不开启 WAL,不允
许任何写路径;插件级 ``user_cache.db`` 走已有的 ``userdb.count_users()``
接口避免重复打开连接。
"""

from __future__ import annotations

import html as _html
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

from core.base.logger import get_logger, PLUGIN
from .. import boot, userdb

log = get_logger(PLUGIN, 'LGTBot')

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')


def _load(name: str) -> str:
    with open(os.path.join(_TEMPLATE_DIR, name), 'r', encoding='utf-8') as f:
        return f.read()


TAB_HTML = _load('dashboard/dashboard.html')
TAB_CSS = _load('dashboard/dashboard.css')
TAB_JS = _load('dashboard/dashboard.js')


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
        return ('', '')
    try:
        s = url.rstrip('/')
        if s.endswith('.git'):
            s = s[:-4]
        parts = s.split('/')
        # 形如 ['https:', '', 'github.com', 'owner', 'repo']
        if len(parts) >= 5 and 'github.com' in parts[2]:
            return (parts[3], parts[4])
    except Exception:
        pass
    return ('', '')


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
            return ('', '')
        full = (proc.stdout or '').strip()
        return (full[:7], full) if full else ('', '')
    except Exception as e:
        log.debug(f'rev-parse {sub_path} HEAD 失败：{e}')
        return ('', '')


def _query_upstream_commit(owner: str, repo: str, branch: str) -> tuple[str, str, str]:
    """GET GitHub ``/repos/{owner}/{repo}/commits/{branch}`` 取最新 commit。

    返回 ``(short_sha, full_sha, error_message)``;失败时前两项空，error 含原因。
    """
    if not owner or not repo:
        return ('', '', '上游仓库地址未配置')
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
        return ('', '', f'GitHub HTTP {e.code} (可能触发匿名 60 次每小时限流)')
    except Exception as e:
        return ('', '', f'网络错误：{e}')
    full = data.get('sha') or ''
    if not full:
        return ('', '', '响应缺少 sha 字段')
    return (full[:7], full, '')


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
        return (0, 0, 0, ('zzz',))
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
    return (nums[0], nums[1], nums[2], pre_tuple)


def _pick_latest_semver(names: list) -> str:
    valid = [n for n in (names or []) if n]
    return max(valid, key=_semver_tuple) if valid else ''


def _semver_gt(a: str, b: str) -> bool:
    return _semver_tuple(a) > _semver_tuple(b)


# ─────────────────────────────────────────────────────────────────────────
# 引擎数据库统计 —— 严格只读
# ─────────────────────────────────────────────────────────────────────────

# 4 个 COUNT 查询。SELECT COUNT 不写库,只读 URI 连接也不允许写;
# SQLite 在 read-only 模式下不会创建 -shm/-wal 旁路文件
_LGTBOT_STAT_SQL = {
    'lgtbot_users':              'SELECT COUNT(*) FROM user',
    'lgtbot_matches':            'SELECT COUNT(*) FROM match',
    'lgtbot_match_attendances':  'SELECT COUNT(*) FROM user_with_match',
    'lgtbot_achievements':       'SELECT COUNT(*) FROM user_with_achievement',
}


def _query_lgtbot_stats() -> tuple[dict, list]:
    """返回 ``(stats_dict, errors_list)``;表缺失或查询异常时，对应 stat 为 ``None``。"""
    stats: dict = {k: None for k in _LGTBOT_STAT_SQL}
    errors: list = []
    if not os.path.isfile(boot.DB_PATH):
        errors.append(f'lgtbot.db 不存在：{boot.DB_PATH}')
        errors.append('数据库将在引擎启动时自动创建。**请注意：卸载本插件时数据库将被删除，请手动做好数据备份！**')
        return stats, errors
    uri = f'file:{boot.DB_PATH}?mode=ro'
    conn = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        for key, sql in _LGTBOT_STAT_SQL.items():
            try:
                cur = conn.execute(sql)
                row = cur.fetchone()
                stats[key] = int(row[0]) if row else 0
            except sqlite3.OperationalError as e:
                errors.append(f'{key}:{e}')
                stats[key] = None
    except Exception as e:
        errors.append(f'打开 lgtbot.db 失败：{e}')
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return stats, errors


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

def get_data() -> str:
    """返回可嵌入 ``<script id="dashboard-data">`` 的 JSON 字符串。

    ``submodule`` 字段只包含本地状态(``status`` / ``local_commit``);上游
    commit 留给「检查更新」按钮去查，避免每次页面渲染都打 GitHub API。
    """
    meta = _get_plugin_meta()
    stats, stat_errs = _query_lgtbot_stats()
    payload = {
        'query_time': int(time.time()),
        'version': meta.get('version', ''),
        'github_url': meta.get('github', ''),
        'engine_running': bool(boot.is_engine_running()),
        'submodule': _get_submodule_info(query_remote=False),
        'stats': {
            'user_cache_total': userdb.count_users(),
            **stats,
            'errors': stat_errs,
        },
        'cache': _cache_sizes(),
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


def _bridge_check_payload() -> dict:
    """对 GitHub tags 做一次查询，返回桥接层(本插件)的版本对比 dict。"""
    meta = _get_plugin_meta()
    local_ver = meta.get('version', '') or ''
    owner, repo = _parse_github_owner_repo(meta.get('github', '') or '')
    if not owner or not repo:
        return {
            'success': False,
            'local_version': local_ver,
            'remote_version': '',
            'has_update': False,
            'error': '无法从 __plugin_meta__ 解析 GitHub 仓库地址',
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
        }
    except Exception as e:
        return {
            'success': False,
            'local_version': local_ver,
            'remote_version': '',
            'has_update': False,
            'error': f'网络错误：{e}',
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
    }


def render_check_update() -> str:
    """同时检查桥接层(本插件)与 lgtbot 子模块(上游 commit)两边的更新。

    返回 ``{success, bridge, submodule}``:
      · ``bridge``    —— 本插件 __plugin_meta__.version vs GitHub tags
      · ``submodule`` —— 子模块本地 HEAD vs 上游 main/master HEAD,含 status
                        (ok / missing / empty),供 UI 决定按钮文案
    任一侧失败不影响另一侧，success 反映「两侧都没致命错误」(子模块网络失败
    会被 UI 单独展示);整体 success 仅当桥接层成功时为 True。
    """
    bridge = _bridge_check_payload()
    submodule = _get_submodule_info(query_remote=True)
    return _fragment({
        'success': bool(bridge.get('success')),
        'bridge': bridge,
        'submodule': submodule,
    })


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
        return _fragment({'success': False, 'message': 'git pull 超时 (超过 60 秒)'})
    except FileNotFoundError:
        return _fragment({
            'success': False,
            'message': '未找到 git 命令，请确认系统已安装 git',
        })
    except Exception as e:
        return _fragment({'success': False, 'message': f'git pull 异常：{e}'})

    success = proc.returncode == 0
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
        return _fragment({
            'success': False,
            'stages': stages,
            'message': '❌ git reset --hard 失败，详见 stages',
        })

    log.warning(f'[do-update-force] 强制更新完成 plugin_dir={plugin_dir} '
                f'(本地未提交修改已丢弃)')
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

    # 1. git 客户端探测
    try:
        proc = _git_run(['git', '--version'], timeout=5.0)
    except FileNotFoundError:
        return _fragment({
            'success': False,
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
        return _fragment({
            'success': False,
            'stages': stages,
            'message': '❌ git init 失败，详见 stages 输出',
        })
    if not run_stage('git remote add',
                     ['git', '-C', plugin_dir, 'remote', 'add', 'origin', remote_url],
                     timeout=10.0):
        return _fragment({
            'success': False,
            'stages': stages,
            'message': '❌ git remote add 失败，详见 stages 输出',
        })
    if not run_stage('git fetch --tags --depth 50',
                     ['git', '-C', plugin_dir, 'fetch', 'origin',
                      '--tags', '--depth', '50'], timeout=120.0):
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


def render_clear_avatar() -> str:
    ok, msg, n = _clear_dir(_cache_dir('avatar'))
    return _fragment({'success': ok, 'message': msg, 'removed': n})


def render_clear_avatar_7d() -> str:
    ok, msg, n = _clear_dir_keep_recent(_cache_dir('avatar'), days=7)
    return _fragment({'success': ok, 'message': msg, 'removed': n})


def render_clear_gen() -> str:
    ok, msg, n = _clear_dir(_cache_dir('gen'))
    return _fragment({'success': ok, 'message': msg, 'removed': n})


def render_clear_gen_7d() -> str:
    ok, msg, n = _clear_dir_keep_recent(_cache_dir('gen'), days=7)
    return _fragment({'success': ok, 'message': msg, 'removed': n})


def render_clear_match_all() -> str:
    ok, msg, n = _clear_dir(_cache_dir('match'))
    return _fragment({'success': ok, 'message': msg, 'removed': n})


def render_clear_match_7d() -> str:
    ok, msg, n = _clear_dir_keep_recent(_cache_dir('match'), days=7)
    return _fragment({'success': ok, 'message': msg, 'removed': n})
