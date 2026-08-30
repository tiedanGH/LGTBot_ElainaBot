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
    assert p['retention_days'] == audit.RETENTION_DAYS
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


def test_audit_detail_column_is_fixed_width_on_mobile():
    """★ 窄屏下详情列给定宽。"""
    page_audit, _m, _l = _pages()
    css = page_audit.TAB_CSS
    blocks = re.findall(r'@media \(max-width: 600px\) \{(.*?)\n\}', css, re.S)
    assert blocks, '没找到窄屏 @media 块'
    m = '\n'.join(blocks)
    assert '.audit-col-detail { width: 100%; max-width: 0; }' in css.replace(m, '')
    assert re.search(r'\.audit-col-detail \{ width: (\d+)px; min-width: \1px; \}', m)
    assert '.audit-table { width: max-content; min-width: 100%; }' in m


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


# ─────────────────────────────────────────────────────────────────────────
# SVG 图标
# ─────────────────────────────────────────────────────────────────────────


def _sticky_top(html: str) -> str:
    """顶栏 + 标签导航那一段(不含各标签的内容)。"""
    i = html.index('<div class="sticky-top">')
    return html[i:html.index('<div class="page-content">', i)]


def test_every_tab_has_an_icon():
    """每个标签都要有,漏一个就是一行图标对不齐的标签。"""
    wm = _main()
    top = _sticky_top(wm._render_html())
    tabs = re.findall(r'<button class="tab[^"]*" data-tab="(\w+)">(.*?)</button>', top)
    assert len(tabs) == 11
    for name, body in tabs:
        assert re.match(r'<svg class="ui-icon tab-icon"><use href="#i-[\w-]+"/></svg>',
                        body), name


def test_top_bar_buttons_have_icons():
    wm = _main()
    top = _sticky_top(wm._render_html())
    for btn, icon in (('fullscreen-btn', 'i-fullscreen'),
                      ('theme-toggle', 'i-theme-auto'),
                      ('planned-restart-btn', 'i-maintenance'),
                      ('restart-btn', 'i-restart')):
        assert re.search(r'id="%s".*?<use href="#%s"/>' % (btn, icon), top), btn


def test_sprite_defines_exactly_the_icons_the_page_uses():
    """★ 引用与定义必须对齐两侧:少一个是空白占位,多一个是没人用的死图元。"""
    wm = _main()
    html = wm._render_html()
    defined = set(re.findall(r'<symbol id="(i-[\w-]+)"', html))
    used = set(re.findall(r'<use href="#(i-[\w-]+)"', html))
    # 主题的三态由 JS 换 href,页面上只写死了自动态那一个
    used |= {'i-theme-light', 'i-theme-dark'}
    assert defined == used, (defined ^ used)
    assert len(defined) == 17


def test_icons_follow_the_current_text_colour():
    """★ 换 SVG 的目的之一就是跟着主题 / 选中态换色 —— 图元里写死颜色就白换了。"""
    wm = _main()
    html = wm._render_html()
    css = html[:html.index('</style>')]
    assert re.search(r'\.ui-icon \{[^}]*stroke: currentColor', css, re.S)
    assert re.search(r'\.ui-icon \{[^}]*fill: none', css, re.S)
    sprite = html[html.index('<svg xmlns'):html.index('</svg>') + 6]
    assert not re.search(r'(?:fill|stroke)="(?!none\b|currentColor\b)[^"]+"', sprite)
    # sprite 本身不占位
    assert '.icon-sprite { display: none; }' in css


def test_theme_button_swaps_the_use_href_not_the_button_text():
    """★ 按钮里现在是 SVG,写 textContent 会把它整个冲掉,图标从此消失。"""
    wm = _main()
    html = wm._render_html()
    assert ("const THEME_ICON = {auto: '#i-theme-auto', light: '#i-theme-light', "
            "dark: '#i-theme-dark'};") in html
    assert "if (use) use.setAttribute('href', THEME_ICON[themeMode]);" in html
    assert 'btn.textContent = THEME_ICON' not in html


def test_planned_restart_rewrites_only_its_label_span():
    """★ 同上:整个按钮写 textContent 会连图标一起抹掉。"""
    wm = _main()
    html = wm._render_html()
    assert 'id="planned-restart-label"' in html
    assert "label.textContent = on ? '取消计划重启' : '计划重启';" in html
    assert 'btn.textContent = on ?' not in html


