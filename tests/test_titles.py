#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""玩家头衔测试 —— users.titles 数据层(头衔与等级)、「我的头衔 / 切换头衔」指令、新建 / 加入房间后的头衔播报。

被测行为:
  · 头衔库整份保存时逐项校验(32 位 OpenID / 数量 / 名字字符 / 等级存在 / 重名 / 装备必须在库里),空库等于删除
  · 等级表整份保存:新增生成键、改名改色立即作用到已有头衔、在用的等级不能删、至少留一个
  · 装备按序号或名字,序号越界再按名字找;等级已不存在的头衔照常保留并灰色兜底;坏文件改名留证
  · 指令回复带序号、KaTeX 上色、等级名与装备标记;找不到头衔时不复述玩家输入
  · 头衔播报对象:建房广播取派发线程上记下的发起人,加入广播取加入的玩家,退出不播;标记只在打标的线程上可见
  · 播报排在回复限制 / 主动私信提示之后
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.LGTBot_ElainaBot.mod import callbacks, dispatcher, state
from plugins.LGTBot_ElainaBot.mod.users import titles

U1 = '0123456789ABCDEF0123456789ABCDEF'
U2 = 'FEDCBA9876543210FEDCBA9876543210'
HOST = 'A1B2C3D4E5F60718293A4B5C6D7E8F90'


@pytest.fixture(autouse=True)
def _titles_files(tmp_path, monkeypatch):
    """每个用例一份独立的 titles.json / levels.json。"""
    monkeypatch.setattr(titles, '_players', titles._JsonFile(str(tmp_path / 'titles.json'), dict))
    monkeypatch.setattr(titles, '_levels', titles._JsonFile(str(tmp_path / 'levels.json'), list))
    callbacks._title_mark.kind = ''
    state.engine_request.uid = ''
    yield
    callbacks._title_mark.kind = ''
    state.engine_request.uid = ''


def _t(name, color='blue'):
    return {'name': name, 'color': color}


def _lv(name, hexcolor, key=''):
    return {'key': key, 'name': name, 'hex': hexcolor}


# ─────────────────────────────────────────────────────────────────────────
# 数据层:头衔
# ─────────────────────────────────────────────────────────────────────────


def test_set_record_validates_every_field_and_writes_nothing_on_error():
    bad = [
        ('U1', [_t('A')], None),
        (U1[:-1] + '!', [_t('A')], None),
        (U1 + '0', [_t('A')], None),
        (U1, 'A', None),
        (U1, [_t(f'T{i}') for i in range(titles.MAX_TITLES + 1)], None),
        (U1, ['A'], None),
        (U1, [_t('')], None),
        (U1, [_t('一' * (titles.NAME_MAX_LEN + 1))], None),
        (U1, [_t('a$b')], None),
        (U1, [_t('a\\b')], None),
        (U1, [_t('a}b')], None),
        (U1, [_t('a\nb')], None),
        (U1, [_t('A', 'gold')], None),
        (U1, [_t('A'), _t(' A ', 'red')], None),
        (U1, [_t('A')], 'B'),
    ]
    for openid, ts, eq in bad:
        assert titles.set_record(openid, ts, eq), (openid, ts, eq)
    assert not os.path.exists(titles._players.path)

    assert titles.set_record(U1, [_t(' 铁蛋 '), _t('Ace', 'pink')], '铁蛋') is None
    assert titles.get(U1) == {'titles': [_t('铁蛋'), _t('Ace', 'pink')], 'equipped': '铁蛋'}
    assert titles.equipped(U1) == _t('铁蛋')
    assert titles.set_record(U1, [_t('Ace', 'pink')], '') is None
    assert titles.equipped(U1) is None


def test_empty_library_deletes_the_record():
    titles.set_record(U1, [_t('A')], 'A')
    assert titles.set_record(U1, [], None) is None
    assert titles.get(U1) is None
    assert titles.delete(U1) is False
    titles.set_record(U2, [_t('B')], None)
    assert titles.delete(U2) is True and titles.all_records() == {}


