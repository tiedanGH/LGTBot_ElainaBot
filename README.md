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

| 能力              | 实现                                                                                                                  |
|-----------------|---------------------------------------------------------------------------------------------------------------------|
| **零配置自动加载**     | 作为 ElainaBot 插件，路径全部自包含在 `plugins/LGTBot_ElainaBot/`                                                                |
| **机器人绑定**       | 多 bot 部署时在仪表盘可视化选择本插件服务的机器人（默认第一个）；消息收发、全量群数据、邀请链接、错误推送均固定走绑定 bot，其他 bot 的事件静默忽略                                    |
| **消息合并**        | C++ 端聚合 "@玩家 文本 + 图片" 到单条媒体消息（避免 QQ 端拆成两条）                                                                          |
| **markdown 图床** | `config.yaml` 指定单个图床上传到 image_hosting，用 markdown 内嵌，保留 `<@>` 原生 mention 和按钮；留空 / 上传失败回退 msg_type=7                  |
| **并发安全上传**      | 上传时 filename 按内容 sha1 唯一化，避免多群同发同尺寸图片时出现错配；同份图片短时间内复用 URL，并发请求共享 in-flight Future                                   |
| **玩家头像**        | 利用 `q.qlogo.cn/qqapp/{appid}/{openid}` 直链，LGTBot 渲染头像无需额外接口                                                         |
| **回调按钮**        | `/新游戏` `/加入` 等命令自动附加交互按钮；游戏结束时附重开快捷按钮；非刷新 callback 按钮的 data 被当作用户消息派发回 LGTBot 引擎                                    |
| **欢迎菜单**        | 单独 @机器人时回复模板菜单，含「帮助 / 游戏列表 / 排行大图 / 战绩」等按钮；游戏快捷开局列表通过 `config.yaml` 字段可配置                                           |
| **全量群适配**       | 监听 `GROUP_MESSAGE_CREATE`（仍强制 `is_at_self` 检查）；主框架的全量群里不再追加刷新按钮，被动配额耗尽直接走主动消息                                       |
| **菜单 logo**     | 仓库自带图片作为欢迎菜单顶部图（依赖图床上传，URL 进程内缓存 23h）                                                                               |
| **昵称持久化**       | 将 username + 头像 URL 落盘 `data/user_cache.db`（SQLite + WAL，5 min 批量 flush），离线用户在排行榜里仍能正确显示昵称                          |
| **Web 面板拓展页**   | 侧边栏「LGTBot 机器人」单页多标签：仪表盘（版本/统计/引擎配置/缓存）/ 引擎编译（子进程 + 实时日志）/ 消息日志（收发/群私过滤 + 自动刷新）/ 用户数据（信息表）+ 「重启」一键整进程重载             |
| **一键更新 / 缓存清理** | 仪表盘内置 tag + 子模块 commit 双重对比；桥接层与子模块分开更新；启动时自动检查新版本；检查更新同时拉取最新 Release 说明；缓存可视化尺寸 + 二次确认清理（赛况 / 头像 / 图片均支持「保留 7 天」）  |
| **市场安装兼容**      | 插件市场 zip 不含 `.git/`；仪表盘可初始化git仓库来修复;子模块 init 内置 SSH→HTTPS 改写,不需要 SSH key 也能拉 lgtbot 嵌套子模块                           |
| **数据库备份**       | 每日自动备份 + 手动按钮 + 一键恢复 + 自动轮转保留 7 份。备份 zip 存主框架 `data/backup/lgtbot/`;SQLite 在线备份用 `Connection.backup()` API,引擎运行中也安全 |
| **引擎编译面板**      | 完整 / 增量 / 桥接层 / 自定义目标 / 列出目标 / 清理重编 / 删 build 共 7 个按钮；子进程独立 session 跑，关闭浏览器不影响；ANSI 彩色输出 + 中途可终止                    |
| **在线配置**        | `data/config.yaml` 在 Web 面板「插件 → 配置」可直接编辑保存                                                                         |
| **优雅退出**        | 进行中对局拒绝释放引擎，避免数据丢失                                                                                                  |

## 开发计划（TODO）

