#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""webui 各 page 的**数据组装 + action 端点**测试(page_audit / page_metrics /
page_logs)+ webui/main.py 的注册与惰性 HTML 契约。

  1. fragment 协议 —— action 端点统一返回 ``<pre id="result">JSON</pre>``,
     前端按这个形状解析;返回体一旦漏了 success / 少个字段,页面只会空白,没有任何报错。
  2. ``get_data()`` 的 ``</script>`` 转义 —— 数据里出现该串会当场截断外层
     ``<script>`` 标签,整个面板 JS 崩掉(日志正文完全是用户可控内容)。
  3. register / unregister 对称性 —— 新增端点忘了登记进 ``_HIDDEN_KEYS`` 会
     漏进侧边栏;忘了 unregister_route 会在插件卸载后留下野路由。

aiohttp 是 webui 的顶层 import,dev 机可能没装 → importorskip 守卫(同 test_build_api)。
"""

from __future__ import annotations

import json
import re

import pytest

from plugins.LGTBot_ElainaBot.mod import audit


def _pages():
    pytest.importorskip('aiohttp')
    from plugins.LGTBot_ElainaBot.mod.webui import page_audit, page_logs, page_metrics
    return page_audit, page_metrics, page_logs


def _fragment_payload(html: str) -> dict:
    """按前端的方式解析 ``<pre id="result">…</pre>`` fragment。"""
    m = re.fullmatch(r'<pre id="result">(.*)</pre>', html, re.S)
    assert m, f'不是合法 fragment: {html[:120]!r}'
    import html as _html
    return json.loads(_html.unescape(m.group(1)))


# ─────────────────────────────────────────────────────────────────────────
# page_audit —— 只读展示层
# ─────────────────────────────────────────────────────────────────────────

def test_audit_payload_mirrors_module_state(tmp_path, monkeypatch):
    """payload 的计数 / 条目 / 类别都取自 audit 模块单一真相源。"""
    page_audit, _m, _l = _pages()
    monkeypatch.setattr(audit, 'AUDIT_DIR', str(tmp_path))
    monkeypatch.setattr(audit, 'AUDIT_PATH', str(tmp_path / 'audit.json'))
    audit.record('build', '增量编译', '目标 X', src=audit.SRC_API)
    audit.record('restart', '重启 LGTBot', '', ok=False, src=audit.SRC_CMD)

    p = page_audit._payload()
    assert p['count'] == 2
    assert p['max_entries'] == audit.MAX_ENTRIES
    assert p['size_bytes'] > 0 and p['oldest_ts'] is not None
    # 新 → 旧
    assert [e['action'] for e in p['entries']] == ['重启 LGTBot', '增量编译']
    assert p['entries'][0]['ok'] is False and p['entries'][0]['src'] == audit.SRC_CMD
    # 类别映射随 payload 下发,前端据此渲染徽标 —— 必须覆盖 CATEGORIES 全量
    assert set(p['categories']) == set(audit.CATEGORIES)
    assert p['categories']['build'] == {'emoji': audit.CATEGORIES['build'][0],
                                        'label': audit.CATEGORIES['build'][1]}


def test_audit_render_list_is_success_fragment(tmp_path, monkeypatch):
    page_audit, _m, _l = _pages()
    monkeypatch.setattr(audit, 'AUDIT_DIR', str(tmp_path))
    monkeypatch.setattr(audit, 'AUDIT_PATH', str(tmp_path / 'audit.json'))
    body = _fragment_payload(page_audit.render_list())
    assert body['success'] is True
    assert body['entries'] == [] and body['count'] == 0     # 空态也要是合法 payload


def test_audit_module_exposes_no_mutation_endpoint():
    """★ 安全约束:审计流**不提供**清空 / 删除端点(防自毁审计)。
    有人日后加了 render_clear / delete_handler,这里立刻变红。"""
    page_audit, _m, _l = _pages()
    public = {n for n in dir(page_audit) if not n.startswith('_')}
    assert not {n for n in public
                if re.search(r'clear|delete|remove|purge|reset', n, re.I)}


def test_audit_get_data_escapes_script_close(tmp_path, monkeypatch):
    """审计 detail 是人工输入(如计划重启原因),可能含 </script>。"""
    page_audit, _m, _l = _pages()
    monkeypatch.setattr(audit, 'AUDIT_DIR', str(tmp_path))
    monkeypatch.setattr(audit, 'AUDIT_PATH', str(tmp_path / 'audit.json'))
    audit.record('config', '保存配置', '维护 </script><script>alert(1)</script>')
    data = page_audit.get_data()
    assert '</script>' not in data
    assert json.loads(data.replace('<\\/script>', '</script>'))['count'] == 1


# ─────────────────────────────────────────────────────────────────────────
# page_metrics —— 三区数据组装
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def _metrics_env(monkeypatch):
    """把 page_metrics 依赖的四个数据源全部替换成可控替身。"""
    _a, page_metrics, _l = _pages()
    monkeypatch.setattr(page_metrics.metrics, 'query_game_stats',
                        lambda: {'available': True, 'errors': [],
                                 'lgtbot_users': 50, 'lgtbot_matches': 120,
                                 'lgtbot_match_attendances': 300,
                                 'lgtbot_achievements': 7})
    monkeypatch.setattr(page_metrics.metrics, 'snapshot',
                        lambda: {'upload_total': 1000, 'upload_fail': 1,
                                 'restart_total': 3})
    monkeypatch.setattr(page_metrics.metrics, 'active_push_today',
                        lambda: {'group': 5, 'user': 2})
    monkeypatch.setattr(page_metrics.userinfo, 'count_users', lambda: 200)
    monkeypatch.setattr(page_metrics.userinfo, 'dm_active_count', lambda d: 11)
    monkeypatch.setattr(page_metrics.uploader, 'hosting_availability',
                        lambda: {'cos': 'ok'})
    return page_metrics


def test_metrics_payload_computes_conversion_and_upload_rate(_metrics_env):
    """两个服务端算好的派生值:玩家转化率(2 位)、上传成功率(4 位)。"""
    p = _metrics_env._payload()
    assert p['stats']['player_conversion'] == 25.0          # 50 / 200
    assert p['stats']['dm_active_10d'] == 11
    assert p['runtime']['upload_rate'] == 99.9              # (1000-1)/1000
    assert p['runtime']['restart_total'] == 3               # snapshot 原样并入
    assert p['runtime']['hosting'] == {'cos': 'ok'}
    assert p['game']['available'] is True
    assert p['active_push'] == {'group': 5, 'user': 2}
    assert isinstance(p['query_time'], int)


def test_metrics_payload_none_instead_of_zero_division(monkeypatch, _metrics_env):
    """分母为 0 / 分子缺失 → None(前端显「—」),绝不能抛 ZeroDivisionError 把整个面板首屏打挂。"""
    monkeypatch.setattr(_metrics_env.userinfo, 'count_users', lambda: 0)
    monkeypatch.setattr(_metrics_env.metrics, 'snapshot',
                        lambda: {'upload_total': 0, 'upload_fail': 0})
    p = _metrics_env._payload()
    assert p['stats']['player_conversion'] is None
    assert p['runtime']['upload_rate'] is None

    monkeypatch.setattr(_metrics_env.metrics, 'query_game_stats',
                        lambda: {'available': False, 'errors': ['db 不存在']})
    p2 = _metrics_env._payload()
    assert p2['stats']['lgtbot_users'] is None
    assert p2['stats']['player_conversion'] is None
    assert p2['game']['errors'] == ['db 不存在']            # 错误照实下发


def test_metrics_render_refresh_returns_full_payload(_metrics_env):
    """刷新端点 = success + 完整 payload(三个区一次取齐,前端只发一次请求)。"""
    body = _fragment_payload(_metrics_env.render_refresh())
    assert body['success'] is True
    assert {'stats', 'runtime', 'game', 'active_push', 'query_time'} <= set(body)
    assert body['stats']['users_total'] == 200


def test_metrics_get_data_is_valid_embeddable_json(_metrics_env):
    data = _metrics_env.get_data()
    assert '</script>' not in data
    assert json.loads(data)['stats']['lgtbot_matches'] == 120


def test_metrics_delta_tag_style_contract():
    """★ 涨跌标签的样式契约(纯前端,只能查模板文本):

      · 标签类 ``.metrics-delta-tag`` 存在,涨=红 #e34d59、跌=绿 #00a870 ——
        与 dau 卡片和 stats_image 胶囊三处同色。JS 不得再往 sub 元素上挂 delta 颜色类,着色只发生在标签 span 内。
    """
    _a, page_metrics, _l = _pages()
    css, js = page_metrics.TAB_CSS, page_metrics.TAB_JS
    assert '.metrics-delta-tag' in css
    assert re.search(r'\.metrics-delta-tag\.metrics-delta-up\s*\{[^}]*#e34d59', css)
    assert re.search(r'\.metrics-delta-tag\.metrics-delta-down\s*\{[^}]*#00a870', css)
    # 旧写法:直接给 .metrics-status-sub 上色 —— 已废弃,复活即变红
    assert not re.search(r'\.metrics-status-sub\.metrics-delta-(up|down)', css)
    assert "classList.add('metrics-delta-up'" not in js
    assert "classList.add('metrics-delta-down'" not in js
    # 两处文案都必须走标签函数包裹
    assert js.count('metricsDeltaTag(') >= 3            # 1 处定义 + 2 处调用
    assert '较昨日同时段 \' + metricsDeltaTag(' in js


# ─────────────────────────────────────────────────────────────────────────
# page_logs —— 环形缓冲(数据层)
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def _logs(monkeypatch):
    _a, _m, page_logs = _pages()
    page_logs.clear_logs()
    yield page_logs
    page_logs.clear_logs()


def test_log_incoming_and_outgoing_shape(_logs):
    """群 / 私聊的 kind 判定来自不同字段:入站看 gid 有无,出站看 is_uid。"""
    _logs.log_incoming('U1', 'G1', '开始游戏')
    _logs.log_incoming('U1', '', '私聊内容')
    _logs.log_outgoing('G1', False, '群回复')
    _logs.log_outgoing('U1', True, '私信回复', image=True)
    got = _logs.get_logs()
    assert [(e['direction'], e['kind'], e['uid'], e['gid'], e['image']) for e in got] == [
        ('in',  'group',   'U1', 'G1', False),
        ('in',  'private', 'U1', '',   False),
        ('out', 'group',   '',   'G1', False),
        ('out', 'private', 'U1', '',   True),
    ]
    assert all(isinstance(e['time'], float) for e in got)


def test_log_none_content_normalized(_logs):
    """content / uid / gid 为 None 时归一成空串 —— JSON 里 null 会让前端筛选与高亮逻辑走进未定义分支。"""
    _logs.log_incoming(None, None, None)
    _logs.log_outgoing('U1', True, None)
    assert all(e['content'] == '' for e in _logs.get_logs())
    assert _logs.get_logs()[0]['uid'] == '' and _logs.get_logs()[0]['gid'] == ''


def test_log_ring_buffer_drops_oldest(_logs):
    """deque(maxlen) 上限:超出后丢最旧,内存不会无限涨。"""
    for i in range(_logs._MAX_LOGS + 20):
        _logs.log_incoming('U1', 'G1', f'msg{i}')
    got = _logs.get_logs()
    assert len(got) == _logs._MAX_LOGS
    assert got[0]['content'] == 'msg20' and got[-1]['content'] == \
        f'msg{_logs._MAX_LOGS + 19}'


def test_log_buffer_lives_in_persistent_dict(_logs):
    """★ 跨热重载:deque 必须挂在 boot._get_persistent() 上
    旧 callbacks 写的日志要能被新注册的页面读到。这里直接核对是同一个对象。"""
    from plugins.LGTBot_ElainaBot.mod import boot
    assert _logs._logs is boot._get_persistent()['logs_deque']
    _logs.log_incoming('U1', 'G1', 'via module')
    assert boot._get_persistent()['logs_deque'][-1]['content'] == 'via module'


def test_log_get_data_escapes_script_close(_logs):
    """消息正文完全是用户可控内容,``</script>`` 必须转义。"""
    _logs.log_incoming('U1', 'G1', 'pwn </script><script>alert(1)</script>')
    data = _logs.get_data()
    assert '</script>' not in data and '<\\/script>' in data
    restored = json.loads(data.replace('<\\/script>', '</script>'))
    assert restored[0]['content'] == 'pwn </script><script>alert(1)</script>'


def test_log_clear_empties_buffer(_logs):
    _logs.log_incoming('U1', 'G1', 'x')
    _logs.clear_logs()
    assert _logs.get_logs() == [] and json.loads(_logs.get_data()) == []


# ─────────────────────────────────────────────────────────────────────────
# page_config —— 「配置管理」的版式契约
# ─────────────────────────────────────────────────────────────────────────

def _page_config():
    pytest.importorskip('aiohttp')
    from plugins.LGTBot_ElainaBot.mod.webui import page_config
    return page_config


def test_config_layout_yaml_full_width_rest_in_grid():
    """插件配置通栏在最上,其余编辑器全在 .cfg-grid 里,且**顺序即行序**。

    grid 靠 DOM 顺序自动配对左右两栏,所以挪动任何一块都会改变分行 ——
    这条断言就是那份"重要更新|更新公告 / 紧急公告|疑难解答 / …"的版式说明书。
    """
    pc = _page_config()
    html = pc.TAB_HTML
    i_grid = html.index('<div class="cfg-grid">')
    assert html.index('cfg-yaml-editor') < i_grid, '插件配置应在分栏之外(通栏)'
    order = ['cfg-important-editor', 'cfg-notice-editor',
             'cfg-urgent-editor', 'cfg-trouble-editor',
             'cfg-sponsors-editor', 'cfg-engine-editor']
    idx = [html.index(e) for e in order]
    assert all(i > i_grid for i in idx), '除插件配置外都要进分栏'
    assert idx == sorted(idx), f'分栏内顺序变了,版式会跟着变: {order}'


def test_config_grid_is_two_columns_only_on_wide_screens():
    """基线单列(窄屏不变),两列只在 @media min-width 里开 —— 且断点要够宽:
    编辑器折成两栏后每栏太窄的话,文本框一行放不下几个字。"""
    import re as _re
    pc = _page_config()
    css = pc.TAB_CSS
    base = css[css.index('.cfg-grid {'):]
    base = base[:base.index('}')]
    assert 'grid-template-columns: 1fr;' in base, base
    m = _re.search(r'@media \(min-width: (\d+)px\)\s*\{\s*\.cfg-grid \{([^}]*)\}', css)
    assert m, '缺少宽屏两列的 @media 规则'
    assert '1fr 1fr' in m.group(2) or 'repeat(2' in m.group(2), m.group(2)
    assert int(m.group(1)) >= 900, f'断点 {m.group(1)}px 太窄,两栏编辑器挤不下'


@pytest.mark.parametrize('sponsor_on', [True, False])
def test_config_html_div_balance_survives_section_strip(monkeypatch, sponsor_on):
    """★ 分栏包了一层 div,``<div>`` / ``</div>`` 必须始终配平 —— 两态都要。

    真实风险:若 SPONSOR 标记一头在分栏内、一头在分栏外,服务端整段切除会顺手吃掉 grid 的收尾 ``</div>``,
    整个页面结构塌掉(浏览器容错后表现为版式乱掉,没有任何报错)。
    """
    pc = _page_config()
    from plugins.LGTBot_ElainaBot.mod import buttons
    monkeypatch.setattr(buttons, 'SPONSOR_ENABLED', sponsor_on)
    html = pc.render_tab_html()
    assert html.count('<div') == html.count('</div>'), (
        html.count('<div'), html.count('</div>'))
    assert html.count('<section') == html.count('</section>')
    # 分栏容器本身在两态下都完好
    assert html.count('<div class="cfg-grid">') == 1


# ─────────────────────────────────────────────────────────────────────────
# webui/main.py —— 注册契约
# ─────────────────────────────────────────────────────────────────────────

def _main():
    pytest.importorskip('aiohttp')
    from plugins.LGTBot_ElainaBot.mod.webui import main as webui_main
    return webui_main


@pytest.fixture
def _clean_registry(monkeypatch):
    """给 web_pages 换上干净的 _registry / _routes + **未包装**的 get_pages。

    ``register()`` 装的过滤 wrap 是故意不解的(见 unregister 尾注),会跨测试残留;
    这里退回链条最内层,让每个用例都从同一个起点开始,与执行顺序无关。
    """
    pytest.importorskip('aiohttp')
    from core.plugin import web_pages
    base = web_pages.get_pages
    while getattr(base, '_lgtbot_wrapped', False):
        base = base._lgtbot_inner
    monkeypatch.setattr(web_pages, '_registry', {}, raising=False)
    monkeypatch.setattr(web_pages, '_routes', {}, raising=False)
    monkeypatch.setattr(web_pages, 'get_pages', base, raising=False)
    return web_pages


def test_register_then_unregister_leaves_nothing_behind(_clean_registry):
    """★ 对称性回归:注册的**每个** page key 与路由都要能被 unregister 清干净。
    新增端点忘了同步 unregister,插件卸载后会留野路由(旧 handler 持有已卸载模块的引用)。"""
    wm = _main()
    wm.register()
    assert _clean_registry._registry and _clean_registry._routes
    wm.unregister()
    assert _clean_registry._registry == {}
    assert _clean_registry._routes == {}


def test_all_action_keys_are_hidden_from_sidebar(_clean_registry):
    """除主页外,注册的每个 key 都必须在 _HIDDEN_KEYS 里 —— 否则会以空白页形态出现在框架侧边栏。"""
    wm = _main()
    wm.register()
    keys = set(_clean_registry._registry)
    assert wm.PAGE_KEY in keys
    assert (keys - {wm.PAGE_KEY}) <= set(wm._HIDDEN_KEYS)
    # 反向:过滤 wrap 之后侧边栏只剩主页
    visible = [p['key'] for p in _clean_registry.get_pages()]
    assert visible == [wm.PAGE_KEY]


def test_lazy_html_dict_defers_provider_until_getitem():
    """★ 双取值防护:框架 get_page_html 先 ``.get('html')`` 做 truthy 检查再 ``[...]`` 取值。
    provider 有副作用(重启端点会释放 C++ 引擎),跑两遍就是对已 freed 的 g_bot_core 二次 deref → tcache double-free。
    所以 .get 只能返回占位,provider 只在 __getitem__ 时调用一次。"""
    wm = _main()
    calls = []
    d = wm._LazyHtmlDict({'key': 'k', 'label': 'L'}, lambda: calls.append(1) or '<b>x</b>')
    assert d.get('html') is True and calls == []      # 检查阶段不触发
    assert d['html'] == '<b>x</b>' and len(calls) == 1
    assert d['html'] == '<b>x</b>' and len(calls) == 2  # 每次真取值各生成一次
    # 其他键保持普通 dict 语义
    assert d.get('label') == 'L' and d['key'] == 'k'
    assert d.get('missing', 'dft') == 'dft'


def test_get_pages_wrap_is_idempotent_and_chains(_clean_registry):
    """幂等(不重复包)+ 链式(保留 _lgtbot_inner,兼容其他插件的后续 wrap)。"""
    wm = _main()
    original = _clean_registry.get_pages
    wm._ensure_get_pages_filters_hidden()
    wrapped = _clean_registry.get_pages
    assert wrapped is not original
    assert wrapped._lgtbot_wrapped is True and wrapped._lgtbot_inner is original
    wm._ensure_get_pages_filters_hidden()             # 第二次:不再套娃
    assert _clean_registry.get_pages is wrapped


def test_hidden_action_entry_shape(_clean_registry):
    """隐藏端点的基础字段:label 为空 + source 归属本插件,框架据此归类。"""
    wm = _main()
    wm._register_hidden_action('__test_key', lambda: '<pre id="result">{}</pre>')
    entry = _clean_registry._registry['__test_key']
    assert entry['source'] == 'plugin' and entry['source_name'] == 'LGTBot_ElainaBot'
    assert entry.get('label') == '' and entry['key'] == '__test_key'
    assert entry['html'] == '<pre id="result">{}</pre>'


def test_build_and_restart_apis_registered_without_panel_auth(_clean_registry):
    """编译 / 重启 API 必须 auth=False(面板登录态不适用,handler 内验 token);
    面板自身的路由必须 auth=True —— 两者搞反等于把面板操作暴露给未认证请求。"""
    wm = _main()
    wm.register()
    routes = {(m, p): info for (m, p), info in _clean_registry._routes.items()}
    open_paths = {p for (m, p), info in routes.items() if not info.get('auth', True)}
    assert open_paths == {
        '/api/ext/lgtbot/build/compile',
        '/api/ext/lgtbot/build/terminate',
        '/api/ext/lgtbot/restart',
        '/api/ext/lgtbot/planned-restart',
    }
    # 面板侧带参路由一律要登录态
    assert routes[('GET', '/api/ext/lgtbot/backup/restore')]['auth'] is True
    assert routes[('POST', '/api/ext/lgtbot/config/save')]['auth'] is True


# ─────────────────────────────────────────────────────────────────────────
# 面板重启:可选「更新内容」+ 等待中房间通知
# ─────────────────────────────────────────────────────────────────────────

async def test_panel_restart_passes_reason_and_notifies_rooms(monkeypatch):
    """★ 面板重启走带 ?reason= 的真路由(隐藏 action 的 provider 接不了参数),
    并把更新内容原样交给房间通知;面板不在任何群里 → 不排除任何群。"""
    wm = _main()
    from plugins.LGTBot_ElainaBot.mod import dispatcher
    from plugins.LGTBot_ElainaBot.mod import state as _st
    _st.waiting_rooms['g:GWAIT'] = {'target_id': 'GWAIT', 'is_uid': False,
                                    'game': 'X', 'since': 0}

    def fake_check():
        _st.waiting_rooms.clear()        # 释放引擎会解散等待中房间(真实副作用)
        return True, '🔁 正在重启'

    monkeypatch.setattr(dispatcher, 'check_and_prepare_restart', fake_check)
    monkeypatch.setattr(dispatcher, 'schedule_exec_after', lambda *a, **k: None)
    monkeypatch.setattr(wm.metrics, 'record_restart', lambda: None)
    monkeypatch.setattr(wm.audit, 'record', lambda *a, **k: None)
    seen = {}

    async def fake_notify(reason='', *, skip_keys=frozenset(), rooms=None):
        seen.update(reason=reason, skip=set(skip_keys), rooms=rooms)
        return 0

    monkeypatch.setattr(dispatcher, '_notify_restart_rooms', fake_notify)
    frag = await wm._render_restart('新版本上线')
    assert '正在重启' in frag
    assert seen['reason'] == '新版本上线' and seen['skip'] == set()
    # ★ 快照在释放引擎之前取到了那个房间
    assert [r['target_id'] for r in seen['rooms']] == ['GWAIT']


async def test_panel_restart_rejected_does_not_notify(monkeypatch):
    """有进行中对局被拒 → 不通知、不调度 exec。"""
    wm = _main()
    from plugins.LGTBot_ElainaBot.mod import dispatcher
    monkeypatch.setattr(dispatcher, 'check_and_prepare_restart',
                        lambda: (False, '⚠️ 有对局'))
    monkeypatch.setattr(wm.audit, 'record', lambda *a, **k: None)
    called = []
    monkeypatch.setattr(dispatcher, 'schedule_exec_after',
                        lambda *a, **k: called.append('exec'))
    monkeypatch.setattr(dispatcher, '_notify_restart_rooms',
                        lambda *a, **k: called.append('notify'))
    await wm._render_restart('x')
    assert called == []


def test_panel_restart_route_registered(_clean_registry):
    """按钮改走 register_route 后,注册 / 注销都要跟上(漏了按钮直接 404)。"""
    wm = _main()
    wm.register()
    assert ('GET', wm._RESTART_PANEL_ROUTE) in _clean_registry._routes
    wm.unregister()
    assert ('GET', wm._RESTART_PANEL_ROUTE) not in _clean_registry._routes


def test_main_js_uses_the_restart_route():
    """模板占位与常量对得上 —— 拼错的话按钮会 fetch 到字面量 __RESTART_ROUTE__。"""
    wm = _main()
    html = wm._render_html()
    assert '__RESTART_ROUTE__' not in html
    assert wm._RESTART_PANEL_ROUTE in html


async def test_panel_restart_handler_forwards_query_reason(monkeypatch):
    """★ handler 要把 ``?reason=`` 交下去 —— 丢了的话面板输入框填了也白填。"""
    wm = _main()
    seen = {}

    async def fake_render(reason=''):
        seen['reason'] = reason
        return '<div id="msg">ok</div>'

    monkeypatch.setattr(wm, '_render_restart', fake_render)

    class _Req:
        query = {'reason': '  修复图床  '}

    resp = await wm.restart_panel_handler(_Req())
    assert seen['reason'] == '修复图床'          # strip 过
    assert 'ok' in resp.text


async def test_panel_restart_handler_truncates_long_reason(monkeypatch):
    """超长更新内容截断,别把推送撑爆(与指令 / API 同一上限)。"""
    wm = _main()
    seen = {}

    async def fake_render(reason=''):
        seen['reason'] = reason
        return '<div id="msg">ok</div>'

    monkeypatch.setattr(wm, '_render_restart', fake_render)

    class _Req:
        query = {'reason': 'x' * 500}

    await wm.restart_panel_handler(_Req())
    assert len(seen['reason']) == wm._RESTART_REASON_MAX
