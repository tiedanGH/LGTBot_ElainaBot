#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""紧急公告 —— 文案(txt)+ 总开关与已通知群记录(json)。

两份磁盘状态:
  · ``data/urgent_notice.txt``   公告文案。管理员在面板「配置管理」里编辑,每次用到都现读(与更新公告 / 疑难解答同款热更新)
  · ``data/urgent_notice.json``  ``enabled`` 开关 + ``notified_groups`` 已通知群。
    **必须落盘**:需求是"重启依然生效,除非手动关闭" —— 内存和 ``boot._get_persistent()`` 都撑不过 ``os.execv``

两处消费:
  1. 欢迎菜单尾部的引用块(``menu_block``)—— 仅在**已启用且文案非空**时出现
  2. 新群第一次建房时额外推一条带 ⚠️ 标题的通知(``notify_message``),发完把群号
     记进 ``notified_groups``,此后该群再建房不再打扰

「关闭公告」与「重置已通知群」是两个**独立**动作:关掉开关不清记录(重新启用时
老群不会被重复打扰),要让所有群重新收到必须显式点面板上的重置按钮。

状态在进程内缓存,并用文件签名(mtime + size)兜底 —— 手工改 json 也能被读到;
正常路径下 ``_write_state`` 写完就同步缓存,不依赖 mtime 精度。
"""

from __future__ import annotations

import json
import os
import time

from core.base.logger import get_logger, PLUGIN
from . import boot, helpers

log = get_logger(PLUGIN, 'LGTBot')

# 公告文案(与其他管理员可编辑文本同一目录);面板 / dispatcher 共用此常量
NOTICE_PATH = os.path.join(boot.DATA_DIR, 'urgent_notice.txt')
# 开关 + 已通知群 —— 与文案分开存:文案是纯文本给人编辑,状态是机器读写的结构
STATE_PATH = os.path.join(boot.DATA_DIR, 'urgent_notice.json')

# 新群首次建房时那条通知的固定标题。正文直接用公告文案原文,**不套引用块**
# 这条消息本身就是公告,再缩进一格只会更难读(菜单里的那份才需要引用块把它与内联指令区分开)。
NOTIFY_TITLE = '# ⚠️ 紧急公告'

# 进程内缓存:_state 是当前状态,_state_sig 是它对应的文件签名(None = 文件不存在)
_state: dict | None = None
_state_sig = None


# ─────────────────────────────────────────────────────────────────────────
# 磁盘状态
# ─────────────────────────────────────────────────────────────────────────

def _blank() -> dict:
    """默认状态:未启用、无已通知群。文件不存在 / 读坏了都退到这里。"""
    return {'enabled': False, 'notified': set(), 'updated_at': ''}


def _file_sig():
    """状态文件签名;文件不存在返回 None。"""
    try:
        st = os.stat(STATE_PATH)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _load_from_disk() -> dict:
    """读 json → 规范化后的状态 dict。任何异常都退到 ``_blank()``。

    容错到底:这份状态出问题时最坏的行为是"公告不显示",绝不能让一份坏 json
    把欢迎菜单或建房流程带崩。
    """
    if not os.path.isfile(STATE_PATH):
        return _blank()
    try:
        with open(STATE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        log.warning(f'读取 urgent_notice.json 失败，按未启用处理: {e}')
        return _blank()
    if not isinstance(data, dict):
        log.warning('urgent_notice.json 根节点不是对象，按未启用处理')
        return _blank()
    raw = data.get('notified_groups')
    groups = set()
    if isinstance(raw, list):
        for g in raw:
            g = str(g).strip()
            if g:
                groups.add(g)
    return {
        'enabled': bool(data.get('enabled')),
        'notified': groups,
        'updated_at': str(data.get('updated_at') or ''),
    }


def _state_dict() -> dict:
    """当前状态(带签名缓存)。**唯一**的状态读入口。"""
    global _state, _state_sig
    sig = _file_sig()
    if _state is not None and _state_sig == sig:
        return _state
    _state = _load_from_disk()
    _state_sig = sig
    return _state


def _write_state(st: dict) -> bool:
    """原子落盘 + 同步缓存。返回是否写成功(失败已记 error 日志)。

    临时文件 + ``os.replace``:写一半被读到的话状态会退成"未启用",而这份文件正是"重启后还生效"的唯一依据。
    """
    global _state, _state_sig
    payload = {
        'enabled': bool(st.get('enabled')),
        # set 存盘要定序,否则每次写文件内容都在抖(备份 / diff 全是噪声)
        'notified_groups': sorted(st.get('notified') or ()),
        'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    tmp = STATE_PATH + '.tmp'
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        log.error(f'写入 urgent_notice.json 失败: {e}')
        return False
    st['updated_at'] = payload['updated_at']
    _state, _state_sig = st, _file_sig()
    return True


# ─────────────────────────────────────────────────────────────────────────
# 公告文案
# ─────────────────────────────────────────────────────────────────────────

def notice_text() -> str:
    """公告文案(现读现用);文件缺失 / 全空白 → ``''``。"""
    return helpers.read_optional_txt(NOTICE_PATH, 'urgent_notice.txt')


def menu_block() -> str:
    """欢迎菜单尾部的引用块;**未启用**或文案为空 → ``''``(连空行都不留)。

    前后各垫一个空行:前面的隔开上一行内联指令,后面的保证 markdown 的 lazy
    continuation 不会把日后可能追加在菜单末尾的普通行吸进引用块(尾部空行不
    产生可见内容)。
    """
    if not is_enabled():
        return ''
    text = notice_text()
    if not text:
        return ''
    return '\n' + helpers.as_quote(text) + '\n\n'


def notify_message() -> str:
    """新群首次建房时那条独立通知的正文;未启用 / 文案为空 → ``''``。"""
    if not is_enabled():
        return ''
    text = notice_text()
    if not text:
        return ''
    return f'{NOTIFY_TITLE}\n\n{text}'


# ─────────────────────────────────────────────────────────────────────────
# 总开关
# ─────────────────────────────────────────────────────────────────────────

def is_enabled() -> bool:
    """公告是否已启用。关闭时即便文案非空也一律不展示、不通知。"""
    return bool(_state_dict().get('enabled'))


def set_enabled(on: bool) -> bool:
    """开 / 关公告并落盘。返回是否写盘成功。

    **不动** ``notified_groups`` —— 关掉再开时,已通知过的群不该被重复打扰。
    """
    st = _state_dict()
    st['enabled'] = bool(on)
    return _write_state(st)


# ─────────────────────────────────────────────────────────────────────────
# 已通知群记录
# ─────────────────────────────────────────────────────────────────────────

def notified_count() -> int:
    """已通知过的群数量(面板按钮上那个 X)。"""
    return len(_state_dict().get('notified') or ())


def is_notified(gid: str) -> bool:
    """该群是否已经收到过紧急公告通知。"""
    return bool(gid) and gid in (_state_dict().get('notified') or ())


def mark_notified(gid: str) -> bool:
    """记下"这个群已通知过"并落盘。已记过 → 直接 True,不重复写盘。"""
    if not gid:
        return False
    st = _state_dict()
    groups = st.setdefault('notified', set())
    if gid in groups:
        return True
    groups.add(gid)
    return _write_state(st)


def reset_notified() -> int:
    """清空已通知群记录,返回清掉的条数。**不改** ``enabled``。

    清空后这些群下次建房会重新收到通知 —— 这是让全部群再看一遍公告的唯一手段(关闭公告本身不清记录,见模块 docstring)。
    """
    st = _state_dict()
    groups = st.setdefault('notified', set())
    n = len(groups)
    if not n:
        return 0
    groups.clear()
    _write_state(st)
    return n


def pending_notify(gid: str) -> str:
    """该群这次建房要不要额外推公告 —— 要推则返回消息正文,否则 ``''``。

    三个条件同时成立才推:公告已启用、文案非空、该群此前没被通知过。
    """
    if not gid or is_notified(gid):
        return ''
    return notify_message()


# ─────────────────────────────────────────────────────────────────────────
# 面板视图
# ─────────────────────────────────────────────────────────────────────────

def state_view() -> dict:
    """面板「配置管理」用的状态快照(按钮文案 / 高亮状态全靠这几个字段)。"""
    st = _state_dict()
    return {
        'enabled': bool(st.get('enabled')),
        'notified_count': len(st.get('notified') or ()),
        'state_path': os.path.abspath(STATE_PATH),
        'updated_at': str(st.get('updated_at') or ''),
    }
