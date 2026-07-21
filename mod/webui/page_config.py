#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
「配置管理」标签 —— 五块编辑器集中管理本插件的所有用户可编辑文本/配置:

  1. 🔧 插件配置 (data/config.yaml)         + 「前往插件模块」link + 热重载按钮
  2. ⚠️ 重要更新 (data/important_update.txt) 置顶提示，空则不渲染区块
  3. 📢 更新公告 (data/update_notice.txt)    保存即时热加载(下次指令触发就生效)
  4. ❓ 疑难解答 (data/troubleshooting.txt)  同上
  5. ⚙️ 引擎配置 (data/engine/lgtbot.json)   保存后需重启 LGTBot 引擎才能生效

保存全部走主框架 ``/api/config-file/save`` 端点(yaml/json/text format)——
不在本插件自建 POST endpoint,复用主框架的注释保留 + 格式校验逻辑。

「热重载配置」按钮调 ``__lgtbot_dash_reload_config`` action,逻辑从原本的
``page_dashboard`` 搬迁到本文件(保留 action key 不变，JS 调用兼容)。
"""

from __future__ import annotations

import html as _html
import json
import os

import yaml
from aiohttp import web

from core.base.logger import get_logger, PLUGIN
from .. import boot
from .. import config as _plugin_config

log = get_logger(PLUGIN, 'LGTBot')

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')


def _load(name: str) -> str:
    with open(os.path.join(_TEMPLATE_DIR, name), 'r', encoding='utf-8') as f:
        return f.read()


TAB_HTML = _load('config/config.html')
TAB_CSS = _load('config/config.css')
TAB_JS = _load('config/config.js')


# ─────────────────────────────────────────────────────────────────────────
# 文件路径常量 —— 跟其他模块(config.py / dispatcher.py)对齐
# ─────────────────────────────────────────────────────────────────────────

_CONFIG_YAML_PATH = os.path.join(boot.DATA_DIR, 'config.yaml')
_IMPORTANT_UPDATE_PATH = os.path.join(boot.DATA_DIR, 'important_update.txt')
_UPDATE_NOTICE_PATH = os.path.join(boot.DATA_DIR, 'update_notice.txt')
_TROUBLESHOOTING_PATH = os.path.join(boot.DATA_DIR, 'troubleshooting.txt')

# 跨重载共享:存上次热重载后 yaml 里的 admin_uids 逗号串。每次 reload 比较
# 当前 yaml 内的 admin_uids 与该值,**仅在真的变化时**给前端返回那条
# 「需重启引擎」note —— 否则保持 reload 面板安静,不再每次都吓用户一跳。
# 首次 reload 时 key 不存在,保守视作"未变"(用户可凭页面上 admin_count
# 数字自行判断),后续每次都准确。
_PERSISTENT_LAST_ADMINS_KEY = 'cfg_last_loaded_admins'


def _read_file(path: str) -> tuple[str, str]:
    """读任意文本文件，返回 ``(content, error_msg)``。文件不存在 → 空内容、无错误。

    用于 update_notice.txt / troubleshooting.txt / config.yaml 这种"用户可编辑"
    文件:首次访问 web 面板时若文件还没生成(dispatcher 的 _read_* 没被触发过),
    显示空 textarea 让用户主动写入，不在这里做隐式 default 注入(那是 dispatcher
    handler 的责任，不应在 UI 渲染路径里副作用 IO)。
    """
    if not os.path.isfile(path):
        return '', ''
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read(), ''
    except Exception as e:
        return '', str(e)


def _read_engine_config() -> tuple[str, str]:
    """读 lgtbot.json,跟原 page_dashboard 同源行为。

    ``boot._ensure_lgtbot_conf`` 保证启动时文件已存在。万一缺失，返回 ``{}\\n``
    让 textarea 至少展示有效 JSON 而非空白(避免用户保存空文件导致引擎启动出错)。
    """
    path = boot.CONF_PATH
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read(), ''
    except FileNotFoundError:
        return '{}\n', ''
    except Exception as e:
        return '', str(e)


# ─────────────────────────────────────────────────────────────────────────
# 数据入口 —— 每次页面渲染调用一次
# ─────────────────────────────────────────────────────────────────────────

def get_data() -> str:
    """返回可嵌入 ``<script id="config-data">`` 的 JSON 字符串。

    五块编辑器的 abs_path / content / read_error 一次性提供，前端 cfgApplyData
    据此填充各个 textarea 与路径显示行。
    """
    cfg_content, cfg_err = _read_file(_CONFIG_YAML_PATH)
    important_content, important_err = _read_file(_IMPORTANT_UPDATE_PATH)
    notice_content, notice_err = _read_file(_UPDATE_NOTICE_PATH)
    trouble_content, trouble_err = _read_file(_TROUBLESHOOTING_PATH)
    engine_content, engine_err = _read_engine_config()

    payload = {
        'config_yaml': {
            'abs_path': os.path.abspath(_CONFIG_YAML_PATH),
            'content': cfg_content,
            'read_error': cfg_err,
        },
        'important_update': {
            'abs_path': os.path.abspath(_IMPORTANT_UPDATE_PATH),
            'content': important_content,
            'read_error': important_err,
        },
        'update_notice': {
            'abs_path': os.path.abspath(_UPDATE_NOTICE_PATH),
            'content': notice_content,
            'read_error': notice_err,
        },
        'troubleshooting': {
            'abs_path': os.path.abspath(_TROUBLESHOOTING_PATH),
            'content': trouble_content,
            'read_error': trouble_err,
        },
        'engine_config': {
            'abs_path': os.path.abspath(boot.CONF_PATH),
            'content': engine_content,
            'read_error': engine_err,
        },
    }
    data_json = json.dumps(payload, ensure_ascii=False, default=str)
    return data_json.replace('</script>', '<\\/script>')


# ─────────────────────────────────────────────────────────────────────────
# Action 端点 —— 热重载 config.yaml(从 page_dashboard 搬迁过来,逻辑不变)
# ─────────────────────────────────────────────────────────────────────────

def _fragment(payload: dict) -> str:
    """把 ``payload`` 包成 ``<pre id="result">...</pre>``,与其他 action 端点一致。"""
    body = json.dumps(payload, ensure_ascii=False, default=str)
    return f'<pre id="result">{_html.escape(body)}</pre>'


def _snapshot_runtime_tunables() -> dict:
    """快照 config._apply_runtime_tunables 会覆盖的 Python 侧运行时常量。

    字段顺序与 config.DEFAULT_CONFIG / yaml 一致，便于 UI 上直观对照。
    前后两次快照的 diff 即得到本次热重载的实际变化。
    """
    from .. import quota as _quota, uploader as _uploader, buttons as _buttons, callbacks as _callbacks
    from .. import dispatcher as _dispatcher, state as _state
    return {
        'bind_bot_appid': _state.bind_bot_appid or '',
        'image_hosting': _uploader.SELECTED_BACKEND or '',
        'refresh_wait_timeout': float(_quota.REFRESH_WAIT_TIMEOUT),
        'image_upload_dedup_ttl': float(_uploader.URL_CACHE_TTL),
        'crash_notify_group': _callbacks.CRASH_NOTIFY_GROUP or '',
        'blocked_commands': list(_dispatcher.BLOCKED_COMMANDS),
        'sandbox_dm_users': sorted(_callbacks.SANDBOX_DM_USERS),
        'menu_game_buttons': list(_buttons.MENU_GAMES),
    }


def render_reload_config() -> str:
    """按当前 ``data/config.yaml`` 热重载所有 Python 侧可调字段，不重启插件/引擎。

    流程:
      1. 拍前快照 → 调 ``config.load_plugin_config()`` (内部已 log.info 各字段
         变化) → 拍后快照
      2. 算 diff,把变化字段以 INFO 日志逐项输出 + 总结一行
      3. 返回 JSON 含 ``changes`` 数组、当前值、admin_count、警示 note 给前端
    """
    from .. import config as _config
    log.info('=' * 60)
    log.info('🔁 [reload-config] 开始热重载插件配置')

    _p = boot._get_persistent()
    # 上次 reload 后落档的 admin 串(首次 reload 时 key 不存在,拿到 None)
    before_admins = _p.get(_PERSISTENT_LAST_ADMINS_KEY)

    before = _snapshot_runtime_tunables()
    try:
        admins_str = _config.load_plugin_config()
    except Exception as e:
        log.error(f'🔁 [reload-config] 失败: {e}')
        log.info('=' * 60)
        return _fragment({
            'success': False,
            'message': f'热重载失败: {e}',
        })
    after = _snapshot_runtime_tunables()

    # 列出变化字段。比较列表 / 集合用 != 即可(已规范化为可哈希 / 可比类型)
    changes: list = []
    for field, new_val in after.items():
        old_val = before[field]
        if old_val != new_val:
            changes.append({
                'field': field,
                'before': old_val,
                'after': new_val,
            })

    admin_count = len([u for u in (admins_str or '').split(',') if u.strip()])

    # 落档当前 admins —— 下次 reload 用作 before 对照
    _p[_PERSISTENT_LAST_ADMINS_KEY] = admins_str
    # 仅当**记录过 before** 且**真的变化**时,才视为 admin 改动 —— 首次 reload
    # 没有 before,保守视作"未变"避免每次都误报
    admin_changed = (before_admins is not None and before_admins != admins_str)

    # 日志:逐项 + 总结
    if changes:
        log.info(f'🔁 [reload-config] 运行时参数变化 {len(changes)} 项:')
        for c in changes:
            log.info(f'   · {c["field"]}: {c["before"]!r} → {c["after"]!r}')
    else:
        log.info('🔁 [reload-config] 运行时参数无变化 (yaml 与运行时一致)')
    if admin_changed:
        log.warning(f'⚠️ [reload-config] admin_uids 已变化 ({before_admins!r} → {admins_str!r})，需重启 LGTBot 引擎才能生效')
    else:
        log.info(f'   · admin_uids: 当前 yaml 中 {admin_count} 人 (未变化)')
    log.info('🔁 [reload-config] 完成')
    log.info('=' * 60)

    payload = {
        'success': True,
        'changes': changes,
        'current': after,
        'admin_count': admin_count,
        'admin_changed': admin_changed,
        'message': '✅ 已按 config.yaml 重新下发到运行时',
    }
    # 仅 admin 真变化时附 note —— 前端 ``if (data.note)`` 才显示「⚠️」一行
    if admin_changed:
        payload['note'] = 'admin_uids 改动需重启 LGTBot 引擎才能生效 (C++ 侧仅在 start() 时读一次)'
    return _fragment(payload)


# ─────────────────────────────────────────────────────────────────────────
# 带校验的配置保存 —— POST /api/ext/lgtbot/config/save
# ─────────────────────────────────────────────────────────────────────────
# config.yaml / lgtbot.json 两个编辑器不再走主框架 /api/config-file/save (只写盘不校验)
# 先语法解析 + 字段 schema 校验,全部通过才原子落盘,一个格式错误不再让插件加载回退默认配置或引擎启动失败。
# 路径由服务端按 target 解析,不信任客户端传路径。

# 图床合法值延迟取自 uploader._UPLOADERS —— 与运行时校验同源,避免两处漂移
def _valid_backends() -> set:
    from .. import uploader as _uploader
    return {name for name, _ in _uploader._UPLOADERS}


def _validate_config_yaml(text: str) -> tuple[list, list]:
    """校验 config.yaml 文本,返回 ``(errors, warnings)``;errors 非空则拒绝保存。

    schema 从 ``config.DEFAULT_CONFIG`` 的字段类型动态推导(list / str / 数值),
    新增配置字段无需改这里。错误级(阻断):语法错误、根不是映射、字段类型不符、
    image_hosting 填了未知图床、refresh_wait_timeout 非正数。警告级(放行):
    字段值为空(yaml 裸 key 解析为 None,运行时按缺省处理)、未知字段。
    """
    errors: list = []
    warnings: list = []
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, 'problem_mark', None)
        pos = f'(第 {mark.line + 1} 行)' if mark else ''
        return [f'YAML 语法错误{pos}: {getattr(e, "problem", e)}'], []
    if data is None:
        return [], ['文件为空 —— 下次插件加载会自动补全默认字段']
    if not isinstance(data, dict):
        return [f'根节点必须是键值映射，当前是 {type(data).__name__}'], []

    missing: list = []
    for key, default in _plugin_config.DEFAULT_CONFIG.items():
        if key not in data:
            missing.append(key)
            continue
        val = data[key]
        if val is None:
            warnings.append(f'{key} 为空，运行时将按未配置处理')
            continue
        if isinstance(default, list):
            if not isinstance(val, list):
                errors.append(f'{key} 应为列表，当前是 {type(val).__name__}')
            else:
                bad = [i for i, e in enumerate(val) if not isinstance(e, (str, int))]
                if bad:
                    errors.append(f'{key} 第 {bad[0] + 1} 项应为字符串')
        elif isinstance(default, str):
            if not isinstance(val, str):
                errors.append(f'{key} 应为字符串，当前是 {type(val).__name__}')
        elif isinstance(default, float):
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                errors.append(f'{key} 应为数值，当前是 {type(val).__name__}')

    # 值级规则(类型已过关的字段才检查)
    backend = data.get('image_hosting')
    if isinstance(backend, str) and backend.strip():
        valid = _valid_backends()
        if backend.strip().lower() not in valid:
            errors.append(f'image_hosting 未知图床 {backend!r}，可选: {sorted(valid)} 或留空')
    timeout = data.get('refresh_wait_timeout')
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout <= 0:
        errors.append(f'refresh_wait_timeout 应为正数，当前 {timeout}')

    if missing:
        warnings.append(f'缺少字段 {"、".join(missing)}(下次加载自动补默认值)')
    for key in data:
        if key not in _plugin_config.DEFAULT_CONFIG:
            warnings.append(f'未知字段 {key}(本插件不会读取)')
    return errors, warnings


def _validate_engine_json(text: str) -> list:
    """校验 lgtbot.json:必须是合法 JSON 且根为对象(引擎 nlohmann 解析要求)。

    更深的结构属于上游引擎的领域,这里不越界校验 —— 语法错误 / 根类型错误才是「引擎启动失败」的实际来源。
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return [f'JSON 语法错误(第 {e.lineno} 行第 {e.colno} 列): {e.msg}']
    if not isinstance(data, dict):
        return [f'根节点必须是 JSON 对象 {{}}，当前是 {type(data).__name__}']
    return []