def test_equip_by_index_or_name():
    assert titles.equip(U1, '1') == (None, 'none')
    titles.set_record(U1, [_t('A'), _t('B', 'red'), _t('2048', 'green')], None)
    assert titles.equip(U1, '2') == (_t('B', 'red'), '')
    assert titles.get(U1)['equipped'] == 'B'
    assert titles.equip(U1, ' A ') == (_t('A'), '')
    assert titles.equip(U1, '３')[0] == _t('2048', 'green')        # 全角数字照样当序号
    assert titles.equip(U1, '2048')[0] == _t('2048', 'green')      # 序号越界再按名字找
    assert titles.equip(U1, '0') == (None, 'missing')
    assert titles.equip(U1, '9' * 5000) == (None, 'missing')
    assert titles.equip(U1, '不存在') == (None, 'missing')
    assert titles.get(U1)['equipped'] == '2048'


def test_render_uses_inline_katex_colour_of_the_level():
    t = _t('铁蛋', 'blue')
    assert titles.render(t) == '$\\textcolor{#2980B9}{\\text{铁蛋}}$'
    assert titles.join_notice(t) == '[头衔] $\\textcolor{#2980B9}{\\text{铁蛋}}$ 加入游戏'
    assert [lv['name'] for lv in titles.levels()] == ['普通', '稀有', '史诗', '传说', '神话', '限定']
    assert titles.rarity(_t('x', 'pink')) == '限定'


def test_dirty_file_is_cleaned_but_unknown_levels_survive():
    with open(titles._players.path, 'w', encoding='utf-8') as f:
        json.dump({U1: {'titles': [_t('A'), _t('A', 'red'), _t('B', 'gone'), 'x', _t('a{b'), {'name': 'C'}], 'equipped': 'B'},
                   U2: 'garbage'}, f, ensure_ascii=False)
    assert titles.get(U1) == {'titles': [_t('A'), _t('B', 'gone')], 'equipped': 'B'}
    assert titles.get(U2) is None
    assert set(titles.all_records()) == {U1}
    # 等级已不存在:灰色兜底,玩家切换装备时也不会把它弄丢
    assert titles.render(_t('B', 'gone')) == '$\\textcolor{#808080}{\\text{B}}$' and titles.rarity(_t('B', 'gone')) == '未知'
    titles.equip(U1, 'A')
    assert titles.get(U1)['titles'] == [_t('A'), _t('B', 'gone')]


def test_corrupt_file_is_set_aside_and_saving_starts_fresh(tmp_path):
    with open(titles._players.path, 'w', encoding='utf-8') as f:
        f.write('{not json')
    assert titles.get(U1) is None
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith('titles.json.corrupt_')]
    assert titles.set_record(U1, [_t('A')], 'A') is None
    assert titles.equipped(U1) == _t('A')


def test_file_changed_on_disk_is_picked_up():
    titles.set_record(U1, [_t('A')], 'A')
    assert titles.equipped(U1) == _t('A')
    with open(titles._players.path, 'w', encoding='utf-8') as f:
        json.dump({U1: {'titles': [_t('Longer', 'red')], 'equipped': 'Longer'}}, f)
    assert titles.equipped(U1) == _t('Longer', 'red')


# ─────────────────────────────────────────────────────────────────────────
# 数据层:等级
# ─────────────────────────────────────────────────────────────────────────


