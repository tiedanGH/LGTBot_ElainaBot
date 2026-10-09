#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据库备份 —— 把 data/ 下的核心数据(LGTBot 引擎 SQLite + 配置 yaml +
管理员自定义 txt)打包成 zip,**存到插件目录外**(框架根 `data/backup/lgtbot/`)。

设计要点:
  · SQLite 用 ``sqlite3.Connection.backup()`` 在线备份,引擎正在写也能拿到一致 snapshot。
  · 触发:启动 60s 后最新 zip 早于 24h 才备份(`schedule_on_load_check`);WebUI 手动按钮
    (`create_backup`)。不做常驻定时器、不挂任何高频 hook,reload / restart 时各检查一次。
  · 轮转:每次成功备份后按 mtime 保留最近 ``RETENTION_COUNT`` 份。
  · 恢复:`restore_backup()` 逐个文件 ``os.replace`` 原子换入,**不停引擎**即热生效
    (原理见其 docstring);只有 ``lgtbot.json`` 引擎配置要重启 LGTBot 才重新加载。
  · 所有 create / restore / delete 都 ``log.info`` 一条带文件名 + 操作结果的 audit 行。
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
from . import audit, boot

log = get_logger(PLUGIN, 'LGTBot')


# ──────── 路径常量 ────────────────────────────────────────────────────────
# boot.PLUGIN_DIR = <root>/plugins/LGTBot_ElainaBot,上溯两级得框架根;
# 放主框架 <root>/data/backup/ 下的 lgtbot/ 子目录,不与主框架自己的 zip 冲突。
_FRAMEWORK_ROOT = os.path.dirname(os.path.dirname(boot.PLUGIN_DIR))
BACKUP_DIR = os.path.join(_FRAMEWORK_ROOT, 'data', 'backup', 'lgtbot')

