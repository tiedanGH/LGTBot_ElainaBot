#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""build_prebuilt/ 的暂存换入(staged install)helper —— boot 与 prebuilt 共用。

引擎运行中安装新预编译包时,原子换入的整目录 rename(build_prebuilt → .old)
在 WSL /mnt、Windows 语义文件系统上会因「进程加载中的 .so 锁住目录」而 EACCES
(纯 ext4 上 rename 打开中的文件是允许的,无此问题)。此时降级:
新版本暂存为 ``build_prebuilt.pending``,由 boot 在**进程启动最早期**
(尚未加载任何 .so,无占用)调用 ``finalize_pending`` 完成换入 ——
预编译包本就需要重启才生效,时机完全契合。

独立成零依赖小模块的原因:完成换入必须发生在 boot 预加载 .so **之前**,
而 boot 不能 import prebuilt(prebuilt 顶部 import boot,会循环);
两边共用同一套路径与状态机,只能放在这个不 import 任何插件模块的文件里。
"""

from __future__ import annotations

import os
import shutil

_PENDING_SUFFIX = '.pending'
_OLD_SUFFIX = '.old'


def pending_dir(prebuilt_dir: str) -> str:
    return prebuilt_dir + _PENDING_SUFFIX


def stage_pending(staging_dir: str, prebuilt_dir: str) -> None:
    """把已通过校验的 staging 暂存为 pending(覆盖旧暂存)。

    staging 与 build_prebuilt 同在插件目录下(同盘),rename 瞬时完成;
    万一跨设备(EXDEV)退化为 move 拷贝。"""
    pend = pending_dir(prebuilt_dir)
    shutil.rmtree(pend, ignore_errors=True)
    try:
        os.rename(staging_dir, pend)
    except OSError:
        shutil.move(staging_dir, pend)


def finalize_pending(prebuilt_dir: str) -> tuple[bool, str]:
    """若存在 pending 暂存,完成 build_prebuilt/ 换入;返回 ``(ok, msg)``。

    无暂存时返回 ``(True, '')``,顺手清理上次换入残留的 ``.old``。
    任何一步失败都不破坏现有 build_prebuilt(先挪旧、失败回滚),留待下次
    启动重试 —— 调用方(boot)只 log,不中断启动。"""
    pend = pending_dir(prebuilt_dir)
    old = prebuilt_dir + _OLD_SUFFIX
    if not os.path.isdir(pend):
        shutil.rmtree(old, ignore_errors=True)
        return True, ''
    shutil.rmtree(old, ignore_errors=True)
    moved_old = False
    if os.path.isdir(prebuilt_dir):
        try:
            os.rename(prebuilt_dir, old)
            moved_old = True
        except OSError as e:
            return False, f'暂存的预编译包换入失败(build_prebuilt 仍被占用?): {e};保留现状,下次启动重试'
    try:
        os.rename(pend, prebuilt_dir)
    except OSError as e:
        if moved_old:
            try:
                os.rename(old, prebuilt_dir)   # 回滚,保证 build_prebuilt 仍可用
            except OSError:
                pass
        return False, f'暂存的预编译包换入失败: {e};已回滚,下次启动重试'
    shutil.rmtree(old, ignore_errors=True)
    return True, '已完成暂存的预编译包安装(build_prebuilt.pending → build_prebuilt)'
