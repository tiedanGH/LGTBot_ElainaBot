#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""破坏性 / 状态变更操作审计 —— 持久化最近 500 条到 ``data/audit/audit.json``。

谁在写:各状态变更端点的**入口层**(webui page_* 按钮端点、dispatcher 的 /重启 与 /计划重启 指令、backup 的自动备份),
每个入口一行 ``audit.record(cat, action, detail, ok, src)``;共享 helper 保持纯函数不挂钩,
由入口标注来源(面板 / 指令 / 自动)。谁在读:``webui/page_audit``(只读展示)。

设计:
  · **文件即真相源** —— 热重载后重读文件即可,不走 boot._get_persistent();
    重启(os.execv)不丢:record() 是同步写盘,返回即已持久化。
  · **整文件原子重写**(tmp + os.replace,照 page_config._atomic_write):
    操作全是人工触发,每次全量重写 ≤500 条(几十 KB)无成本,换来任意时刻
    (包括 execv 瞬间)读到的都是完整 JSON,没有追加式的撕裂行问题;
    截断到 MAX_ENTRIES 也顺手完成。
  · **record() 永不抛异常** —— 审计失败绝不影响业务动作本身,调用方零负担。
  · 损坏容错:audit.json 解析失败时尽力改名 ``.corrupt_<ts>`` 留证,从空续记。
  · 放 ``data/audit/`` 子目录:框架配置入口非递归扫 data/ 根,子目录不可见
    不污染配置列表;backup 的打包白名单不含此目录 → 恢复旧备份不会把审计
    历史一起回滚(防"恢复备份抹掉审计"的自毁路径)。
  · **不提供任何清空 API / 端点**(防自毁审计);容量靠 500 条滚动淘汰。
"""

from __future__ import annotations

import json
import os
import threading
import time

from core.base.logger import get_logger, PLUGIN

from . import boot

log = get_logger(PLUGIN, 'LGTBot')

# 滚动保留的最大条数(固定常量)
MAX_ENTRIES = 500

AUDIT_DIR = os.path.join(boot.DATA_DIR, 'audit')
AUDIT_PATH = os.path.join(AUDIT_DIR, 'audit.json')

# 触发来源 —— 双入口操作(重启 / 计划重启)靠它区分从哪里发起
SRC_PANEL = '面板'
SRC_CMD = '指令'
SRC_AUTO = '自动'
SRC_API = 'API'      # 编译 API 等对其他插件开放的 HTTP 接口触发

# cat 短码 → (emoji, 中文标签)。单一真相源:后端只存短码,page_audit 把
# 此映射随 payload 下发,前端据此渲染类别徽标与筛选按钮。
CATEGORIES = {
    'build':   ('🛠️', '引擎编译'),
    'backup':  ('💾', '数据备份'),
    'cache':   ('🧹', '缓存清理'),
    'update':  ('⬇️', '更新维护'),
    'config':  ('⚙️', '配置变更'),
    'restart': ('🔁', '重启'),
    'bind':    ('🔗', '机器人绑定'),
    'match':   ('🎮', '对局干预'),
}

# 单 asyncio loop 下各入口天然串行;锁防未来从引擎工作线程记录(对齐 page_logs 的 threading.Lock 先例),成本可忽略。
_lock = threading.Lock()


def _load_raw() -> list:
    """读 audit.json 为 list(文件内按时间正序)。必须在持有 _lock 时调用。

    不存在 → [];损坏(JSON 解析失败 / 根不是 list)→ 改名留证 + 返回 []。
    """
    try:
        with open(AUDIT_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        log.warning('[audit] audit.json 根节点不是 list,按损坏处理')
    except FileNotFoundError:
        return []
    except Exception as e:
        log.warning(f'[audit] audit.json 解析失败,按损坏处理: {e}')
    # 损坏:尽力改名保留现场,失败也吞掉(从空续记不受影响)
    try:
        os.replace(AUDIT_PATH, f'{AUDIT_PATH}.corrupt_{int(time.time())}')
    except OSError:
        pass
    return []


def _atomic_write(entries: list) -> None:
    """临时文件 + os.replace 原子落盘(同 page_config._atomic_write)。"""
    os.makedirs(AUDIT_DIR, exist_ok=True)
    tmp = AUDIT_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(entries, f, ensure_ascii=False)
    os.replace(tmp, AUDIT_PATH)


def record(cat: str, action: str, detail: str = '',
           ok: bool = True, src: str = SRC_PANEL) -> None:
    """追加一条审计记录并同步落盘。

    永不抛异常 —— 任何失败仅 log.warning,业务动作不受影响。
    返回即已持久化(重启入口靠这一点保证 os.execv 前落盘)。
    """
    try:
        with _lock:
            entries = _load_raw()
            entries.append({
                'ts': int(time.time()),
                'cat': str(cat),
                'action': str(action),
                'detail': str(detail)[:500],
                'ok': bool(ok),
                'src': str(src),
            })
            _atomic_write(entries[-MAX_ENTRIES:])
    except Exception as e:
        log.warning(f'[audit] 记录失败(不影响业务): {e}')


def get_entries() -> list:
    """全部审计记录,新 → 旧。异常时返回 [](展示层按空态渲染)。"""
    try:
        with _lock:
            return list(reversed(_load_raw()))
    except Exception as e:
        log.warning(f'[audit] 读取失败: {e}')
        return []


def file_status() -> dict:
    """审计文件概况 {count, oldest_ts, size_bytes},供状态卡展示。异常返零值。"""
    try:
        with _lock:
            entries = _load_raw()
            size = os.path.getsize(AUDIT_PATH) if os.path.isfile(AUDIT_PATH) else 0
        return {
            'count': len(entries),
            'oldest_ts': entries[0].get('ts') if entries else None,
            'size_bytes': size,
        }
    except Exception as e:
        log.warning(f'[audit] 状态读取失败: {e}')
        return {'count': 0, 'oldest_ts': None, 'size_bytes': 0}