# 轮转 / 自动备份参数
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
        # 引擎 JSON 配置
        ('data/engine/lgtbot.json',   boot.CONF_PATH,      'plain'),
        # 插件 yaml 配置
        ('data/config.yaml',          os.path.join(boot.DATA_DIR, 'config.yaml'),   'plain'),
        # 管理员自定义文本(若有)
        ('data/update_notice.txt',    os.path.join(boot.DATA_DIR, 'update_notice.txt'),    'plain'),
        ('data/important_update.txt', os.path.join(boot.DATA_DIR, 'important_update.txt'), 'plain'),
        ('data/urgent_notice.txt',    os.path.join(boot.DATA_DIR, 'urgent_notice.txt'),    'plain'),
        # 紧急公告的开关 + 已通知群记录
        ('data/urgent_notice.json',   os.path.join(boot.DATA_DIR, 'urgent_notice.json'),   'plain'),
        ('data/troubleshooting.txt',  os.path.join(boot.DATA_DIR, 'troubleshooting.txt'),  'plain'),
        ('data/sponsors.txt',         os.path.join(boot.DATA_DIR, 'sponsors.txt'),         'plain'),
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
        # readonly 打开避免误写源 db;timeout 等待引擎释放锁
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
    """执行一次完整备份,返回 ``{success, zip_path, size_bytes, included, skipped, message}``。"""
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
    """把 zip 内容**原子覆盖**到 ``plugins/LGTBot_ElainaBot/data/``,引擎继续运行不挂。

    流程:
      1. 预检 zip 成员(zip-slip + 必须 ``data/`` 开头)
      2. 整 zip 解到 plugin_dir 下的临时目录 ``.restore_tmp_<ts>/``(与目标同 fs,
         rename 才原子);不直接解到 ``data/``:``extractall`` 对已有文件是原地 O_TRUNC
         截断,引擎某条指令的 ``sqlite3_open`` 撞上写一半的窗口会读到撕裂的 db
      3. 每个文件 ``os.replace`` 原子换入 ``data/`` 对应位置,任意时刻 open 看到的
         要么旧完整文件、要么新完整文件
      4. 对刚替换过的 ``*.db`` 文件,把旁路 ``-journal`` / ``-wal`` / ``-shm``
         rename 为 ``.stale_<ts>`` 备查 —— 否则 SQLite 会把它们当未完成事务回滚,
         把恢复的数据滚成残缺态

    热生效:引擎对 ``lgtbot.db`` **没有常驻连接**(``bot_core/db_manager.cc:ExecuteTransaction``
    每条指令现开现关),原子替换后下一条指令立即看到恢复的数据。公告 / 疑难解答 ``*.txt``
    由 dispatcher 每条指令现读,同样立即生效。

    需重启:``lgtbot.json`` 在 ``LGTBot_Create`` 时解析进引擎内存;``data/config.yaml``
    在 ``@on_load`` 时 apply。由 UI 提示用户按需点「🔁 重启 LGTBot」。

    **本函数不停引擎、不重启**,重启只走 ``dispatcher.check_and_prepare_restart``。
    不要在覆盖前调 ``release_bot_if_not_processing_games``:会 null-deref 引擎,之后重启再 double-free。
    """
    if not zip_name or '/' in zip_name or '\\' in zip_name or '..' in zip_name:
        return {'success': False, 'message': '非法备份文件名'}
    zip_path = os.path.join(BACKUP_DIR, zip_name)
    if not os.path.isfile(zip_path):
        return {'success': False, 'message': f'备份文件不存在: {zip_name}'}

    log.info(f'[backup] ⏪ 准备恢复 {zip_name} → {boot.PLUGIN_DIR}/data/')

    ts = f'{int(time.time())}_{os.getpid()}'
    tmp_root = os.path.join(boot.PLUGIN_DIR, f'.restore_tmp_{ts}')
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.namelist():
                norm = os.path.normpath(member)
                if norm.startswith('..') or os.path.isabs(norm):
                    return {
                        'success': False,
                        'message': f'备份文件包含非法路径 {member!r}(zip slip 防护)',
                    }
                if not (norm.startswith('data' + os.sep) or norm == 'data'):
                    return {
                        'success': False,
                        'message': f'备份文件含非 data/ 路径 {member!r}',
                    }
            os.makedirs(tmp_root, exist_ok=True)
            zf.extractall(tmp_root)

        replaced: list[str] = []
        swept_sidecars: list[str] = []
        src_root = os.path.join(tmp_root, 'data')
        dst_root = boot.DATA_DIR
        if not os.path.isdir(src_root):
            return {'success': False, 'message': '备份 zip 内不含 data/ 目录'}
        for root_dir, _dirs, files in os.walk(src_root):
            rel = os.path.relpath(root_dir, src_root)
            dst_dir = dst_root if rel == '.' else os.path.join(dst_root, rel)
            os.makedirs(dst_dir, exist_ok=True)
            for fname in files:
                src_path = os.path.join(root_dir, fname)
                dst_path = os.path.join(dst_dir, fname)
                os.replace(src_path, dst_path)
                replaced.append(
                    os.path.relpath(dst_path, boot.PLUGIN_DIR).replace(os.sep, '/')
                )
                if fname.endswith('.db'):
                    for suffix in ('-journal', '-wal', '-shm'):
                        side = dst_path + suffix
                        if os.path.isfile(side):
                            try:
                                bak = f'{side}.stale_{ts}'
                                os.replace(side, bak)
                                swept_sidecars.append(
                                    os.path.relpath(bak, boot.PLUGIN_DIR).replace(os.sep, '/')
                                )
                            except OSError as e:
                                log.warning(f'[backup] 移走过期 sidecar {side} 失败: {e}')

        log.info(f'[backup] ✅ 已恢复 {zip_name},替换 {len(replaced)} 文件,'
                 f'清理 {len(swept_sidecars)} 个过期 sidecar')
        return {
            'success': True,
            'zip_name': zip_name,
            'replaced_files': replaced,
            'swept_sidecars': swept_sidecars,
            'message': '已恢复成功，战绩 / 成就等数据立即生效。引擎配置需重启才能重新加载',
        }
    except Exception as e:
        log.error(f'[backup] 恢复 {zip_name} 失败: {e}')
        return {'success': False, 'message': f'恢复失败: {e}'}
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


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
    过期(> 24h)就触发一次新备份。不开常驻定时器,只在 startup / reload 时检查一次。

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

    # create_backup 同步阻塞,但耗时短,直接在 asyncio loop 上跑
    try:
        result = create_backup()
        if result.get('success'):
            log.info(f'[backup] 自动备份完成: {result.get("zip_name")}')
            audit.record('backup', '自动备份', str(result.get('zip_name') or ''),
                         src=audit.SRC_AUTO)
        else:
            log.warning(f'[backup] 自动备份失败: {result.get("message")}')
            audit.record('backup', '自动备份', str(result.get('message') or ''),
                         ok=False, src=audit.SRC_AUTO)
    except Exception as e:
        log.error(f'[backup] 自动备份异常: {e}')
        audit.record('backup', '自动备份', str(e), ok=False, src=audit.SRC_AUTO)
