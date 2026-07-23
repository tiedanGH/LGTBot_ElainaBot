#!/usr/bin/env bash
#
# pack_prebuilt.sh <os_tag>
#
# 把一次完整构建(bash build.sh)的**运行时子集**打成
#   dist/lgtbot-<os_tag>-py<X.Y>-<bridgesha7>.zip
# 并在包内写 manifest.json(os / python_tag / boost / bridge_sha / submodule_sha / build_time / files[{path,size,sha256}])。
#
# 只收运行时真正需要的文件,排除 .a / 测试工具 / CMake 脚手架。
# 符号链接 (libmd4c.so → .so.0 → .so.0.5.2)**不以 --symlinks 存**:Python 的 zipfile.extractall 无法还原符号链接
# (会把链接目标路径当文件内容写出,坏掉 ld.so 的 soname 解析),所以让 zip 默认解引用成多个真实文件(每个几百 KB, 可忽略),下载端 extract 即得真实文件。
#
# Linux only(与本项目一致)。CI 三发行版各自跑一遍。
set -euo pipefail

OS_TAG="${1:?usage: pack_prebuilt.sh <os_tag>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# ── 必需产物校验(缺任一即失败,不产出半成品包)──────────────────────────
require() { test -e "$1" || { echo "❌ 缺少运行时产物: $1" >&2; exit 1; }; }
require LGTBot_ElainaBot.so
require build/libbot_core.so
require build/markdown2image
require build/match_game_runner
require build/config_runner
GAMES=$(find build/plugins -mindepth 2 -name 'libgame.so' | wc -l)
test "$GAMES" -gt 0 || { echo "❌ build/plugins 下没有 libgame.so" >&2; exit 1; }
echo "✅ 运行时产物齐备(libgame.so × $GAMES)"

# ── 元数据 ────────────────────────────────────────────────────────────────
# Python ABI:桥接层链接的 boost_python 库名内嵌 Python 版本(libboost_python311 → 3.11),
# 这是唯一权威来源(build.sh 的 ABI 匹配器最终选了它)。
BOOST=$(ldd LGTBot_ElainaBot.so | grep -oE 'libboost_python3[0-9]+' | head -1 || true)
PYABI=$(printf '%s' "$BOOST" | grep -oE '3[0-9]+' || true)
if [ -n "$PYABI" ]; then
  PYTAG="${PYABI:0:1}.${PYABI:1}"       # 311 → 3.11
else
  PYTAG="unknown"
fi
BRIDGE_SHA=$(git rev-parse --short=7 HEAD 2>/dev/null || echo "unknown")
SUB_SHA=$(git -C lgtbot rev-parse --short=7 HEAD 2>/dev/null || echo "")
BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "📦 os=$OS_TAG python=$PYTAG boost=$BOOST bridge=$BRIDGE_SHA sub=$SUB_SHA"

# ── 收集运行时子集到 stage/ ────────────────────────────────────────────────
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/build/plugins"

cp -a LGTBot_ElainaBot.so "$STAGE/"

# 核心共享库 + 两个 runner 子进程 + 渲染器
for f in libbot_core.so libcalsht_dw.so libtinyexpr.so \
         markdown2image match_game_runner config_runner; do
  [ -e "build/$f" ] && cp -a "build/$f" "$STAGE/build/"
done
# md4c 符号链接链(cp -a 保留链接;打包时由 zip 默认解引用成真实文件)
for f in build/libmd4c*.so*; do
  [ -e "$f" ] && cp -a "$f" "$STAGE/build/"
done
# 每个游戏目录(libgame.so + icon.png + resource/)整体搬入
cp -a build/plugins/. "$STAGE/build/plugins/"

# ── manifest.json(遍历 stage 计 sha256;写入 stage 后一并入包)────────────
OS_TAG="$OS_TAG" PYTAG="$PYTAG" BOOST="$BOOST" \
BRIDGE_SHA="$BRIDGE_SHA" SUB_SHA="$SUB_SHA" BUILD_TIME="$BUILD_TIME" \
python3 - "$STAGE" <<'PY'
import hashlib, json, os, sys
stage = sys.argv[1]
files = []
for root, _dirs, names in os.walk(stage):
    for n in names:
        p = os.path.join(root, n)
        if os.path.islink(p):          # 符号链接不计(zip 会解引用成真实文件)
            continue
        rel = os.path.relpath(p, stage).replace(os.sep, '/')
        if rel == 'manifest.json':
            continue
        with open(p, 'rb') as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        files.append({'path': rel, 'size': os.path.getsize(p), 'sha256': digest})
files.sort(key=lambda e: e['path'])
manifest = {
    'os':            os.environ['OS_TAG'],
    'python_tag':    os.environ['PYTAG'],
    'boost':         os.environ['BOOST'],
    'bridge_sha':    os.environ['BRIDGE_SHA'],
    'submodule_sha': os.environ['SUB_SHA'],
    'build_time':    os.environ['BUILD_TIME'],
    'files':         files,
}
with open(os.path.join(stage, 'manifest.json'), 'w', encoding='utf-8') as fh:
    json.dump(manifest, fh, ensure_ascii=False, indent=2)
print(f'manifest: {len(files)} files')
PY

# ── 打 zip(不加 --symlinks → 默认解引用符号链接为真实文件)─────────────────
mkdir -p "$ROOT/dist"
ZIPNAME="lgtbot-${OS_TAG}-py${PYTAG}-${BRIDGE_SHA}.zip"
( cd "$STAGE" && zip -rq "$ROOT/dist/$ZIPNAME" . )
echo "✅ 打包完成: dist/$ZIPNAME ($(du -h "$ROOT/dist/$ZIPNAME" | cut -f1))"
