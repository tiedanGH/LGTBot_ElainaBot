#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""prebuilt 模块测试 —— asset 解析 / 匹配标注 / marker 切换 / manifest / 解压安装。

被测重点(纯逻辑 + 文件系统,网络部分不涉及):
  · _parse_asset 解析合法名 / 拒绝非法名
  · _annotate 标注 matches_local / is_latest / installed
  · set_mode / current_mode / active_mode_running 的 marker 读写(切预编译需目录在)
  · installed_manifest 读取
  · read_state / _write_state 往返
  · _safe_members zip-slip + 白名单防护
  · _extract_and_swap 端到端安装 + manifest sha256 校验(篡改即拒绝、不污染现有包)
  · self_check 结构
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile

import pytest

# conftest.py 已 inject 假 boot(PREBUILT_DIR/LOCAL_BUILD_DIR 已备),这里安全 import
from plugins.LGTBot_ElainaBot.mod import prebuilt


@pytest.fixture(autouse=True)
def _clean_prebuilt_fs():
    """每个测试前后清 build_prebuilt / staging / data/prebuilt,避免串扰。"""
    def _wipe():
        for p in (prebuilt.PREBUILT_DIR, prebuilt._STAGING_DIR,
                  prebuilt.PREBUILT_DIR + '.old', prebuilt._PREBUILT_DATA):
            shutil.rmtree(p, ignore_errors=True)
    _wipe()
    yield
    _wipe()


# ─────────────────────────────────────────────────────────────────────────
# _parse_asset
# ─────────────────────────────────────────────────────────────────────────

def test_parse_asset_valid():
    a = prebuilt._parse_asset({
        'name': 'lgtbot-ubuntu-24.04-py3.12-abc1234.zip',
        'size': 123, 'updated_at': '2026-07-01T00:00:00Z',
        'browser_download_url': 'https://x/y.zip',
    })
    assert a is not None
    assert a['os'] == 'ubuntu-24.04'
    assert a['python_tag'] == '3.12'
    assert a['sha'] == 'abc1234'
    assert a['url'] == 'https://x/y.zip'


def test_parse_asset_accepts_unknown_sha():
    # 打包机 git 不可用时 sha 段退化成字面量 unknown —— 仍要能解析出来,否则该档
    # 在列表里被静默丢弃(debian-12 容器里就踩过这个坑)。
    a = prebuilt._parse_asset({'name': 'lgtbot-debian-12-py3.11-unknown.zip'})
    assert a is not None
    assert a['os'] == 'debian-12' and a['python_tag'] == '3.11' and a['sha'] == 'unknown'


@pytest.mark.parametrize('name', [
    'lgtbot-ubuntu-24.04.zip',            # 缺 py / sha
    'lgtbot-debian-12-py3.11.zip',        # 缺 sha
    'something-else.zip',
    'lgtbot-ubuntu-24.04-py3.12-abc1234.tar.gz',
    'lgtbot-debian-12-py3.11-nope.zip',   # sha 段既非 hex 也非 unknown
])
def test_parse_asset_rejects_bad(name):
    assert prebuilt._parse_asset({'name': name}) is None


# ─────────────────────────────────────────────────────────────────────────
# _annotate
# ─────────────────────────────────────────────────────────────────────────

def test_annotate_matches_latest_installed(monkeypatch):
    monkeypatch.setattr(prebuilt, 'local_os_tag', lambda: 'debian-12')
    monkeypatch.setattr(prebuilt, 'local_python_tag', lambda: '3.11')
    monkeypatch.setattr(prebuilt, 'installed_manifest', lambda: {'bridge_sha': 'old4444'})

    assets = [
        {'os': 'debian-12', 'python_tag': '3.11', 'sha': 'new1111', 'updated_at': '2026-07-02T00:00:00Z'},
        {'os': 'debian-12', 'python_tag': '3.11', 'sha': 'old4444', 'updated_at': '2026-07-01T00:00:00Z'},
        {'os': 'ubuntu-24.04', 'python_tag': '3.12', 'sha': 'zzz9999', 'updated_at': '2026-07-02T00:00:00Z'},
    ]
    out = prebuilt._annotate(assets)
    # 本机 debian-12 / py3.11 → 前两个 match,第三个不 match
    assert out[0]['matches_local'] and out[1]['matches_local']
    assert not out[2]['matches_local']
    # 同 os 内 updated_at 最新者 is_latest(debian 的 new1111 / ubuntu 的 zzz9999)
    assert out[0]['is_latest'] and not out[1]['is_latest'] and out[2]['is_latest']
    # 已安装 sha == old4444
    assert out[1]['installed'] and not out[0]['installed']