def test_title_carries_the_inlined_site_logo():
    """★ 站标走 data URI:面板 HTML 由框架路由吐出,页面里的相对路径落不到插件
    目录,单为一张图开一条静态路由不值当。"""
    wm = _main()
    html = wm._render_html()
    assert re.search(r'<h1><img class="topbar-logo" src="data:image/png;base64,[A-Za-z0-9+/=]+"'
                     r' alt="">LGTBot 机器人</h1>', html)
    assert '__LOGO_DATA_URI__' not in html          # 占位符必须被替换掉
    css = html[:html.index('</style>')]
    assert '.topbar-logo { width: 26px; height: 26px;' in css
    # 标题要缩进:不补的话站标贴着滚动区左沿,比下面每个标签都更靠左
    indent = re.search(r'\.topbar h1 \{[^}]*padding-left: (\d+)px', css, re.S)
    assert indent and int(indent.group(1)) > 0


def test_missing_logo_file_does_not_break_the_page(tmp_path, monkeypatch):
    """★ 读不到图就是一张破图,页面其余部分照常 —— 面板不该因为少一张装饰图
    整个打不开(站标在 import 期读盘,抛出去就是插件加载失败)。"""
    wm = _main()
    monkeypatch.setattr(wm, '_LOGO_PATH', str(tmp_path / 'nope.png'))
    assert wm._logo_data_uri() == ''
    monkeypatch.setattr(wm, '_LOGO_DATA_URI', '')
    html = wm._render_html()
    assert '<img class="topbar-logo" src="" alt="">LGTBot 机器人' in html
    assert '<div class="tabs">' in html


def test_restart_icon_is_drawn_heavier_but_smaller_than_the_rest():
    """★ 重启那枚照 🔁 画成回路,描边比其余图标粗一档 —— 写在图元上才压得过
    .ui-icon 继承下来的那一档;同时单独收小一号,否则又粗又同尺寸会显得大一圈。"""
    wm = _main()
    html = wm._render_html()
    css = html[:html.index('</style>')]
    sym = re.search(r'<symbol id="i-restart".*?</symbol>', html, re.S).group(0)
    assert sym.count('stroke-width="2"') == 4       # 四段(两条弧 + 两个箭头)都要加粗
    stroke = re.search(r'\.ui-icon \{[^}]*stroke-width: ([\d.]+)', css, re.S)
    assert stroke and float(stroke.group(1)) < 2
    size = re.search(r'\.ui-icon \{[^}]*width: (\d+)px', css, re.S)
    own = re.search(r'#restart-btn \.ui-icon \{ width: (\d+)px', css)
    assert size and own and int(own.group(1)) < int(size.group(1))


def test_theme_toggle_is_tri_state_and_defaults_to_auto():
    """★ 主题三态:自动 → 浅色 → 深色 → 自动,默认自动。

    「自动」= 跟随主框架面板的夜间模式。框架把开关写在同源 localStorage 的
    ``elaina_dark``('1' = 夜间),本页是框架面板 iframe 里的一块,只能靠这个键跟随
    键名写错的话自动模式永远停在浅色,而且不会报任何错。
    """
    wm = _main()
    html = wm._render_html()
    assert "const THEME_MODES = ['auto', 'light', 'dark']" in html
    # 自动态由框架开关解析,不是跟随系统 prefers-color-scheme
    assert "localStorage.getItem(FRAMEWORK_DARK_KEY) === '1'" in html
    assert "return frameworkDark() ? 'dark' : 'light';" in html
    # 默认自动:没存过 / 存的是旧值都落到 auto
    assert "applyTheme(saved || 'auto')" in html
    # 首屏按钮就是自动态图标,不能还是浅色那一个
    assert re.search(r'id="theme-toggle".*?<use href="#i-theme-auto"', html)
    # 点击按 THEME_MODES 轮转,原来的「明暗对翻」写法必须消失
    assert 'THEME_MODES.indexOf(themeMode) + 1' in html
    assert "cur === 'dark' ? 'light' : 'dark'" not in html


def test_theme_auto_follows_framework_toggle_live():
    """★ 框架切夜间模式时本页要实时跟随:同源跨 document 的 storage 事件。

    只在自动模式下跟 —— 用户手动选过浅色 / 深色是显式覆盖,再跟就是把人家的
    选择改掉了。
    """
    wm = _main()
    html = wm._render_html()
    i = html.index("addEventListener('storage'")
    # 只截这个 listener 自己的函数体 —— 放宽窗口会串到下面那个
    # visibilitychange listener(它也写了 themeMode === 'auto'),漏判就无声无息
    body = html[i:html.index('});', i)]
    assert 'FRAMEWORK_DARK_KEY' in body
    assert "themeMode === 'auto'" in body


