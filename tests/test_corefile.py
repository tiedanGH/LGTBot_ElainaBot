#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""corefile.analyze 测试 —— 用**按格式合成**的 ELF64 core 验证 note 解析。

为什么是合成而非真 core:开发机的 WSL 是 WSL1(内核 4.4-Microsoft),没有
``/proc/sys/kernel/core_pattern`` 也不产生 core dump,拿不到真样本。合成件按
``fs/binfmt_elf.c`` 的 note 写法构造(NT_PRSTATUS / NT_PRPSINFO / NT_SIGINFO /
NT_FILE),覆盖的是解析代码本身:ELF 头 → PT_NOTE → note 迭代 → 各字段取值。

结构偏移写错的风险由被测代码自己兜:RIP 命中映射区间是自校验(偏移错了几乎
不可能落在任何区间内,于是只是不报模块、不会报错的模块);信号值也做了 0<sig<65 的合理性检查。
"""

from __future__ import annotations

import os
import struct

import pytest

from plugins.LGTBot_ElainaBot.mod import corefile


# ─────────────────────────────────────────────────────────────────────────
# 合成 core
# ─────────────────────────────────────────────────────────────────────────

def _note(ntype: int, desc: bytes, name: bytes = b'CORE\0') -> bytes:
    """一条 note:namesz / descsz / type + name(4 对齐)+ desc(4 对齐)。"""
    pad = lambda b: b + b'\0' * (-len(b) % 4)
    return struct.pack('<III', len(name), len(desc), ntype) + pad(name) + pad(desc)


def _prstatus(signo: int, rip: int) -> bytes:
    d = bytearray(336)
    struct.pack_into('<i', d, 0, signo)          # pr_info.si_signo
    struct.pack_into('<h', d, 12, signo)         # pr_cursig
    struct.pack_into('<Q', d, 112 + 16 * 8, rip)  # pr_reg[16] = rip
    return bytes(d)


def _prpsinfo(fname: bytes, psargs: bytes) -> bytes:
    d = bytearray(136)
    d[40:40 + len(fname)] = fname
    d[56:56 + len(psargs)] = psargs
    return bytes(d)


def _siginfo(signo: int, code: int, addr: int) -> bytes:
    d = bytearray(128)
    struct.pack_into('<i', d, 0, signo)
    struct.pack_into('<i', d, 8, code)
    struct.pack_into('<Q', d, 16, addr)
    return bytes(d)


def _nt_file(entries) -> bytes:
    """entries: [(start, end, path)]。"""
    body = struct.pack('<QQ', len(entries), 4096)
    for s, e, _p in entries:
        body += struct.pack('<QQQ', s, e, 0)
    for _s, _e, p in entries:
        body += p.encode() + b'\0'
    return body


def _make_core(path, notes: bytes, machine: int = 62, etype: int = 4,
               elfclass: int = 2) -> str:
    """拼一个只有 PT_NOTE 的最小 core:ELF64 头 + 1 个 phdr + note 段。"""
    ehsize, phentsize = 64, 56
    phoff = ehsize
    note_off = phoff + phentsize
    eh = bytearray(64)
    eh[0:4] = b'\x7fELF'
    eh[4] = elfclass
    eh[5] = 1                                     # little endian
    eh[6] = 1
    struct.pack_into('<HH', eh, 16, etype, machine)
    struct.pack_into('<I', eh, 20, 1)             # e_version
    struct.pack_into('<Q', eh, 32, phoff)         # e_phoff
    struct.pack_into('<HH', eh, 52, ehsize, phentsize)
    struct.pack_into('<H', eh, 56, 1)             # e_phnum
    ph = bytearray(56)
    struct.pack_into('<I', ph, 0, 4)              # PT_NOTE
    struct.pack_into('<Q', ph, 8, note_off)       # p_offset
    struct.pack_into('<Q', ph, 32, len(notes))    # p_filesz
    with open(path, 'wb') as f:
        f.write(bytes(eh) + bytes(ph) + notes)
    return str(path)


def _full_core(tmp_path, *, signo=11, rip=0x7F0000001234,
               game='lgtbot_hp', code=1, addr=0x1234) -> str:
    so = f'/opt/bot/build/plugins/{game}/libgame.so'
    maps = [(0x400000, 0x401000, '/opt/bot/build/match_game_runner'),
            (0x7F0000000000, 0x7F0000010000, so),
            (0x7F1000000000, 0x7F1000010000, '/opt/bot/build/libbot_core.so')]
    notes = (_prstatus(signo, rip)
             and _note(corefile._NT_PRSTATUS, _prstatus(signo, rip))
             + _note(corefile._NT_PRPSINFO,
                     _prpsinfo(b'match_game_runn',
                               f'/opt/bot/build/match_game_runner {so}'.encode()))
             + _note(corefile._NT_SIGINFO, _siginfo(signo, code, addr))
             + _note(corefile._NT_FILE, _nt_file(maps)))
    return _make_core(os.path.join(str(tmp_path), 'core-match_game_runn-42418-1786852068'),
                      notes)


# ─────────────────────────────────────────────────────────────────────────
# 正常解析
# ─────────────────────────────────────────────────────────────────────────

def test_analyze_extracts_signal_game_and_module(tmp_path):
    """★ 一个 core 能给出的全部信息:信号 + si_code 人话 + 出错地址 + 游戏名
    + 崩溃 PC 所在模块 + 可执行名 / 命令行。"""
    r = corefile.analyze(_full_core(tmp_path))
    assert r['ok'] is True and not r['error']
    assert r['signal'] == 11 and r['signal_name'] == 'SIGSEGV'
    assert 'SEGV_MAPERR' in r['signal_detail']
    assert r['fault_addr'] == 0x1234
    assert r['game'] == 'lgtbot_hp'               # 来自 NT_FILE 的完整路径
    assert r['crash_module'] == 'libgame.so'      # RIP 落在游戏 .so 区间内
    assert r['exe'] == 'match_game_runn'
    assert 'libgame.so' in r['cmdline']
    assert r['mapping_count'] == 3


def test_analyze_module_is_engine_core_when_rip_elsewhere(tmp_path):
    """崩在引擎核心库而非游戏 .so —— 模块要如实报 libbot_core.so。"""
    r = corefile.analyze(_full_core(tmp_path, rip=0x7F1000000500))
    assert r['crash_module'] == 'libbot_core.so'
    assert r['game'] == 'lgtbot_hp'               # 游戏名仍来自映射,不受 RIP 影响


def test_analyze_rip_outside_all_mappings_reports_no_module(tmp_path):
    """★ 自校验:RIP 不落在任何映射里(偏移算错就会这样)→ 只是不报模块,
    绝不报出一个错的。"""
    r = corefile.analyze(_full_core(tmp_path, rip=0xDEAD0000BEEF))
    assert r['ok'] is True
    assert r['crash_module'] == ''
    assert r['game'] == 'lgtbot_hp'


def test_analyze_game_from_nt_file_when_cmdline_truncated(tmp_path):
    """★ NT_FILE 是游戏名的**主来源**:内核把 pr_psargs 截断到 80 字节,真实部署路径
    一长就够不到 argv[1] 的游戏名。这里把 psargs 造成截断态,只能靠映射表认出来。"""
    so = '/srv/prod/ElainaBot_v2/plugins/LGTBot_ElainaBot/build/plugins/lgtbot_dxj/libgame.so'
    truncated = ('/srv/prod/ElainaBot_v2/plugins/LGTBot_ElainaBot/build/match_game_runner '
                 '/srv/pro')[:80]
    notes = (_note(corefile._NT_PRSTATUS, _prstatus(11, 0x7F0000000100))
             + _note(corefile._NT_PRPSINFO,
                     _prpsinfo(b'match_game_runn', truncated.encode()))
             + _note(corefile._NT_FILE,
                     _nt_file([(0x7F0000000000, 0x7F0000010000, so)])))
    r = corefile.analyze(_make_core(os.path.join(str(tmp_path), 'core-t-1-2'), notes))
    assert 'libgame.so' not in r['cmdline']       # 命令行确实够不到游戏名
    assert r['game'] == 'lgtbot_dxj'              # 仍认出来了 —— 来自 NT_FILE
    assert r['crash_module'] == 'libgame.so'


def test_analyze_game_from_cmdline_when_no_nt_file(tmp_path):
    """没有 NT_FILE(内核较老)时退路:argv[1] 就是游戏 .so 路径。"""
    so = '/opt/bot/build/plugins/numcomb/libgame.so'
    notes = (_note(corefile._NT_PRSTATUS, _prstatus(6, 0))
             + _note(corefile._NT_PRPSINFO,
                     _prpsinfo(b'match_game_runn', f'/r {so}'.encode())))
    r = corefile.analyze(_make_core(os.path.join(str(tmp_path), 'core-x-1-2'), notes))
    assert r['game'] == 'numcomb'
    assert r['signal_name'] == 'SIGABRT'
    assert r['crash_module'] == ''                # 无映射表,无从判断


def test_analyze_falls_back_to_cursig(tmp_path):
    """有些内核 pr_info.si_signo 是 0、只填 pr_cursig —— 两处都要看。"""
    d = bytearray(_prstatus(0, 0))
    struct.pack_into('<h', d, 12, 7)              # 只留 pr_cursig=SIGBUS
    notes = _note(corefile._NT_PRSTATUS, bytes(d))
    r = corefile.analyze(_make_core(os.path.join(str(tmp_path), 'core-y-1-2'), notes))
    assert r['signal'] == 7 and r['signal_name'] == 'SIGBUS'


# ─────────────────────────────────────────────────────────────────────────
# 异常输入:一律 ok=False + 原因,绝不抛
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('data,frag', [
    (b'not an elf at all........', '不是 ELF'),
    (b'\x7fELF' + b'\x01' + b'\0' * 59, '64 位'),
])
def test_analyze_rejects_non_core(tmp_path, data, frag):
    p = os.path.join(str(tmp_path), 'bad')
    with open(p, 'wb') as f:
        f.write(data)
    r = corefile.analyze(p)
    assert r['ok'] is False and frag in r['error']


def test_analyze_rejects_regular_elf(tmp_path):
    """普通可执行 / .so(e_type != ET_CORE)不当 core 解析。"""
    p = _make_core(os.path.join(str(tmp_path), 'exe'), b'', etype=2)
    r = corefile.analyze(p)
    assert r['ok'] is False and 'core dump' in r['error']


def test_analyze_missing_file_and_empty_notes(tmp_path):
    r = corefile.analyze(os.path.join(str(tmp_path), 'nope'))
    assert r['ok'] is False and r['error']
    r2 = corefile.analyze(_make_core(os.path.join(str(tmp_path), 'c2'), b''))
    assert r2['ok'] is False and 'PT_NOTE' in r2['error']


def test_analyze_survives_truncated_notes(tmp_path):
    """note 段被截断(core 写盘时磁盘满 / 被 kill)→ 不抛,能读多少算多少。"""
    full = _full_core(tmp_path)
    with open(full, 'rb') as f:
        blob = f.read()
    p = os.path.join(str(tmp_path), 'core-cut-1-2')
    with open(p, 'wb') as f:
        f.write(blob[:len(blob) // 2])            # 砍掉后一半
    r = corefile.analyze(p)                       # 不抛即通过
    assert isinstance(r, dict) and 'ok' in r


def test_analyze_ignores_absurd_mapping_count(tmp_path):
    """NT_FILE 的 count 明显不合理(畸形 core)→ 丢掉映射表而不是分配几 GB。"""
    bogus = struct.pack('<QQ', 1 << 40, 4096)
    notes = (_note(corefile._NT_PRSTATUS, _prstatus(11, 0))
             + _note(corefile._NT_FILE, bogus))
    r = corefile.analyze(_make_core(os.path.join(str(tmp_path), 'core-z-1-2'), notes))
    assert r['mapping_count'] == 0 and r['signal'] == 11


def test_sig_name_fallback():
    assert corefile.sig_name(11) == 'SIGSEGV'
    assert corefile.sig_name(99) == 'sig99'
    assert corefile.sig_name(None) == ''
