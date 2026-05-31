#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""uploader 模块测试 —— filename 唯一化 / 图片尺寸解析 / 并发去重 / TTL cache。

被测重点(对应 plan 的 10 个 case):
  · _unique_filename 兜底逻辑(空 / 无扩展名 / 正常)
  · get_image_size 解析 PNG / JPEG / GIF / WebP + 异常 fallback
  · upload_image **并发去重核心**:
      - 同 data ×N 并发 → backend 调 1 次(in-flight Future 互斥,历史 size 错配 fix)
      - 不同 data ×N 并发 → backend 调 N 次,每次 unique filename 不同
      - 30s TTL cache 命中,不重复打图床
      - URL_CACHE_TTL=0 关闭去重,但 filename 唯一化仍生效
      - upload_image_cached(菜单 logo 路径)自动受益于 in-flight 互斥
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
import re

import pytest

from plugins.LGTBot_ElainaBot.mod import uploader


# ─────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_backend():
    """替换 uploader._do_upload 为 fake,记录每次调用 + 返回 fake URL。

    返回 (mock_fn, calls_list)。calls 是 [(data_first4, filename, user_id), ...]
    每个测试都拿干净的 mock —— autouse 的 _clean_runtime_state 已恢复 URL_CACHE_TTL
    和清空 _inflight / _url_cache_v2。
    """
    calls: list = []
    original = uploader._do_upload

    async def fake_do_upload(data, filename, user_id=''):
        calls.append((data[:4], filename, user_id))
        await asyncio.sleep(0.02)   # 模拟图床 RTT
        return f'https://fake.cdn/{filename}'

    uploader._do_upload = fake_do_upload
    yield fake_do_upload, calls
    uploader._do_upload = original


# ─────────────────────────────────────────────────────────────────────────
# 1-3. _unique_filename 三种兜底
# ─────────────────────────────────────────────────────────────────────────


def test_unique_filename_basic():
    """正常 filename:base 后追加 sha1[:8],扩展名保留"""
    result = uploader._unique_filename('match.png', 'abcdef1234567890')
    assert result == 'match_abcdef12.png'


def test_unique_filename_empty_fallback():
    """空 filename → 'image_<hash>.png'(base 用 image,扩展用 .png)"""
    result = uploader._unique_filename('', 'deadbeef12345678')
    assert result == 'image_deadbeef.png'


def test_unique_filename_no_ext_fallback():
    """无扩展名 → 仅 base 加哈希,默认补 .png 扩展"""
    result = uploader._unique_filename('foo', '1234567890abcdef')
    # 实现是 os.path.splitext 拿不到扩展时 ext 为空,代码兜底 ext='.png'
    assert result == 'foo_12345678.png'


# ─────────────────────────────────────────────────────────────────────────
# 4-8. get_image_size 4 个格式 + 异常
# ─────────────────────────────────────────────────────────────────────────


def test_get_image_size_png():
    """PNG 文件头 89 50 4E 47 0D 0A 1A 0A,紧跟 IHDR 含 width/height (big-endian)"""
    # PNG signature + IHDR chunk header(实际只读 offset 16-24)
    png = b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\x0DIHDR' + struct.pack('>II', 800, 600)
    assert uploader.get_image_size(png) == (800, 600)


def test_get_image_size_jpeg():
    """JPEG SOF0 marker (FFC0) 含 (precision, height, width, components),按 plan
    解析逻辑从 marker 偏移 +3 处读 height/width (big-endian, 2 bytes each)"""
    # 构造最小 JPEG:SOI(FFD8) + APP0(可选,这里用最简 SOF0 直接跟着)
    # SOF0 段:FF C0 [length:2] [precision:1] [height:2] [width:2] [components:1]
    jpeg = (b'\xff\xd8'                          # SOI
            + b'\xff\xc0'                        # SOF0 marker
            + b'\x00\x11'                        # segment length
            + b'\x08'                            # 8-bit precision
            + struct.pack('>H', 720)             # height
            + struct.pack('>H', 1280)            # width
            + b'\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01')  # components
    assert uploader.get_image_size(jpeg) == (1280, 720)


def test_get_image_size_gif():
    """GIF 'GIF8' 前缀,offset 6-10 是 width/height (little-endian 2 bytes each)"""
    gif = b'GIF89a' + struct.pack('<HH', 400, 300) + b'\x00' * 10
    assert uploader.get_image_size(gif) == (400, 300)


def test_get_image_size_webp_vp8():
    """WebP VP8 lossy 格式:RIFF + 'WEBP' + 'VP8 ' chunk,offset 26-30 width/height"""
    # RIFF[4] + size[4] + 'WEBP'[4] + 'VP8 '[4] + chunk_size[4] + ...
    # 实际 width/height 在 offset 26-30
    webp = (b'RIFF' + b'\x00' * 4 + b'WEBP'
            + b'VP8 ' + b'\x00' * 4
            + b'\x00' * 6                          # padding to reach offset 26
            + struct.pack('<HH', 640, 480))
    w, h = uploader.get_image_size(webp)
    assert w == 640 and h == 480


def test_get_image_size_bad_data_returns_default():
    """异常 / 非图片数据 → fallback (300, 300)"""
    assert uploader.get_image_size(b'not an image') == (300, 300)
    assert uploader.get_image_size(b'') == (300, 300)
    assert uploader.get_image_size(b'\x00' * 100) == (300, 300)


