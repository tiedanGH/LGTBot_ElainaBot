#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""图床上传调度 + 图片尺寸解析

主框架 image_hosting 模块(≥2.0.0)的图床按 ``beds/`` 自动发现,通过
``status()`` 报告可用性、按 ``upload_<name>``(或 ``upload_<name>_url``)
动态派发。本模块**不再硬编码图床名单**:config.yaml 的 `image_hosting`
字段指定**唯一**目标图床(以模块 status() 的键为准,如 cos / bilibili /
chatglm / xingye / nature / qq_file),或填 ``any`` 交给模块的
``upload_any`` 按优先级自动依次尝试。上传成功 → 返回 URL;上传失败 /
未配置 / image_hosting 模块未启用 → 返回 None,由上层回退到 msg_type=7。

> 设计取舍:单选图床是刻意的 —— 逐个尝试时单条失败的网络往返常达数秒,
> 叠加多条会让游戏命令响应明显卡顿,「单选 + 失败即降级媒体消息」保证
> 快速失败。``any`` 作为显式 opt-in 提供,选它即接受该延迟风险。

qq_file(QQ 分片文件)注意事项:返回的是 QQ 官方 COS **预签名直链,带
ttl 过期**,适合即时查看的游戏图;上传走绑定 bot 的 sender,作用域优先用
当前消息的目标群 / 用户(callbacks 透传)。菜单 logo 等长缓存场景对
qq_file / any 自动收紧缓存时长(见 upload_image_cached)。

