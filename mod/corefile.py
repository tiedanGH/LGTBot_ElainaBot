#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ELF core dump 轻量解析 —— 从游戏子进程的 core 文件里读出「哪个游戏、什么信号」。

背景:游戏跑在引擎 fork 出的 ``match_game_runner`` 子进程里
(``bot_core/match_child_client.cc``:``argv = {runner_exe, game_library}``)。
游戏代码崩溃只打死这个子进程,主进程毫发无伤 —— 于是内核按 ``core_pattern``
在子进程 cwd(= ``boot.BUILD_DIR``,见 boot._make_runner_wrapper 的 chdir)
落一个 ``core-match_game_runn-<pid>-<ts>`` 文件,主进程侧没有任何 dump。

**能解析出什么(不依赖 gdb / 调试符号)**
core 文件是 ELF,PT_NOTE 段里带若干 note,本模块只读这几个:
  · ``NT_PRSTATUS``  信号号(``pr_info.si_signo``)+ x86_64 下的崩溃 PC(RIP)
  · ``NT_PRPSINFO``  可执行名(``pr_fname``)与命令行(``pr_psargs``,内核截断到 80B)
  · ``NT_SIGINFO``   ``si_code`` / ``si_addr``(访问了哪个非法地址)
  · ``NT_FILE``      全部文件映射的**完整路径** —— 其中
    ``…/plugins/<game>/libgame.so`` 直接给出游戏名,且不像 psargs 那样被截断
把 RIP 落在哪个映射区间里一查,还能说出崩溃发生在游戏 .so 还是引擎核心库。

**做不到什么(不是偷懒,是硬限制)**
真正的调用栈(哪一行代码)需要栈回溯:解析 ``.eh_frame`` / DWARF CFI 逐帧展开,
并且要有与 core **完全匹配**的二进制 + 调试符号。Release 构建默认不带符号,
纯 Python 也没有可靠的 unwinder。这一步应当交给 gdb:
``gdb <build>/match_game_runner <core 文件>`` 然后 ``bt``。