# ─────────────────────────────────────────────────────────────────────────
# 9-13. upload_image 并发 / cache / TTL 行为
# ─────────────────────────────────────────────────────────────────────────


async def test_upload_image_inflight_dedup(mock_backend):
    """同一份 data ×10 并发 → backend 只被调用 1 次,所有协程拿到同一 URL"""
    _, calls = mock_backend
    uploader.SELECTED_BACKEND = 'cos'    # 必须设,否则 upload_image 短路返 None

    # 用 module 顶层 mock:_do_upload 已被 mock_backend 替换,这里仍需 mock
    # _get_hosting 等让 upload_image 不短路。实际上 _do_upload 是真正调用 backend
    # 的入口,我们 mock 它,upload_image 的去重逻辑测试与 backend 无关。
    # 但 upload_image 仍会检查 SELECTED_BACKEND 和 _UPLOADERS_MAP —— 让它走完
    # 实际上 mock_backend 直接替换的是 _do_upload,upload_image 包装层会跳过
    # SELECTED_BACKEND 检查直接调它(因为我们没 mock 包装层)。
    # 看 upload_image 实现:它**调** _do_upload 之前没检查 SELECTED_BACKEND,
    # 只有 _do_upload 自身查。所以 mock _do_upload 已足够。

    data = b'PNG_SAMEDATA' + b'\x00' * 100
    results = await asyncio.gather(*[
        uploader.upload_image(data, 'match.png')
        for _ in range(10)
    ])

    # 10 个调用应拿到完全相同的 URL
    assert len(set(results)) == 1
    # backend 真正被调用次数应为 1(in-flight Future 共享)
    assert len(calls) == 1


async def test_upload_image_different_data_concurrent(mock_backend):
    """10 份不同 data 并发 + 同原 filename → backend 调 10 次,但**每次 filename 都
    不同**(各带不同 sha1[:8])—— 这是 cos_key 冲突 fix 的核心:不同 data 永远不
    会撞同一图床路径,即便用户传同 filename + 同尺寸 + 同时间戳。
    """
    _, calls = mock_backend
    datas = [f'IMG_{i:02d}'.encode() + b'\x00' * 100 for i in range(10)]

    results = await asyncio.gather(*[
        uploader.upload_image(d, 'match.png') for d in datas
    ])

    # 10 个 URL 全不同
    assert len(set(results)) == 10
    # backend 被调 10 次
    assert len(calls) == 10
    # 每次的 unique filename 应该符合 'match_<8 hex>.png' 模式 + 10 个都不同
    filenames = {c[1] for c in calls}
    assert len(filenames) == 10
    for f in filenames:
        assert re.fullmatch(r'match_[0-9a-f]{8}\.png', f), \
            f'filename pattern broken: {f!r}'


async def test_upload_image_url_cache_hit(mock_backend):
    """同 data 第二次请求 30s 内 → 命中 cache,backend 不再调"""
    _, calls = mock_backend
    data = b'CACHE_TEST' + b'\x00' * 100

    url1 = await uploader.upload_image(data, 'match.png')
    assert len(calls) == 1

    # 立即第二次 —— 应命中 _url_cache_v2
    url2 = await uploader.upload_image(data, 'match.png')
    assert url1 == url2
    assert len(calls) == 1, f'第二次应命中 cache 不调 backend: calls={calls}'


async def test_upload_image_ttl_zero_disables_dedup(mock_backend):
    """URL_CACHE_TTL=0 → 关闭去重,同 data ×3 并发,backend 调 3 次。
    但 **filename 唯一化仍生效**(根治 cos_key 冲突,不可关闭)。"""
    _, calls = mock_backend
    uploader.URL_CACHE_TTL = 0   # 关闭去重

    data = b'TTL_ZERO' + b'\x00' * 100
    results = await asyncio.gather(*[
        uploader.upload_image(data, 'match.png') for _ in range(3)
    ])

    # 关闭去重 → backend 调 3 次
    assert len(calls) == 3
    # 但 3 次 filename 都一样(同 data → 同 sha1 → 同 unique filename)
    filenames = {c[1] for c in calls}
    assert len(filenames) == 1
    # 校验 filename 形态:仍带 hash 后缀
    only_fname = next(iter(filenames))
    assert re.fullmatch(r'match_[0-9a-f]{8}\.png', only_fname)


async def test_upload_image_cached_benefits_from_inflight(mock_backend):
    """upload_image_cached(菜单 logo 路径)内部调 upload_image,**自动受益于 in-flight
    互斥** —— 多用户并发请求菜单时,backend 只调 1 次。
    """
    _, calls = mock_backend
    logo_data = b'LOGO' + b'\x01' * 200

    results = await asyncio.gather(*[
        uploader.upload_image_cached(logo_data, 'menu_logo.png', cache_key='menu:logo')
        for _ in range(5)
    ])

    # 5 个调用,全部非 None,URL 一致
    assert all(r is not None for r in results)
    assert len({r['url'] for r in results}) == 1
    # backend 只被调 1 次(in-flight 在 upload_image 层去重)
    assert len(calls) == 1
