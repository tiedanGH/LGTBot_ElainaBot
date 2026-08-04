#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# LGTBot × ElainaBot 一键编译脚本 (Linux)
#
# 用法：
#   cd plugins/LGTBot_ElainaBot
#   bash build.sh                          # 标准编译（无测试），构建全部
#   bash build.sh --test                   # 带 LGTBot 测试模式编译 (-DWITH_TEST=ON)
#   bash build.sh --clean                  # 清理后重编译
#   bash build.sh --clean --test           # 清理 + 测试模式
#   bash build.sh -j 8                     # 指定并行 jobs
#   bash build.sh --debug                  # Debug 构建 (默认 Release)
#   bash build.sh --asan                   # 启用 AddressSanitizer (-DWITH_ASAN=ON)
#   bash build.sh --glog                   # 启用 glog 日志 (默认 OFF)
#   bash build.sh --no-games               # 不编译内置游戏插件 (默认 ON)
#
# 仅构建指定目标 (重复 --target / -t 可指定多个):
#   bash build.sh -t LGTBot_ElainaBot      # 只编桥接层 .so (改 LGTBot_ElainaBot.cc 后常用)
#   bash build.sh -t numcomb -t alchemist  # 只编两个游戏
#   bash build.sh -t bot_core              # 只编 LGTBot 核心库
#   bash build.sh -t markdown2image        # 只编 markdown 转图二进制
#   bash build.sh --list-targets           # 列出所有可选目标 (CMake 已知 target)
#
# 增量编译 (跳过依赖检查 + CMake 重新配置,直接进 cmake --build):
#   bash build.sh -i                       # 增量构建全部
#   bash build.sh -i -t LGTBot_ElainaBot   # 增量只编桥接层 .so (最常用,秒级)
#   要求 build/ 已存在,与 --clean 互斥;首次构建仍须不带 -i 跑一遍完整流程
#
# 产物：plugins/LGTBot_ElainaBot/LGTBot_ElainaBot.so （ElainaBot 主框架启动时自动加载）
#       游戏 .so 在 build/plugins/<game>/libgame.so;markdown2image 在 build/markdown2image
# -----------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── 默认 LGTBot 编译选项 ────────────────────────────────────────────────
JOBS="$(nproc 2>/dev/null || echo 4)"
CLEAN=0
BUILD_TYPE="Release"
WITH_GCOV="OFF"
WITH_ASAN="OFF"
WITH_GLOG="OFF"
WITH_SQLITE="ON"
WITH_TEST="OFF"      # ← 默认关闭，用 --test 开启
WITH_GAMES="ON"
TARGETS=()           # 仅构建指定目标;为空则构建全部
LIST_TARGETS=0
INCREMENTAL=0        # 跳过依赖检查 + CMake 重新配置(要求 build/ 已存在)

# ── 参数解析 ─────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --clean)      CLEAN=1; shift ;;
        --test)       WITH_TEST="ON"; shift ;;
        --no-test)    WITH_TEST="OFF"; shift ;;
        --debug)      BUILD_TYPE="Debug"; shift ;;
        --release)    BUILD_TYPE="Release"; shift ;;
        --asan)       WITH_ASAN="ON"; shift ;;
        --gcov)       WITH_GCOV="ON"; shift ;;
        --glog)       WITH_GLOG="ON"; shift ;;
        --no-sqlite)  WITH_SQLITE="OFF"; shift ;;
        --no-games)   WITH_GAMES="OFF"; shift ;;
        -j)           JOBS="$2"; shift 2 ;;
        -j*)          JOBS="${1#-j}"; shift ;;
        --target)     TARGETS+=("$2"); shift 2 ;;
        --target=*)   TARGETS+=("${1#--target=}"); shift ;;
        -t)           TARGETS+=("$2"); shift 2 ;;
        -t*)          TARGETS+=("${1#-t}"); shift ;;
        --list-targets) LIST_TARGETS=1; shift ;;
        --incremental|-i) INCREMENTAL=1; shift ;;
        -h|--help)
            sed -n '2,33p' "$0"; exit 0 ;;
        *)            echo "未知参数: $1 (使用 --help 查看)"; exit 1 ;;
    esac
