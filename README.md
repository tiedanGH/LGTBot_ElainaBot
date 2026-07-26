<div align="center">

![Logo](https://github.com/Slontia/lgtbot/blob/master/images/logo_transparent_colorful.svg)

# LGTBot × ElainaBot

**QQ 官方机器人版 LGTBot 适配插件**

![lang](https://img.shields.io/badge/language-Python%20%2F%20C%2B%2B20-green.svg)
![platform](https://img.shields.io/badge/platform-QQ%20Official%20Bot-blue.svg)
![license](https://img.shields.io/badge/license-LGPLv2-orange.svg)
[![CMake](https://github.com/tiedanGH/LGTBot_ElainaBot/actions/workflows/cmake.yml/badge.svg)](https://github.com/tiedanGH/LGTBot_ElainaBot/actions/workflows/cmake.yml)
[![pytest](https://github.com/tiedanGH/LGTBot_ElainaBot/actions/workflows/pytest.yml/badge.svg)](https://github.com/tiedanGH/LGTBot_ElainaBot/actions/workflows/pytest.yml)
[![QQ Group](https://img.shields.io/badge/QQ%20Group-1059834024-0085F0.svg)](https://qun.qq.com/universal-share/share?ac=1&authKey=GLoA6W7KujPW%2B%2B%2FeirVZVVEn61q%2FAmLFyd9mkJ8u%2Bv0E%2B2IooquHavHi9iaJSxKK&busi_data=eyJncm91cENvZGUiOiIxMDU5ODM0MDI0IiwidG9rZW4iOiJsTUFlUHZsdVJpSUhTc2dLSTBoeDI2M0IxS09kTGg3NzFsd1dvaVVLajVqTTIvRm9zaGlMTHBrekRIOGdVZHlaIiwidWluIjoiMjI5NTgyNDkyNyJ9&data=IMqVKIvDehyMv2ooaqlgzql0-Q9XENN4pK6qGR1mqYoZH5AFDBMmrflWNEFN-EOLeKuJTxLABAwgaaUnUp-iyw&svctype=4&tempid=h5_group_info)

</div>

---

## 致谢与项目来源

本插件是一个**适配层 / 集成包**，核心游戏引擎完全来自上游项目：

> **[LGTBot](https://github.com/Slontia/lgtbot)** — © [@Slontia](https://github.com/Slontia)
>
> *「LGT」源自日本漫画家甲斐谷忍《Liar Game》中的虚构组织「**L**iar **G**ame **T**ournament 事务所」*
>
> 一个基于 C++20 的多人文字推理游戏裁判机器人库，包含 50+ 种不同风格的游戏。游戏逻辑、引擎核心、图片渲染均由原作者 Slontia 设计实现。

本插件并未对 LGTBot 引擎做任何功能性修改，仅做：
1. 把 [LGTBot](https://github.com/Slontia/lgtbot) 适配 [ElainaBot_v2](https://github.com/ElainaCore/ElainaBot_v2) QQ 官方机器人框架
2. 处理 QQ 协议特有的限制（媒体消息合并、@mention 格式、按钮交互等）

**所有荣誉归原作者所有 —— 强烈建议先去 [LGTBot 主仓库](https://github.com/Slontia/lgtbot) 给原项目点 Star。**

| 上游项目                                                                  | 作者                                     | 协议     |
|-----------------------------------------------------------------------|----------------------------------------|--------|
| [LGTBot 引擎](https://github.com/Slontia/lgtbot)                        | [@Slontia](https://github.com/Slontia) | LGPLv2 |
| [lgtbot-khl](https://github.com/Slontia/lgtbot-khl) (KOOK 适配，本项目参考实现) | [@Slontia](https://github.com/Slontia) | LGPLv2 |
| [ElainaBot_v2 框架](https://github.com/ElainaCore/ElainaBot_v2)         | [@冷曦](https://github.com/lengxi-root)  | MIT    |
| 本适配层                                                                  | 铁蛋                                     | LGPLv2 |

---

## 简介

把 LGTBot 的 50+ 种游戏通过 ElainaBot 主框架接入到 **QQ 官方机器人**。

**作为 ElainaBot 插件零配置启动**：编译项目 → 启动主框架 → 自动加载 → 在群里 @ 机器人即可游玩。

## 工作原理

```
┌─────────────────────┐    @handler               ┌──────────────────────────────┐
│ ElainaBot 主框架    │ ─────────────────────►    │ plugins/LGTBot_ElainaBot/    │
│  (QQ Webhook / WS)  │                           │  main.py                     │
│  MessageSender      │ ◄──── send_to_xxx ─────── │   ↓ Boost.Python             │
└─────────────────────┘     run_coroutine_        │  LGTBot_ElainaBot.so         │
                            threadsafe            │   ↓ FFI                      │
                                                  │  libbot_core (C++)           │
                                                  │  + 50+ games                 │
                                                  └──────────────────────────────┘
```

## 快速开始

详见 [DEPLOY.md](./DEPLOY.md)，三步：

```bash
# 1. 准备 lgtbot 子模块
cd plugins/LGTBot_ElainaBot
git clone --recursive https://github.com/Slontia/lgtbot.git lgtbot

# 2. 一键编译
bash build.sh

# 3. 启动主框架
cd ../.. && python3 main.py
```

## 关键特性

| 能力            | 实现                                                                                                                                                                          |
|---------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **零配置自动加载**   | 作为 ElainaBot 插件，路径全部自包含在 `plugins/LGTBot_ElainaBot/`；编译后启动主框架即自动加载，在群里 @ 机器人即可游玩                                                                                            |
| **预编译部署**     | CI 为 Ubuntu 22.04 / 24.04、Debian 12 预编译引擎 + 全部游戏并发布到滚动 release；面板内镜像测速 / 选择 + 下载 / 手动上传 + 本地·预编译一键切换，**无需本地 Boost.Python / C++20 工具链**                                      |
| **Web 面板拓展页** | 侧边栏「LGTBot 机器人」单页多标签：仪表盘 / 指标面板 / 配置管理 / 预编译部署 / 引擎编译 / 数据备份 / 消息日志 / 操作审计 / 崩溃转储 / 用户数据；含一键更新（tag + 子模块双重对比）、缓存清理、运行环境自检、在线编辑配置与整进程重启                                      |
| **消息渲染与图床**   | C++ 端聚合「@玩家 文本 + 图片」为单条媒体消息（避免 QQ 端拆成两条）；`config.yaml` 选定图床时用 markdown 内嵌图片，保留 `<@>` 原生 mention 与按钮，留空 / 上传失败回退 `msg_type=7`                                                |
| **按钮与菜单交互**   | 单独 @ 机器人回复模板欢迎菜单（帮助 / 游戏列表 / 排行大图 / 战绩 + `config.yaml` 可配置的快捷开局）；`/新游戏` `/加入` 等自动附交互按钮，游戏结束附「查看战绩 / 重开一局」；非刷新按钮的 data 当作用户消息派发回引擎                                           |
| **全量群适配**     | 监听 `GROUP_MESSAGE_CREATE`（仍强制 `is_at_self` 检查）；全量群内不再追加刷新按钮，被动配额耗尽直接走主动消息                                                                                                   |
| **机器人绑定**     | 多 bot 部署时在仪表盘可视化选择本插件服务的机器人（默认第一个）；消息收发、全量群数据、邀请链接、错误推送均固定走绑定 bot，其他 bot 的事件静默忽略                                                                                            |
| **数据库备份**     | 每日自动 + 手动按钮 + 一键恢复 + 下载到本地 + 自动轮转保留 7 份；zip 存主框架 `data/backup/lgtbot/`，SQLite 在线备份用 `Connection.backup()` API，引擎运行中也安全                                                      |
| **对局安全保护**    | 「计划重启」维护模式暂停创建新游戏而不影响进行中对局与已建房间；重启 / 退出时进行中对局拒绝释放引擎，避免战绩丢失；状态跨热重载保持、真重启后自动恢复                                                                                                |
| **进行中对局跟踪**   | 仪表盘实时列出所有**已开局**的对局（群 / 私聊、游戏名、已进行时长）；引擎崩溃（SIGSEGV）时向这些对局推送中断通知；按引擎「开局 / 结束」事件跟踪、自动去重崩溃源、跨热重载保持，进程重启后清空                                                                     |
| **运维观测**      | **指标面板**（数据统计 / 图床上传成功率 / 崩溃与重启累计 / 配额压力 / 今日主动消息 / 排行榜 / 10 日趋势）+ **操作审计**（编译 / 备份 / 清缓存 / 更新 / 配置 / 重启 / 换绑等状态变更）+ **崩溃转储**（查看引擎 SIGSEGV/SIGBUS/SIGABRT 的 backtrace dump） |
| **市场安装兼容**    | 插件市场 zip 不含 `.git/`；仪表盘可初始化 git 仓库来修复，子模块 init 内置 SSH→HTTPS 改写，不需要 SSH key 也能拉 lgtbot 嵌套子模块                                                                                 |

## 开发计划（TODO）

规划中的改进方向，欢迎在 [QQ 群](https://qun.qq.com/universal-share/share?ac=1&authKey=GLoA6W7KujPW%2B%2B%2FeirVZVVEn61q%2FAmLFyd9mkJ8u%2Bv0E%2B2IooquHavHi9iaJSxKK&busi_data=eyJncm91cENvZGUiOiIxMDU5ODM0MDI0IiwidG9rZW4iOiJsTUFlUHZsdVJpSUhTc2dLSTBoeDI2M0IxS09kTGg3NzFsd1dvaVVLajVqTTIvRm9zaGlMTHBrekRIOGdVZHlaIiwidWluIjoiMjI5NTgyNDkyNyJ9&data=IMqVKIvDehyMv2ooaqlgzql0-Q9XENN4pK6qGR1mqYoZH5AFDBMmrflWNEFN-EOLeKuJTxLABAwgaaUnUp-iyw&svctype=4&tempid=h5_group_info) 或 issue 中讨论：

- [x] **CI 预编译产物** —— GitHub Actions 三发行版矩阵编译引擎核心 + 50+ 游戏插件，发布到滚动 `prebuilt` release；面板「📦 预编译部署」下载切换，用户无需本地搭 Boost.Python / C++20 工具链
- [x] **破坏性操作历史日志** —— 恢复 / 删除 / 清缓存 / 编译等危险端点的「何时 / 做了什么」写入持久化文件并在面板可查，形成可追溯的审计流
- [x] **CI 真桥接冒烟测试** —— 在 CI 里 import 真编译出的 `.so`，用临时 db 启动引擎、发送指令断言响应、再走 restart-release 路径，覆盖 mock 单测照不到的 Boost.Python ABI 与 `g_bot_core` 生命周期回归
- [ ] **复用主框架用户数据** —— 去掉插件私有的 `data/user_cache.db`，昵称 / 头像直接从 ElainaBot 主框架的用户数据库读取，消除一份冗余缓存及其同步开销

## QQ 协议相关限制（已知）

QQ 官方机器人协议层面的限制，**所有 QQ Bot 都会遇到**，与 LGTBot 无关：

| 限制                                                  | 影响                               | 当前应对                                                                                                |
|-----------------------------------------------------|----------------------------------|-----------------------------------------------------------------------------------------------------|
| 主动消息需 `msg_id` / `event_id` 引用                      | 倒计时类被动推送可能失败                     | 5 分钟事件上下文缓存；全量群内可绕过被动配额走真主动消息(详见上「全量群适配」)；私信侧官方已默认允许向好友主动推送，配置 `sandbox_dm_users: ["all"]` 即全员直推不丢弃 |
| Markdown 图片 URL 必须 QQ 开放平台报备的域名                     | 直发本地图片无法内嵌 markdown              | 启用主框架 image_hosting，在本插件 `config.yaml` 选定单个图床 → 上传后内嵌 URL；未配置 / 失败回退 `msg_type=7`                   |
| 媒体消息（`msg_type=7`）的 content 不解析 `<@openid>`，且无法附加按钮 | 同条消息里的 @ 既不高亮也不 ping；图文混合消息不能带按钮 | 自动转为可读的 `@昵称`（牺牲 ping 换图文同条 + 文字可读）；仅文本回复附按钮，或启用图床发送Markdown图文混合消息（支持原生@）                           |
| Linux only（Boost.Python + C++20）                    | Windows 编译复杂度极高                  | 仅在 Linux/WSL 上构建                                                                                    |

## 文件结构

```
plugins/LGTBot_ElainaBot/
├── main.py                  ElainaBot 插件入口（元数据 + 生命周期）
├── LGTBot_ElainaBot.cc      C++ ↔ Python 桥接层（Boost.Python 模块）
├── CMakeLists.txt           构建配置（自动探测 Python / Boost.Python 版本）
├── build.sh                 一键编译脚本（依赖自检 + 多种编译选项）
├── CLAUDE.md                AI 协作约定
├── DEPLOY.md                部署指南
├── README.md                本文档
├── LICENSE                  LGPLv2 许可证
│
├── mod/                     插件功能模块（用 `mod/` 而非 `app/` 是为了让 Web 面板「插件管理」不把内部子模块当 toggle 项暴露,误关会崩）
│   ├── __init__.py
│   ├── state.py             共享运行时状态容器（含跨重载持久化）
│   ├── boot.py              C++ 扩展加载（chdir + lib*.so 预加载 + RTLD_GLOBAL）
│   ├── buttons.py           按钮模板 + 命令触发正则
│   ├── helpers.py           通用工具（sender / coro / mention / target_key）
│   ├── quota.py             被动消息引用配额管理（绕过 5 条限制）
│   ├── callbacks.py         C++ 引擎回调（cb_* 入口 + 异步发送实现）
│   ├── dispatcher.py        @handler 注册（消息派发 + INTERACTION 处理）
│   ├── config.py            data/config.yaml 读写
│   ├── userdb.py            用户昵称 / 头像 SQLite 持久化（5 min 批量 flush）
│   ├── uploader.py          图床上传调度（COS / B站）+ 图片尺寸解析
│   ├── backup.py            数据备份（创建 / 恢复 / 删除 / 轮转 + 启动自动检查）
│   ├── audit.py             状态变更操作审计（持久化最近 500 条,record 永不抛异常）
│   ├── metrics.py           运行指标计数（上传 / 崩溃 / 配额,重启不丢）+ lgtbot.db 统计查询
│   ├── prebuilt.py          预编译包下载 / 校验 / 原子安装 / 本地·预编译切换 + 依赖自检
│   ├── _prebuilt_swap.py    build_prebuilt 暂存换入:目录被运行中引擎占用时暂存 pending,boot 重启换入
│   ├── log_attribution.py   类级 monkey-patch ，把本插件 push 的消息在 Web 面板正确归类
│   └── webui/               Web 面板拓展页（侧边栏「LGTBot 机器人」/ 多标签）
│       ├── __init__.py
│       ├── main.py          入口：页面注册 + 主页面拼装（读 templates/ 并填充占位）+ 隐藏 action 端点路由
│       ├── page_dashboard.py「仪表盘」标签：版本 / 机器人绑定 / 运行环境自检 / 进行中对局 / 缓存清理 + 检查更新 / git pull / 子模块 update
│       ├── page_metrics.py  「指标面板」标签：数据统计 + 运行指标 + 游戏数据（统一刷新）
│       ├── page_config.py   「配置管理」标签：插件和引擎全部配置的内置编辑器 + 热重载
│       ├── page_build.py    「引擎编译」标签：子进程 + state.json + build.log + ANSI 解析成结构化段 + 编译动作
│       ├── page_prebuilt.py 「预编译部署」标签：镜像测速·选择 / 包列表 / 下载进度 / 本地·预编译切换
│       ├── page_backup.py   「数据备份」标签：列出 / 创建 / 恢复 / 删除备份 zip
│       ├── page_logs.py     「消息日志」标签 + 日志缓冲数据层（log_incoming / log_outgoing / get_logs / clear_logs）
│       ├── page_audit.py    「操作审计」标签：只读展示审计记录（唯一 action 是刷新）
│       ├── page_crash.py    「崩溃转储」标签：崩溃重启概况 + 列出（含触发源 uid·gid）/ 大弹窗查看 / 下载 / 删除 dump
│       ├── page_users.py    「用户数据」标签：查 user_cache.db + 模板加载
│       └── templates/       前端模板（纯 HTML / CSS / JS，按功能分子目录）
│           ├── main/        主骨架 / 全局 + 通用 CSS / 公共 JS
│           ├── dashboard/   「仪表盘」标签 HTML / CSS / JS
│           ├── metrics/     「指标面板」标签 HTML / CSS / JS
│           ├── config/      「配置管理」标签 HTML / CSS / JS
│           ├── build/       「引擎编译」标签 HTML / CSS / JS
│           ├── prebuilt/    「预编译部署」标签 HTML / CSS / JS
│           ├── backup/      「数据备份」标签 HTML / CSS / JS
│           ├── logs/        「消息日志」标签 HTML / CSS / JS
│           ├── audit/       「操作审计」标签 HTML / CSS / JS
│           ├── crash/       「崩溃转储」标签 HTML / CSS / JS
│           └── users/       「用户数据」标签 HTML / CSS / JS
│
├── _images/                 仓库内置静态资源
│   └── logo_transparent_colorful.png   欢迎菜单顶部 logo
│
├── tools/
│   ├── pack_prebuilt.sh     CI 打包脚本：收运行时子集 + manifest.json → dist/*.zip
│   └── trim_prebuilt_release.py  CI 发布前清理：滚动 release 仅保留最新 + 上一批两个版本
│
├── .github/workflows/       GitHub Actions CI
│   ├── cmake.yml            Ubuntu 编译 + ctest + 桥接冒烟
│   ├── pytest.yml           Python 单元测试
│   ├── prebuilt.yml         三发行版矩阵预编译 → 打包 → 发滚动 prebuilt release
│   └── update-lgtbot.yml    每周自动 bump lgtbot 子模块（变更则触发 prebuilt）
│
├── lgtbot/                  ⬇ git submodule（LGTBot 上游源码）
│
├── build/                   ⚙️ CMake 本地编译产物（运行时不可删）
│   ├── libbot_core.so       引擎核心库（运行时由 boot.py 用 ctypes 预加载）
│   ├── markdown2image       游戏图片渲染器
│   └── plugins/<game>/libgame.so   各游戏插件
│
├── build_prebuilt/          📦 下载的预编译包解压目录（与 build/ 并存）
│   ├── LGTBot_ElainaBot.so   预编译的桥接层（预编译模式下 boot 从此 import）
│   ├── build/               编译产物（同顶层 build/ 布局：libbot_core.so / runner / plugins/…）
│   └── manifest.json        包元数据（平台 / Python / sha,用于对比更新）
│
└── data/                    🗂 运行时数据（自动创建）
    ├── config.yaml          插件配置（Web UI 可在线编辑）
    ├── user_cache.db        用户昵称 / 头像缓存（删除可自动重建，无副作用）
    ├── build/               引擎编译状态 + 日志（WebUI「引擎编译」标签使用）
    │   ├── state.json       当前 / 上次编译的 PID + 命令 + 时间
    │   ├── build.log        子进程 stdout/stderr（含 ANSI 颜色码）
    │   └── build_target_input.json  自定义目标名临时参数（前端 POST 写入）
    ├── audit/               操作审计（WebUI「操作审计」标签使用）
    │   └── audit.json       最近 500 条状态变更记录（重启不丢,滚动淘汰最旧）
    ├── metrics/             运行指标（WebUI「指标面板」标签使用）
    │   └── metrics.json     上传 / 崩溃 / 配额计数器（重启不丢）
    ├── prebuilt/            预编译部署状态（WebUI「预编译部署」标签使用）
    │   ├── active           构建来源开关：build | build_prebuilt（boot 启动时读）
    │   └── state.json       下载进度（前端轮询）
    └── engine/              引擎内部数据
        ├── lgtbot.json      LGTBot 引擎全局选项（首次启动写入空 JSON）
        ├── lgtbot.db        SQLite（用户 / 对局 / 排行榜）
        └── images/          引擎临时渲染图片（可清理）
```

## 许可证

本适配层与 LGTBot 引擎保持一致，使用 **LGPLv2** 协议。

游戏逻辑、引擎核心、图片渲染等核心实现的著作权归 [@Slontia](https://github.com/Slontia) 所有，请遵守上游项目的 [LICENSE](https://github.com/Slontia/lgtbot/blob/master/LICENSE)。

## 链接

- 🎮 LGTBot 上游仓库：https://github.com/Slontia/lgtbot
- 🟢 KOOK 版（本项目参考实现）：https://github.com/Slontia/lgtbot-khl
- 🤖 ElainaBot_v2 主框架：[https://github.com/ElainaCore/ElainaBot_v2](https://github.com/ElainaCore/ElainaBot_v2)
- 📖 部署指南：[DEPLOY.md](./DEPLOY.md)
