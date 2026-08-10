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
