#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/pack_prebuilt.sh + tools/check_prebuilt.py 测试。

端到端部分(Linux only,需 cc / zip / ldd / readelf / Python.h):用 cc 现编一棵迷你编译树 ——
桥接层是真的 CPython 扩展,链接 build/libbot_core.so;新版布局里 libbot_core 再链接装在 build/ 深层子目录、
带版本号符号链接链的 libreproc,match_game_runner 链接同样藏在深处的 libipc,和 CI 一样只靠 RUNPATH 找到。跑打包脚本后挪走编译树,
再由 check_prebuilt 按面板的方式解压、照 boot 的方式加载,有 / 无 reproc 两种布局都要能加载。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import sysconfig
import zipfile

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PACK = os.path.join(_REPO, 'tools', 'pack_prebuilt.sh')
_CHECK = os.path.join(_REPO, 'tools', 'check_prebuilt.py')
_REPROC_LIB = os.path.join('build', 'lgtbot', 'bot_core', 'reproc', 'reproc', 'lib')
_IPC_LIB = os.path.join('build', 'lgtbot', 'bot_core', 'ipc')


def _load_check():
    spec = importlib.util.spec_from_file_location('check_prebuilt', _CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_ldd_lines():
    text = ('\tlinux-vdso.so.1 (0x00007ffd5a3e2000)\n'
            '\tlibbot_core.so => /pkg/build/libbot_core.so (0x00007f0000000000)\n'
            '\tlibreproc.so.14 => not found\n'
            '\t/lib64/ld-linux-x86-64.so.2 (0x00007f0000100000)\n')
    assert _load_check().parse_ldd(text) == [
        ('libbot_core.so', '/pkg/build/libbot_core.so'),
        ('libreproc.so.14', None),
    ]


def test_pick_python():
    check = _load_check()
    assert check.pick_python('unknown') == sys.executable
    assert check.pick_python('') == sys.executable
    assert check.pick_python('3.99') is None


def _zip_with(path, members: dict) -> str:
    with zipfile.ZipFile(path, 'w') as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return str(path)


def test_check_main_rejects_bad_input(tmp_path, capsys):
    """非 zip、缺桥接层、本机没有包对应的 Python:都在跑 ldd 之前就拒绝。"""
    check = _load_check()
    junk = tmp_path / 'junk.txt'
    junk.write_text('x')
    assert check.main(['check', str(junk)]) == 2
    no_bridge = _zip_with(tmp_path / 'a.zip', {'build/libbot_core.so': b'x'})
    assert check.main(['check', no_bridge]) == 1
    assert '包结构不对' in capsys.readouterr().out
    py399 = _zip_with(tmp_path / 'b.zip', {'LGTBot_ElainaBot.so': b'x', 'build/libbot_core.so': b'x',
                                          'manifest.json': '{"python_tag": "3.99"}'})
    assert check.main(['check', py399]) == 2
    assert 'python3.99' in capsys.readouterr().out


# ──────── 端到端:迷你编译树 ─────────────────────────────────────────────────

def _toolchain_missing() -> str:
    if not sys.platform.startswith('linux'):
        return '仅 Linux'
    for tool in ('cc', 'zip', 'ldd', 'readelf', 'bash'):
        if not shutil.which(tool):
            return f'缺 {tool}'
    if not os.path.isfile(os.path.join(sysconfig.get_paths()['include'], 'Python.h')):
        return '缺 Python.h'
    return ''


e2e = pytest.mark.skipif(bool(_toolchain_missing()), reason=_toolchain_missing() or 'ok')

_BRIDGE_C = r'''
#include <Python.h>
int bot_core_answer(void);
static PyObject *start(PyObject *self, PyObject *args) { return PyLong_FromLong(bot_core_answer()); }
static PyMethodDef methods[] = {{"start", start, METH_NOARGS, NULL}, {NULL, NULL, 0, NULL}};
static PyMethodDef no_methods[] = {{NULL, NULL, 0, NULL}};
static struct PyModuleDef mod = {PyModuleDef_HEAD_INIT, "LGTBot_ElainaBot", NULL, -1, BRIDGE_METHODS};
PyMODINIT_FUNC PyInit_LGTBot_ElainaBot(void) { return PyModule_Create(&mod); }
'''


def _cc(cwd, *args):
    subprocess.run(['cc', '-Wl,--enable-new-dtags', *args], cwd=cwd, check=True)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def _versioned_lib(lib_dir, stem, soname_ver, full_ver, src) -> None:
    """编出 <stem>.so.<full_ver>(soname <stem>.so.<soname_ver>)+ 两级符号链接,同 CMake 的 VERSION / SOVERSION。"""
    _write(os.path.join(lib_dir, f'{stem}.c'), src)
    _cc(lib_dir, '-shared', '-fPIC', f'-Wl,-soname,{stem}.so.{soname_ver}', f'{stem}.c',
        '-o', f'{stem}.so.{full_ver}')
    os.symlink(f'{stem}.so.{full_ver}', os.path.join(lib_dir, f'{stem}.so.{soname_ver}'))
    os.symlink(f'{stem}.so.{soname_ver}', os.path.join(lib_dir, f'{stem}.so'))


def _make_tree(root, reproc: bool, with_start: bool = True) -> None:
    """在 root 下造一棵迷你编译树 + tools/ 副本(打包脚本按自身位置定位仓库根)。"""
    build = os.path.join(root, 'build')
    os.makedirs(os.path.join(root, 'tools'))
    shutil.copy(_PACK, os.path.join(root, 'tools'))
    shutil.copy(_CHECK, os.path.join(root, 'tools'))
    _write(os.path.join(build, 'main.c'), 'int main(void) { return 0; }\n')
    rpath = [build]
    core_args = []
    runner_args = ['main.c']
    if reproc:
        lib = os.path.join(root, _REPROC_LIB)
        _versioned_lib(lib, 'libreproc', '14', '14.2.5', 'int reproc_answer(void) { return 41; }\n')
        _write(os.path.join(build, 'core.c'),
               'int reproc_answer(void);\nint bot_core_answer(void) { return reproc_answer() + 1; }\n')
        core_args = [f'-L{lib}', '-lreproc', f'-Wl,-rpath,{lib}']
        rpath.append(lib)
        ipc = os.path.join(root, _IPC_LIB)
        _versioned_lib(ipc, 'libipc', '3', '3.0.1', 'int ipc_answer(void) { return 3; }\n')
        _write(os.path.join(build, 'runner.c'), 'int ipc_answer(void);\nint main(void) { return ipc_answer() - 3; }\n')
        runner_args = ['runner.c', f'-L{ipc}', '-lipc', f'-Wl,-rpath,{ipc}']
    else:
        _write(os.path.join(build, 'core.c'), 'int bot_core_answer(void) { return 42; }\n')
    _cc(build, '-shared', '-fPIC', '-Wl,-soname,libbot_core.so', 'core.c', '-o', 'libbot_core.so', *core_args)
    for exe in ('markdown2image', 'config_runner'):
        _cc(build, 'main.c', '-o', exe)
    _cc(build, *runner_args, '-o', 'match_game_runner')
    game = os.path.join(build, 'plugins', 'demo')
    _write(os.path.join(game, 'game.c'), 'int game_answer(void) { return 1; }\n')
    _cc(game, '-shared', '-fPIC', 'game.c', '-o', 'libgame.so')
    methods = 'methods' if with_start else 'no_methods'
    _write(os.path.join(root, 'bridge.c'), _BRIDGE_C.replace('BRIDGE_METHODS', methods))
    _cc(root, '-shared', '-fPIC', f'-I{sysconfig.get_paths()["include"]}', 'bridge.c',
        '-o', 'LGTBot_ElainaBot.so', f'-L{build}', '-lbot_core', f'-Wl,-rpath,{":".join(rpath)}')


def _pack(root) -> str:
    r = subprocess.run(['bash', os.path.join(root, 'tools', 'pack_prebuilt.sh'), 'test-os'],
                       cwd=root, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    (name,) = os.listdir(os.path.join(root, 'dist'))
    return os.path.join(root, 'dist', name)


def _check(target) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, _CHECK, target], capture_output=True, text=True)