# target → (绝对路径, 校验器)。校验器返回 (errors, warnings) 或仅 errors。
_SAVE_TARGETS = {
    'config_yaml': (_CONFIG_YAML_PATH, _validate_config_yaml),
    'engine_json': (None, _validate_engine_json),   # 路径运行时取 boot.CONF_PATH
}


def _atomic_write(path: str, content: str) -> None:
    """临时文件 + os.replace 原子落盘,避免写一半被读到。"""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(content)
    os.replace(tmp, path)


async def save_config_handler(request: 'web.Request') -> 'web.Response':
    """``POST /api/ext/lgtbot/config/save`` —— body ``{"target", "content"}``。

    校验失败返回 ``{'success': False, 'errors': [...]}``,**不落盘**;
    通过则原子写入并把警告一并带回给前端展示。
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'success': False, 'errors': ['请求体不是合法 JSON']}, status=400)
    target = str(body.get('target') or '')
    content = body.get('content')
    if target not in _SAVE_TARGETS or not isinstance(content, str):
        return web.json_response({'success': False, 'errors': ['target 无效或缺少 content']}, status=400)

    path, validator = _SAVE_TARGETS[target]
    if path is None:
        path = boot.CONF_PATH
    result = validator(content)
    errors, warnings = result if isinstance(result, tuple) else (result, [])
    if errors:
        log.info(f'🛡️ [配置校验] {target} 保存被拒: {errors}')
        return web.json_response({'success': False, 'errors': errors, 'warnings': warnings})

    try:
        _atomic_write(path, content)
    except Exception as e:
        log.error(f'写入 {target} 失败: {e}')
        return web.json_response({'success': False, 'errors': [f'写盘失败: {e}']})
    log.info(f'💾 [配置校验] {target} 校验通过并已保存'
             + (f'(警告 {len(warnings)} 条)' if warnings else ''))
    return web.json_response({'success': True, 'errors': [], 'warnings': warnings})