def test_theme_resolved_in_head_before_first_paint():
    """★ 首屏就要定主题:main.js 等 DOMContentLoaded,那之前整页按
    ``data-theme="light"`` 画一遍 —— 夜间模式下就是一记白闪。

    首屏脚本必然与 main.js 重复一份取值逻辑,那就把「键名一致」钉死:
    任一处改键名而另一处没跟上,首屏与稳定态就会是两个主题。
    """
    wm = _main()
    html = wm._render_html()
    head = html[:html.index('<style>')]
    assert '<script>' in head and 'data-theme' in head
    for const, key in (('STORAGE_THEME', 'lgtbot-page-theme'),
                       ('FRAMEWORK_DARK_KEY', 'elaina_dark')):
        assert f"const {const} = '{key}';" in html      # main.js 的常量
        assert f"'{key}'" in head                       # 首屏脚本用同一个键
    assert "|| 'auto'" in head                          # 首屏默认也是自动


def test_clean_badge_threshold_and_wiring():
    """★ 「清理」标记:仪表盘图片缓存 / 崩溃转储 core 文件超 256MB 时亮橙色。

    两处都挂在各自的 apply 数据函数里 —— 清理动作结束会重新拉一次 payload 并
    apply,标记因此在清干净的那一刻自动消失,不需要额外通知谁。
    """
    wm = _main()
    html = wm._render_html()
    assert 'const TAB_CLEAN_THRESHOLD = 256 * 1024 * 1024;' in html
    # 阈值判定是「严格大于」,恰好等于阈值不算超
    assert "classList.toggle('on', (bytes || 0) > TAB_CLEAN_THRESHOLD)" in html
    # 两个标签各挂一枚,id 与调用处对得上
    for tab, badge in (('dashboard', 'dash-clean-badge'), ('crash', 'crash-clean-badge')):
        assert re.search(r'data-tab="%s"[^>]*>.*?<span class="tab-clean-badge" id="%s">清理</span>'
                         % (tab, badge), html), badge
        assert f"setTabCleanBadge('{badge}'" in html
    # 仪表盘算的是三类图片缓存的**合计**,崩溃转储算 core 文件总占用
    assert "setTabCleanBadge('dash-clean-badge', cacheTotal)" in html
    assert "setTabCleanBadge('crash-clean-badge', data.core_bytes)" in html


def test_clean_badge_shows_on_every_tab_and_is_orange():
    """★ 标记要一直看得见:它的作用是在别的标签上提醒去清理,只在自己标签聚焦时显示就等于没提醒。"""
    wm = _main()
    html = wm._render_html()
    css = html[:html.index('</style>')]
    # 显示条件只看 .on,不带 .tab.active 限定
    assert '.tabs .tab .tab-clean-badge.on { display: inline-block; }' in css
    assert not re.search(r'\.tabs \.tab\.active \.tab-clean-badge', css)
    assert 'color: var(--warn)' in css
    # 浅 / 深两套变量都定义了,否则一套主题下会取不到色
    assert len(re.findall(r'--warn:\s*#[0-9a-f]{6};', css)) == 2


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


# ─────────────────────────────────────────────────────────────────────────
# page_review —— 昵称审核标签
# ─────────────────────────────────────────────────────────────────────────

def _page_review():
    pytest.importorskip('aiohttp')
    from plugins.LGTBot_ElainaBot.mod.webui import page_review
    return page_review


def test_review_badge_counts_unhandled_and_shows_anywhere():
    """★ 未处理违规的角标要在**任何**标签下都看得见。"""
    wm = _main()
    html = wm._render_html()
    css = html[:html.index('</style>')]
    assert '<span class="tab-alert-badge" id="review-pending-badge">' in html
    assert '.tabs .tab .tab-alert-badge.on { display: inline-block; }' in css
    assert not re.search(r'\.tabs \.tab\.active \.tab-alert-badge', css)
    assert 'color: var(--warn)' in css
    # 计数来自「违规且未处理」,总开关关闭时不亮
    assert "reviewSyncBadge(st.pending || 0, !!d.enabled)" in html


