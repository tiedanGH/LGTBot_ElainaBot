#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""赞助功能测试 —— 总开关 ``sponsor_enabled`` 的两态行为。

这个功能的核心风险不是"能不能显示",而是**关闭时是否真的一点都不露**：
插件会进插件市场，第三方部署方的用户绝不该看到作者的收款引导。所以本文件
把「关」态当成被测主角：

  · 关：三个入口(更多功能 / 关于 / 更新公告)都不带赞助按钮
  · 关：「赞助支持」指令原样转发给引擎，插件不自造回复
  · 关：历史消息里的旧按钮(INTERACTION)只 ack，不回赞助菜单
  · 开：名单走 markdown **引用块**(保留行内语法) + 实时读盘
  · 开：回执附「赞助页面 / 爱发电」两个 link 按钮
  · 「赞助支持」在 _EXCLUSIVE_RES 内 —— 否则关态转发会让引擎收到两次
  · config 下发：非 bool 值一律忽略并保留现值(避免"以为开了其实没开")
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.LGTBot_ElainaBot.mod import buttons, config, dispatcher


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

def _ev(*, is_interaction=False, is_group=True, content='赞助支持'):
    ev = MagicMock()
    ev.is_group = is_group
    ev.is_direct = not is_group
    ev.is_interaction = is_interaction
    ev.group_id = 'G1' if is_group else ''
    ev.user_id = 'U1'
    ev.content = content
    ev.reply = AsyncMock()
    ev.ack_interaction = AsyncMock()
    return ev


@pytest.fixture(autouse=True)
def _no_foreign(monkeypatch):
    """本插件的 handler 都先过 is_foreign_event 闸,测试里恒放行。"""
    monkeypatch.setattr(dispatcher.helpers, 'is_foreign_event', lambda e: False)


@pytest.fixture
def sponsors_file(tmp_path, monkeypatch):
    """把名单文件指到 tmp,返回一个「写入并返回路径」的函数。"""
    path = tmp_path / 'sponsors.txt'
    monkeypatch.setattr(dispatcher, '_SPONSORS_PATH', str(path))

    def _write(text: str):
        path.write_text(text, encoding='utf-8')
        return path
    return _write


# ─────────────────────────────────────────────────────────────────────────
# 1. 引用块渲染
# ─────────────────────────────────────────────────────────────────────────

def test_as_quote_prefixes_every_line_including_blanks():
    """空行也要带 ``>`` —— 否则 QQ 客户端会在空行处把引用截成两块。"""
    assert dispatcher._as_quote('a\n\nb') == '> a\n>\n> b'


def test_as_quote_keeps_inline_markdown():
    """引用(而非代码块)的意义:管理员在 txt 里写的加粗 / emoji 照常生效。"""
    assert dispatcher._as_quote('**铁蛋** ❤️') == '> **铁蛋** ❤️'


# ─────────────────────────────────────────────────────────────────────────
# 2. 按钮组的开关闸
# ─────────────────────────────────────────────────────────────────────────

def test_buttons_hidden_when_disabled(monkeypatch):
    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', False)
    assert buttons.build_sponsor_entry_buttons() == []
    flat = [b['text'] for row in buttons.build_more_features_buttons() for b in row]
    flat += [b['text'] for row in buttons.build_about_buttons() for b in row]
    assert not any('赞助' in t for t in flat)


def test_buttons_appear_when_enabled(monkeypatch):
    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', True)
    assert buttons.build_sponsor_entry_buttons() == [[buttons.BTN_SPONSOR]]
    assert buttons.build_more_features_buttons()[-1] == [buttons.BTN_SPONSOR]
    assert buttons.build_about_buttons()[-1] == [buttons.BTN_SPONSOR]
    # 「更多功能」加完仍不超 QQ 键盘 5 行上限
    assert len(buttons.build_more_features_buttons()) <= 5


def test_sponsor_entry_is_callback_not_input_fill():
    """入口按钮走 type=1(点击直接触发),不回填输入框;style=1。"""
    assert buttons.BTN_SPONSOR['type'] == 1
    assert buttons.BTN_SPONSOR['style'] == 1
    assert buttons.BTN_SPONSOR['data'] == '赞助支持'


def test_sponsor_reply_buttons_are_links():
    """回执底部两个都是 link 按钮(只有 text + link,不依赖 bot 进程存活)。"""
    row = buttons.build_sponsor_buttons()[0]
    assert len(row) == 2
    assert all(set(b) == {'text', 'link'} for b in row)
    assert 'tiedan.site/pages/support' in row[0]['link']
    assert 'afdian.com' in row[1]['link']


# ─────────────────────────────────────────────────────────────────────────
# 3. 「赞助支持」handler:开 / 关两态
# ─────────────────────────────────────────────────────────────────────────