done

# --incremental 与 --clean 互斥:clean 会把 build/ 删了,incremental 又要求它存在
if [[ $INCREMENTAL -eq 1 && $CLEAN -eq 1 ]]; then
    echo "[!] --incremental 与 --clean 互斥(clean 会删 build/, incremental 要求它存在)"
    exit 1
fi
if [[ $INCREMENTAL -eq 1 && ! -d build ]]; then
    echo "[!] --incremental 要求 build/ 已存在 —— 请先不带 -i 跑一次完整构建"
    exit 1
fi

# ── 增量模式:跳过子模块/依赖/配置,直接进 cmake --build ───────────────────
# 注:list-targets 也走全流程的尾巴(读 build/Makefile),所以不在这里 early-return
if [[ $INCREMENTAL -eq 1 ]]; then
    echo "── 增量模式:跳过依赖检查 + CMake 配置 ──"
    echo "  Parallel : $JOBS"
    echo "  Targets  : $([ ${#TARGETS[@]} -eq 0 ] && echo '(全部)' || echo "${TARGETS[*]}")"
else

# ── 子模块检查 ───────────────────────────────────────────────────────────
# lgtbot 上游 .gitmodules 把 7 个嵌套子模块都登记成 ssh (git@github.com:...)
if [[ ! -f "lgtbot/CMakeLists.txt" ]]; then
    echo "[!] lgtbot/ 子模块为空，尝试初始化..."
    if [[ -d "../../.git" ]] || [[ -d ".git" ]]; then
        git -c url.https://github.com/.insteadOf=git@github.com: \
            -c url.https://github.com/.insteadOf=ssh://git@github.com/ \
            submodule update --init --recursive lgtbot || true
    else
        echo "[!] 当前目录不是 git 仓库 (.git/ 不存在)。"
        echo "    若是从插件市场下载,请先在主框架 Web 面板"
        echo "    『LGTBot 机器人 → 仪表盘 → 📥 初始化为 git 仓库』完成初始化,"
        echo "    再重新运行 bash build.sh。"
    fi
    if [[ ! -f "lgtbot/CMakeLists.txt" ]]; then
        echo "[!] 需先准备 lgtbot/ 源码 (https://github.com/slontia/lgtbot)"
        exit 1
    fi
fi

# ── TODO lgtbot Qt5 multiarch 探测补丁(上游 PR c87143b2 未合并前的兜底) ──────────
# 上游 CMakeLists 的 Qt5 预检漏了 Debian/Ubuntu 的 multiarch 路径(/usr/lib/x86_64-linux-gnu/...),
# 导致 markdown2image 的 Qt5 WebKit 后端在这些发行版探测不到 → 图片渲染被禁。
# 配置前把补丁临时打到 lgtbot/ **工作区**(不提交子模块改动)。幂等:已含该路径 / 上游已合并 / 补丁打不上 → 跳过并提示,不中断构建。
_QT5_PATCH="$SCRIPT_DIR/tools/patches/lgtbot-qt5-multiarch.patch"
if [[ -f "$_QT5_PATCH" && -f "lgtbot/CMakeLists.txt" ]]; then
    if grep -q 'x86_64-linux-gnu/cmake/Qt5Core' lgtbot/CMakeLists.txt; then
        :   # 已含 multiarch 路径(补丁已打 / 上游已合并),跳过
    elif patch -p1 -d lgtbot --forward < "$_QT5_PATCH" >/dev/null 2>&1; then
        echo "✅ 已应用 lgtbot Qt5 multiarch 探测补丁 (上游 PR c87143b2)"
    else
        echo "[!] Qt5 multiarch 补丁未能应用 (缺 patch 命令 / 上游已变动);"
        echo "    multiarch 布局下 Qt5 WebKit 可能仍探测不到,markdown2image 将无图。"
    fi
fi

