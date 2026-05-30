#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""图床上传调度 + 图片尺寸解析

主框架的 image_hosting 模块只暴露各图床独立的 upload_* 方法。本模块由
config.yaml 的 `image_hosting` 字段指定**唯一**目标图床（cos / nature /
bilibili / chatglm / ukaka / xingye），上传成功 → 返回 URL；上传失败 / 未
配置 / image_hosting 模块未启用 → 返回 None，由上层回退到 msg_type=7。

> 设计取舍：早期版本会按优先级遍历所有 status() 启用的图床直至成功，但
> 单条失败的网络往返常达数秒，叠加多条会让游戏命令响应明显卡顿，因此
> 改为「单选 + 失败即降级媒体消息」的快速失败策略。

注意：QQ 官方机器人 markdown 中的 `![alt](url)` 要求 URL 域名已在
QQ Bot 开放平台「消息 URL 配置」报备，否则消息会被丢弃 / 不显示。
COS bucket CDN 与 Nature 的 download.nature.qq.com 是最易过审的目标。
"""

from __future__ import annotations
import asyncio
import os
import struct
import hashlib
import time as _time
from core.base.logger import get_logger, PLUGIN

log = get_logger(PLUGIN, 'LGTBot')


# ──────── 图片尺寸解析（不依赖 PIL）─────────────────────────────────────
# 直接读 PNG / JPEG / GIF / WebP 文件头，解析失败时返回 (300, 300) 作为占位

def get_image_size(data: bytes) -> tuple[int, int]:
    try:
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            return struct.unpack('>II', data[16:24])
        if data[:3] == b'GIF':
            return struct.unpack('<HH', data[6:10])
        if data[:2] == b'\xff\xd8':  # JPEG
            i = 2
            while i < len(data):
                while i < len(data) and data[i] != 0xFF:
                    i += 1
                while i < len(data) and data[i] == 0xFF:
                    i += 1
                marker = data[i]; i += 1
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack('>HH', data[i + 3:i + 7])
                    return (w, h)
                i += struct.unpack('>H', data[i:i + 2])[0]
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            ck = data[12:16]
            if ck == b'VP8 ':
                w, h = struct.unpack('<HH', data[26:30])
                return (w & 0x3fff, h & 0x3fff)
            if ck == b'VP8L':
                b0, b1, b2, b3 = data[21:25]
                return (1 + (((b1 & 0x3f) << 8) | b0),
                        1 + (((b3 & 0x0f) << 10) | (b2 << 2) | ((b1 & 0xc0) >> 6)))
            if ck == b'VP8X':
                return (1 + (data[24] | (data[25] << 8) | (data[26] << 16)),
                        1 + (data[27] | (data[28] << 8) | (data[29] << 16)))
    except Exception as e:
        log.debug(f'图片尺寸解析失败: {e}')
    return (300, 300)


# ──────── 单个图床的上传适配（统一返回 URL 或 None）──────────────────────
# image_hosting 模块没有提供"自动选择"接口，只有 7 个独立 upload_*。
# 返回类型大多统一为「URL 字符串 或 (False, reason) 元组」，仅 COS 返回
# dict（含 file_url 键），QQ 频道返回 URL 已知是 404（test 插件已确认坏）。
# 这里把所有可用图床的成败语义统一成「成功 → URL 字符串，失败 → None」。

async def _try_cos(hosting, data, filename, user_id):
    """COS 单独适配：返回 dict 而不是字符串"""
    try:
        r = await hosting.upload_cos(data, filename, user_id=user_id or None)
    except Exception as e:
        log.warning(f'COS 上传异常: {e}')
        return None
    if isinstance(r, dict) and r.get('file_url'):
        return r['file_url']
    log.warning(f'COS 上传失败: {r}')
    return None


def _make_simple_uploader(method_name: str, label: str):
    """工厂：把 image_hosting 那些「URL 字符串 或 (False, reason)」格式的
    upload_* 方法统一适配成本插件需要的 (str | None) 接口。新增图床时只需
    在 _UPLOADERS 元组里加一行。"""
    async def _try(hosting, data, filename, user_id):
        method = getattr(hosting, method_name, None)
        if method is None:
            return None
        try:
            r = await method(data)
        except Exception as e:
            log.warning(f'{label} 上传异常: {e}')
            return None
        if isinstance(r, str) and r.startswith('http'):
            return r
        log.warning(f'{label} 上传失败: {r}')
        return None
    _try.__name__ = f'_try_{method_name}'
    return _try


# 全部受支持的图床名（与 image_hosting 模块的 status() / upload_* 命名一致）。
# 不接 QQ 频道：image_hosting.upload_qq 返回的 URL 是 MD5 拼接 404 的假地址
# （test 插件已确认），lgtbot 群机器人场景也没有 channel_id。
_UPLOADERS = (
    ('cos',      _try_cos),
    ('nature',   _make_simple_uploader('upload_nature',   'Nature')),
    ('bilibili', _make_simple_uploader('upload_bilibili', 'B站')),
    ('chatglm',  _make_simple_uploader('upload_chatglm',  'ChatGLM')),
    ('ukaka',    _make_simple_uploader('upload_ukaka',    'Ukaka')),
    ('xingye',   _make_simple_uploader('upload_xingye',   '星野')),
)
_UPLOADERS_MAP = dict(_UPLOADERS)

# 由 config.py 在加载 / 重载配置时写入；空串 = 不启用图床（直接走 msg_type=7）。
SELECTED_BACKEND: str = ''


# ──────── 对外接口 ───────────────────────────────────────────────────────

def _get_hosting():
    """从 BotManager 取 image_hosting 模块，未启用则返回 None"""
    try:
        from core.bot.manager import _bot_manager_ref
        bm = _bot_manager_ref
        if bm is None or bm.module_manager is None:
            return None
        return bm.module_manager.get('image_hosting')
    except Exception:
        return None


# ──────── 并发安全 + 去重上传 ─────────────────────────────────────────────
# 解决两个独立 bug,同一份机制覆盖:
#
#  (A) **filename 唯一化** —— 根治 cos_key 冲突
#      主框架 image_hosting 的 COS storage key 是
#         {prefix}{user_id}/{ts(秒级)}/{base}_{W}x{H}.ext
#      lgtbot 引擎对游戏图常用固定 filename (e.g. 'match.png') 且渲染图尺寸
#      常一致(同一游戏的棋盘 / 卡牌);两个不同群同时玩同款游戏时 user_id=''
#      也相同 → cos_key 完全一样 → 后写覆盖,两条消息拿到同 URL 但 size 是
#      各自本地从原 data 算的 → 出现「size 数字对得上 X 图但 URL 加载出来
#      是 Y 图」的现象。把 base 部分加上 sha1(data)[:8] 后 cos_key 必然按
#      内容隔离,内容相同则 key 相同(COS 端去重,符合预期)。
#
#  (B) **in-flight Future 去重 + 短 TTL URL cache** —— 避免同一份 data 被
#      并发上传多次(菜单 logo / 多群同时拉同一份图等场景)。dict 操作是
#      µs 级,不构成阻塞;不同 data 各自走独立 Future,完全并发,**没有
#      全局锁**,满足「多群同时游戏不互锁、不影响上传速度」要求。
#
# _url_cache_v2 / _inflight 是模块级 dict,不挂 boot._get_persistent():
# 热重载时 in-flight 协程随旧模块销毁,新模块开始干净状态,30s 缓存丢了
# 重传一次也无所谓。
# ─────────────────────────────────────────────────────────────────────────

# 由 config.py 在加载 / 重载时写入。**单位:秒**;0 = 关闭去重(每次都重新上传,
# 仅保留 filename 唯一化保护 cos_key);负数由 config.py 自动归 0。默认 60s。
URL_CACHE_TTL: float = 60.0
_URL_CACHE_MAX = 256           # 缓存条目上限,超出按 expires_at 删最早
_inflight: dict[str, asyncio.Future] = {}      # sha1(data) → 正在跑的 Future
_url_cache_v2: dict[str, dict] = {}            # sha1(data) → {url, expires_at}


def _unique_filename(filename: str, sha1_hex: str) -> str:
    """在 filename 的 base 段后追加 ``_<sha1[:8]>``,扩展名保留。

    空 filename 用 ``image.png`` 兜底。**保留原 base** 让 COS 上的对象路径
    仍然可读(便于人工排查),只是末尾多了 8 字符内容哈希,保证不同 data
    的对象 key 必然不同。
    """
    base, ext = os.path.splitext(filename or 'image.png')
    if not ext:
        ext = '.png'
    return f'{base}_{sha1_hex[:8]}{ext}'


def _gc_url_cache_v2(now: float) -> None:
    """轻量清理:删过期条目;若仍超 ``_URL_CACHE_MAX`` 删 expires_at 最早的几条。

    O(n) 一遍扫,30s TTL + 256 上限下 n 极小,不构成性能问题。
    """
    expired = [k for k, v in _url_cache_v2.items() if v['expires_at'] <= now]
    for k in expired:
        _url_cache_v2.pop(k, None)
    overflow = len(_url_cache_v2) - _URL_CACHE_MAX
    if overflow > 0:
        oldest = sorted(_url_cache_v2.items(), key=lambda kv: kv[1]['expires_at'])
        for k, _ in oldest[:overflow]:
            _url_cache_v2.pop(k, None)


async def _do_upload(data: bytes, filename: str, user_id: str = '') -> str | None:
    """实际调用图床上传 —— ``upload_image()`` 的内部实现,**不带去重缓存**。

    保留以前 ``upload_image`` 的全部 backend 调度逻辑,只剥离出来供新的
    去重包装层(``upload_image``)调用。直接调本函数会绕过缓存与 in-flight
    互斥,**仅供同模块内部使用**。
    """
    backend = SELECTED_BACKEND
    if not backend:
        return None

    fn = _UPLOADERS_MAP.get(backend)
    if fn is None:
        # 理论上 config.py 已校验过，这里防御兜底
        log.warning(f'未知图床 {backend!r}，已禁用')
        return None

    hosting = _get_hosting()
    if hosting is None:
        return None

    try:
        status = hosting.status() if hasattr(hosting, 'status') else {}
    except Exception:
        status = {}
    if not status.get(backend):
        log.warning(f'图床 {backend} 在 image_hosting 模块中未启用，请检查主框架配置')
        return None

    url = await fn(hosting, data, filename, user_id)
    if url:
        log.info(f'图床 {backend} 上传成功: {url}')
        return url
    return None


async def upload_image(data: bytes, filename: str, user_id: str = '') -> str | None:
    """用 config.yaml 指定的单个图床上传。

    无论 ``URL_CACHE_TTL`` 是否为 0,filename 都会被改写成 ``<base>_<sha1[:8]>.ext``
    传给 backend —— filename 唯一化是 size 错配 bug 的根治手段,不可关闭。

    当 ``URL_CACHE_TTL > 0``(默认 30s),启用:
      · content-hash URL cache —— 同一份 data 在 TTL 内复用 URL,**不打图床**
      · in-flight Future 互斥 —— 同一份 data 并发请求时,**backend 只被调一次**
      · 不同 data 各自独立 Future,完全并发,**无全局锁、无速度退化**

    当 ``URL_CACHE_TTL == 0``(配置关闭去重),每次都直接走 ``_do_upload``,
    不读不写缓存、不参与 in-flight 互斥 —— 同份 data 并发会有重复上传,但
    由于 filename 唯一化保证不同 data 必不撞 cos_key,**size 错配 bug
    仍被阻断**;运营关闭去重时只损失带宽,不损失正确性。

    失败 / 未配置 → 返回 None。
    """
    h = hashlib.sha1(data).hexdigest()
    unique = _unique_filename(filename, h)

    # ── 去重已关闭(TTL==0)→ 直通 _do_upload,不读不写 cache / inflight ──
    if URL_CACHE_TTL <= 0:
        return await _do_upload(data, unique, user_id)

    now = _time.monotonic()

    # 1. URL 缓存命中 → 直接返回
    ent = _url_cache_v2.get(h)
    if ent is not None and ent['expires_at'] > now:
        return ent['url']

    # 2. 已有 in-flight 协程在上传同份 data → 共享同一个 Future
    fut = _inflight.get(h)
    if fut is not None:
        return await fut

    # 3. 占位 Future,实际跑上传
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    _inflight[h] = fut
    try:
        url = await _do_upload(data, unique, user_id)
        if url:
            _url_cache_v2[h] = {'url': url, 'expires_at': now + URL_CACHE_TTL}
            _gc_url_cache_v2(now)
        if not fut.done():
            fut.set_result(url)
        return url
    except Exception as e:
        if not fut.done():
            fut.set_exception(e)
        raise
    finally:
        # in-flight 标记尽快清掉,让后续请求要么命中 cache 要么发起新一轮
        _inflight.pop(h, None)


# ──────── 带缓存的上传（用于固定图片，如菜单 logo） ───────────────────────
# 进程内 dict，不挂 boot._get_persistent()：菜单 logo 这类静态图重启后重传
# 一次的代价可以接受，没必要跨重载持久化；同时也避免 C++ 扩展属性堆积。
_url_cache: dict = {}


async def upload_image_cached(
    data: bytes,
    filename: str,
    *,
    cache_key: str,
    ttl_seconds: int = 23 * 3600,
) -> dict | None:
    """带 TTL 缓存的图床上传。

    返回 ``{'url', 'width', 'height', 'expires_at'}`` 或 ``None``。

    - ``cache_key`` 是逻辑标识（如 ``'menu:logo'``），与 data 的 MD5 前缀拼成
      完整缓存键 → 文件内容变化时自动重传，旧 content_id 条目立即清理（避免
      字典无限增长）。
    - 命中且未过期 → 直接返回缓存条目。
    - 未命中 / 过期 → 调用 ``upload_image()`` 上传；成功则写缓存。
    - 上传失败但有过期旧缓存 → 仍然返回旧条目（兜底，菜单宁可显示老 URL
      也不要因瞬时图床故障变成纯文字）。
    - 没有 ``image_hosting`` 配置 / 全部失败且无旧缓存 → 返回 ``None``。
    """
    content_id = hashlib.md5(data).hexdigest()[:12]
    full_key = f'{cache_key}:{content_id}'
    now = _time.monotonic()

    entry = _url_cache.get(full_key)
    if entry is not None and entry['expires_at'] > now:
        return entry

    url = await upload_image(data, filename)
    if url:
        width, height = get_image_size(data)
        new_entry = {
            'url': url,
            'width': width,
            'height': height,
            'expires_at': now + ttl_seconds,
        }
        # 清理同 cache_key 下其他 content_id 的旧条目（含本次刚过期的那条）
        prefix = f'{cache_key}:'
        for k in list(_url_cache.keys()):
            if k.startswith(prefix) and k != full_key:
                del _url_cache[k]
        _url_cache[full_key] = new_entry
        return new_entry

    # 上传失败：返回过期旧缓存（如有）兜底
    if entry is not None:
        log.info(f'图床上传失败，复用过期缓存: {cache_key}')
        return entry
    return None
