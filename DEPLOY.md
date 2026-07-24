# LGTBot × ElainaBot 部署指南

> 把 [LGTBot](https://github.com/slontia/lgtbot) 游戏引擎接入 ElainaBot QQ 主框架，
> 一键编译 → 启动主框架即可使用，**无需任何额外配置**。

---

## 1. 系统依赖（仅编译时）

目前只支持 **Linux**（lgtbot 引擎依赖 POSIX/Boost.Python，Windows 上编译复杂度极高）。

> 💡 **不想装工具链？** 若你的发行版 / Python 版本在预编译覆盖范围内（Ubuntu 22.04 / 24.04、Debian 12），可直接下载预编译包运行，**跳过本节所有编译依赖**，见 [§3.1 预编译包部署](#31-预编译包部署免本地工具链)。

### Ubuntu / Debian
```bash
sudo apt update
sudo apt install -y \
    build-essential cmake git patch \
    libcurl4-openssl-dev \
    python3-dev \
    libboost-python-dev libboost-system-dev \
    libgflags-dev libgoogle-glog-dev libsqlite3-dev \
    qtbase5-dev libqt5webkit5-dev
```

> `qtbase5-dev` + `libqt5webkit5-dev` 供 markdown2image 渲染游戏图片（默认 Qt5 WebKit 后端）；缺失时构建会跳过图片支持，引擎将无图片渲染。

### CentOS / RHEL
```bash
sudo yum install -y \
    gcc-c++ cmake git patch \
    libcurl-devel python3-devel \
    boost-python3-devel boost-devel \
    gflags-devel glog-devel sqlite-devel \
    qt5-qtbase-devel qt5-qtwebkit-devel
```

> **C++20 要求**：GCC ≥ 10 / Clang ≥ 12。Ubuntu 20.04 默认 GCC 9，需 `sudo apt install g++-10` 并 `export CXX=g++-10`。

---

## 2. 准备 lgtbot 源码

### 2.1 从插件市场安装 (推荐路径)

主框架插件市场下载到的 zip **不包含 `.git/`**(`web/tools/_market/install.py` 解压时显式过滤)，所以 `git` 命令都无法运行。但插件自带一键修复入口:

1. 确保系统已装 git 客户端 (`git --version` 能跑就行，无需 SSH key)
2. 启动主框架，访问 Web 面板 →「LGTBot 机器人 → 仪表盘」
3. 「📦 版本与更新」→ 桥接层行点 **「📥 初始化为 git 仓库」**
   - 后端执行 `git init -b main` → `remote add origin` → `fetch --tags --depth 50` → `reset --mixed v<当前版本>`
   - `reset --mixed` 只动 index 不动工作区， `data/`、`build/`、`lgtbot/` 全保留
4. 同一面板 → 子模块行点 **「⬇ 初始化子模块」**
   - 后端用 `git -c url.https://...insteadOf=git@github.com:` **临时改写 SSH→HTTPS**，递归生效到 lgtbot 嵌套的 7 个子模块，**不需要 SSH key**
5. 切到「引擎编译」tab → 「🛠 完整编译」

> 💡 **没装 git 也能检查与更新版本**：「版本与更新」的检查不依赖本地 git；检测到新版本时会出现 **「⬇ 下载更新」** 按钮，直接下载最新 release 源码包覆盖更新。初始化 git 仓库仅在需要 `git pull` / 开发时才必须。

### 2.2 从源仓库 clone (开发者路径)

如果你自己 clone 了本仓库到 `plugins/LGTBot_ElainaBot/`，`lgtbot/` 是上游的 git 子模块。若该目录为空:

```bash
cd plugins/LGTBot_ElainaBot
git clone --recursive https://github.com/slontia/lgtbot.git lgtbot
```

或在已有 git 仓库中:
```bash
git submodule update --init --recursive plugins/LGTBot_ElainaBot/lgtbot
```

注:lgtbot 上游的 `.gitmodules` 把嵌套子模块全部登记为 SSH url。如果你没 SSH key,改用 dashboard 的「⬇ 初始化子模块」按钮(已内置 SSH→HTTPS 改写),或者手动加 `-c url."https://github.com/".insteadOf="git@github.com:"` 给上面的 `git submodule update`。

---

## 3. 一键编译

```bash
cd plugins/LGTBot_ElainaBot
bash build.sh                          # 标准编译（Release，无测试）
bash build.sh --test                   # 带 LGTBot 单元测试 (-DWITH_TEST=ON)
bash build.sh --clean                  # 清理后重编译
bash build.sh --clean --test           # 清理 + 测试模式
bash build.sh -j 8                     # 8 进程并行
bash build.sh --debug                  # Debug 构建（含调试符号）
bash build.sh --asan                   # 启用 AddressSanitizer 排查内存问题
bash build.sh --glog                   # 启用 glog 日志（默认关闭）
bash build.sh --no-games               # 不编译内置游戏（仅引擎）
bash build.sh -t LGTBot_ElainaBot      # 仅编桥接层 .so（改了 LGTBot_ElainaBot.cc 后最常用）
bash build.sh -t numcomb -t alchemist  # 仅编两个游戏（增量调试某游戏）
bash build.sh --list-targets           # 列出所有可选目标
bash build.sh -i                       # 增量编译：跳过依赖检查 + CMake 配置（秒级）
bash build.sh -i -t LGTBot_ElainaBot   # 增量 + 仅编桥接层（迭代 .cc 时最快路径）
bash build.sh --help                   # 查看所有参数
```

| 参数                      | CMake 选项             | 默认        | 说明                                                  |
|-------------------------|----------------------|-----------|-----------------------------------------------------|
| `--test` / `--no-test`  | `-DWITH_TEST`        | `OFF`     | LGTBot 内部单元测试（开发调试用）                                |
| `--debug` / `--release` | `-DCMAKE_BUILD_TYPE` | `Release` | 构建类型                                                |
| `--asan`                | `-DWITH_ASAN`        | `OFF`     | AddressSanitizer                                    |
| `--gcov`                | `-DWITH_GCOV`        | `OFF`     | 覆盖率统计                                               |
| `--glog`                | `-DWITH_GLOG`        | `OFF`     | glog 日志（默认关闭，加此参数启用）                                |
| `--no-sqlite`           | `-DWITH_SQLITE`      | `ON`      | SQLite 持久化（关闭后无排行榜/历史）                              |
| `--no-games`            | `-DWITH_GAMES`       | `ON`      | 50+ 内置游戏插件                                          |
| `-t` / `--target NAME`  | `cmake --target`     | `(全部)`    | 仅构建指定目标，可重复多次（如 `-t numcomb`）                       |
| `--list-targets`        | —                    | —         | 列出 CMake 已知目标，方便挑 `-t` 参数                           |
| `-i` / `--incremental`  | —                    | —         | 跳过 依赖检查 + CMake 直接构建；要求 `build/` 已存在，与 `--clean` 互斥 |

> 生产部署使用 `bash build.sh` 即可；只有需要跑 LGTBot 自带测试用例时才加 `--test`。
>
> ⚠️ 编译完成后 **请勿删除 `build/`** —— LGTBot 引擎运行时会从该目录动态加载游戏 `.so`。

### 3.1 预编译包部署（免本地工具链）

CI 已为 **Ubuntu 22.04 / 24.04、Debian 12** 各预编译一份引擎核心 + 全部游戏，发布到本仓库滚动的预发布 release。若本机在此范围，无需 §1 的编译依赖，直接在 Web 面板下载：

1. 启动主框架后打开 Web 面板 → 侧边栏「LGTBot 机器人」→「📦 预编译部署」tab
2. 在**仪表盘「🧪 运行环境」**看依赖：运行时依赖齐全即可；编译依赖标灰「预编译无需」可忽略
3. 点「📶 测速」选择延迟低的下载镜像，刷新**预编译包**列表
4. 在**预编译包**列表选**与本机匹配**（发行版 + Python 版本一致）的包，点「⬇ 下载」，等进度条完成
5. 到**构建来源**分区点「📦 用预编译包」，再点右上角「🔁 重启 LGTBot」生效

- 预编译包解压到 `plugins/LGTBot_ElainaBot/build_prebuilt/`，与本地 `build/` **并存**，可随时切换（每次切换都需要重启）。
- **引擎运行中更新包**：若预编译目录被运行中引擎占用导致换入失败，会自动暂存，**重启 LGTBot 后自动完成安装**（面板会提示，无需人工干预）。
- 匹配以**发行版 + Python 小版本**为准：桥接层 `.so` 锁定 Boost.Python ABI，不匹配的包无法加载。列表对**系统与 Python 分别标注**（系统不匹配红、Python 不匹配橙，系统权重更高），下载时按「完全不匹配 / 系统不匹配 / 部分匹配」分级提示。
- 列表按时间倒序，与已安装 `build_prebuilt/manifest.json` 对比提示。

> 需要改桥接层源码或用未覆盖的发行版 / Python 时，仍走 §3 本地编译。

### 3.2 在 Docker / 纯运行时环境用预编译包所需的运行时库

预编译包只含**引擎自己编译出的产物**，不含它运行时动态链接的系统库。像主框架镜像这类纯运行时环境，缺这些库会让 import 直接失败。

`python:3.11-slim`（**Debian 12 + Python 3.11**），需选择 `lgtbot-debian-12-py3.11` 预编译包。

需补装的**运行时库**，Debian 12 对应包名：

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
        libboost-python1.74.0 libprotobuf32 libcurl4 libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/*
```

图片渲染：`markdown2image` 引擎出图需要 Qt 运行库，仅图片渲染不可用，补装图片渲染：

```bash
apt-get install -y --no-install-recommends libqt5webkit5
```

- **以自检面板为准**：面板对真实产物跑 `ldd` 实测，「编译依赖」标灰「预编译无需」可忽略。
- 其他发行版包名不同（如 RHEL/CentOS 用 `boost-python3` / `protobuf` / `libcurl` / `sqlite-libs`），按自检红项提示的 soname 逐一对应安装即可。

---

## 4. 启动主框架（零配置）

```bash
cd ../..                # 回到 ElainaBot_v2 根目录
python3 main.py         # 启动主框架，自动加载插件
```

启动应看到类似日志：
```
[插件:LGTBot] LGTBot 管理员配置：1 人
[插件:LGTBot] 初始化 LGTBot 引擎: 游戏数=52, db=plugins/LGTBot_ElainaBot/data/engine/lgtbot.db, conf=plugins/LGTBot_ElainaBot/data/engine/lgtbot.json
[插件:LGTBot] ✅ LGTBot 引擎已就绪
[插件:LGTBot_ElainaBot] 大型插件加载完成 (1 个处理器, 0.12s)
```

完成。在 QQ 群里 @ 机器人发送 `#帮助` 即可看到游戏列表。

---

## 5. 配置 `data/config.yaml`

首次启动时插件会自动生成下面这份模板（字段顺序与 yaml 输出一致）：

```yaml
# 绑定机器人 appid（可在仪表盘配置）。留空 = 自动使用框架第一个 bot
bind_bot_appid: ''
# LGTBot 内部管理员 openid 列表（不同于 ElainaBot 的 owner_ids）
admin_uids: []
# 游戏图片走 markdown 内嵌时使用的图床。留空 = 不启用图床，所有图片直接以 msg_type=7 发送
image_hosting: ''
# 被动消息配额（5条）耗尽时等待用户点击「刷新」按钮的最长秒数，超时改走主动消息
refresh_wait_timeout: 15.0
# 同份图片重复上传去重 TTL（秒），并发请求共享上传结果；0 = 关闭去重，负数自动归 0
image_upload_dedup_ttl: 60.0
# 严重问题通知群 openid，引擎崩溃时向此群主动推送崩溃报告；留空 = 不推送
crash_notify_group: ''
# 屏蔽指令列表：命中的消息不再转发给引擎，用于化解与其他插件的指令冲突
blocked_commands: []
# 沙箱用户 openid 列表，列表内用户私信走主动消息直推；填 ["all"]（仅此一项）= 全员直推模式
sandbox_dm_users: []
# 欢迎菜单里「游戏快捷开局」按钮列表，游戏名需与 /游戏列表 输出一致
menu_game_buttons:
  - '数字蜂巢'
  - '天赋云巢'
  - '炼金术士'
  - '差值投标'
  - '决胜五子'
  - '彩虹奇兵'
```

**配置项说明（按 yaml 字段顺序）：**

| 字段                       | 类型          | 默认     | 说明                                                                                                           |
|--------------------------|-------------|--------|--------------------------------------------------------------------------------------------------------------|
| `bind_bot_appid`         | `str`       | `''`   | 绑定机器人 appid（仪表盘可视化选择）。留空自动使用第一个 bot。绑定后所有功能均走该 bot，**其他 bot 的事件被静默忽略**。appid 为纯数字，手工填写可不加引号                  |
| `admin_uids`             | `list[str]` | `[]`   | LGTBot 内部管理员的 QQ openid 列表                                                                                   |
| `image_hosting`          | `str`       | `''`   | markdown 图片内嵌使用的图床（`cos` / `nature` / `bilibili` / `chatglm` / `ukaka` / `xingye`），留空 = 直接走 msg_type=7       |
| `refresh_wait_timeout`   | `float`     | `15.0` | 配额耗尽时阻塞等待用户点击刷新按钮的秒数；超时改走主动消息（不再用过期 msg_id 强发）                                                               |
| `image_upload_dedup_ttl` | `float`     | `60.0` | 同份图片上传去重 TTL（秒）。`>0` 启用 content-hash URL 缓存 + in-flight Future 共享（多并发上传只打图床一次）；`0` 关闭去重每次重传；filename 唯一化始终启用 |
| `crash_notify_group`     | `str`       | `''`   | 严重问题通知群 openid —— 引擎崩溃时向此群主动推送崩溃报告。留空 = 不推送。该群需给本 bot 开了全量推送权限,主动消息才能落地                                      |
| `blocked_commands`       | `list[str]` | `[]`   | 屏蔽指令列表：命中的消息（文本 / 按钮回调）不再转发给 LGTBot 引擎，化解其他插件的指令冲突。带 / 不带 `/` **严格按配置匹配**，`指令 参数` 形式也命中。                     |
| `sandbox_dm_users`       | `list[str]` | `[]`   | 私信主动直推名单。填 `["all"]`（仅此一项）= 全员直推模式：官方现已默认允许 bot 向好友推送主动私信，配额耗尽后对任意用户直推、不再丢弃。                                 |
| `menu_game_buttons`      | `list[str]` | (6 项)  | 欢迎菜单里「游戏快捷开局」按钮列表，每行最多 3 个；游戏名需与 `/游戏列表` 输出一致                                                                |

> 💡 **图床（可选）**：启用 ElainaBot 主框架的 `image_hosting` 模块并配置目标图床后，把图床名填到本插件 `config.yaml` 的 `image_hosting` 字段
> 本插件就会用 markdown `![](url)` 内嵌发送游戏图片（保留原生 `<@>` 提及和按钮）。**仅尝试指定的这一个图床**，上传失败立即回退 `msg_type=7` 媒体
> 注意：图床域名需先在 QQ 开放平台「消息 URL 配置」里报备，否则消息不显示（COS 自有 CDN 与 Nature 的 download.nature.qq.com 最易过审）。

> 💡 **全量群（可选）**：主框架 `config/bot.yaml` 里 `non_at_message.enabled` 或 `non_at_message.group_whitelist` 配的群，本插件会自动适配——
> 监听 `GROUP_MESSAGE_CREATE`（仍强制 `is_at_self` 检查，日常对话不会触发引擎），且这些群里 bot 不再追加「刷新会话」按钮，被动配额耗尽时直接走主动消息。
> 改动 `non_at_message.group_whitelist` 后约 5 秒（主框架配置 mtime 缓存）即生效，无需重启。

**两种填写方式（任选其一）：**

**A. Web 面板在线编辑（推荐）**

1. ElainaBot 主面板 → 左侧「插件」→ 找到 `LGTBot_ElainaBot` → 点击「配置」
2. 直接编辑 `config.yaml`，保存即生效
3. 部分修改可能需要在「插件管理」里 reload 一下本插件

**B. 命令行直接编辑**

1. 让目标管理员在群里给 bot 发任意消息
2. Web 面板「日志」找到该用户的 `user_id`（即 openid）
3. 编辑 `plugins/LGTBot_ElainaBot/data/config.yaml`：
   ```yaml
   admin_uids:
     - 'AAAA-BBBB-CCCC-DDDD'
     - 'EEEE-FFFF-GGGG-HHHH'
   ```
4. 重启 ElainaBot 或在 Web 面板禁用→重新启用本插件

---

## 6. 卸载

```bash
# 框架运行中：通过 Web 面板「插件」选项卡禁用 LGTBot
# 或彻底移除：
rm -rf plugins/LGTBot_ElainaBot
```

> **安全关闭：** 插件 `@on_unload` 会调用 `release_bot_if_not_processing_games`，存在进行中游戏时会拒绝释放并打印警告，请等待对局结束或 `kill -9`。

> **数据备份不丢：** 插件每天自动备份核心数据库到 `<framework_root>/data/backup/lgtbot/LGTBot_*.zip`。卸载只删 `plugins/LGTBot_ElainaBot/`，**备份目录保留**。重新安装后可在「💾 数据备份」tab 一键「↩ 恢复」回卸载前的状态。

---

## 7. 故障排查

| 现象                                                                 | 排查                                                                                                                                                                                                                                                                                                                                                 |
|--------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `LGTBot_ElainaBot C++ 扩展未编译或导入失败`                                  | 重跑 `bash build.sh`；查看 `plugins/LGTBot_ElainaBot/LGTBot_ElainaBot.so` 是否存在                                                                                                                                                                                                                                                                          |
| `CMake 报错「找不到 Boost.Python」` + 日志 `Python3: 3.13.x` 远超系统默认         | 发行版 `libboost-python-dev` 只为系统默认 Python 编译(22.04→3.10 / 24.04→3.12 / Debian 12→3.11);装了 deadsnakes python3.13 后 CMake 默认选最高版本,跟 `libboost_python310.so` 等 ABI 不匹配。`build.sh` 已自动按系统已装的 boost-python ABI 反推选用匹配的 Python(看 build 日志「Python 选择」段);若仍误判可手动 `PYTHON3=python3.10 bash build.sh` 强制指定,或装匹配版本 `sudo apt install python3.10 python3.10-dev` |
| `git pull` / `git submodule update` 报错 / build.sh 提示「.git/ 不存在」    | 从插件市场安装的目录里没有 `.git/`(市场显式过滤)。打开仪表盘 → 桥接层行点「📥 初始化为 git 仓库」一键修复;再点「⬇ 初始化子模块」拉 lgtbot(已内置 SSH→HTTPS 改写,无需 SSH key)。详见 §2.1                                                                                                                                                                                                                          |
| `libbot_core.so: cannot open shared object file`                   | `LGTBot_ElainaBot.so` 链接 `libbot_core.so` 但 ld.so 默认不搜 `build/`。本插件已用 `ctypes.CDLL` 在 import 阶段预加载 `build/lib*.so`；若仍报错，确认 `build/libbot_core.so` 存在，或手动 `LD_LIBRARY_PATH=plugins/LGTBot_ElainaBot/build python3 main.py`                                                                                                                          |
| `ImportError: undefined symbol: ...boost::python...`               | Boost.Python 与编译时的 Python 版本不匹配 — `bash build.sh --clean` 重编译                                                                                                                                                                                                                                                                                      |
| 切到预编译后引擎起不来 / 桥接 `undefined symbol` / 游戏 spawn 失败                 | 多半是下载的预编译包**与本机发行版 / Python 小版本不匹配**(桥接 `.so` 锁 Boost.Python ABI)。在「📦 预编译部署」列表按标注选**本机匹配**的包重下;`config_runner` / `match_game_runner` 的路径由 `boot.py` 自动重定位(设 `LGTBOT_MATCH_RUNNER` + `LD_LIBRARY_PATH` + 桥接传 `config_runner_path_`),若仍失败切回「🧱 用本地编译」并重启,或用 §3 本地编译                                                                          |
| `Load mod failed: ... undefined symbol: _ZN6google10LogMessage...` | 仅在 `--glog` 编译时可能出现（glog 默认关闭，一般不会遇到）。glog 符号不可见，本插件已在 `main.py` 用 `RTLD_GLOBAL` 解决；若仍出现，试 `LD_PRELOAD=$(ldconfig -p \| grep libglog \| awk '{print $4}' \| head -1) python3 main.py`，或干脆不加 `--glog` 重编                                                                                                                                          |
| `图片渲染失败 (markdown2image 调用未生成文件)` 或 `markdown2image 二进制缺失`         | 本插件在 `import` 时会切到 `build/` 目录让 LGTBot 找到 `markdown2image`。若仍报错：① 检查 `plugins/LGTBot_ElainaBot/build/markdown2image` 是否存在并可执行（`chmod +x`）；② 手动测试 `cd build && echo '# hi' \| ./markdown2image --output /tmp/x.png --width 400 --nowith_css --noprint_info`；③ 部分游戏依赖字体，需 `apt install fonts-noto-cjk`。不影响游戏核心运行，仅影响图片输出                             |
| `LGTBot 引擎启动失败`                                                    | 查 `build/plugins/` 下是否有各 `libgame.so`；首次编译需要等待所有 game 子项编译完成                                                                                                                                                                                                                                                                                       |
| 消息发不出去 / 无响应                                                       | 检查主框架日志中 sender 是否成功初始化；QQ Bot `appid/secret` 是否正确                                                                                                                                                                                                                                                                                                 |
| 段错误 / `Segmentation fault (core dumped)`                           | 通常是 ASAN 编译产物未通过 LD_PRELOAD 启动 —— `bash build.sh --clean` 重编（默认 ASAN OFF）                                                                                                                                                                                                                                                                          |
| 排行榜 / 战绩里离线用户显示成 `<XXXX…YYYY>` 这种截断 openid                         | 该用户从未在本插件运行期间发过消息，`data/user_cache.db` 没有他的昵称记录。等其下次发言即自动补齐。`rm data/user_cache.db` 等于清空全部昵称缓存，下次有用户发消息时表会自动重建（无副作用）                                                                                                                                                                                                                               |