def _hide_build(root) -> None:
    """模拟用户机:编译树不在了,烤进二进制的 RUNPATH 全部落空。"""
    os.rename(os.path.join(root, 'build'), os.path.join(root, 'build.hidden'))


def _drop(zip_path, *names) -> None:
    """从包里剔除指定成员,模拟漏带库的旧包。"""
    with zipfile.ZipFile(zip_path) as zf:
        items = [(i, zf.read(i)) for i in zf.infolist() if i.filename not in names]
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for info, data in items:
            zf.writestr(info, data)


@e2e
def test_pack_bundles_nested_build_libs(tmp_path):
    """共享库(libbot_core → libreproc)和可执行文件(runner → libipc)链接的深层库,整条符号链接链都收进 build/ 顶层。"""
    _make_tree(str(tmp_path), reproc=True)
    with zipfile.ZipFile(_pack(str(tmp_path))) as zf:
        names = set(zf.namelist())
        assert {'build/libreproc.so', 'build/libreproc.so.14', 'build/libreproc.so.14.2.5'} <= names
        assert {'build/libipc.so', 'build/libipc.so.3', 'build/libipc.so.3.0.1'} <= names
        # Python 的 extractall 还原不了符号链接:包里必须是解引用后的真实文件
        assert zf.read('build/libreproc.so') == zf.read('build/libreproc.so.14.2.5')
        assert not any(n.startswith('build/lgtbot/') for n in names)