COS bucket CDN 与 Nature 的 download.nature.qq.com 是推荐图床。
"""

from __future__ import annotations
import asyncio
import inspect
import os
import struct
import hashlib
import time as _time
from core.base.logger import get_logger, PLUGIN

from . import metrics

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


# ──────── 通用图床适配(动态发现,统一返回 URL 或 None)────────────────────
# image_hosting ≥2.0.0 的图床在 beds/ 下自动发现:status() 报告全部图床名,
# ``upload_<name>_url``(若有)统一返回 URL 字符串,``upload_<name>`` 可能返回
# URL / dict(cos: file_url,qq_file: url)/ (False, reason)。这里做一层通用
# 适配:优先 *_url 变体,kwargs 按目标方法签名过滤(同模块 upload_any 的做法),
# 结果统一成「成功 → URL 字符串,失败 → None」。框架将来新增图床零改动支持。
# 不接 QQ 频道(qq_channel):其 upload 返回的 URL 是 MD5 拼接 404 的假地址
# (test 插件已确认),lgtbot 群机器人场景也没有 channel_id。

def _bound_sender_and_tm():
    """绑定 bot 的 (sender, token_manager);无 bot → (None, None)。

    qq_file 需要 sender(不传时模块用「第一个在线 bot」,多 bot 部署下不一定
    是绑定 bot);upload_any 里 qq_channel 需要 token_manager。
    """
    try:
        from . import helpers
        bot = helpers.get_bound_bot()
        if bot is not None:
            return getattr(bot, 'sender', None), getattr(bot, 'token_manager', None)
    except Exception:
        pass
    return None, None


def _extract_url(result) -> str | None:
    """把各图床的返回值归一成 URL:str 直链 / dict 的 file_url·url 键;其余 None。"""
    if isinstance(result, str) and result.startswith('http'):
        return result
    if isinstance(result, dict):
        url = result.get('file_url') or result.get('url')
        if isinstance(url, str) and url.startswith('http'):
            return url
    return None


async def _call_backend(hosting, backend: str, data: bytes, filename: str,
                        user_id: str, target_id: str, target_is_uid: bool) -> str | None:
    """调用单个图床(优先 ``upload_<backend>_url``),kwargs 按签名过滤。

    透传给各后端可能用到的参数:
      · filename / file_name —— cos 的 key、qq_file 的文件名
      · user_id —— cos 的路径前缀
      · sender / target_id / target_type —— qq_file 的上传通道与作用域
        (作用域用当前消息目标,群/私信各自隔离;缺省时模块自行回退)
    """
    fn = (getattr(hosting, f'upload_{backend}_url', None)
          or getattr(hosting, f'upload_{backend}', None))
    if fn is None:
        log.warning(f'image_hosting 模块无图床 {backend!r}(已移除或未安装),上传跳过')
        return None
    sender, _tm = _bound_sender_and_tm()
    kwargs = {
        'filename': filename,
        'file_name': filename,
        'user_id': user_id or None,
        'sender': sender,
        'target_id': target_id or None,
        'target_type': ('user' if target_is_uid else 'group') if target_id else None,
    }
    try:
        params = inspect.signature(fn).parameters
        kwargs = {k: v for k, v in kwargs.items() if k in params and v is not None}
    except (TypeError, ValueError):
        kwargs = {}
    try:
        r = await fn(data, **kwargs)
    except Exception as e:
        log.warning(f'图床 {backend} 上传异常: {e}')
        return None
    url = _extract_url(r)
    if url is None:
        log.warning(f'图床 {backend} 上传失败: {r}')
    return url


# 由 config.py 在加载 / 重载配置时写入;空串 = 不启用图床(直接走 msg_type=7);
# 'any' = 交给 image_hosting.upload_any 按优先级自动依次尝试。
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


def _display_name(hosting, backend: str) -> str:
    """图床中文显示名(get_bed().display_name);拿不到回退 backend 原名。"""
    try:
        bed = hosting.get_bed(backend) if hasattr(hosting, 'get_bed') else None
        return getattr(bed, 'display_name', '') or backend
    except Exception:
        return backend


def hosting_availability() -> dict:
    """检测配置的图床是否可用 —— **仅查配置 + 主框架 image_hosting.status()**,
    不做真实上传探测(不打网络),供指标面板的可用性徽章用。

    返回 ``{'backend', 'display', 'state', 'label'}``:
      · ``backend``  当前 config 选定的图床名(空 = 未配置;'any' = 自动)
      · ``display``  中文显示名(get_bed().display_name;any → '自动')
      · ``state``    'unset'(未配置,回退 msg_type=7)/ 'ok'(可用)/
                     'module_off'(image_hosting 模块未启用)/
                     'backend_off'(模块启用但该图床未开或配置不完整)/
                     'unknown'(模块无此图床 —— 已移除或未安装)
      · ``label``    面向用户的简短中文说明(徽章 tooltip 用)

    图床名单**动态**取自模块 status()(≥2.0.0 的 is_available 已含"配置完整 +
    SDK 就绪"语义)。与 ``_do_upload`` 的早退判定同源,徽章「可用」等价于
    「真实上传不会因配置 / 模块未启用而早退」(仍可能因网络失败,那是成功率
    指标的范畴,不在本函数职责内)。
    """
    backend = SELECTED_BACKEND
    if not backend:
        return {'backend': '', 'display': '', 'state': 'unset',
                'label': '未配置图床，游戏图直发 msg_type=7'}
    hosting = _get_hosting()
    if hosting is None:
        return {'backend': backend, 'display': backend, 'state': 'module_off',
                'label': '主框架 image_hosting 模块未启用'}
    try:
        status = hosting.status() if hasattr(hosting, 'status') else {}
    except Exception:
        status = None            # 状态查询异常 → 按未启用处理,不拖崩面板
    if backend == 'any':
        enabled = [n for n, v in (status or {}).items() if v]
        if enabled:
            return {'backend': 'any', 'display': '自动', 'state': 'ok',
                    'label': '自动依次尝试：' + ' / '.join(enabled)}
        return {'backend': 'any', 'display': '自动', 'state': 'backend_off',
                'label': 'image_hosting 模块没有任何已启用的图床'}
    if status is not None and backend not in status:
        return {'backend': backend, 'display': backend, 'state': 'unknown',
                'label': f'image_hosting 模块无图床 {backend!r}（已移除或未安装）'}
    display = _display_name(hosting, backend)
    if status and status.get(backend):
        return {'backend': backend, 'display': display, 'state': 'ok',
                'label': f'{display} 已就绪'}
    return {'backend': backend, 'display': display, 'state': 'backend_off',
            'label': f'主框架 image_hosting 未启用 {backend}（或配置不完整）'}


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


async def _do_upload(data: bytes, filename: str, user_id: str = '',
                     target_id: str = '', target_is_uid: bool = False) -> str | None:
    """实际调用图床上传 —— ``upload_image()`` 的内部实现,**不带去重缓存**。

    ``backend == 'any'`` 时交给模块的 ``upload_any``(按 priority 依次尝试全部
    可用图床,首个成功返回;附带绑定 bot 的 sender / token_manager 供 qq_file /
    qq_channel 使用);否则只调所选单个图床(快速失败)。直接调本函数会绕过
    缓存与 in-flight 互斥,**仅供同模块内部使用**。
    """
    backend = SELECTED_BACKEND
    if not backend:
        return None

    hosting = _get_hosting()
    if hosting is None:
        return None

    if backend == 'any':
        sender, tm = _bound_sender_and_tm()
        try:
            url = await hosting.upload_any(
                data, filename, token_manager=tm, sender=sender)
        except Exception as e:
            log.warning(f'upload_any 上传异常: {e}')
            url = None
    else:
        try:
            status = hosting.status() if hasattr(hosting, 'status') else {}
        except Exception:
            status = {}
        if not status.get(backend):
            log.warning(f'图床 {backend} 在 image_hosting 模块中未启用 / 不存在，请检查主框架配置')
            return None
        url = await _call_backend(hosting, backend, data, filename,
                                  user_id, target_id, target_is_uid)
    # 指标:一次真实图床往返(dedup 缓存命中与上面的未配置早退都不经过这里)
    metrics.record_upload(bool(url))
    if url:
        log.info(f'图床 {backend} 上传成功: {url}')
        return url
    return None


async def upload_image(data: bytes, filename: str, user_id: str = '', *,
                       target_id: str = '', target_is_uid: bool = False) -> str | None:
    """用 config.yaml 指定的图床上传('any' 则自动依次尝试)。

    ``target_id`` / ``target_is_uid`` 为当前消息的发送目标(qq_file 的上传
    作用域用;其他图床按签名过滤后自动忽略)。同份 data 并发时 in-flight
    去重以先到者的 target 为准 —— 预签名直链与作用域无关,任何目标可见。

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
        return await _do_upload(data, unique, user_id, target_id, target_is_uid)

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
        url = await _do_upload(data, unique, user_id, target_id, target_is_uid)
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

# 预签名直链后端(qq_file / any 可能选中 qq_file)的长缓存上限:直链带 ttl,
# 缓存超过 ttl 会让菜单挂出过期图片链接
_PRESIGNED_CACHE_TTL_CAP = 30 * 60


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
    - qq_file / any 后端的直链是**带 ttl 的预签名 URL**(精确 ttl 无法从 URL
      接口获知),缓存时长自动收紧到 30 分钟上限 —— logo 体积小,重传成本低,
      换取链接不过期。
    """
    if SELECTED_BACKEND in ('qq_file', 'any'):
        ttl_seconds = min(ttl_seconds, _PRESIGNED_CACHE_TTL_CAP)
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
