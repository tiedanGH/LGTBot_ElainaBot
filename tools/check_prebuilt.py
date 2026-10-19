#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预编译包自检 —— CI 打包后调用(prebuilt.yml),不通过就不发布。

按面板安装的方式用 zipfile 解压(还原不了符号链接),再站在用户机的角度检查:
  · 依赖:包内每个 ELF 在 ``LD_LIBRARY_PATH=<包>/build``(boot 给子进程设的值)下跑 ldd,
    找不到的库、或只能经烤进二进制的 RUNPATH(编译树)找到的库都算漏带 —— 用户机上没有编译树。
  · 导入:照 boot 的顺序 chdir 到 build/、两趟 RTLD_GLOBAL 预加载 ``build/lib*.so`` 再 import 桥接层,
    用 manifest 记录的 Python 版本。

编译树还在时,包里有、但 boot 预加载不到的库会被 RUNPATH 兜底而让导入检查误过,所以 CI 先挪走 build/ 再跑。

用法:
    python3 tools/check_prebuilt.py dist/lgtbot-<os>-py<X.Y>-<sha>.zip
    python3 tools/check_prebuilt.py <已解压的包目录>

退出码:0 通过;1 检查不通过;2 参数或环境问题。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

_LDD_RE = re.compile(r'^\s*(\S+)\s+=>\s+(?:(not found)|(\S+)\s+\(0x[0-9a-f]+\))')
_RUNPATH_RE = re.compile(r'\((?:RUNPATH|RPATH)\)\s+Library (?:runpath|rpath): \[(.*)\]')

# 照搬 boot 的加载顺序;argv[1] = 包根目录
_IMPORT_PROBE = r'''
import ctypes, glob, os, sys
pkg = sys.argv[1]
build = os.path.join(pkg, 'build')
os.chdir(build)
libs = sorted(glob.glob(os.path.join(build, 'lib*.so')))
for _ in range(2):
    for lib in libs:
        try:
            ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
        except OSError:
            pass
sys.setdlopenflags(os.RTLD_NOW | os.RTLD_GLOBAL)
sys.path.insert(0, pkg)
import LGTBot_ElainaBot as m
if not hasattr(m, 'start'):
    sys.exit('桥接层缺少 start,不是真正的 C++ 扩展')
'''


def elf_files(root: str) -> list[str]:
    found = []
    for d, _dirs, names in os.walk(root):
        for n in names:
            path = os.path.join(d, n)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            with open(path, 'rb') as f:
                if f.read(4) == b'\x7fELF':
                    found.append(path)
    return sorted(found)


def runpaths(elf: str) -> list[str]:
    """ELF 里烤进去的 RUNPATH / RPATH 目录;空项与 $ORIGIN 这类相对写法跳过。"""
    out = subprocess.run(['readelf', '-d', elf], capture_output=True, text=True).stdout
    dirs = []
    for m in _RUNPATH_RE.finditer(out):
        dirs += [d for d in m.group(1).split(':') if d and '$' not in d]
    return dirs


def parse_ldd(text: str) -> list[tuple[str, str | None]]:
    """ldd 输出 → ``[(soname, 解析到的路径 | None)]``,None 表示 not found;没有 ``=>`` 的行(vdso、加载器)跳过。"""
    deps = []
    for line in text.splitlines():
        m = _LDD_RE.match(line)
        if m:
            deps.append((m.group(1), None if m.group(2) else m.group(3)))
    return deps


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def check_deps(pkg: str) -> tuple[int, dict[str, list[str]]]:
    """返回 ``(ELF 个数, {问题描述: [涉及的 ELF 相对路径]})``,问题为空即通过。"""
    pkg = os.path.realpath(pkg)
    elves = elf_files(pkg)
    build = os.path.join(pkg, 'build')
    tree_dirs = {os.path.realpath(d) for elf in elves for d in runpaths(elf)}
    env = dict(os.environ, LD_LIBRARY_PATH=build)
    problems: dict[str, list[str]] = {}
    for elf in elves:
        out = subprocess.run(['ldd', elf], capture_output=True, text=True, env=env, cwd=build).stdout
        for so, path in parse_ldd(out):
            if path is None:
                issue = f'找不到 {so}'
            else:
                real = os.path.realpath(path)
                if _under(real, pkg) or not any(_under(real, d) for d in tree_dirs):
                    continue
                issue = f'{so} 只能从编译树找到({os.path.dirname(path)}),包里没带'
            problems.setdefault(issue, []).append(os.path.relpath(elf, pkg))
    return len(elves), problems


def pick_python(tag: str) -> str | None:
    """manifest 的 python_tag 对应的解释器;tag 不是 X.Y(如 unknown)时用当前解释器,是 X.Y 但没装返回 None。"""
    if not re.fullmatch(r'3\.\d+', tag or ''):
        return sys.executable
    return shutil.which(f'python{tag}')


def check_import(pkg: str, python: str) -> str:
    """照 boot 的方式 import 桥接层,返回错误信息,空串表示成功。"""
    env = {k: v for k, v in os.environ.items() if k != 'LD_LIBRARY_PATH'}
    try:
        r = subprocess.run([python, '-I', '-c', _IMPORT_PROBE, os.path.realpath(pkg)],
                           capture_output=True, text=True, env=env, timeout=120)
    except subprocess.TimeoutExpired:
        return '导入超时'
    if r.returncode == 0:
        return ''
    lines = (r.stderr or r.stdout).strip().splitlines()
    return lines[-1] if lines else f'退出码 {r.returncode}'


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    src = argv[1]
    with tempfile.TemporaryDirectory(prefix='prebuilt-check-') as tmp:
        if os.path.isdir(src):
            pkg = os.path.realpath(src)
        elif zipfile.is_zipfile(src):
            pkg = os.path.realpath(tmp)
            with zipfile.ZipFile(src) as zf:
                zf.extractall(pkg)
        else:
            print(f'❌ 既不是 zip 也不是目录: {src}')
            return 2
        if not (os.path.isfile(os.path.join(pkg, 'LGTBot_ElainaBot.so'))
                and os.path.isdir(os.path.join(pkg, 'build'))):
            print('❌ 包结构不对:缺 LGTBot_ElainaBot.so 或 build/')
            return 1
        try:
            with open(os.path.join(pkg, 'manifest.json'), encoding='utf-8') as f:
                tag = json.load(f).get('python_tag', '')
        except (OSError, ValueError):
            tag = ''
        python = pick_python(tag)
        if python is None:
            print(f'❌ 包是 Python {tag} 的,本机没有 python{tag},无法做导入检查')
            return 2

        count, problems = check_deps(pkg)
        if problems:
            print(f'❌ 依赖检查:{len(problems)} 项问题')
            for issue, elves in sorted(problems.items()):
                print(f'   {issue} ← {", ".join(elves)}')
        else:
            print(f'✅ 依赖检查:{count} 个 ELF 的依赖都能解析')

        err = check_import(pkg, python)
        if err:
            print(f'❌ 导入检查({python}):{err}')
        else:
            print(f'✅ 导入检查({python}):桥接层可加载')
    return 1 if problems or err else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