def test_annotate_splits_os_and_py_match(monkeypatch):
    """系统与 Python 分别对比:os_match / py_match 独立,matches_local = 两者都中。
    前端据此细分「系统不匹配(红)/ Python 不匹配(橙)」及下载确认的完全 / 部分不匹配。"""
    monkeypatch.setattr(prebuilt, 'local_os_tag', lambda: 'debian-12')
    monkeypatch.setattr(prebuilt, 'local_python_tag', lambda: '3.11')
    monkeypatch.setattr(prebuilt, 'installed_manifest', lambda: None)
    assets = [
        {'os': 'debian-12',    'python_tag': '3.11', 'sha': 'a' * 7, 'updated_at': ''},  # 全匹配
        {'os': 'debian-12',    'python_tag': '3.10', 'sha': 'b' * 7, 'updated_at': ''},  # 仅 py 不匹配(部分)
        {'os': 'ubuntu-22.04', 'python_tag': '3.11', 'sha': 'c' * 7, 'updated_at': ''},  # 仅系统不匹配
        {'os': 'ubuntu-22.04', 'python_tag': '3.10', 'sha': 'd' * 7, 'updated_at': ''},  # 完全不匹配
    ]
    full, py_off, os_off, both_off = prebuilt._annotate(assets)
    assert full['os_match'] and full['py_match'] and full['matches_local']
    assert py_off['os_match'] and not py_off['py_match'] and not py_off['matches_local']
    assert not os_off['os_match'] and os_off['py_match'] and not os_off['matches_local']
    assert not both_off['os_match'] and not both_off['py_match']


def test_list_remote_sorts_newest_first(monkeypatch):
    """列表按 updated_at 倒序(新的在上);同批内按 os/py 稳定排序。"""
    monkeypatch.setattr(prebuilt, 'local_os_tag', lambda: 'debian-12')
    monkeypatch.setattr(prebuilt, 'local_python_tag', lambda: '3.11')
    monkeypatch.setattr(prebuilt, 'installed_manifest', lambda: None)
    fake_rel = {'assets': [
        {'name': 'lgtbot-ubuntu-22.04-py3.10-aaaaaaa.zip', 'size': 1,
         'updated_at': '2026-07-20T00:00:00Z', 'browser_download_url': 'u'},
        {'name': 'lgtbot-ubuntu-22.04-py3.10-bbbbbbb.zip', 'size': 1,
         'updated_at': '2026-07-24T00:00:00Z', 'browser_download_url': 'u'},
        {'name': 'lgtbot-debian-12-py3.11-ccccccc.zip', 'size': 1,
         'updated_at': '2026-07-22T00:00:00Z', 'browser_download_url': 'u'},
    ]}
    monkeypatch.setattr(prebuilt, '_api_get_json', lambda p: fake_rel)
    out = prebuilt.list_remote()
    assert out['success']
    assert [a['sha'][0] for a in out['assets']] == ['b', 'c', 'a']   # 新 → 旧


# ─────────────────────────────────────────────────────────────────────────
# marker 切换
# ─────────────────────────────────────────────────────────────────────────

def test_mode_defaults_to_local():
    assert prebuilt.current_mode() == 'local'


def test_set_mode_prebuilt_requires_dir():
    # build_prebuilt 不存在 → 拒绝切换
    r = prebuilt.set_mode(True)
    assert not r['success']
    assert prebuilt.current_mode() == 'local'


