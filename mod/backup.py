#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据库备份 —— 把 data/ 下的核心数据(LGTBot 引擎 SQLite + 配置 yaml +
管理员自定义 txt)打包成 zip,**存到插件目录外**(框架根 `data/backup/lgtbot/`)。

设计要点:
  · 备份位置:``<framework_root>/data/backup/lgtbot/`` —— 框架根而非插件根,
    路径反推见 ``_FRAMEWORK_ROOT``,基于 ``boot.PLUGIN_DIR`` 上溯两级。
  · SQLite 在线备份用 ``sqlite3.Connection.backup()`` API,SQLite 内部锁
    保证一致 snapshot,即使 LGTBot 引擎正在写也安全(只复制已 commit 页)。
  · 触发:启动 60s 后查最新 zip mtime 是否 > 24h(`schedule_on_load_check`);
    WebUI 手动按钮(`create_backup`)。**不**做 long-running cron,reload
    / restart 每次都触发检查,够用。
  · 轮转:每次成功备份后保留最近 ``RETENTION_COUNT=7`` 份,按 mtime 排序删多余。
  · 恢复:`restore_backup()` **必须先调用方调 release_bot_if_not_processing_games**,
    本模块只做 unpack;不停引擎不重启,让 UI 层 + 用户决策。

安全准则(对齐 page_dashboard.py 的 audit 风格):
  · 所有 create / restore / delete 都 ``log.info`` 一条 audit 行,带文件名
    + 操作结果,方便事后追溯「这个备份谁动的」。
  · 自动备份只在「距今 > AUTO_INTERVAL_S」时触发,不在任何高频 hook 内调用。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import time
import zipfile
from datetime import datetime

from core.base.logger import get_logger, PLUGIN
from . import boot

log = get_logger(PLUGIN, 'LGTBot')


# ──────── 路径常量 ────────────────────────────────────────────────────────
# 把备份放在**框架根** data/backup/lgtbot/,而非插件目录内。
#
# 反推:boot.PLUGIN_DIR = <root>/plugins/LGTBot_ElainaBot
# 上溯两级得 <root>(框架根);主框架已有 <root>/data/backup/ 目录,
# 选用子目录 lgtbot/ 归类,文件名不会与主框架自己的 zip 冲突。
_FRAMEWORK_ROOT = os.path.dirname(os.path.dirname(boot.PLUGIN_DIR))
BACKUP_DIR = os.path.join(_FRAMEWORK_ROOT, 'data', 'backup', 'lgtbot')

# 轮转 / 自动备份参数。MVP 阶段硬编码。
RETENTION_COUNT = 7              # 保留最近 N 份
AUTO_INTERVAL_S = 24 * 3600.0    # 启动检查阈值:最新 zip 早于 24h 才触发新备份
_ON_LOAD_DELAY_S = 60.0          # @on_load 后等 N 秒再检查,避开启动忙峰


# ──────── 需要备份的源文件清单(相对 plugin_dir,zip 内保留 data/ 前缀)──
# (relative_path_in_zip, absolute_path_on_disk, kind)
# kind:
#   · 'sqlite' —— 用 sqlite3 backup() API,源文件在引擎运行时可能被持续写入
#   · 'plain'  —— 普通文件,直接 zip(yaml / txt 等不会高频写)
def _collect_sources() -> list[tuple[str, str, str]]:
    """生成本次备份需要打进 zip 的文件清单。

    返回 ``[(arc_name_in_zip, abs_src_path, kind), ...]``。仅包含磁盘上实际
    存在的文件 —— 一份全新部署可能还没 config.yaml,不应让 backup 报错。
    """
    candidates: list[tuple[str, str, str]] = [
        # SQLite 核心数据(战绩 / 成就)
        ('data/engine/lgtbot.db',     boot.DB_PATH,        'sqlite'),
        # 引擎 JSON 配置(boot.CONF_PATH = data/engine/lgtbot.json)
        ('data/engine/lgtbot.json',   boot.CONF_PATH,      'plain'),
        # 插件 yaml 配置
        ('data/config.yaml',          os.path.join(boot.DATA_DIR, 'config.yaml'),   'plain'),
        # 管理员自定义文本(若有)
        ('data/update_notice.txt',    os.path.join(boot.DATA_DIR, 'update_notice.txt'),    'plain'),
        ('data/important_update.txt', os.path.join(boot.DATA_DIR, 'important_update.txt'), 'plain'),
        ('data/troubleshooting.txt',  os.path.join(boot.DATA_DIR, 'troubleshooting.txt'),  'plain'),
    ]
    return [(arc, abs_p, kind) for arc, abs_p, kind in candidates
            if os.path.isfile(abs_p)]