解析全程只读文件头 + PT_NOTE 段(通常几十 KB),不会把几百 MB 的 core 读进内存。
任何异常 / 格式不符都返回 ``ok=False`` 并带原因,绝不抛给调用方。
"""

from __future__ import annotations

import os
import re
import struct

# note 类型(linux include/uapi/linux/elf.h + fs/binfmt_elf.c)
_NT_PRSTATUS = 1
_NT_PRPSINFO = 3
_NT_SIGINFO = 0x53494749
_NT_FILE = 0x46494C45

_EM_X86_64 = 62
_ET_CORE = 4

# elf_prstatus(x86_64)字段偏移 —— 见 include/linux/elfcore.h:
#   elf_siginfo pr_info(0,12) / short pr_cursig(12) / pad(14) /
#   ulong pr_sigpend(16) / pr_sighold(24) / pid,ppid,pgrp,sid(32..48) /
#   timeval ×4(48..112) / elf_gregset_t pr_reg(112, 27×8) / int pr_fpvalid(328)
_PRSTATUS_SIGNO = 0        # pr_info.si_signo
_PRSTATUS_CURSIG = 12
_PRSTATUS_REGS = 112
_PRSTATUS_MIN = 336
# x86_64 user_regs_struct 里 rip 是第 17 个 ulong(下标 16)
_REG_RIP_INDEX = 16

# elf_prpsinfo(x86_64):char×4(0..4) / pad / ulong pr_flag(8) / uid,gid(16,20) /
#   pid,ppid,pgrp,sid(24..40) / char pr_fname[16](40) / char pr_psargs[80](56)
_PRPSINFO_FNAME = 40
_PRPSINFO_PSARGS = 56
_PRPSINFO_MIN = 136

# siginfo_t 前 24 字节:int si_signo / int si_errno / int si_code / pad /
#   union 起始处对 SIGSEGV 是 void *si_addr
_SIGINFO_CODE = 8
_SIGINFO_ADDR = 16
_SIGINFO_MIN = 24

_SIG_NAMES = {
    2: 'SIGINT', 3: 'SIGQUIT', 4: 'SIGILL', 5: 'SIGTRAP', 6: 'SIGABRT',
    7: 'SIGBUS', 8: 'SIGFPE', 9: 'SIGKILL', 11: 'SIGSEGV', 13: 'SIGPIPE',
    15: 'SIGTERM', 24: 'SIGXCPU', 25: 'SIGXFSZ', 31: 'SIGSYS',
}
# SIGSEGV / SIGBUS 的 si_code → 人话(include/uapi/asm-generic/siginfo.h)
_SEGV_CODES = {1: '地址未映射 (SEGV_MAPERR)', 2: '无访问权限 (SEGV_ACCERR)'}
_BUS_CODES = {1: '物理地址不存在 (BUS_ADRALN)', 2: '地址不对齐 (BUS_ADRERR)',
              3: '硬件错误 (BUS_OBJERR)'}

# 游戏 .so 路径 → 游戏目录名(引擎按 build/plugins/<game>/libgame.so 布局加载)
_GAME_SO_RE = re.compile(r'[/\\]plugins[/\\]([^/\\]+)[/\\]libgame\.so$')

# 只读文件头 + PT_NOTE:note 段封顶,防畸形 core 的 p_filesz 撑爆内存
_MAX_NOTE_BYTES = 16 * 1024 * 1024
_MAX_MAPPINGS = 8192


def sig_name(sig) -> str:
    """信号号 → 名称;不认识的原样带号返回。"""
    if sig is None:
        return ''
    return _SIG_NAMES.get(sig, f'sig{sig}')


def _sig_detail(sig, code) -> str:
    """si_code 的人话解释(只有 SEGV / BUS 有意义)。"""
    if code is None:
        return ''
    if sig == 11:
        return _SEGV_CODES.get(code, '')
    if sig == 7:
        return _BUS_CODES.get(code, '')
    return ''


def _cstr(buf: bytes) -> str:
    return buf.split(b'\0', 1)[0].decode('utf-8', 'replace').strip()


def _iter_notes(blob: bytes):
    """遍历 note 段:namesz / descsz / type + name(4 字节对齐)+ desc(同)。"""
    pos = 0
    n = len(blob)
    while pos + 12 <= n:
        nsz, dsz, ntype = struct.unpack_from('<III', blob, pos)
        pos += 12
        if nsz > n or dsz > n:              # 畸形长度,停止解析
            return
        pos += (nsz + 3) & ~3
        if pos + dsz > n:
            return
        yield ntype, blob[pos:pos + dsz]
        pos += (dsz + 3) & ~3


def _parse_nt_file(desc: bytes) -> list:
    """NT_FILE:``long count; long page_size;`` 后接 count 个 (start,end,ofs) 三元组,
    再接 count 个 NUL 结尾的路径。返回 ``[(start, end, path)]``。"""
    if len(desc) < 16:
        return []
    count, _page = struct.unpack_from('<QQ', desc, 0)
    if not 0 < count <= _MAX_MAPPINGS:
        return []
    pos = 16
    spans = []
    for _ in range(count):
        if pos + 24 > len(desc):
            return []
        start, end, _ofs = struct.unpack_from('<QQQ', desc, pos)
        pos += 24
        spans.append((start, end))
    names = desc[pos:].split(b'\0')
    return [(s, e, n.decode('utf-8', 'replace'))
            for (s, e), n in zip(spans, names) if n]


def analyze(path: str) -> dict:
    """解析一个 core 文件,返回展示用 dict。

    失败一律 ``{'ok': False, 'error': ...}`` —— 面板按「无法解析」渲染,不影响
    列表 / 下载 / 删除(那些只看文件本身)。
    """
    out = {
        'ok': False, 'error': '',
        'signal': None, 'signal_name': '', 'signal_detail': '',
        'fault_addr': None, 'exe': '', 'cmdline': '',
        'game': '', 'crash_module': '', 'mapping_count': 0,
    }
    try:
        with open(path, 'rb') as f:
            hdr = f.read(64)
            if len(hdr) < 64 or hdr[:4] != b'\x7fELF':
                out['error'] = '不是 ELF 文件'
                return out
            if hdr[4] != 2:
                out['error'] = '仅支持 64 位 core'
                return out
            etype, machine = struct.unpack_from('<HH', hdr, 16)
            if etype != _ET_CORE:
                out['error'] = f'不是 core dump (e_type={etype})'
                return out
            phoff, = struct.unpack_from('<Q', hdr, 32)
            phentsize, phnum = struct.unpack_from('<HH', hdr, 54)
            if not phentsize or not phnum or phnum > 65535:
                out['error'] = 'program header 表异常'
                return out
            f.seek(phoff)
            phdrs = f.read(phentsize * phnum)

            note_spans = []
            for i in range(phnum):
                p = phdrs[i * phentsize:(i + 1) * phentsize]
                if len(p) < 40:
                    break
                ptype, = struct.unpack_from('<I', p, 0)
                if ptype != 4:                          # PT_NOTE
                    continue
                off, = struct.unpack_from('<Q', p, 8)
                sz, = struct.unpack_from('<Q', p, 32)
                if sz:
                    note_spans.append((off, min(sz, _MAX_NOTE_BYTES)))
            if not note_spans:
                out['error'] = 'core 内没有 PT_NOTE 段'
                return out

            rip = None
            si_code = None
            maps: list = []
            for off, sz in note_spans:
                f.seek(off)
                blob = f.read(sz)
                for ntype, desc in _iter_notes(blob):
                    if ntype == _NT_PRSTATUS and len(desc) >= _PRSTATUS_MIN:
                        if out['signal'] is None:       # 只认**第一个** —— 那是崩溃线程
                            signo, = struct.unpack_from('<i', desc, _PRSTATUS_SIGNO)
                            if not signo:               # 有些内核只填 pr_cursig
                                signo, = struct.unpack_from('<h', desc, _PRSTATUS_CURSIG)
                            if 0 < signo < 65:
                                out['signal'] = signo
                            if machine == _EM_X86_64:
                                rip, = struct.unpack_from(
                                    '<Q', desc,
                                    _PRSTATUS_REGS + _REG_RIP_INDEX * 8)
                    elif ntype == _NT_PRPSINFO and len(desc) >= _PRPSINFO_MIN:
                        out['exe'] = _cstr(desc[_PRPSINFO_FNAME:_PRPSINFO_FNAME + 16])
                        out['cmdline'] = _cstr(desc[_PRPSINFO_PSARGS:_PRPSINFO_PSARGS + 80])
                    elif ntype == _NT_SIGINFO and len(desc) >= _SIGINFO_MIN:
                        si_code, = struct.unpack_from('<i', desc, _SIGINFO_CODE)
                        out['fault_addr'], = struct.unpack_from('<Q', desc, _SIGINFO_ADDR)
                    elif ntype == _NT_FILE:
                        maps.extend(_parse_nt_file(desc))
    except OSError as e:
        out['error'] = f'读取失败: {e}'
        return out
    except Exception as e:                              # 畸形 core:报错不抛
        out['error'] = f'解析失败: {e}'
        return out

    out['mapping_count'] = len(maps)
    out['signal_name'] = sig_name(out['signal'])
    out['signal_detail'] = _sig_detail(out['signal'], si_code)

    # 游戏名:映射里的 plugins/<game>/libgame.so —— 完整路径,不受 psargs 80B 截断影响
    for _s, _e, p in maps:
        m = _GAME_SO_RE.search(p)
        if m:
            out['game'] = m.group(1)
            break
    # 退路:psargs 第二个参数就是游戏 .so 路径(argv[1]),没被截断时也能认出来
    if not out['game'] and out['cmdline']:
        for tok in out['cmdline'].split():
            m = _GAME_SO_RE.search(tok)
            if m:
                out['game'] = m.group(1)
                break

    # 崩溃 PC 落在哪个映射里 —— 顺带自校验:偏移若算错,RIP 几乎不可能命中任何区间,
    # 那就只是不报模块,而不会报出一个错的
    if rip:
        for s, e, p in maps:
            if s <= rip < e and p:
                out['crash_module'] = os.path.basename(p)
                break

    out['ok'] = out['signal'] is not None or bool(maps)
    if not out['ok']:
        out['error'] = 'note 段里没有可用信息'
    return out