# ── 依赖自检 ─────────────────────────────────────────────────────────────
echo "── 依赖检查 ─────────────"
need=()
command -v cmake >/dev/null 2>&1 || need+=("cmake")
command -v g++   >/dev/null 2>&1 || command -v clang++ >/dev/null 2>&1 || need+=("g++ 或 clang++")
[[ -f /usr/include/curl/curl.h ]] || pkg-config --exists libcurl 2>/dev/null || need+=("libcurl-dev")

# Python3 dev headers
PY=$(command -v python3 || true)
if [[ -z "$PY" ]]; then
    need+=("python3")
else
    PY_INC=$($PY -c "import sysconfig; print(sysconfig.get_path('include'))")
    [[ -f "$PY_INC/Python.h" ]] || need+=("python3-dev (找不到 Python.h)")
fi

# Boost.Python：多重检测，任一通过即认可
# 单纯用 ldconfig -p 不可靠：apt 装完 dev 包后系统缓存可能没立刻刷新；
# 不同发行版库文件命名也千差万别（libboost_python3.so / .py310 / -mt 等），
# 单一正则覆盖不全。这里依次尝试 4 种方法：
#   1. ldconfig 缓存（最快，但可能过期）
#   2. 直接扫常见 lib 目录（绕过缓存）
#   3. dpkg 包级查询（Debian/Ubuntu）
#   4. rpm 包级查询（CentOS/RHEL/Fedora）
_have_boost_python() {
    ldconfig -p 2>/dev/null | grep -qE 'libboost_python[0-9a-z.+-]*\.so' && return 0
    local d
    for d in /usr/lib/x86_64-linux-gnu /usr/lib64 /usr/lib /usr/local/lib /usr/local/lib64; do
        [[ -d "$d" ]] || continue
        find "$d" -maxdepth 1 -name 'libboost_python*.so*' 2>/dev/null | grep -q . && return 0
    done
    if command -v dpkg-query >/dev/null 2>&1; then
        dpkg-query -W -f='${Package} ${db:Status-Status}\n' 'libboost-python*-dev' 2>/dev/null \
            | grep -q ' installed$' && return 0
    fi
    if command -v rpm >/dev/null 2>&1; then
        rpm -q --quiet boost-python3-devel 2>/dev/null && return 0
        rpm -q --quiet boost-python36-devel 2>/dev/null && return 0
    fi
    return 1
}
if ! _have_boost_python; then
    need+=("libboost-python3-dev")
fi