def test_levels_can_be_added_edited_and_reordered():
    rows = [dict(lv) for lv in titles.levels()]
    rows[1].update(name='稀有款', hex='3498db')                   # 改名改色,不带 # 也认
    rows.append(_lv(' 金色传说 ', '#ffd700'))
    assert titles.set_levels(rows) is None
    got = titles.levels()
    assert got[1] == {'key': 'blue', 'name': '稀有款', 'hex': '#3498DB'}
    assert got[-1]['name'] == '金色传说' and got[-1]['hex'] == '#FFD700' and got[-1]['key']
    assert got[-1]['key'] not in {lv['key'] for lv in titles.DEFAULT_LEVELS}
    # 已有头衔跟着等级改名改色
    titles.set_record(U1, [_t('A', 'blue'), _t('B', got[-1]['key'])], 'B')
    assert titles.render(_t('A', 'blue')) == '$\\textcolor{#3498DB}{\\text{A}}$'
    assert titles.rarity(titles.equipped(U1)) == '金色传说'
    assert titles.set_levels(list(reversed(got))) is None
    assert [lv['key'] for lv in titles.levels()] == [lv['key'] for lv in reversed(got)]


def test_level_in_use_cannot_be_deleted():
    titles.set_record(U1, [_t('A', 'red'), _t('B', 'red')], None)
    rest = [lv for lv in titles.levels() if lv['key'] != 'red']
    err = titles.set_levels(rest)
    assert err and '神话' in err and '2' in err
    assert any(lv['key'] == 'red' for lv in titles.levels())
    titles.set_record(U1, [_t('A', 'blue')], None)
    assert titles.set_levels(rest) is None
    assert all(lv['key'] != 'red' for lv in titles.levels())


def test_set_levels_validates_and_writes_nothing_on_error():
    base = titles.levels()
    bad = [
        [], 'x',
        [dict(base[0])] * 2,
        [_lv('', '#000000')],
        [_lv('一' * (titles.LEVEL_NAME_MAX_LEN + 1), '#000000')],
        [_lv('a*b', '#000000')],
        [_lv('A', 'red')], [_lv('A', '#12345')], [_lv('A', '#1234567')],
        [_lv('A', '#000000'), _lv(' A ', '#111111')],
        [_lv('A', '#000000', key='nope')],
        ['A'],
        [_lv(f'L{i}', '#000000') for i in range(titles.MAX_LEVELS + 1)],
    ]
    for rows in bad:
        assert titles.set_levels(rows), rows
    assert not os.path.exists(titles._levels.path)
    assert titles.levels() == base


def test_broken_levels_file_falls_back_to_defaults(tmp_path):
    with open(titles._levels.path, 'w', encoding='utf-8') as f:
        f.write('[oops')
    assert [lv['key'] for lv in titles.levels()] == [lv['key'] for lv in titles.DEFAULT_LEVELS]
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith('levels.json.corrupt_')]
    with open(titles._levels.path, 'w', encoding='utf-8') as f:
        json.dump([{'key': 'k', 'name': 'A', 'hex': 'zz'}, 'x', {'key': 'g', 'name': '金', 'hex': '#ffd700'}], f)
    assert titles.levels() == [{'key': 'g', 'name': '金', 'hex': '#FFD700'}]
    with open(titles._levels.path, 'w', encoding='utf-8') as f:
        json.dump([{'key': 'k', 'name': '', 'hex': '#000000'}, 'x'], f)       # 一项能用的都没有
    assert [lv['key'] for lv in titles.levels()] == [lv['key'] for lv in titles.DEFAULT_LEVELS]


# ─────────────────────────────────────────────────────────────────────────
# 指令
# ─────────────────────────────────────────────────────────────────────────


def _ev(content, *, group=True, uid=U1):
    ev = MagicMock()
    ev.event_type = dispatcher.GROUP_AT_MESSAGE_CREATE if group else dispatcher.C2C_MESSAGE_CREATE
    ev.is_group, ev.is_direct = group, not group
    ev.group_id = 'G1' if group else ''
    ev.channel_id = ''
    ev.user_id = uid
    ev.content = content
    ev.appid = ''
    ev.is_interaction = False
    ev.reply = AsyncMock()
    return ev


async def _call(handler, pattern, content, **kw):
    ev = _ev(content, **kw)
    await handler(ev, re.search(pattern, content))
    return ev.reply.await_args.args[0]