async def test_sponsor_reply_when_enabled(monkeypatch, sponsors_file):
    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', True)
    sponsors_file('**阿蛋** ×3\n某位好心人')
    ev = _ev()
    await dispatcher.lgtbot_sponsor(ev, None)

    md = ev.reply.await_args.args[0]
    assert '## ❤️ 赞助支持' in md
    assert '> **阿蛋** ×3' in md          # 引用块 + 行内加粗保留
    assert '> 某位好心人' in md
    assert '```' not in md               # 明确不是代码块
    assert '2295824927' in md            # 赞助后可私信联系
    assert '付费解锁' in md               # 与 LGPLv2 一致的措辞
    assert ev.reply.await_args.kwargs['buttons'] == buttons.build_sponsor_buttons()


async def test_sponsor_list_is_read_live(monkeypatch, sponsors_file):
    """每次指令都重新读盘 —— 面板改完下一条指令就生效,不需要重载插件。"""
    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', True)
    sponsors_file('第一版')
    ev1 = _ev()
    await dispatcher.lgtbot_sponsor(ev1, None)
    assert '> 第一版' in ev1.reply.await_args.args[0]

    sponsors_file('第二版')
    ev2 = _ev()
    await dispatcher.lgtbot_sponsor(ev2, None)
    assert '> 第二版' in ev2.reply.await_args.args[0]


async def test_sponsor_forwards_to_engine_when_disabled(monkeypatch):
    """关态:消息事件原样转发给引擎(_from_exclusive 跳过专属指令闸),插件不回复。"""
    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', False)
    ev = _ev()
    with patch.object(dispatcher, 'lgtbot_dispatch', new=AsyncMock()) as fwd:
        await dispatcher.lgtbot_sponsor(ev, None)
    ev.reply.assert_not_awaited()
    fwd.assert_awaited_once()
    assert fwd.await_args.kwargs.get('_from_exclusive') is True


async def test_sponsor_stale_button_only_acked_when_disabled(monkeypatch):
    """关态 + INTERACTION(历史消息里的旧按钮):ack 掉即可,既不回复也不转发。"""
    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', False)
    ev = _ev(is_interaction=True)
    with patch.object(dispatcher, 'lgtbot_dispatch', new=AsyncMock()) as fwd:
        await dispatcher.lgtbot_sponsor(ev, None)
    ev.ack_interaction.assert_awaited_once()
    ev.reply.assert_not_awaited()
    fwd.assert_not_awaited()


def test_sponsor_is_exclusive_command():
    """必须在专属指令表里 —— 否则关态转发后 catch-all 会让引擎收到第二次。"""
    assert dispatcher._is_exclusive_command('赞助支持')
    assert dispatcher._is_exclusive_command('/赞助支持')


# ─────────────────────────────────────────────────────────────────────────
# 4. 「更新公告」下方的入口按钮
# ─────────────────────────────────────────────────────────────────────────

async def test_update_notice_carries_sponsor_button_only_when_enabled(monkeypatch):
    monkeypatch.setattr(dispatcher, '_read_update_notice', lambda: '公告正文')
    monkeypatch.setattr(dispatcher, '_read_important_update', lambda: '')

    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', True)
    ev_on = _ev(content='更新公告')
    await dispatcher.lgtbot_update_notice(ev_on, None)
    assert ev_on.reply.await_args.kwargs['buttons'] == [[buttons.BTN_SPONSOR]]

    # 关态不能传空 keyboard,应该整个不带 buttons 参数
    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', False)
    ev_off = _ev(content='更新公告')
    await dispatcher.lgtbot_update_notice(ev_off, None)
    assert 'buttons' not in ev_off.reply.await_args.kwargs


# ─────────────────────────────────────────────────────────────────────────
# 5. config 下发
# ─────────────────────────────────────────────────────────────────────────

def _base_cfg(**overrides) -> dict:
    cfg = {k: (list(v) if isinstance(v, list) else v)
           for k, v in config.DEFAULT_CONFIG.items()}
    cfg.update(overrides)
    return cfg


def test_default_config_ships_sponsor_off():
    assert config.DEFAULT_CONFIG['sponsor_enabled'] is False


def test_apply_toggles_sponsor_flag(monkeypatch):
    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', False)
    config._apply_runtime_tunables(_base_cfg(sponsor_enabled=True))
    assert buttons.SPONSOR_ENABLED is True
    config._apply_runtime_tunables(_base_cfg(sponsor_enabled=False))
    assert buttons.SPONSOR_ENABLED is False


def test_apply_missing_key_means_off(monkeypatch):
    """老配置文件没有这个字段 → 按关闭处理,不继承上一次的开启状态。"""
    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', True)
    cfg = _base_cfg()
    cfg.pop('sponsor_enabled')
    config._apply_runtime_tunables(cfg)
    assert buttons.SPONSOR_ENABLED is False