def test_review_js_placeholders_are_all_substituted():
    """模板占位与常量对得上 —— 拼错的话按钮会 fetch 到字面量 __REVIEW_*__。"""
    wm = _main()
    html = wm._render_html()
    assert '__REVIEW_' not in html
    for const in (wm._REVIEW_REFRESH_KEY, wm._REVIEW_TOGGLE_KEY,
                  wm._REVIEW_SCAN_START_KEY, wm._REVIEW_SCAN_PAUSE_KEY,
                  wm._REVIEW_SCAN_RESET_KEY, wm._REVIEW_VERDICT_ROUTE):
        assert const in html, const


def test_review_actions_are_hidden_and_route_is_symmetric(_clean_registry):
    """五个无参 action 都要进 _HIDDEN_KEYS(否则漏进侧边栏);带参端点走真路由,
    注册 / 注销必须对称(漏了 unregister 会在插件卸载后留下野路由)。"""
    wm = _main()
    for key in (wm._REVIEW_REFRESH_KEY, wm._REVIEW_TOGGLE_KEY,
                wm._REVIEW_SCAN_START_KEY, wm._REVIEW_SCAN_PAUSE_KEY,
                wm._REVIEW_SCAN_RESET_KEY):
        assert key in wm._HIDDEN_KEYS, key
    wm.register()
    assert ('GET', wm._REVIEW_VERDICT_ROUTE) in _clean_registry._routes
    wm.unregister()
    assert ('GET', wm._REVIEW_VERDICT_ROUTE) not in _clean_registry._routes


def test_review_toggle_refuses_to_enable_without_the_llm(monkeypatch):
    """★ 中央 LLM 不可用时不许开启 —— 开一个只会报错的功能没有意义。
    关闭方向永远放行:关掉一个坏掉的功能不该被任何前置条件挡住。"""
    pr = _page_review()
    from plugins.LGTBot_ElainaBot.mod import nickname_review as nr
    monkeypatch.setattr(nr, 'ENABLED', False)
    monkeypatch.setattr(nr, 'llm_status',
                        lambda: {'available': False, 'message': '没有可用接口'})
    saved = []
    monkeypatch.setattr(nr, 'save_settings',
                        lambda **kw: saved.append(kw) or (True, ''))
    body = _fragment_payload(pr.render_toggle())
    assert body['success'] is False and '没有可用接口' in body['message']
    assert saved == []                           # 连设置都不该落盘

    # 已开启 + LLM 掉线 → 允许关闭
    monkeypatch.setattr(nr, 'ENABLED', True)
    monkeypatch.setattr(pr.audit, 'record', lambda *a, **k: None)
    body = _fragment_payload(pr.render_toggle())
    assert body['success'] is True
    assert saved == [{'enabled': False}]


def test_review_payload_reports_the_three_states(monkeypatch):
    """未启用 / 已启用 / 已启用但 LLM 掉线 —— 前端据 enabled + llm_available 两个字段区分,payload 必须两个都给。"""
    pr = _page_review()
    from plugins.LGTBot_ElainaBot.mod import nickname_review as nr
    monkeypatch.setattr(nr, 'llm_status', lambda: {'available': False, 'message': 'x'})
    monkeypatch.setattr(nr, 'ENABLED', True)
    p = pr._payload()
    assert p['enabled'] is True and p['llm_available'] is False and p['llm_message'] == 'x'
    assert {'stats', 'scan', 'entries', 'fail_closed'} <= set(p)


async def test_review_verdict_handler_validates_and_dispatches(monkeypatch):
    """带参端点:非法 op / 缺 key → 400,记录不存在 → 404。"""
    pr = _page_review()
    from plugins.LGTBot_ElainaBot.mod import nickname_review as nr
    monkeypatch.setattr(pr.audit, 'record', lambda *a, **k: None)

    class _Req:
        def __init__(self, **q):
            self.query = q

    assert (await pr.verdict_handler(_Req(op='acquit'))).status == 400
    assert (await pr.verdict_handler(_Req(key='k', op='drop'))).status == 400
    monkeypatch.setattr(nr, 'get_verdict', lambda k: None)
    assert (await pr.verdict_handler(_Req(key='k', op='acquit'))).status == 404

    monkeypatch.setattr(nr, 'get_verdict',
                        lambda k: {'sample': 's', 'flagged': True,
                                   'source': 'llm', 'handled': False, 'ts': 0})
    calls = []
    monkeypatch.setattr(nr, 'acquit', lambda k: calls.append(('acquit', k)) or True)
    monkeypatch.setattr(nr, 'set_handled',
                        lambda k, v: calls.append(('handled', k, v)) or True)
    monkeypatch.setattr(nr, 'pending_count', lambda: 0)
    monkeypatch.setattr(nr, 'revoke', lambda k: calls.append(('revoke', k)) or True)
    monkeypatch.setattr(nr, 'condemn', lambda k: calls.append(('condemn', k)) or True)
    for op in ('acquit', 'handled', 'reopen', 'revoke', 'condemn'):
        assert (await pr.verdict_handler(_Req(key='k', op=op))).status == 200
    assert calls == [('acquit', 'k'), ('handled', 'k', True), ('handled', 'k', False),
                     ('revoke', 'k'), ('condemn', 'k')]