def test_set_mode_prebuilt_then_local():
    # 预编译「可用」以 build_prebuilt/build/ 存在为准(与 boot 判据一致),仅有空
    # build_prebuilt/ 不算 —— 故这里要建到 build/ 子目录才允许切换。
    os.makedirs(os.path.join(prebuilt.PREBUILT_DIR, 'build'), exist_ok=True)
    r = prebuilt.set_mode(True)
    assert r['success'] and prebuilt.current_mode() == 'prebuilt'
    r2 = prebuilt.set_mode(False)
    assert r2['success'] and prebuilt.current_mode() == 'local'


# ─────────────────────────────────────────────────────────────────────────
# manifest / state
# ─────────────────────────────────────────────────────────────────────────

def test_installed_manifest_missing_then_present():
    assert prebuilt.installed_manifest() is None
    os.makedirs(prebuilt.PREBUILT_DIR, exist_ok=True)
    with open(prebuilt.INSTALLED_MANIFEST, 'w', encoding='utf-8') as f:
        json.dump({'bridge_sha': 'deadbee'}, f)
    assert prebuilt.installed_manifest()['bridge_sha'] == 'deadbee'


def test_state_roundtrip():
    prebuilt._write_state(running=True, stage='download', progress=42)
    st = prebuilt.read_state()
    assert st['running'] is True and st['stage'] == 'download' and st['progress'] == 42


# ─────────────────────────────────────────────────────────────────────────
# 下载镜像:选择持久化 + 下载顺序偏好
# ─────────────────────────────────────────────────────────────────────────

def test_selected_mirror_roundtrip():
    assert prebuilt.get_selected_mirror() is None          # 未选
    prebuilt.set_selected_mirror('https://m.example/')
    assert prebuilt.get_selected_mirror() == 'https://m.example/'
    prebuilt.set_selected_mirror('')                       # 显式选直连
    assert prebuilt.get_selected_mirror() == ''


def test_download_mirror_order_prefers_selected(monkeypatch):
    monkeypatch.setattr(prebuilt, '_ranked_mirror_prefixes', lambda: ['', 'a', 'b'])
    # 参数优先
    assert prebuilt._download_mirror_order('b')[0] == 'b'
    # 无参时读 marker
    prebuilt.set_selected_mirror('a')
    order = prebuilt._download_mirror_order(None)
    assert order[0] == 'a' and set(order) == {'', 'a', 'b'}   # 选定排最前,其余保留不丢


def test_mode_info_shape():
    mi = prebuilt.mode_info()
    assert set(mi.keys()) == {'running', 'selected', 'prebuilt_installed'}
    assert mi['running'] in ('local', 'prebuilt')


def test_prebuilt_ready_requires_build_subdir():
    # 预编译包解压后保留 build/ 前缀 → 真正可用要 build_prebuilt/build/ 存在;
    # 仅有空的 build_prebuilt/ 不算(boot 会因缺 build/ 回落本地)。
    assert not prebuilt.prebuilt_ready()
    os.makedirs(prebuilt.PREBUILT_DIR, exist_ok=True)
    assert not prebuilt.prebuilt_ready()               # 只有壳目录,不算就绪
    os.makedirs(os.path.join(prebuilt.PREBUILT_DIR, 'build'), exist_ok=True)
    assert prebuilt.prebuilt_ready()                   # build/ 到位才就绪
    assert prebuilt.mode_info()['prebuilt_installed'] is True


def test_extract_then_ready_and_switchable(tmp_path):
    """端到端:装包 → build_prebuilt/build/ 就绪 → 允许切到预编译(路径布局对齐)。"""
    pkg = str(tmp_path / 'ok.zip')
    _build_pkg(pkg, {
        'LGTBot_ElainaBot.so': b'bridge',
        'build/libbot_core.so': b'core',
        'build/plugins/wordle/libgame.so': b'game',
    })
    prebuilt._extract_and_swap(pkg)
    assert prebuilt.prebuilt_ready()
    r = prebuilt.set_mode(True)
    assert r['success'] and prebuilt.current_mode() == 'prebuilt'