规划中的改进方向，欢迎在 [QQ 群](https://qun.qq.com/universal-share/share?ac=1&authKey=GLoA6W7KujPW%2B%2B%2FeirVZVVEn61q%2FAmLFyd9mkJ8u%2Bv0E%2B2IooquHavHi9iaJSxKK&busi_data=eyJncm91cENvZGUiOiIxMDU5ODM0MDI0IiwidG9rZW4iOiJsTUFlUHZsdVJpSUhTc2dLSTBoeDI2M0IxS09kTGg3NzFsd1dvaVVLajVqTTIvRm9zaGlMTHBrekRIOGdVZHlaIiwidWluIjoiMjI5NTgyNDkyNyJ9&data=IMqVKIvDehyMv2ooaqlgzql0-Q9XENN4pK6qGR1mqYoZH5AFDBMmrflWNEFN-EOLeKuJTxLABAwgaaUnUp-iyw&svctype=4&tempid=h5_group_info) 或 issue 中讨论：

- [ ] **复用主框架用户数据** —— 去掉插件私有的 `data/user_cache.db`，昵称 / 头像直接从 ElainaBot 主框架的用户数据库读取，消除一份冗余缓存及其同步开销
- [ ] **运行时指标面板** —— Web 面板新增标签，展示进程内存 / CPU、活跃对局数、消息吞吐、引擎崩溃重启次数等运行时指标
- [ ] **配置保存前校验** —— Web 面板在线编辑 `config.yaml` / `lgtbot.json` 时先解析 + 字段 schema 校验再落盘，避免一个格式错误让 bot 崩溃或引擎启动失败
- [ ] **破坏性操作历史日志** —— 恢复 / 删除 / 清缓存 / 编译等危险端点的「何时 / 做了什么」写入持久化文件并在面板可查，形成可追溯的审计流
- [ ] **活跃对局的重启 / 崩溃保护** —— 跟踪有进行中对局的群 / 用户，强制重启或主核崩溃恢复后主动推送「对局已中断，请重新开始」；并探索经子进程 runner 对局面快照 / 恢复
- [ ] **CI 预编译产物** —— GitHub Actions 编译引擎核心 + 50+ 游戏插件并发布预编译包，用户无需本地搭 Boost.Python / C++20 工具链即可下载部署
- [ ] **CI 真桥接冒烟测试** —— 在 CI 里 import 真编译出的 `.so`，用临时 db 启动引擎、发送指令断言响应、再走 restart-release 路径，覆盖 mock 单测照不到的 Boost.Python ABI 与 `g_bot_core` 生命周期回归

## QQ 协议相关限制（已知）

QQ 官方机器人协议层面的限制，**所有 QQ Bot 都会遇到**，与 LGTBot 无关：

| 限制                                          | 影响                     | 当前应对                                                                              |
|---------------------------------------------|------------------------|-----------------------------------------------------------------------------------|
| 主动消息需 `msg_id` / `event_id` 引用              | 倒计时类被动推送可能失败           | 5 分钟事件上下文缓存；全量群内可绕过被动配额走真主动消息(详见上「全量群适配」)                                         |
| Markdown 图片 URL 必须 QQ 开放平台报备的域名             | 直发本地图片无法内嵌 markdown    | 启用主框架 image_hosting，在本插件 `config.yaml` 选定单个图床 → 上传后内嵌 URL；未配置 / 失败回退 `msg_type=7` |
| 媒体消息（`msg_type=7`）的 content 不解析 `<@openid>` | 图文同条消息里的 @ 既不高亮也不 ping | 自动转为可读的 `@昵称`（牺牲 ping 换图文同条 + 文字可读）                                               |
| 媒体消息无法附加按钮（QQ 协议）                           | 图片消息不能带按钮              | 仅文本回复附按钮                                                                          |
| Linux only（Boost.Python + C++20）            | Windows 编译复杂度极高        | 仅在 Linux/WSL 上构建                                                                  |

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
│   ├── log_attribution.py   类级 monkey-patch ，把本插件 push 的消息在 Web 面板正确归类
│   └── webui/               Web 面板拓展页（侧边栏「LGTBot 机器人」/ 多标签）
│       ├── __init__.py
│       ├── main.py          入口：页面注册 + 主页面拼装（读 templates/ 并填充占位）+ 隐藏 action 端点路由
│       ├── page_dashboard.py「仪表盘」标签：版本 / 统计 / 缓存清理 + 检查更新 / git pull / 子模块 update / 缓存清理
│       ├── page_config.py   「配置管理」标签：插件和引擎全部配置的内置编辑器 + 热重载
│       ├── page_build.py    「引擎编译」标签：子进程 + state.json + build.log + ANSI 转 HTML + 编译动作
│       ├── page_logs.py     「消息日志」标签 + 日志缓冲数据层（log_incoming / log_outgoing / get_logs / clear_logs）
│       ├── page_users.py    「用户数据」标签：查 user_cache.db + 模板加载
│       └── templates/       前端模板（纯 HTML / CSS / JS，按功能分子目录）
│           ├── main/        主骨架 / 全局 + 通用 CSS / 公共 JS
│           ├── dashboard/   「仪表盘」标签 HTML / CSS / JS
│           ├── config/      「配置管理」标签 HTML / CSS / JS
│           ├── build/       「引擎编译」标签 HTML / CSS / JS
│           ├── logs/        「消息日志」标签 HTML / CSS / JS
│           └── users/       「用户数据」标签 HTML / CSS / JS
│
├── _images/                 仓库内置静态资源
│   └── logo_transparent_colorful.png   欢迎菜单顶部 logo
│
├── .github/workflows/cmake.yml   GitHub Actions CI（Ubuntu 编译 + ctest）
│
├── lgtbot/                  ⬇ git submodule（LGTBot 上游源码）
│
├── build/                   ⚙️ CMake 编译产物（运行时不可删）
│   ├── libbot_core.so       引擎核心库（运行时由 boot.py 用 ctypes 预加载）
│   ├── markdown2image       游戏图片渲染器
│   └── plugins/<game>/libgame.so   各游戏插件
│
└── data/                    🗂 运行时数据（自动创建）
    ├── config.yaml          插件配置（Web UI 可在线编辑）
    ├── user_cache.db        用户昵称 / 头像缓存（删除可自动重建，无副作用）
    ├── build/               引擎编译状态 + 日志（WebUI「引擎编译」标签使用）
    │   ├── state.json       当前 / 上次编译的 PID + 命令 + 时间
    │   ├── build.log        子进程 stdout/stderr（含 ANSI 颜色码）
    │   └── build_target_input.json  自定义目标名临时参数（前端 POST 写入）
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