def test_review_scan_section_needs_both_switch_and_llm():
    """批量扫描的两个前置条件都要在界面上体现 —— 服务端已经会拒,界面同步灰掉才不会让人白点一次。"""
    wm = _main()
    html = wm._render_html()
    assert "classList.toggle('disabled', !(d.enabled && d.llm_available))" in html


def test_review_tab_sits_between_crash_and_audit():
    wm = _main()
    html = wm._render_html()
    order = re.findall(r'data-tab="(\w+)"', html)
    i = order.index('review')
    assert order[i - 1] == 'crash' and order[i + 1] == 'audit'


def test_review_sections_use_the_shared_section_gap():
    """四个区块之间的间距走仪表盘那套 —— .review 漏进那条规则就会挤成一坨。"""
    wm = _main()
    css = wm._render_html()
    css = css[:css.index('</style>')]
    rule = re.search(r'((?:\.[\w-]+,\s*)+)\.prebuilt \{ display: flex; flex-direction: column; gap: 14px;',
                     css)
    assert rule and '.review,' in rule.group(1)


async def test_review_settings_handler_saves_only_selections(monkeypatch):
    """★ 面板只保存「选了哪个接口 / 哪个模型」;接口地址与 API Key 归中央模块。"""
    pr = _page_review()
    from plugins.LGTBot_ElainaBot.mod import nickname_review as nr
    monkeypatch.setattr(pr.audit, 'record', lambda *a, **k: None)
    saved = []
    monkeypatch.setattr(nr, 'save_settings',
                        lambda **kw: saved.append(kw) or (True, ''))

    class _Req:
        def __init__(self, **q):
            self.query = q

    assert (await pr.settings_handler(_Req())).status == 400
    # 必须带上另一个合法项:只传坏 batch_size 的话,会先撞上「没有要保存的项」
    assert (await pr.settings_handler(_Req(model='m', batch_size='abc'))).status == 400
    r = await pr.settings_handler(
        _Req(provider_id='p1', model='m1', batch_size='999', fail_closed='1'))
    assert r.status == 200
    assert saved == [{'provider_id': 'p1', 'model': 'm1',
                      'batch_size': 100, 'fail_closed': True}]


def test_review_call_card_shows_the_cumulative_total():
    """今日调用下面那行小字是累计调用。"""
    wm = _main()
    html = wm._render_html()
    assert "'累计调用 ' + (scan.calls_total || 0)" in html
    assert 'daily_limit' not in html


def test_review_payload_carries_provider_options(monkeypatch):
    """两个下拉的选项来自中央的公开配置,payload 得把它带下去。"""
    pr = _page_review()
    from plugins.LGTBot_ElainaBot.mod import nickname_review as nr
    monkeypatch.setattr(nr, 'public_config', lambda: {'providers': [
        {'id': 'p1', 'name': 'P1', 'enabled': True, 'models': ['m1', 'm2'],
         'model_priority': [], 'disabled_models': ['m2']},
        {'id': 'p2', 'name': 'P2', 'enabled': False, 'models': ['m9']},
    ]})
    p = pr._payload()
    assert p['providers'] == [{'id': 'p1', 'name': 'P1', 'models': ['m1']}]
    assert {'provider_id', 'model', 'batch_size', 'fail_closed', 'enabled'} <= set(p)
    assert 'entries' in p and 'allowed' in p


def test_review_progress_shows_resolved_and_queued_not_calls():
    """★ 进度条要单列「取到昵称 / 新送审」:换绑之后老玩家查不到昵称,进度会跑满却一次都没送审。"""
    wm = _main()
    html = wm._render_html()
    i = html.index("document.getElementById('review-scan-text').textContent")
    body = html[i:i + 320]
    assert '取到昵称' in body and '新送审' in body
    assert '今日调用' not in body