# ─────────────────────────────────────────────────────────────────────────
# zip 安全 + 解压安装
# ─────────────────────────────────────────────────────────────────────────

def _build_pkg(path: str, files: dict, *, tamper: bool = False) -> None:
    """造一个预编译包 zip:files={relpath: bytes},自动补 manifest(sha256)。
    tamper=True 时把 manifest 里第一个文件的 sha256 改错(模拟损坏下载)。"""
    manifest = {'os': 'test', 'python_tag': '3.11', 'boost': '', 'bridge_sha': 'abc1234',
                'submodule_sha': '', 'build_time': 't', 'files': []}
    for rel, content in files.items():
        digest = hashlib.sha256(content).hexdigest()
        manifest['files'].append({'path': rel, 'size': len(content), 'sha256': digest})
    if tamper and manifest['files']:
        manifest['files'][0]['sha256'] = '0' * 64
    with zipfile.ZipFile(path, 'w') as zf:
        for rel, content in files.items():
            zf.writestr(rel, content)
        zf.writestr('manifest.json', json.dumps(manifest))


def test_safe_members_rejects_traversal(tmp_path):
    bad = str(tmp_path / 'bad.zip')
    with zipfile.ZipFile(bad, 'w') as zf:
        zf.writestr('../evil.so', b'x')
    with zipfile.ZipFile(bad, 'r') as zf:
        with pytest.raises(ValueError):
            prebuilt._safe_members(zf)


def test_safe_members_rejects_unexpected_top(tmp_path):
    bad = str(tmp_path / 'bad2.zip')
    with zipfile.ZipFile(bad, 'w') as zf:
        zf.writestr('etc/passwd', b'x')          # 非 build/ 且非白名单文件
    with zipfile.ZipFile(bad, 'r') as zf:
        with pytest.raises(ValueError):
            prebuilt._safe_members(zf)


def test_extract_and_swap_installs(tmp_path):
    pkg = str(tmp_path / 'ok.zip')
    _build_pkg(pkg, {
        'LGTBot_ElainaBot.so': b'bridge-bytes',
        'build/libbot_core.so': b'core-bytes',
        'build/plugins/wordle/libgame.so': b'game-bytes',
        'build/config_runner': b'#!runner',
    })
    prebuilt._extract_and_swap(pkg)
    # build_prebuilt/ 被填充,关键文件到位
    assert os.path.isfile(os.path.join(prebuilt.PREBUILT_DIR, 'LGTBot_ElainaBot.so'))
    assert os.path.isfile(os.path.join(prebuilt.PREBUILT_DIR, 'build', 'libbot_core.so'))
    assert os.path.isfile(os.path.join(prebuilt.PREBUILT_DIR, 'build', 'plugins', 'wordle', 'libgame.so'))
    assert os.path.isfile(prebuilt.INSTALLED_MANIFEST)
    # staging 已清理
    assert not os.path.isdir(prebuilt._STAGING_DIR)


def test_extract_and_swap_rejects_tampered_sha(tmp_path):
    pkg = str(tmp_path / 'bad.zip')
    _build_pkg(pkg, {'build/libbot_core.so': b'core-bytes'}, tamper=True)
    with pytest.raises(ValueError):
        prebuilt._extract_and_swap(pkg)
    # 校验失败 → 不留下半成品 build_prebuilt/
    assert not os.path.isdir(prebuilt.PREBUILT_DIR)
    assert not os.path.isdir(prebuilt._STAGING_DIR)


def test_install_uploaded_success(tmp_path):
    """手动上传包:校验通过 → 装入 build_prebuilt/,临时文件删除,state=done。"""
    pkg = str(tmp_path / 'up.zip')
    _build_pkg(pkg, {'LGTBot_ElainaBot.so': b'up-bridge', 'build/libbot_core.so': b'up-core'})
    r = prebuilt.install_uploaded(pkg)
    assert r['success']
    assert os.path.isfile(os.path.join(prebuilt.PREBUILT_DIR, 'LGTBot_ElainaBot.so'))
    assert not os.path.exists(pkg)                      # 取完即删
    assert prebuilt.read_state().get('stage') == 'done'