async def test_my_titles_lists_index_colour_level_and_equipped():
    titles.set_record(U1, [_t('铁蛋', 'blue'), _t('Ace', 'orange')], 'Ace')
    text = await _call(dispatcher.lgtbot_my_titles, dispatcher._P_MY_TITLES, '我的头衔')
    lines = text.split('\n')
    assert lines[0] == f'<@{U1}>'
    assert '1、$\\textcolor{#2980B9}{\\text{铁蛋}}$【稀有】' in lines
    assert '2、$\\textcolor{#FF8C00}{\\text{Ace}}$【传说】（装备中）' in lines
    assert lines[-1] == '> 发送「切换头衔 序号/名称」更换头衔'
    dm = await _call(dispatcher.lgtbot_my_titles, dispatcher._P_MY_TITLES, '/我的头衔', group=False)
    assert not dm.startswith('<@') and dm.startswith('## ')
    assert await _call(dispatcher.lgtbot_my_titles, dispatcher._P_MY_TITLES, '我的头衔', uid=U2) == f'<@{U2}>\n您还没有头衔'


async def test_equip_title_replies_and_never_echoes_input():
    p = dispatcher._P_EQUIP_TITLE
    h = dispatcher.lgtbot_equip_title
    assert (await _call(h, p, '切换头衔 1')).endswith('您还没有头衔')
    titles.set_record(U1, [_t('铁蛋', 'blue'), _t('Ace', 'orange')], None)
    assert (await _call(h, p, '切换头衔 2')).endswith('✅ 已装备头衔：$\\textcolor{#FF8C00}{\\text{Ace}}$')
    assert titles.get(U1)['equipped'] == 'Ace'
    assert (await _call(h, p, '切换头衔 Ace')).endswith('您已经装备了此头衔')
    assert (await _call(h, p, '/切换头衔铁蛋')).endswith('✅ 已装备头衔：$\\textcolor{#2980B9}{\\text{铁蛋}}$')
    for arg in ('奇怪的输入', '99'):
        text = await _call(h, p, f'切换头衔 {arg}')
        assert arg not in text and '我的头衔' in text
    assert titles.get(U1)['equipped'] == '铁蛋'
    usage = await _call(h, p, '切换头衔')
    assert '用法' in usage and '我的头衔' in usage and '切换头衔 无' in usage


async def test_equip_title_by_command_is_audited_only_when_it_changes(monkeypatch):
    audits = []
    monkeypatch.setattr(dispatcher.audit, 'record', lambda *a, **k: audits.append((a, k)))
    monkeypatch.setattr(titles.userinfo, 'get_name', lambda uid: {U1: '铁蛋'}.get(uid, ''))
    p, h = dispatcher._P_EQUIP_TITLE, dispatcher.lgtbot_equip_title
    titles.set_record(U1, [_t('A'), _t('B', 'red')], None)
    await _call(h, p, '切换头衔 2')
    assert audits == [(('title', '切换头衔', f'铁蛋 ({U1})：装备 无 → B'), {'src': dispatcher.audit.SRC_CMD})]
    assert (await _call(h, p, '切换头衔 B')).endswith('您已经装备了此头衔')
    for bad in ('切换头衔 不存在', '切换头衔'):
        await _call(h, p, bad)
    assert len(audits) == 1                                      # 切的就是当前这个、或没切换成:不记
    await _call(h, p, '切换头衔 1')
    assert audits[-1][0][2] == f'铁蛋 ({U1})：装备 B → A'
    titles.set_record(U2, [_t('C')], None)
    await _call(h, p, '切换头衔 C', uid=U2)
    assert audits[-1][0][2] == f'{U2}：装备 无 → C'               # 没有昵称只写 OpenID