# ──────── SQLite 在线备份 helper ──────────────────────────────────────────

def _backup_sqlite_to_tmp(src_path: str, tmp_path: str) -> bool:
    """用 ``sqlite3.Connection.backup()`` 把源 db 安全复制到临时文件。

    SQLite 内部页锁保证拿到一致 snapshot —— 即使 LGTBot 引擎正在写
    lgtbot.db,backup() 也只会复制已 commit 的页。失败(锁超时 / 文件
    损坏 / 编译时 sqlite 不支持 backup 等)返回 False,调用方应跳过此 db
    继续打包其他文件,而不是整个 backup 失败。
    """
    src = None
    dst = None
    try:
        # readonly 打开避免误写源 db;timeout 5s 等待引擎释放锁(MB 级 db
        # backup 通常 < 1s,5s 富余)
        src = sqlite3.connect(f'file:{src_path}?mode=ro', uri=True, timeout=5.0)
        dst = sqlite3.connect(tmp_path)
        src.backup(dst)
        return True
    except sqlite3.Error as e:
        log.warning(f'[backup] sqlite backup failed for {src_path}: {e}')
        return False
    except Exception as e:
        log.warning(f'[backup] sqlite backup unexpected error for {src_path}: {e}')
        return False
    finally:
        for c in (src, dst):
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass


# ──────── 公开:核心 create / list / restore / delete / prune ─────────────

def _zip_filename_now() -> str:
    """返回当前时间戳的 zip 文件名 —— ``LGTBot_YYYY-MM-DD_HHMMSS.zip``,
    人类可读、按字典序也就是时间序,方便 ls 查看。
    """
    return 'LGTBot_' + datetime.now().strftime('%Y-%m-%d_%H%M%S') + '.zip'