def test_review_panel_shows_the_last_review_error():
    """★ 送审失败的原因要显示在面板上,不能只进日志。"""
    wm = _main()
    html = wm._render_html()
    assert 'id="review-scan-error"' in html
    i = html.index("getElementById('review-scan-error')")
    body = html[i:i + 420]
    # 断言真的把错误文本写进去了 —— 只查 last_error / permanent 出现过的话,
    # 把赋值改成空串照样能过(周围的取值代码里也有这两个词)
    assert 'errEl.textContent = err.message' in body
    assert "scan.last_error" in body and 'err.permanent' in body


def test_review_records_split_into_violations_and_whitelist():
    """★ 两个大区:违规记录与白名单记录各管各的动作。"""
    wm = _main()
    html = wm._render_html()
    for frag in ('id="review-list"', 'id="review-allow-list"',
                 'id="review-toggle-handled"', 'id="review-allowed-count"',
                 '⚠️ 违规记录', '✅ 白名单记录'):
        assert frag in html, frag
    assert "op: 'revoke'" in html and "op: 'condemn'" in html


def test_review_records_widen_from_one_to_three_columns():
    """★ 断点必须递增 —— 三列的 min-width 若不大于两列的,两列规则会因为源序在后而反过来赢,超宽屏永远只有两列。"""
    wm = _main()
    css = wm._render_html()
    css = css[:css.index('</style>')]
    assert '.review-list { display: grid; gap: 8px; grid-template-columns: 1fr; }' in css
    bps = [(int(w), int(n)) for w, n in re.findall(
        r'@media \(min-width: (\d+)px\) \{ \.review-list \{ '
        r'grid-template-columns: repeat\((\d+), 1fr\); \} \}', css)]
    assert [n for _, n in bps] == [2, 3]
    assert bps[0][0] < bps[1][0]


def test_nickname_popup_closes_but_survives_hopping_between_names():
    """★ 关闭监听挂在捕获阶段:点另一个昵称时它先于该昵称自己的 handler 跑,
    必须放行,否则气泡刚重定位就被关掉。"""
    wm = _main()
    html = wm._render_html()
    assert re.search(r"document\.addEventListener\('click', ev => \{\s*"
                     r"if \(!ev\.target\.classList\.contains\('review-name'\)\) hide\(\);\s*"
                     r"\}, true\);", html)
    assert "if (ev.key === 'Escape') hide();" in html
    assert "window.addEventListener('scroll', hide, true);" in html


def test_verdict_prompts_name_the_nickname_being_acted_on():
    """★ 昵称是用户可控文本 —— 必须走函数形式的 replace,否则名字里的 $& 会被当成替换模式,弹窗显示的就不是真名。"""
    wm = _main()
    html = wm._render_html()
    for op in ('acquit', 'revoke', 'condemn'):
        assert re.search(r"\n    %s: '确认[^']*昵称：%%s" % op, html), op
    assert "ask.replace('%s', () => name || key)" in html
    # 名字从同一行的昵称元素上取,按钮不重复挂一份
    assert ("b.closest('.review-row').querySelector('.review-name').dataset.name" in html)


def test_handled_records_are_collapsed_by_default():
    """★ 已处理默认折叠,靠标题右侧的按钮展开;按钮自身要体现当前是展开还是收起。"""
    wm = _main()
    html = wm._render_html()
    assert 'let reviewShowHandled = false;' in html
    assert "reviewShowHandled ? '隐藏已处理' : '展开已处理'" in html
    # 折叠时已处理的行根本不渲染
    assert 'const shown = reviewShowHandled ? pending.concat(handled) : pending;' in html
    assert "btn.classList.toggle('active', reviewShowHandled)" in html
    css = html[:html.index('</style>')]
    assert '#review-toggle-handled.active' in css


def test_tab_alert_badge_is_vertically_centred():
    """★ 角标要和标签文字垂直居中 —— vertical-align: middle 对齐的是小写 x 高的
    中线,在中文标题里会目视下沉。"""
    wm = _main()
    css = wm._render_html()
    css = css[:css.index('</style>')]
    rule = re.search(r'\.tabs \.tab \.tab-alert-badge \{(.*?)\}', css, re.S).group(1)
    assert 'vertical-align: middle' in rule
    assert 'position: relative' in rule and 'top: -1px' in rule