if [[ ${#need[@]} -gt 0 ]]; then
    echo "[!] 缺少依赖："
    for d in "${need[@]}"; do echo "    - $d"; done
    echo
    echo "Ubuntu/Debian 一键安装："
    echo "  sudo apt update && sudo apt install -y \\"
    echo "    build-essential cmake libcurl4-openssl-dev \\"
    echo "    python3-dev libboost-python-dev libboost-system-dev \\"
    echo "    libgflags-dev libgoogle-glog-dev libsqlite3-dev"
    exit 1
fi
echo "✅ 依赖齐全"

# ── Python 3 兼容版本探测 ─────────────────────────────────────────────────
# CMake find_package(Python3) 默认 VERSION 策略会选系统**最高版本**(可能扫到
# deadsnakes 装的 python3.13),但发行版的 libboost-python-dev 只为系统默认
# Python 编译(22.04→3.10,24.04→3.12,Debian 12→3.11)。两者错配 → 找不到
# libboost_python<XYZ>.so → 在 CMakeLists 的 Boost.Python 查找处 FATAL_ERROR。
#
# 本段扫 /usr/lib/.../libboost_python<XYZ>.so 提取 ABI 版本(310 / 312 等),
# 反推应用的 Python 解释器,显式 -DPython3_EXECUTABLE 传给 cmake,避免错配。
# 优先级:PYTHON3 环境变量 > 扫到的 boost-python ABI 匹配 > $(command -v python3) 兜底
PY3_EXE=""
if [[ -n "${PYTHON3:-}" ]]; then
    PY3_EXE=$(command -v "$PYTHON3" || true)
    if [[ -z "$PY3_EXE" ]]; then
        echo "[!] 环境变量 PYTHON3=$PYTHON3 找不到对应可执行文件"
        exit 1
    fi
    echo "── Python 选择 ────────"
    echo "[i] 用户指定 PYTHON3=$PYTHON3 → $PY3_EXE"
else
    # 扫已安装的 libboost_python<XYZ>.so,收集 ABI 版本号
    _BP_VERS=()
    for d in /usr/lib/x86_64-linux-gnu /usr/lib64 /usr/lib /usr/local/lib /usr/local/lib64; do
        [[ -d "$d" ]] || continue
        while IFS= read -r so; do
            v=$(basename "$so" | sed -nE 's/^libboost_python([0-9]+)\..*/\1/p')
            [[ -n "$v" ]] && _BP_VERS+=("$v")
        done < <(find "$d" -maxdepth 1 -name 'libboost_python[0-9]*.so*' 2>/dev/null)
    done
    # 去重 + 数值降序(高版本优先,例如 312 优先于 310)
    if [[ ${#_BP_VERS[@]} -gt 0 ]]; then
        IFS=$'\n' _BP_VERS=($(printf '%s\n' "${_BP_VERS[@]}" | sort -urn))
        unset IFS
    fi
    echo "── Python 选择 ────────"
    if [[ ${#_BP_VERS[@]} -gt 0 ]]; then
        echo "[i] 系统已安装 boost-python ABI: ${_BP_VERS[*]}"
    fi
    # 对每个 ABI 版本查找匹配解释器:310 → python3.10
    for ver in "${_BP_VERS[@]}"; do
        if [[ "$ver" =~ ^([0-9])([0-9]+)$ ]]; then
            py_ver="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}"
            cand=$(command -v "python$py_ver" || true)
            if [[ -n "$cand" ]]; then
                PY3_EXE="$cand"
                echo "[i] 匹配 libboost_python${ver} → 选用 python$py_ver ($PY3_EXE)"
                break
            else
                echo "[i] libboost_python${ver} 存在,但找不到 python$py_ver 解释器"
            fi
        fi
    done
    # 兜底:没有任何 ABI 匹配,退回到 $(command -v python3)
    if [[ -z "$PY3_EXE" ]]; then
        PY3_EXE=$(command -v python3 || true)
        if [[ -z "$PY3_EXE" ]]; then
            echo "[!] 找不到 python3"; exit 1
        fi
        if [[ ${#_BP_VERS[@]} -gt 0 ]]; then
            echo "[!] 未找到与 boost-python ABI(${_BP_VERS[*]})匹配的 Python 解释器"
            echo "    回退到默认 python3 ($PY3_EXE),后续 CMake 可能仍报 Boost.Python 找不到"
            echo "    可手动安装匹配的 Python: 例如 ABI=310 → sudo apt install python3.10 python3.10-dev"
        else
            echo "[i] 系统无 libboost_python<XYZ>.so,使用默认 python3 ($PY3_EXE) 让 CMake 兜底报错"
        fi
    fi
fi

# ── 清理 ─────────────────────────────────────────────────────────────────
if [[ $CLEAN -eq 1 ]]; then
    echo "── 清理 build/ ────────"
    rm -rf build
fi

# ── 配置 + 编译 ──────────────────────────────────────────────────────────
echo "── 编译选项 ───────────"
cat <<EOF
  Build Type   : $BUILD_TYPE
  WITH_TEST    : $WITH_TEST   $([ "$WITH_TEST" = "ON" ] && echo '← 测试模式已启用')
  WITH_GAMES   : $WITH_GAMES
  WITH_GLOG    : $WITH_GLOG
  WITH_SQLITE  : $WITH_SQLITE
  WITH_ASAN    : $WITH_ASAN
  WITH_GCOV    : $WITH_GCOV
  Parallel     : $JOBS
  Targets      : $([ ${#TARGETS[@]} -eq 0 ] && echo '(全部)' || echo "${TARGETS[*]}")
EOF

echo "── CMake 配置 ─────────"
cmake -S . -B build \
    -DPython3_EXECUTABLE="$PY3_EXE" \
    -DCMAKE_BUILD_TYPE="$BUILD_TYPE" \
    -DWITH_GCOV="$WITH_GCOV" \
    -DWITH_ASAN="$WITH_ASAN" \
    -DWITH_GLOG="$WITH_GLOG" \
    -DWITH_SQLITE="$WITH_SQLITE" \
    -DWITH_TEST="$WITH_TEST" \
    -DWITH_GAMES="$WITH_GAMES"

fi  # end of: if INCREMENTAL == 0

# ── --list-targets:列出 CMake 已知 target,方便用户挑 -t 参数 ───────────
if [[ $LIST_TARGETS -eq 1 ]]; then
    echo "── 可用编译目标 ───────"
    # `make help` 是 CMake 生成 Makefile 时自带的目标列表;比 cmake --target help
    # 输出更整洁(后者可能在新版 CMake 上 require generator-specific 支持)。
    if [[ -f build/Makefile ]]; then
        make -C build help 2>/dev/null | sed -n '/^\.\.\./p' | head -200
    else
        cmake --build build --target help 2>/dev/null | head -200
    fi
    echo
    echo "提示:bash build.sh -t <target> [-t <target> ...] 仅构建指定目标"
    exit 0
fi

# ── 实际编译 ─────────────────────────────────────────────────────────────
# 多 target 时一次 cmake --build 调用里挂多个 --target,CMake ≥3.15 支持;
# 留空 TARGETS 时不传 --target,走默认 all。
#
# 游戏目标要额外带上资源复制:lgtbot/games/CMakeLists.txt 里 icon.png 与 resource/ 的复制
# 是 make_output_dir_<GAME> / resource_dir_<GAME> 两个 **仅挂 ALL** 的 custom target,
# 与游戏库 <GAME> 之间没有依赖边(rule_binary 才有 add_dependencies)
# 单目标构建的依赖图里没有它们,只有完整编译会执行。这里在调用侧把伴生 target 显式加进构建列表。
# 判定游戏与上游同款:games/<t>/mygame.cc 存在(CMakeLists.txt:31)。
target_args=()
for t in "${TARGETS[@]}"; do
    target_args+=(--target "$t")
    if [[ -f "lgtbot/games/$t/mygame.cc" ]]; then
        target_args+=(--target "make_output_dir_$t" --target "resource_dir_$t")
        echo "[i] 游戏目标 $t:附加资源复制 (icon.png + resource/)"
    fi
done

echo "── 编译 (-j $JOBS) ────"
cmake --build build -j "$JOBS" "${target_args[@]}"

# ── 验证产物 ─────────────────────────────────────────────────────────────
# 只在「肯定构建过 LGTBot_ElainaBot.so」的两种情形下校验:
#   · 未指定 -t  (走默认 all,.so 必然在 all 里)
#   · -t LGTBot_ElainaBot 显式指定
# 否则 (比如 `-t numcomb`) 不要因 .so 不存在而报错 —— 用户根本没让构建它。
WANT_SO=0
if [[ ${#TARGETS[@]} -eq 0 ]]; then
    WANT_SO=1
else
    for t in "${TARGETS[@]}"; do
        [[ "$t" == "LGTBot_ElainaBot" ]] && WANT_SO=1 && break
    done
fi

SO_PATH="$SCRIPT_DIR/LGTBot_ElainaBot.so"
if [[ $WANT_SO -eq 1 ]]; then
    if [[ ! -f "$SO_PATH" ]]; then
        # 部分 CMake 版本不遵守 LIBRARY_OUTPUT_DIRECTORY，到 build/ 找
        FOUND=$(find build -name 'LGTBot_ElainaBot.so' -print -quit || true)
        if [[ -n "$FOUND" ]]; then
            cp "$FOUND" "$SO_PATH"
        fi
    fi
    if [[ ! -f "$SO_PATH" ]]; then
        echo "[!] 编译完成但未找到 LGTBot_ElainaBot.so"
        exit 1
    fi
fi

echo
echo "════════════════════════════════════════════════════════════════"
if [[ $WANT_SO -eq 1 ]]; then
    echo " ✅ 编译成功"
    echo "    $SO_PATH"
else
    echo " ✅ 已构建目标: ${TARGETS[*]}"
fi
echo "════════════════════════════════════════════════════════════════"