@pytest.mark.parametrize('bad', ['true', 'yes', 1, ['x']])
def test_apply_non_bool_keeps_current_value(monkeypatch, bad):
    """近似值(字符串 / 1)一律忽略并保留现值 —— 宁可不变也不要猜。"""
    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', True)
    config._apply_runtime_tunables(_base_cfg(sponsor_enabled=bad))
    assert buttons.SPONSOR_ENABLED is True
    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', False)
    config._apply_runtime_tunables(_base_cfg(sponsor_enabled=bad))
    assert buttons.SPONSOR_ENABLED is False


def test_validator_requires_real_bool():
    """面板校验器与运行时同语义:bool 字段填数字是错误,填 true/false 通过。"""
    pytest.importorskip('aiohttp')
    from plugins.LGTBot_ElainaBot.mod.webui import page_config

    errors, _w = page_config._validate_config_yaml('sponsor_enabled: 1')
    assert any('sponsor_enabled' in e and '布尔' in e for e in errors)

    errors2, _w2 = page_config._validate_config_yaml('sponsor_enabled: true')
    assert not any('sponsor_enabled' in e for e in errors2)


# ─────────────────────────────────────────────────────────────────────────
# 6. Web 面板:关态整段不渲染
# ─────────────────────────────────────────────────────────────────────────

def _page_config():
    pytest.importorskip('aiohttp')
    from plugins.LGTBot_ElainaBot.mod.webui import page_config
    return page_config


def test_panel_section_rendered_when_enabled(monkeypatch):
    pc = _page_config()
    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', True)
    html = pc.render_tab_html()
    assert 'cfg-sponsors-editor' in html
    assert '赞助鸣谢' in html


def test_panel_section_stripped_when_disabled(monkeypatch):
    """关态:区块从 HTML 里整段切掉 —— 不是 CSS 隐藏,源码里也不留赞助字样。"""
    pc = _page_config()
    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', False)
    html = pc.render_tab_html()
    assert 'cfg-sponsors-editor' not in html
    assert '赞助' not in html
    # 只切赞助那一段,前后的区块必须都还在
    assert 'cfg-trouble-editor' in html
    assert 'cfg-engine-editor' in html
    assert html.count('<section class="dash-section">') == \
        pc.TAB_HTML.count('<section class="dash-section">') - 1


def test_panel_data_omits_sponsors_when_disabled(monkeypatch):
    """关态连数据都不下发:名单内容与文件路径都不进页面源码。"""
    pc = _page_config()
    import json

    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', False)
    off = json.loads(pc.get_data())
    assert 'sponsors' not in off
    assert 'troubleshooting' in off          # 其他块不受影响

    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', True)
    on = json.loads(pc.get_data())
    assert 'sponsors' in on
    assert on['sponsors']['abs_path'].endswith('sponsors.txt')


def test_panel_js_entry_stripped_when_disabled(monkeypatch):
    """只藏 HTML 不够 —— JS 里的 saveHint 文案同样会进页面源码。"""
    pc = _page_config()

    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', True)
    assert 'cfg-sponsors-editor' in pc.render_tab_js()

    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', False)
    js = pc.render_tab_js()
    assert 'cfg-sponsors-editor' not in js
    assert '赞助' not in js
    # 只切赞助那一项,其余表项与后续代码必须完好
    for key in ('config_yaml', 'important_update', 'update_notice',
                'troubleshooting', 'engine_config'):
        assert f"dataKey: '{key}'" in js
    assert 'function cfgApplyData' in js
    assert 'cfgReloadConfig' in js
    assert js.count('dataKey:') == pc.TAB_JS.count('dataKey:') - 1


def test_every_editor_has_a_data_key():
    """cfgApplyData 靠 dataKey 找数据 —— 漏一个该编辑器就永远填不上内容。"""
    pc = _page_config()
    # 表项数(按 editorId 计)必须与 dataKey 数一致
    assert pc.TAB_JS.count('dataKey:') == pc.TAB_JS.count('editorId:')


@pytest.mark.parametrize('attr,label', [('TAB_HTML', 'html'), ('TAB_JS', 'js')])
def test_panel_markers_missing_falls_back_to_full_template(monkeypatch, attr, label):
    """模板被改坏(标记不见了)时原样返回,不做半截切割。"""
    pc = _page_config()
    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', False)
    monkeypatch.setattr(pc, attr, 'no markers here')
    render = pc.render_tab_html if label == 'html' else pc.render_tab_js
    assert render() == 'no markers here'


# ─────────────────────────────────────────────────────────────────────────
# 7. 备份清单
# ─────────────────────────────────────────────────────────────────────────

def test_sponsors_txt_is_backed_up():
    """名单是手工维护的内容,丢了没法从任何地方重建 —— 必须进备份清单。"""
    from plugins.LGTBot_ElainaBot.mod import backup, boot
    import os

    path = os.path.join(boot.DATA_DIR, 'sponsors.txt')
    os.makedirs(boot.DATA_DIR, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('someone')
    try:
        arcs = [arc for arc, _p, _k in backup._collect_sources()]
        assert 'data/sponsors.txt' in arcs
    finally:
        os.remove(path)
