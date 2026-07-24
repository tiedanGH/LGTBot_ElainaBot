#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""滚动 prebuilt release 的批次清理 —— CI publish job 调用(prebuilt.yml)。

滚动 release 只保留**两个版本**的预编译包:本次上传的最新批 + 上一批,更早的批次删除。
「批」按 asset 名末段的 commit sha 分组(同一次构建的三个发行版包sha 相同,天然成批)。
重跑同一 commit 时新包与现存最新批**同名**(softprops clobber 覆盖、不新增批),
此时保留现存两批;否则只保留现存最新一批,上传后合计回到两批。不匹配预编译命名的 asset 一律不动;``sha=unknown`` 也算独立批。

用法(publish job,gh 已认证):
    gh release view prebuilt --repo "$REPO" --json assets > assets.json
    python3 tools/trim_prebuilt_release.py assets.json dist

参数:assets.json = ``gh release view --json assets`` 的输出;
     dist = 本次构建产物目录(从第一个 zip 名解析本批 sha)。
环境:``REPO``(owner/repo)。纯决策逻辑在 ``plan_trim``,pytest 有覆盖。
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys

# 与面板 prebuilt._ASSET_RE 的 sha 段一致:7-40 位 hex 或字面 unknown
_ASSET_SHA_RE = re.compile(r'-py[\d.]+-([0-9a-f]{7,40}|unknown)\.zip$')


def incoming_sha(dist_dir: str) -> str:
    """从 dist 目录第一个预编译 zip 的文件名解析本次构建的批次 sha。"""
    for path in sorted(glob.glob(os.path.join(dist_dir, '*.zip'))):
        m = _ASSET_SHA_RE.search(os.path.basename(path))
        if m:
            return m.group(1)
    raise SystemExit(f'dist 目录 {dist_dir!r} 里没有可识别的预编译 zip')


def plan_trim(assets: list, new_sha: str) -> list:
    """纯决策:给定现存 assets 与本次批 sha,返回应删除的 asset 名列表。

    目标:上传后 release 上恰好 ≤2 批。现存按批(sha)分组、按组内最新 createdAt 排序;
    本次 sha 已存在(重跑,同名覆盖不新增批)保留最新两组,否则保留最新一组。其余组的 asset 全部进删除列表。"""
    groups: dict = {}                      # sha -> {'latest': createdAt, 'names': [...]}
    for a in assets:
        m = _ASSET_SHA_RE.search(a.get('name', ''))
        if not m:
            continue                       # 不认识的 asset 一律不动
        g = groups.setdefault(m.group(1), {'latest': '', 'names': []})
        g['names'].append(a['name'])
        ts = a.get('createdAt') or ''
        if ts > g['latest']:
            g['latest'] = ts
    order = sorted(groups, key=lambda s: groups[s]['latest'], reverse=True)
    keep_n = 2 if new_sha in groups else 1
    keep = set(order[:keep_n])
    print(f'existing batches (new->old): {order}; keep: {sorted(keep)}; incoming: {new_sha}')
    return [name for sha in order if sha not in keep for name in groups[sha]['names']]


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(f'用法: {sys.argv[0]} <assets.json> <dist_dir>')
    with open(sys.argv[1], encoding='utf-8') as f:
        assets = json.load(f).get('assets') or []
    repo = os.environ['REPO']
    for name in plan_trim(assets, incoming_sha(sys.argv[2])):
        print('delete stale asset:', name)
        subprocess.run(['gh', 'release', 'delete-asset', 'prebuilt', name,
                        '--repo', repo, '-y'], check=False)


if __name__ == '__main__':
    main()