async def test_equip_title_none_takes_the_title_off(monkeypatch):
    audits = []
    monkeypatch.setattr(dispatcher.audit, 'record', lambda *a, **k: audits.append((a, k)))
    monkeypatch.setattr(titles.userinfo, 'get_name', lambda uid: {U1: '铁蛋'}.get(uid, ''))
    p, h = dispatcher._P_EQUIP_TITLE, dispatcher.lgtbot_equip_title
    assert (await _call(h, p, '切换头衔 无')).endswith('您还没有头衔')
    titles.set_record(U1, [_t('A'), _t('B', 'red')], None)
    assert (await _call(h, p, '切换头衔 无')).endswith('您当前没有装备头衔')
    assert audits == []                                          # 本来就没装备:不记
    titles.equip(U1, 'B')
    assert (await _call(h, p, '切换头衔无')).endswith('✅ 已取消装备头衔')
    assert titles.get(U1) == {'titles': [_t('A'), _t('B', 'red')], 'equipped': None}
    assert audits == [(('title', '切换头衔', f'铁蛋 ({U1})：装备 B → 无'), {'src': dispatcher.audit.SRC_CMD})]


def test_title_commands_never_reach_the_engine():
    for text in ('我的头衔', '/我的头衔', '切换头衔', '切换头衔 2', '/切换头衔 铁 蛋', '切换头衔2'):
        assert dispatcher._is_exclusive_command(text), text
    assert not dispatcher._is_exclusive_command('我的头衔们')


def test_engine_thread_records_the_requester_on_its_own_thread():
    seen = {}
    state.engine_request.uid = 'MAIN'

    def fake_engine(content, uid, gid):
        seen['uid'] = state.engine_request.uid

    t = threading.Thread(target=dispatcher._run_engine, args=(fake_engine, ('/新游戏 五子棋', HOST, 'G1'), HOST))
    t.start()
    t.join()
    assert seen == {'uid': HOST}
    assert state.engine_request.uid == 'MAIN'


# ─────────────────────────────────────────────────────────────────────────
# 播报对象
# ─────────────────────────────────────────────────────────────────────────

_JOIN = f'玩家 <@{U2}> 加入了游戏\n\n游戏名称：五子棋\n- 当前用户数：2'
_LEAVE = f'玩家 <@{U2}> 退出了游戏\n\n游戏名称：五子棋\n- 当前用户数：1'
_NEW = '现在玩家可以在群里通过「/加入」报名比赛\n\n游戏名称：五子棋'


def test_new_game_broadcast_picks_the_requester_once():
    state.engine_request.uid = HOST
    callbacks.cb_match_event('G1', False, 'new_game', '五子棋')
    assert callbacks._take_title_uid(_NEW) == HOST
    assert callbacks._take_title_uid(_NEW) == ''


def test_join_broadcast_picks_the_joiner_and_leave_picks_nobody():
    state.engine_request.uid = 'SOMEONE_ELSE'
    callbacks.cb_match_event('G1', False, 'join_leave', '五子棋')
    assert callbacks._take_title_uid(_JOIN) == U2
    callbacks.cb_match_event('G1', False, 'join_leave', '五子棋')
    assert callbacks._take_title_uid(_LEAVE) == ''


def test_other_broadcasts_reset_the_mark():
    state.engine_request.uid = HOST
    for kind in ('game_started', 'announce', 'all_left'):
        callbacks.cb_match_event('G1', False, 'new_game', '五子棋')
        callbacks.cb_match_event('G1', False, kind, '')
        assert callbacks._take_title_uid(_NEW) == '', kind
    callbacks.cb_match_event('G1', False, 'game_started', '')
    assert callbacks._take_title_uid(_NEW) == ''


def test_mark_is_invisible_to_other_threads():
    callbacks.cb_match_event('G1', False, 'join_leave', '五子棋')
    seen = []
    t = threading.Thread(target=lambda: seen.append(callbacks._take_title_uid(_JOIN)))
    t.start()
    t.join()
    assert seen == ['']
    assert callbacks._take_title_uid(_JOIN) == U2


