#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""page_dashboard 更新标识测试 —— _get_update_hint 用运行版本现算。

回归背景:「更新桥接层」git pull 后插件热重载,运行版本已是新版,但启动
自检缓存(600s 防抖)仍是 pull 前的判定 —— hint 必须现算才会消失。
page_dashboard 顶部 import aiohttp,dev 机常无 → importorskip 守卫。
"""

from __future__ import annotations

import pytest

from plugins.LGTBot_ElainaBot.mod import boot


def _pd():
    pytest.importorskip('aiohttp')
    from plugins.LGTBot_ElainaBot.mod.webui import page_dashboard
    return page_dashboard


def _seed_cache(remote: str, success: bool = True):
    boot._get_persistent()['startup_update_check'] = {
        'ts': 1.0,
        'bridge': {'success': success, 'has_update': True,   # 缓存判定故意留 True
                   'remote_version': remote, 'local_version': '2.7.0'},
    }


@pytest.fixture(autouse=True)
def _clean_cache():
    boot._get_persistent().pop('startup_update_check', None)
    yield
    boot._get_persistent().pop('startup_update_check', None)


def test_update_hint_recomputes_against_running_version(monkeypatch):
    """缓存说有新版,但运行版本已 ≥ remote(pull + 热重载后)→ 标识消失。"""
    pd = _pd()
    _seed_cache('v2.7.1')
    monkeypatch.setattr(pd, '_get_plugin_meta', lambda: {'version': '2.7.1'})
    assert pd._get_update_hint() == {'has_update': False, 'remote_version': ''}

    # 运行版本仍是旧版(pull 前 / 未热重载)→ 标识照常显示
    monkeypatch.setattr(pd, '_get_plugin_meta', lambda: {'version': '2.7.0'})
    assert pd._get_update_hint() == {'has_update': True, 'remote_version': 'v2.7.1'}


def test_update_hint_empty_without_cache_or_on_failure(monkeypatch):
    pd = _pd()
    monkeypatch.setattr(pd, '_get_plugin_meta', lambda: {'version': '2.7.0'})
    assert pd._get_update_hint() == {'has_update': False, 'remote_version': ''}
    _seed_cache('v9.9.9', success=False)          # 检查失败的缓存不产生标识
    assert pd._get_update_hint() == {'has_update': False, 'remote_version': ''}


# ─────────────────────────────────────────────────────────────────────────
# 进行中的对局:群局展示名的三级降级
# ─────────────────────────────────────────────────────────────────────────

def test_active_matches_group_name_fallback_chain(monkeypatch):
    """★ 群局展示名 **备注名 → 群名称 → openid** 三级降级,并用 ``name_src``
    告诉前端命中了哪一级(备注名要加粗突出,自动取来的群名不加)。"""
    from plugins.LGTBot_ElainaBot.mod import state
    pd = _pd()
    monkeypatch.setattr(pd, '_group_remarks',
                        lambda: {'GR': {'name': '运维备注群'}})
    monkeypatch.setattr(pd.userinfo, 'get_group_names',
                        lambda gids: {'GN': '真实群名'})
    monkeypatch.setattr(pd.userinfo, 'get_name', lambda uid: '铁蛋')
    state.active_matches.clear()
    for i, (tid, is_uid) in enumerate([('GR', False), ('GN', False),
                                       ('GX', False), ('U1', True)]):
        state.active_matches[f'k{i}'] = {'target_id': tid, 'is_uid': is_uid,
                                         'game': '五子棋', 'since': 100 - i}
    try:
        view = {m['id']: m for m in pd._active_matches_view()}
        assert (view['GR']['name'], view['GR']['name_src']) == ('运维备注群', 'remark')
        assert (view['GN']['name'], view['GN']['name_src']) == ('真实群名', 'group')
        assert (view['GX']['name'], view['GX']['name_src']) == ('', '')   # 前端显 openid
        assert (view['U1']['name'], view['U1']['name_src']) == ('铁蛋', 'user')
    finally:
        state.active_matches.clear()


def test_active_matches_skips_group_lookups_for_dm_only(monkeypatch):
    """纯私聊场景不读备注文件、不查群名 —— 省掉无谓的磁盘 IO 与查询;
    有备注名的群也不再查群名(备注优先级更高,查了也用不上)。"""
    from plugins.LGTBot_ElainaBot.mod import state
    pd = _pd()
    calls = {'remarks': 0, 'names': []}

    def _remarks():
        calls['remarks'] += 1
        return {'GR': {'name': '运维备注群'}}
    monkeypatch.setattr(pd, '_group_remarks', _remarks)
    monkeypatch.setattr(pd.userinfo, 'get_group_names',
                        lambda gids: calls['names'].append(list(gids)) or {})
    monkeypatch.setattr(pd.userinfo, 'get_name', lambda uid: '铁蛋')
    state.active_matches.clear()
    state.active_matches['k'] = {'target_id': 'U1', 'is_uid': True,
                                 'game': 'x', 'since': 1}
    try:
        pd._active_matches_view()
        assert calls == {'remarks': 0, 'names': []}          # 纯私聊:两者都不碰

        state.active_matches['g'] = {'target_id': 'GR', 'is_uid': False,
                                     'game': 'x', 'since': 2}
        pd._active_matches_view()
        assert calls['remarks'] == 1
        assert calls['names'] == []                          # 备注已覆盖 → 不查群名
    finally:
        state.active_matches.clear()


def test_match_name_rendering_bolds_remark_only():
    """前端契约:备注名包 <b>,群名普通字重,无名回退灰字 mono openid。"""
    pd = _pd()
    js, css = pd.TAB_JS, pd.TAB_CSS
    assert "m.name_src === 'remark'" in js
    assert 'dash-match-remark' in js and 'dash-match-remark' in css
    assert 'dash-match-id' in js                              # openid 兜底仍在


# ─────────────────────────────────────────────────────────────────────────
# 机器人绑定区的折叠契约(纯前端,只能查模板文本)
# ─────────────────────────────────────────────────────────────────────────

def test_bot_section_payload_carries_raw_binding():
    """折叠判定要区分「显式绑定」与「回退第一个」,靠的是 ``bind_configured``
    这个**原始配置值**(``bound_appid`` 永远有值,分不出来)。"""
    pd = _pd()
    assert 'bind_configured' in pd.TAB_JS
    assert "'bind_configured'" in open(pd.__file__, encoding='utf-8').read()


def test_bot_section_collapse_markup_and_rules():
    """折叠范式与「运行环境」一致;摘要只在折叠态出现,展开后由 CSS 隐藏
    (展开时下方列表已含同样信息,再显示一份就是重复)。"""
    pd = _pd()
    html, css, js = pd.TAB_HTML, pd.TAB_CSS, pd.TAB_JS
    # 结构:section id + 可点标题 + caret + 摘要容器 + 可折叠 body
    for frag in ('id="dash-bot-section"', 'id="dash-bot-toggle"',
                 'id="dash-bot-caret"', 'id="dash-bot-summary"',
                 'id="dash-bot-body"'):
        assert frag in html, frag
    # body 初始就带 collapsed:此刻列表还没被 JS 填充,先收起不会闪
    assert 'class="dash-bot-body collapsed"' in html
    # 摘要仅折叠态可见
    assert '#dash-bot-section:not(.is-collapsed) .dash-bot-summary' in css
    assert '.dash-bot-body.collapsed' in css
    # 折叠态只在首屏定一次,之后换绑刷新不得再改(否则绑定结果提示会被收走)
    assert 'dashBotInited' in js


def test_bot_rows_show_both_group_permissions():
    """全量消息与主动推送是 QQ 后台分别开通的两种权限,面板要**各显各的**;
    折叠摘要与展开列表共用同一个渲染函数,不会只改一处。"""
    pd = _pd()
    js = pd.TAB_JS
    assert 'dashBotPermHtml' in js
    # 断言**带 emoji 的实际输出串**,不是光看标签文字 —— 后者在注释里也出现,
    # 删掉渲染代码照样能蒙混过关
    assert '🌐 全量 ' in js and '📢 主动 ' in js
    assert 'bot.proactive' in js or '.proactive' in js
    # 两处调用同一函数 —— 1 处定义 + 摘要 + 列表行
    assert js.count('dashBotPermHtml') >= 3