def create_backup() -> dict:
    """执行一次完整备份,返回 ``{success, zip_path, size_bytes, included, skipped, message}``。

    流程:
      1. 收集源文件清单(只含磁盘上存在的)
      2. SQLite 文件先用 backup() API 复制到 tmp dir
      3. 全部 plain 文件 + tmp dir 中的 sqlite 拷贝 → zipfile.ZIP_DEFLATED 打包
      4. 清 tmp dir + prune_old + 返回结果
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    sources = _collect_sources()
    if not sources:
        log.warning('[backup] 无任何源文件可备份(data/ 是空的?跳过)')
        return {
            'success': False,
            'message': '没有任何可备份的数据文件(data/ 目录为空)',
            'included': [],
            'skipped': [],
        }

    zip_name = _zip_filename_now()
    zip_path = os.path.join(BACKUP_DIR, zip_name)
    tmp_dir = os.path.join(BACKUP_DIR, f'.tmp_{int(time.time() * 1000)}')
    os.makedirs(tmp_dir, exist_ok=True)

    included: list[str] = []
    skipped: list[dict] = []   # [{path, reason}]
    try:
        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for arc_name, abs_src, kind in sources:
                if kind == 'sqlite':
                    # SQLite 先 backup() 到 tmp,再 zip 进去
                    tmp_db = os.path.join(tmp_dir, os.path.basename(arc_name))
                    if _backup_sqlite_to_tmp(abs_src, tmp_db):
                        zf.write(tmp_db, arcname=arc_name)
                        included.append(arc_name)
                    else:
                        skipped.append({'path': arc_name, 'reason': 'sqlite backup() 失败,跳过'})
                else:
                    try:
                        zf.write(abs_src, arcname=arc_name)
                        included.append(arc_name)
                    except OSError as e:
                        skipped.append({'path': arc_name, 'reason': f'读文件失败: {e}'})
    except Exception as e:
        log.error(f'[backup] 写 zip 失败 {zip_path}: {e}')
        # 失败别留半截 zip,删干净
        if os.path.isfile(zip_path):
            try:
                os.remove(zip_path)
            except OSError:
                pass
        return {
            'success': False,
            'message': f'打包 zip 失败: {e}',
            'included': included,
            'skipped': skipped,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # 必须至少有一个文件入包,否则视为失败
    if not included:
        log.warning('[backup] 所有源文件都跳过了,删空 zip')
        try:
            os.remove(zip_path)
        except OSError:
            pass
        return {
            'success': False,
            'message': '所有源文件都跳过(可能 SQLite 全锁住),备份未生成',
            'included': [],
            'skipped': skipped,
        }

    size_bytes = os.path.getsize(zip_path)
    log.info(f'[backup] ✅ 创建 {zip_name}({size_bytes} 字节,{len(included)} 个文件)')
    if skipped:
        log.info(f'[backup] 跳过 {len(skipped)} 项: {[s["path"] for s in skipped]}')

    pruned = prune_old(RETENTION_COUNT)
    return {
        'success': True,
        'zip_path': zip_path,
        'zip_name': zip_name,
        'size_bytes': size_bytes,
        'included': included,
        'skipped': skipped,
        'pruned': pruned,
        'message': f'已生成备份 {zip_name}',
    }


def list_backups() -> list[dict]:
    """扫 BACKUP_DIR 下所有 LGTBot_*.zip,按 mtime 降序(最新在前),
    返回 ``[{name, path, size_bytes, mtime_ts}, ...]``。

    异常 / 目录不存在 → 返回空列表(UI 会渲染成「(尚无备份)」)。
    """
    if not os.path.isdir(BACKUP_DIR):
        return []
    entries: list[dict] = []
    try:
        for entry in os.scandir(BACKUP_DIR):
            if not entry.is_file(follow_symlinks=False):
                continue
            if not entry.name.startswith('LGTBot_') or not entry.name.endswith('.zip'):
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            entries.append({
                'name': entry.name,
                'path': entry.path,
                'size_bytes': st.st_size,
                'mtime_ts': st.st_mtime,
            })
    except OSError as e:
        log.warning(f'[backup] 扫描 {BACKUP_DIR} 失败: {e}')
        return []
    entries.sort(key=lambda e: e['mtime_ts'], reverse=True)
    return entries


def restore_backup(zip_name: str) -> dict:
    """把 zip 解压覆盖到 ``plugins/LGTBot_ElainaBot/data/``。

    **本函数不停引擎、不重启**;调用方(WebUI render_restore)负责在调本函数
    前先调 ``boot.LGTBot_ElainaBot.release_bot_if_not_processing_games()``
    做活跃游戏预检,完成后提示用户手动重启。
    """
    if not zip_name or '/' in zip_name or '\\' in zip_name or '..' in zip_name:
        # 防路径穿越 —— 只接受纯文件名,不接受路径
        return {'success': False, 'message': '非法备份文件名'}
    zip_path = os.path.join(BACKUP_DIR, zip_name)
    if not os.path.isfile(zip_path):
        return {'success': False, 'message': f'备份文件不存在: {zip_name}'}

    log.info(f'[backup] ⏪ 准备恢复 {zip_name} → {boot.PLUGIN_DIR}/data/')
    try:
        # zip 内是 data/... 相对路径,unpack 到 plugin_dir 等于把 data/ 整个还原
        # zipfile.extractall 不会自动覆盖只读文件;先验证 zip 内成员路径全部
        # 在 data/ 下,防恶意 zip 解压到上级目录(zip slip)
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.namelist():
                norm = os.path.normpath(member)
                if norm.startswith('..') or os.path.isabs(norm):
                    return {
                        'success': False,
                        'message': f'备份文件包含非法路径 {member!r}(zip slip 防护)',
                    }
                # 必须 data/ 开头
                if not norm.startswith('data' + os.sep) and norm != 'data':
                    return {
                        'success': False,
                        'message': f'备份文件含非 data/ 路径 {member!r}',
                    }
            zf.extractall(boot.PLUGIN_DIR)
    except Exception as e:
        log.error(f'[backup] 恢复 {zip_name} 失败: {e}')
        return {'success': False, 'message': f'解压失败: {e}'}

    log.info(f'[backup] ✅ 已恢复 {zip_name},请重启 LGTBot 引擎加载新数据')
    return {
        'success': True,
        'zip_name': zip_name,
        'message': '已恢复成功,请点击「🔁 重启 LGTBot」加载新数据',
    }


def delete_backup(zip_name: str) -> dict:
    """删单个备份文件。同 restore 的路径穿越防护。"""
    if not zip_name or '/' in zip_name or '\\' in zip_name or '..' in zip_name:
        return {'success': False, 'message': '非法备份文件名'}
    zip_path = os.path.join(BACKUP_DIR, zip_name)
    if not os.path.isfile(zip_path):
        return {'success': False, 'message': f'备份文件不存在: {zip_name}'}
    try:
        os.remove(zip_path)
    except OSError as e:
        log.warning(f'[backup] 删除 {zip_name} 失败: {e}')
        return {'success': False, 'message': f'删除失败: {e}'}
    log.info(f'[backup] 🗑 已删除 {zip_name}')
    return {'success': True, 'zip_name': zip_name, 'message': '已删除'}


def prune_old(retention: int = RETENTION_COUNT) -> list[str]:
    """按 mtime 排序保留最近 ``retention`` 份,删多出来的旧 zip。返回被删的文件名列表。

    `retention <= 0` 视为不轮转(防误传)。
    """
    if retention <= 0:
        return []
    backups = list_backups()
    if len(backups) <= retention:
        return []
    # list_backups 已按 mtime 降序,留前 retention 个,删剩下的
    to_delete = backups[retention:]
    deleted: list[str] = []
    for b in to_delete:
        try:
            os.remove(b['path'])
            deleted.append(b['name'])
        except OSError as e:
            log.warning(f'[backup] 轮转删除 {b["name"]} 失败: {e}')
    if deleted:
        log.info(f'[backup] 轮转删除 {len(deleted)} 份旧备份: {deleted}')
    return deleted


# ──────── @on_load 自动备份检查 ──────────────────────────────────────────

def schedule_on_load_check() -> None:
    """@on_load 钩子调用 —— 后台 asyncio task,等 60s 后查最新 zip 是否过期,
    过期(> 24h)就触发一次新备份。

    设计:不开 long-running 定时器,只在 startup / reload 时检查一次。运行
    时长 ≥ 24h 的部署其实很少见(用户经常 reload 插件 / 重启进程),每次
    冷启动机会跑这一遍足够。

    异常吞掉,不影响主流程。
    """
    from . import state
    loop = state.event_loop
    if loop is None or loop.is_closed():
        log.debug('[backup] state.event_loop 未就绪,跳过 on_load 自动备份检查')
        return
    try:
        asyncio.run_coroutine_threadsafe(_on_load_check_coro(), loop)
    except Exception as e:
        log.warning(f'[backup] 调度 on_load 自动备份失败: {e}')


async def _on_load_check_coro() -> None:
    """asyncio 后台 task —— 等 60s 让插件 ready,再做时效检查 + 触发备份。"""
    try:
        await asyncio.sleep(_ON_LOAD_DELAY_S)
    except asyncio.CancelledError:
        return

    backups = list_backups()
    now = time.time()
    if backups:
        latest_age = now - backups[0]['mtime_ts']
        if latest_age < AUTO_INTERVAL_S:
            log.debug(f'[backup] 最新备份 {backups[0]["name"]} 仅 {latest_age:.0f}s 前,'
                      f'未到 {AUTO_INTERVAL_S:.0f}s 自动备份阈值,跳过')
            return
        log.info(f'[backup] 最新备份 {backups[0]["name"]} 距今 {latest_age / 3600:.1f}h '
                 f'(> {AUTO_INTERVAL_S / 3600:.0f}h),触发自动备份')
    else:
        log.info('[backup] 尚无任何备份,触发首次自动备份')

    # 真正跑备份 —— create_backup 是同步阻塞,但耗时短(< 2s),
    # 在 asyncio loop 上同步跑可接受(不影响其他协程的总体延迟)。
    try:
        result = create_backup()
        if result.get('success'):
            log.info(f'[backup] 自动备份完成: {result.get("zip_name")}')
        else:
            log.warning(f'[backup] 自动备份失败: {result.get("message")}')
    except Exception as e:
        log.error(f'[backup] 自动备份异常: {e}')