@e2e
@pytest.mark.parametrize('reproc', [True, False], ids=['with-reproc', 'without-reproc'])
def test_packed_prebuilt_loads_without_build_tree(tmp_path, reproc):
    root = str(tmp_path)
    _make_tree(root, reproc=reproc)
    zip_path = _pack(root)
    _hide_build(root)
    r = _check(zip_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert '✅ 依赖检查' in r.stdout and '✅ 导入检查' in r.stdout


@e2e
def test_check_flags_lib_left_in_build_tree(tmp_path):
    """包里漏带 libreproc:编译树还在时由 RUNPATH 检查报出,挪走后 ldd 找不到、导入也失败。"""
    root = str(tmp_path)
    _make_tree(root, reproc=True)
    zip_path = _pack(root)
    _drop(zip_path, 'build/libreproc.so', 'build/libreproc.so.14', 'build/libreproc.so.14.2.5')
    r = _check(zip_path)
    assert r.returncode == 1
    assert 'libreproc.so.14 只能从编译树找到' in r.stdout
    _hide_build(root)
    r = _check(zip_path)
    assert r.returncode == 1
    assert '找不到 libreproc.so.14' in r.stdout and '❌ 导入检查' in r.stdout


@e2e
def test_check_needs_unversioned_name_for_boot_preload(tmp_path):
    """只带 libreproc.so.14:子进程靠 LD_LIBRARY_PATH 找得到,但 boot 只预加载 lib*.so,主进程导入失败。"""
    root = str(tmp_path)
    _make_tree(root, reproc=True)
    zip_path = _pack(root)
    _drop(zip_path, 'build/libreproc.so')
    _hide_build(root)
    r = _check(zip_path)
    assert r.returncode == 1
    assert '✅ 依赖检查' in r.stdout and '❌ 导入检查' in r.stdout


@e2e
def test_check_rejects_bridge_without_start(tmp_path):
    root = str(tmp_path)
    _make_tree(root, reproc=False, with_start=False)
    zip_path = _pack(root)
    _hide_build(root)
    r = _check(zip_path)
    assert r.returncode == 1
    assert '缺少 start' in r.stdout


@e2e
def test_check_accepts_extracted_dir(tmp_path):
    root = str(tmp_path)
    _make_tree(root, reproc=True)
    zip_path = _pack(root)
    _hide_build(root)
    pkg = os.path.join(root, 'pkg')
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(pkg)
    r = _check(pkg)
    assert r.returncode == 0, r.stdout + r.stderr