async def test_send_callbacks_hand_the_title_uid_to_their_task(monkeypatch):
    monkeypatch.setattr(state, 'event_loop', asyncio.get_running_loop())
    got = {}

    async def fake_text(key, tid, is_uid, msg, btns, title_uid=''):
        got['text'] = title_uid

    async def fake_mixed(key, tid, is_uid, segs, images, plain, btns, title_uid=''):
        got['mixed'] = title_uid

    monkeypatch.setattr(callbacks, '_serialized_text_send', fake_text)
    monkeypatch.setattr(callbacks, '_serialized_mixed_send', fake_mixed)
    monkeypatch.setattr(callbacks, '_read_rendered_image', lambda p: b'png')
    callbacks.cb_match_event('G1', False, 'join_leave', '五子棋')
    callbacks.cb_send_text_message('G1', False, _JOIN)
    callbacks.cb_match_event('G1', False, 'join_leave', '五子棋')
    callbacks.cb_send_image_message('G1', False, ['/x.png'], _JOIN + '\x01IMG0\x01')
    for _ in range(5):
        await asyncio.sleep(0)
    assert got == {'text': U2, 'mixed': U2}


# ─────────────────────────────────────────────────────────────────────────
# 播报顺序
# ─────────────────────────────────────────────────────────────────────────


async def _run_send(monkeypatch, *, push: bool, game: str, title_uid=HOST, mixed=False):
    """跑一次建房广播的串行发送,返回按送达顺序排列的消息首行。"""
    from plugins.LGTBot_ElainaBot.mod import helpers
    monkeypatch.setattr(state, 'event_loop', asyncio.get_running_loop())
    # 每个用例一个新 loop,上个用例建的锁绑在已关闭的 loop 上,争用时会让排队的 task 直接出错
    monkeypatch.setattr(callbacks, '_send_locks', {})
    monkeypatch.setattr(helpers, 'can_push_group', lambda gid: push)
    monkeypatch.setattr(callbacks.page_logs, 'log_outgoing', lambda *a, **k: None)
    sent = []

    async def fake_send(target_id, is_uid, msg, extra):
        await asyncio.sleep(0)
        sent.append(msg.split('\n')[0])

    monkeypatch.setattr(callbacks, '_send_text_quota_managed', fake_send)
    monkeypatch.setattr(callbacks, '_send_mixed_message', lambda tid, is_uid, segs, images, plain, extra: fake_send(tid, is_uid, plain, extra))
    callbacks._pending_tip_keys.clear()
    callbacks._pending_dm_warn_keys.clear()
    try:
        callbacks.cb_match_event('G1', False, 'new_game', game)
        callbacks._title_mark.kind = ''
        if mixed:
            await callbacks._serialized_mixed_send('g:G1', 'G1', False, [('text', _NEW)], {}, _NEW, None, title_uid)
        else:
            await callbacks._serialized_text_send('g:G1', 'G1', False, _NEW, None, title_uid)
        for _ in range(20):
            await asyncio.sleep(0)
    finally:
        callbacks._pending_tip_keys.clear()
        callbacks._pending_dm_warn_keys.clear()
    return sent


async def test_title_notice_follows_the_reply_limit_tip(monkeypatch):
    titles.set_record(HOST, [_t('铁蛋', 'purple')], '铁蛋')
    sent = await _run_send(monkeypatch, push=False, game='五子棋')
    assert sent == [_NEW.split('\n')[0], '## ⚠️ 消息回复限制', '[头衔] $\\textcolor{#8E44AD}{\\text{铁蛋}}$ 加入游戏']


async def test_title_notice_follows_the_dm_warning(monkeypatch):
    titles.set_record(HOST, [_t('铁蛋', 'purple')], '铁蛋')
    game = next(iter(callbacks._DM_LIMITED_GAMES))
    sent = await _run_send(monkeypatch, push=True, game=game, mixed=True)
    assert len(sent) == 3 and sent[1].startswith('## ') and sent[2].startswith('[头衔] ')


async def test_no_notice_without_an_equipped_title(monkeypatch):
    titles.set_record(HOST, [_t('铁蛋', 'purple')], None)
    assert len(await _run_send(monkeypatch, push=True, game='五子棋')) == 1
    assert len(await _run_send(monkeypatch, push=True, game='五子棋', title_uid='')) == 1