def test_install_uploaded_tampered_rejected(tmp_path):
    """上传包 sha256 不符 → 拒绝安装,不留半成品,state=error。"""
    pkg = str(tmp_path / 'bad.zip')
    _build_pkg(pkg, {'build/libbot_core.so': b'x'}, tamper=True)
    r = prebuilt.install_uploaded(pkg)
    assert not r['success']
    assert not os.path.isdir(prebuilt.PREBUILT_DIR)
    assert prebuilt.read_state().get('stage') == 'error'


def test_extract_and_swap_replaces_existing(tmp_path):
    # 先装一版(含一个多余文件),再装新版 → 旧内容被整体替换
    os.makedirs(os.path.join(prebuilt.PREBUILT_DIR, 'build'), exist_ok=True)
    with open(os.path.join(prebuilt.PREBUILT_DIR, 'stale.txt'), 'w') as f:
        f.write('old')
    pkg = str(tmp_path / 'new.zip')
    _build_pkg(pkg, {'LGTBot_ElainaBot.so': b'v2', 'build/libbot_core.so': b'v2'})
    prebuilt._extract_and_swap(pkg)
    # 旧的 stale.txt 不应残留(整目录换新)
    assert not os.path.exists(os.path.join(prebuilt.PREBUILT_DIR, 'stale.txt'))
    with open(os.path.join(prebuilt.PREBUILT_DIR, 'LGTBot_ElainaBot.so'), 'rb') as f:
        assert f.read() == b'v2'


# ─────────────────────────────────────────────────────────────────────────
# self_check
# ─────────────────────────────────────────────────────────────────────────

# 该模块 import aiohttp,故用 importorskip 守卫。
def _dash():
    pytest.importorskip('aiohttp')
    from plugins.LGTBot_ElainaBot.mod.webui import page_dashboard
    return page_dashboard


def test_self_check_shape():
    sc = _dash().self_check()
    assert set(sc.keys()) == {'runtime', 'compile', 'mode'}
    assert all('optional_prebuilt' in c for c in sc['compile'])   # 编译依赖都标了「预编译可免」
    assert sc['mode']['running'] in ('local', 'prebuilt')


def test_self_check_runtime_static_fallback():
    """无构建产物(或无 ldd)时回退静态检查:runtime 含预编译产物实测的 4 个外部依赖;
    ldd 证实非运行时依赖的 Boost.System(header-only)/ glog(默认 OFF)/ gflags
    (--as-needed 丢弃)不再出现;Qt 图片渲染是唯一 warn 项。"""
    sc = _dash().self_check()
    rt_names = ' '.join(c['name'] for c in sc['runtime'])
    for kw in ('Boost.Python', 'libcurl 运行时', 'SQLite3 运行时', 'Protobuf 运行时'):
        assert kw in rt_names, f'{kw} 应在 runtime'
    for kw in ('Boost.System', 'glog', 'gflags'):
        assert kw not in rt_names, f'{kw} 不应在 runtime(ldd 实测非运行时依赖)'
    assert 'protoc' not in rt_names            # protoc 是编译期工具
    assert all('optional_prebuilt' not in c for c in sc['runtime'])
    warns = [c for c in sc['runtime'] if c.get('warn')]
    assert len(warns) == 1 and warns[0]['name'].startswith('图片渲染 Qt')
    comp_names = ' '.join(c['name'] for c in sc['compile'])
    assert 'protoc' in comp_names and 'CMake' in comp_names and '开发头' in comp_names
    assert 'Qt' not in comp_names              # Qt 从 compile 移到 runtime warn


def test_self_check_runtime_missing_reports_red(monkeypatch):
    """硬依赖缺失:ok=False 且无 warn(前端红点计严重异常),detail 可操作。"""
    pd = _dash()
    monkeypatch.setattr(pd, '_lib_present', lambda t: False)   # 所有 .so 都缺
    sc = pd.self_check()
    boost = next(c for c in sc['runtime'] if c['name'].startswith('Boost.Python'))
    assert boost['ok'] is False and not boost.get('warn')
    assert 'DEPLOY' in boost['detail'] and '预编译无需' not in boost['detail']


