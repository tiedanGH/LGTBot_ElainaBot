#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""game_switch 测试 —— 停用名单 / 显示名缓存 / 游戏信息读取 / 启动前的视图目录 / 面板保存 / 按钮过滤。

每个用例在 tmp_path 里搭 ``build/plugins/<game>/`` 布局,把 boot 与 game_switch 的路径换过去。
视图目录要建符号链接,当前平台建不了(Windows 无权限)时这几条跳过;读真 libgame.so 的那条用 cc 现编,Linux only。
"""

from __future__ import annotations

import html as _html
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

from plugins.LGTBot_ElainaBot.mod import boot, buttons, game_switch


def _can_symlink() -> bool:
    d = tempfile.mkdtemp()
    try:
        os.symlink(d, os.path.join(d, 'link'))
        return True
    except OSError:
        return False
    finally:
        shutil.rmtree(d, ignore_errors=True)


needs_symlink = pytest.mark.skipif(not _can_symlink(), reason='当前平台不能建符号链接')


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """``<tmp>/build/plugins`` + ``<tmp>/data``,路径常量全部指过来;返回 plugins 目录。"""
    build = tmp_path / 'build'
    plugins = build / 'plugins'
    plugins.mkdir(parents=True)
    monkeypatch.setattr(boot, 'BUILD_DIR', str(build))
    monkeypatch.setattr(boot, 'GAME_PATH', str(plugins))
    monkeypatch.setattr(game_switch, 'VIEW_DIR', str(build / 'plugins_enabled'))
    monkeypatch.setattr(game_switch, 'DISABLED_PATH', str(tmp_path / 'data' / 'games_disabled.json'))
    monkeypatch.setattr(game_switch, '_info_cache', {'key': None, 'games': []})
    monkeypatch.setattr(game_switch, '_names_cache', (None, frozenset()))
    monkeypatch.setitem(boot._get_persistent(), game_switch._APPLIED_KEY, None)
    # 默认名单单独测,其余用例从空名单起步
    monkeypatch.setattr(game_switch, 'DEFAULT_DISABLED', {})
    return plugins


def _game(plugins, module: str, lib: bytes = b'ELF') -> None:
    """一个游戏目录:libgame.so + icon.png + resource/。"""
    d = plugins / module
    (d / 'resource').mkdir(parents=True)
    (d / 'libgame.so').write_bytes(lib)
    (d / 'icon.png').write_bytes(b'icon-' + module.encode())
    (d / 'resource' / 'bg.png').write_bytes(b'bg-' + module.encode())


def _fake_probe(monkeypatch, names: dict) -> list:
    """把子进程读取换成按模块名查表,返回调用记录。"""
    calls = []

    def probe(libs):
        calls.append(libs)
        return [{'name': names.get(os.path.basename(os.path.dirname(p)), ''),
                 'developer': '铁蛋', 'description': '简介'} for p in libs]
    monkeypatch.setattr(game_switch, '_probe', probe)
    return calls


# ──────── 停用名单 ────────────────────────────────────────────────────────

def test_load_disabled_formats(layout):
    assert game_switch.load_disabled() == {}
    os.makedirs(os.path.dirname(game_switch.DISABLED_PATH))
    path = game_switch.DISABLED_PATH
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'lie': 'LIE', 'avalon': None, 'num': 5, '': 'x'}, f)
    assert game_switch.load_disabled() == {'lie': 'LIE', 'avalon': '', 'num': ''}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(['lie', '', 7], f)
    assert game_switch.load_disabled() == {'lie': ''}
    with open(path, 'w', encoding='utf-8') as f:
        f.write('{坏掉的 json')
    assert game_switch.load_disabled() == {}


def test_disabled_names_follow_the_file(layout):
    """拦截用的显示名随文件走:保存后立刻生效,显示名为空的不参与比对。"""
    assert game_switch.disabled_names() == frozenset()
    game_switch.save_disabled({'lie': 'LIE', 'ghost': ''})
    assert game_switch.disabled_names() == {'LIE'}
    game_switch.save_disabled({'lie': 'LIE', 'avalon': '阿瓦隆'})
    assert game_switch.disabled_names() == {'LIE', '阿瓦隆'}
    os.remove(game_switch.DISABLED_PATH)
    assert game_switch.disabled_names() == frozenset()


def test_missing_file_falls_back_to_defaults(layout, monkeypatch):
    """名单文件还不存在时按默认名单生效;文件一旦存在,哪怕是空名单也以文件为准。"""
    monkeypatch.setattr(game_switch, 'DEFAULT_DISABLED', {'lie': 'LIE', 'ghost': '幽灵'})
    loaded = game_switch.load_disabled()
    assert loaded == {'lie': 'LIE', 'ghost': '幽灵'}
    loaded['avalon'] = '阿瓦隆'
    assert 'avalon' not in game_switch.DEFAULT_DISABLED
    assert game_switch.disabled_names() == {'LIE', '幽灵'}

    _game(layout, 'alchemist')
    _game(layout, 'lie')
    _fake_probe(monkeypatch, {'alchemist': '炼金术士', 'lie': 'LIE'})
    assert game_switch.panel_state()['disabled'] == ['lie']
    # 文件还没写出来就在面板里保存:当前构建里没有的默认项照旧保留
    ok, _msg, changes = game_switch.save_selection([])
    assert ok and changes == {'disabled': [], 'enabled': ['lie']}
    assert game_switch.load_disabled() == {'ghost': '幽灵'}

    game_switch.save_disabled({})
    assert game_switch.load_disabled() == {} and game_switch.disabled_names() == frozenset()


def test_first_engine_start_writes_the_defaults(layout, monkeypatch):
    """首次启动引擎时把默认名单写成文件;之后在面板里全部恢复,再启动也不会重新套用。"""
    monkeypatch.setattr(game_switch, 'DEFAULT_DISABLED', {'lie': 'LIE'})
    for m in ('alchemist', 'lie'):
        _game(layout, m)
    built = []
    monkeypatch.setattr(game_switch, '_build_view', lambda src, enabled: built.append(enabled))
    assert game_switch.prepare_game_path() == game_switch.VIEW_DIR and built == [['alchemist']]
    with open(game_switch.DISABLED_PATH, encoding='utf-8') as f:
        assert json.load(f) == {'lie': 'LIE'}

    game_switch.save_disabled({})
    assert game_switch.prepare_game_path() == str(layout) and len(built) == 1
    assert game_switch.load_disabled() == {}


def test_defaults_apply_even_if_they_cannot_be_written(layout, monkeypatch):
    monkeypatch.setattr(game_switch, 'DEFAULT_DISABLED', {'lie': 'LIE'})
    for m in ('alchemist', 'lie'):
        _game(layout, m)

    def read_only(mapping):
        raise OSError('read-only file system')
    monkeypatch.setattr(game_switch, 'save_disabled', read_only)
    built = []
    monkeypatch.setattr(game_switch, '_build_view', lambda src, enabled: built.append(enabled))
    assert game_switch.prepare_game_path() == game_switch.VIEW_DIR and built == [['alchemist']]
    assert not os.path.exists(game_switch.DISABLED_PATH)


# ──────── 游戏信息 ────────────────────────────────────────────────────────

def test_list_games_reads_once_until_a_lib_changes(layout, monkeypatch):
    _game(layout, 'lie')
    _game(layout, 'avalon')
    (layout / 'not_a_game').mkdir()
    calls = _fake_probe(monkeypatch, {'lie': 'LIE', 'avalon': '阿瓦隆'})
    games = game_switch.list_games()
    assert [(g['module'], g['name']) for g in games] == [('avalon', '阿瓦隆'), ('lie', 'LIE')]
    assert game_switch.list_games() == games and len(calls) == 1
    (layout / 'lie' / 'libgame.so').write_bytes(b'ELF-rebuilt')
    game_switch.list_games()
    assert len(calls) == 2


def test_probe_parses_records_and_fills_the_gaps(layout, monkeypatch):
    """游戏库往 stdout 打的东西不影响解析;子进程没读到的、超时剩下的都记成错误。"""
    def run(cmd, **kw):
        assert cmd[:3] == [sys.executable, '-I', '-c']
        assert kw['env']['LD_LIBRARY_PATH'].split(os.pathsep)[0] == boot.BUILD_DIR
        out = ('game says hi\x1e{"name": "LIE", "developer": "a", "description": "b"}\n'
               'noise\x1e{"error": "dlopen failed"}\n')
        return subprocess.CompletedProcess(cmd, -11, out, '')
    monkeypatch.setattr(game_switch.subprocess, 'run', run)
    recs = game_switch._probe(['a', 'b', 'c'])
    assert recs[0]['name'] == 'LIE' and recs[1] == {'error': 'dlopen failed'}
    assert 'error' in recs[2]

    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 30, output=b'\x1e{"name": "X"}\n')
    monkeypatch.setattr(game_switch.subprocess, 'run', timeout)
    recs = game_switch._probe(['a', 'b'])
    assert recs[0] == {'name': 'X'} and 'error' in recs[1]
    assert game_switch._probe([]) == []


# ──────── 启动前的视图目录 ────────────────────────────────────────────────

def test_view_sits_where_the_bridge_expects_it():
    """桥接层从 game_path 反推 config_runner(上一级目录)和崩溃转储目录(找 /build/plugins)。"""
    view = game_switch.VIEW_DIR
    assert os.path.dirname(view) == os.path.dirname(boot.GAME_PATH) == boot.BUILD_DIR
    assert '/build/plugins' in view.replace(os.sep, '/')


@needs_symlink
def test_nothing_disabled_passes_plugins_and_drops_the_old_view(layout):
    _game(layout, 'lie')
    os.makedirs(os.path.join(game_switch.VIEW_DIR, 'stale'))
    assert game_switch.prepare_game_path() == str(layout)
    assert not os.path.lexists(game_switch.VIEW_DIR)
    assert boot._get_persistent()[game_switch._APPLIED_KEY] == []


@needs_symlink
def test_view_mirrors_enabled_games_with_real_dirs(layout):
    """引擎只认真实目录:每个启用的游戏一层真实目录,里面逐项链回原目录;停用的不出现。"""
    for m in ('alchemist', 'avalon', 'lie'):
        _game(layout, m)
    game_switch.save_disabled({'avalon': '阿瓦隆'})
    assert game_switch.prepare_game_path() == game_switch.VIEW_DIR
    view = game_switch.VIEW_DIR
    assert sorted(os.listdir(view)) == ['alchemist', 'lie']
    for m in ('alchemist', 'lie'):
        d = os.path.join(view, m)
        assert os.path.isdir(d) and not os.path.islink(d)
        assert sorted(os.listdir(d)) == ['icon.png', 'libgame.so', 'resource']
        for entry in os.listdir(d):
            assert os.path.islink(os.path.join(d, entry))
        with open(os.path.join(d, 'resource', 'bg.png'), 'rb') as f:
            assert f.read() == b'bg-' + m.encode()
    assert boot._get_persistent()[game_switch._APPLIED_KEY] == ['avalon']
    assert not os.path.lexists(view + '.tmp') and not os.path.lexists(view + '.old')


@needs_symlink
def test_rebuilding_or_dropping_the_view_keeps_the_real_games(layout):
    """视图里全是符号链接,重建、清掉它都不能删到原游戏目录。"""
    for m in ('alchemist', 'lie'):
        _game(layout, m)
    game_switch.save_disabled({'lie': 'LIE'})
    game_switch.prepare_game_path()
    game_switch.save_disabled({'alchemist': '炼金术士'})
    game_switch.prepare_game_path()
    assert os.listdir(game_switch.VIEW_DIR) == ['lie']
    game_switch.save_disabled({})
    game_switch.prepare_game_path()
    for m in ('alchemist', 'lie'):
        assert (layout / m / 'libgame.so').read_bytes() == b'ELF'
        assert (layout / m / 'resource' / 'bg.png').read_bytes() == b'bg-' + m.encode()


def test_all_or_unknown_disabled_loads_everything(layout):
    """全部停用会让引擎找不到游戏;名单里只有当前构建没有的游戏 —— 两种都全量加载。"""
    _game(layout, 'lie')
    game_switch.save_disabled({'lie': 'LIE'})
    assert game_switch.prepare_game_path() == str(layout)
    game_switch.save_disabled({'ghost': ''})
    assert game_switch.prepare_game_path() == str(layout)
    assert not os.path.lexists(game_switch.VIEW_DIR)
    assert boot._get_persistent()[game_switch._APPLIED_KEY] == []


def test_view_falls_back_when_symlinks_fail(layout, monkeypatch):
    for m in ('alchemist', 'lie'):
        _game(layout, m)
    game_switch.save_disabled({'lie': 'LIE'})

    def no_symlink(*a, **k):
        raise OSError('symlinks not supported')
    monkeypatch.setattr(game_switch.os, 'symlink', no_symlink)
    assert game_switch.prepare_game_path() == str(layout)
    for p in (game_switch.VIEW_DIR, game_switch.VIEW_DIR + '.tmp', game_switch.VIEW_DIR + '.old'):
        assert not os.path.lexists(p)
    assert boot._get_persistent()[game_switch._APPLIED_KEY] == []


def test_applied_is_reported_only_while_the_engine_runs(layout, monkeypatch):
    boot._get_persistent()[game_switch._APPLIED_KEY] = ['lie']
    assert game_switch.applied_disabled() is None
    monkeypatch.setattr(boot, 'is_engine_running', lambda: True)
    assert game_switch.applied_disabled() == ['lie']


# ──────── 面板保存 ────────────────────────────────────────────────────────

def test_save_selection_rules(layout, monkeypatch):
    for m in ('alchemist', 'avalon', 'lie'):
        _game(layout, m)
    _fake_probe(monkeypatch, {'alchemist': '炼金术士', 'avalon': '阿瓦隆', 'lie': ''})
    ok, msg, _ = game_switch.save_selection(['alchemist', 'avalon', 'lie'])
    assert not ok and '至少保留' in msg and game_switch.load_disabled() == {}

    # 换编译来源前停用的 ghost 不在当前构建里:原样保留;新提交的未知模块忽略
    game_switch.save_disabled({'ghost': '幽灵'})
    ok, _msg, changes = game_switch.save_selection(['avalon', 'lie', 'nobody'])
    assert ok and changes == {'disabled': ['avalon', 'lie'], 'enabled': []}
    assert game_switch.load_disabled() == {'avalon': '阿瓦隆', 'ghost': '幽灵', 'lie': ''}

    mtime = os.stat(game_switch.DISABLED_PATH).st_mtime_ns
    ok, msg, changes = game_switch.save_selection(['lie', 'avalon'])
    assert ok and '没有变化' in msg and changes == {'disabled': [], 'enabled': []}
    assert os.stat(game_switch.DISABLED_PATH).st_mtime_ns == mtime

    ok, _msg, changes = game_switch.save_selection(['lie'])
    assert ok and changes == {'disabled': [], 'enabled': ['avalon']}
    assert game_switch.load_disabled() == {'ghost': '幽灵', 'lie': ''}


def test_panel_state_lists_only_current_games(layout, monkeypatch):
    _game(layout, 'lie')
    _fake_probe(monkeypatch, {'lie': 'LIE'})
    game_switch.save_disabled({'lie': 'LIE', 'ghost': ''})
    st = game_switch.panel_state()
    assert [g['module'] for g in st['games']] == ['lie']
    assert st['disabled'] == ['lie'] and st['applied'] is None


def _pc():
    pytest.importorskip('aiohttp')
    from plugins.LGTBot_ElainaBot.mod.webui import page_config
    return page_config


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


async def test_save_handler_validates_and_audits(layout, monkeypatch):
    pc = _pc()
    for m in ('alchemist', 'lie'):
        _game(layout, m)
    _fake_probe(monkeypatch, {'alchemist': '炼金术士', 'lie': 'LIE'})
    audits = []
    monkeypatch.setattr(pc.audit, 'record', lambda *a, **k: audits.append((a, k)))

    for bad in (ValueError('not json'), {'disabled': 'lie'}, {'disabled': [1]}, ['lie']):
        resp = await pc.games_save_handler(_Req(bad))
        assert resp.status == 400 and json.loads(resp.text)['success'] is False
    assert audits == [] and game_switch.load_disabled() == {}

    resp = await pc.games_save_handler(_Req({'disabled': ['lie']}))
    body = json.loads(resp.text)
    assert body['success'] and body['disabled'] == ['lie'] and len(body['games']) == 2
    assert audits[-1][0][1:] == ('游戏开关', '停用 LIE')
    assert game_switch.disabled_names() == {'LIE'}

    resp = await pc.games_save_handler(_Req({'disabled': ['alchemist', 'lie']}))
    body = json.loads(resp.text)
    assert not body['success'] and '至少保留' in body['message']
    assert audits[-1][1].get('ok') is False
    assert game_switch.load_disabled() == {'lie': 'LIE'}

    n = len(audits)
    resp = await pc.games_save_handler(_Req({'disabled': ['lie']}))
    assert json.loads(resp.text)['success'] and len(audits) == n    # 没变化不记审计

    resp = await pc.games_save_handler(_Req({'disabled': []}))
    assert audits[-1][0][1:] == ('游戏开关', '恢复 LIE')


def test_list_fragment_carries_the_panel_state(layout, monkeypatch):
    pc = _pc()
    _game(layout, 'lie')
    _fake_probe(monkeypatch, {'lie': 'LIE'})
    html = pc.render_games_list()
    data = json.loads(_html.unescape(html[len('<pre id="result">'):-len('</pre>')]))
    assert data['success'] and data['games'][0]['name'] == 'LIE'

    def boom():
        raise RuntimeError('磁盘坏了')
    monkeypatch.setattr(game_switch, 'panel_state', boom)
    assert '读取游戏列表失败' in pc.render_games_list()


# ──────── 按钮 ────────────────────────────────────────────────────────────

def test_disabled_games_leave_the_menu_and_rematch(layout, monkeypatch):
    monkeypatch.setattr(buttons, 'MENU_GAMES', ['五子棋', 'LIE', '阿瓦隆'])
    game_switch.save_disabled({'lie': 'LIE'})
    rows = buttons.build_menu_buttons()
    labels = [b['text'] for row in rows for b in row]
    assert '五子棋' in labels and '阿瓦隆' in labels and 'LIE' not in labels

    assert buttons.build_game_over_buttons('LIE') == [[buttons.BTN_RECORD]]
    assert len(buttons.build_game_over_buttons('五子棋')[0]) == 2


# ──────── 读真 libgame.so(Linux only)─────────────────────────────────────

_FAKE_GAME_C = r'''
#include <stdbool.h>
#include <stdint.h>
typedef struct { const char *name_, *developer_, *description_; bool shuffled_player_id_; } GameProperties;
typedef struct { const void *data_; uint32_t size_; } Achievements;
typedef struct { uint32_t bench_; bool is_formal_; } Options;
typedef struct { const GameProperties *properties_; const char *module_name_, *rule_;
                 Achievements achievements_; Options default_generic_options_; } GameInfo;
static const GameProperties props = {"测试游戏", "铁蛋", "一段简介", false};
void GetGameInfo(GameInfo *out) {
    out->properties_ = &props; out->module_name_ = "demo"; out->rule_ = "";
    out->achievements_.data_ = 0; out->achievements_.size_ = 0;
    out->default_generic_options_.bench_ = 0; out->default_generic_options_.is_formal_ = true;
}
'''


@pytest.mark.skipif(not sys.platform.startswith('linux') or not shutil.which('cc'), reason='需要 Linux + cc')
def test_real_libgame_metadata(layout):
    src = layout.parent / 'game.c'
    src.write_text(_FAKE_GAME_C, encoding='utf-8')
    (layout / 'demo').mkdir()
    subprocess.run(['cc', '-shared', '-fPIC', str(src), '-o', str(layout / 'demo' / 'libgame.so')], check=True)
    (layout / 'broken').mkdir()
    (layout / 'broken' / 'libgame.so').write_bytes(b'not an elf')
    games = {g['module']: g for g in game_switch.list_games()}
    assert (games['demo']['name'], games['demo']['developer'], games['demo']['description']) == \
        ('测试游戏', '铁蛋', '一段简介')
    assert games['broken']['name'] == '' and games['broken']['error']