def test_self_check_qt_warn_item(monkeypatch):
    """Qt 图片渲染是 warn 级:缺失 ok=False + warn=True(前端黄点计警告不计异常);
    Qt5 WebKit / Qt6 WebEngine 任一存在即 ok。"""
    pd = _dash()
    monkeypatch.setattr(pd, '_lib_present', lambda t: t.startswith('libQt5WebKit'))
    qt = next(c for c in pd.self_check()['runtime'] if c.get('warn'))
    assert qt['ok'] and 'Qt5' in qt['detail']
    monkeypatch.setattr(pd, '_lib_present', lambda t: t == 'libQt6WebEngineCore')
    qt = next(c for c in pd.self_check()['runtime'] if c.get('warn'))
    assert qt['ok'] and 'Qt6' in qt['detail']
    monkeypatch.setattr(pd, '_lib_present', lambda t: False)
    qt = next(c for c in pd.self_check()['runtime'] if c.get('warn'))
    assert qt['ok'] is False and '仍可启动' in qt['detail']


def test_self_check_ldd_scan(monkeypatch):
    """有产物 + ldd 可用:逐项系统库按 ldd 实测缺失集判定(soname 精确匹配);
    引擎自有库(相互 NEEDED,ldd 裸跑必 not found)被排除,不误报为缺失。"""
    pd = _dash()
    from plugins.LGTBot_ElainaBot.mod import boot
    fake_so = os.path.join(boot.BUILD_DIR, 'libbot_core.so')
    open(fake_so, 'w').close()
    try:
        monkeypatch.setattr(pd.shutil, 'which',
                            lambda n: '/usr/bin/ldd' if n == 'ldd' else None)
        # 缺 boost_python(精确版本 soname)+ 自有库 libbot_core(应被排除)
        monkeypatch.setattr(pd, '_run_ldd',
                            lambda p: {'libboost_python310.so.1.74.0', 'libbot_core.so'})
        rt = pd.self_check()['runtime']
        boost = next(c for c in rt if c['name'] == 'Boost.Python 运行时')
        assert boost['ok'] is False and 'libboost_python310' in boost['detail']
        curl = next(c for c in rt if c['name'] == 'libcurl 运行时')      # 未缺 → 仍逐项列出且 ok
        assert curl['ok'] is True and 'ldd' in curl['detail']
        assert not any('libbot_core' in c['name'] for c in rt)          # 自有库被排除
        # 全部可解析 → 4 项系统库都 ok,Boost.Python 仍**逐项在列**(不折叠成汇总)
        monkeypatch.setattr(pd, '_run_ldd', lambda p: set())
        rt = pd.self_check()['runtime']
        boost = next(c for c in rt if c['name'] == 'Boost.Python 运行时')
        assert boost['ok'] is True
    finally:
        os.remove(fake_so)


def test_self_check_header_multiarch(monkeypatch, tmp_path):
    """curl.h 等在 Debian multiarch 路径(/usr/include/<triplet>/curl/curl.h)时也应检出,
    不因只查 /usr/include/curl/curl.h 而误报缺失。"""
    pd = _dash()
    inc = tmp_path / 'usr' / 'include'
    (inc / 'x86_64-linux-gnu' / 'curl').mkdir(parents=True)
    (inc / 'x86_64-linux-gnu' / 'curl' / 'curl.h').write_text('')
    # 把 _header_present 里硬编码的 /usr/include/ 前缀重定向到 tmp,验证 multiarch 分支
    real_glob = pd.glob.glob
    monkeypatch.setattr(pd.glob, 'glob',
                        lambda pat: real_glob(pat.replace('/usr/include/', str(inc) + '/')))
    monkeypatch.setattr(pd.os.path, 'isfile', lambda p: False)   # 非 multiarch 直查路径不存在
    assert pd._header_present('/usr/include/curl/curl.h') is True
