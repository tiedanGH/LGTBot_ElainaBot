# LGTBot × ElainaBot 详细说明

> 各项特性的完整说明、QQ 协议相关限制、工作原理与文件结构。项目简介与快速开始见 [README.md](./README.md)，部署步骤见 [DEPLOY.md](./DEPLOY.md)。

---

## 关键特性

### 🚀 部署与加载

- **零配置加载** —— 作为 ElainaBot 插件，路径全部自包含在 `plugins/LGTBot_ElainaBot/`
- **预编译部署** —— **无需本地 Boost.Python / C++20 工具链**即可上线
  - CI 为 Ubuntu 22.04 / 24.04、Debian 12 预编译引擎 + 全部游戏，自检脱离编译树也能加载
- **市场安装兼容** —— 仪表盘可初始化 git 仓库
- **游戏开关** —— 引擎全量编译，面板「配置管理」里按游戏停用。默认停用 9 个风险游戏，在面板内可恢复

### 💬 消息与交互

- **消息渲染与图床** —— C++ 端聚合「@玩家 文本 + 图片」并按引擎原始消息排版
- **按钮与菜单交互** —— 单独 @ 机器人回复模板欢迎菜单（面板可配置的快捷游戏）
  - `/新游戏` `/加入` 等自动附交互按钮，游戏结束附快捷重开按钮
- **数据统计指令** —— 统计卡片图，按日、月、年周期统计涨跌、主动消息额度与今日总计、10 日趋势、好友 / 群聊总数
  - **游戏统计**：`数据统计 游戏名` 查单个游戏的累计与近 7 日局数 / 玩家、热度排名、12 周趋势、局数与实力排行
- **玩家头衔** —— 持有头衔的玩家在新建 / 加入房间后播报头衔提示
- **更新公告** —— 按日期行自动分段，默认只发最新一段
  - 「历史更新公告」通过日期区间列表汇总，可查看指定时间段的更新公告与重要更新
- **紧急公告** —— 面板「配置管理」里可编辑公告文案，实时读盘
  - **紧急通知**：启用期间，群聊**首次**新建游戏时额外收到紧急公告，之后该群不再重复
- **全量群适配** —— 监听 `GROUP_MESSAGE_CREATE`（仍强制 `is_at_self` 检查）
  - 日常聊天不进引擎，但消息 ID 会登记进被动引用池：对局刷屏时先用这些被动额度，少发送主动消息
- **两种群权限分开判定** —— **全量消息**（群内全部消息）与**主动推送**（可发主动消息）由群主分别开通
  - 决定 刷新按钮、消息回复限制 的**只有主动推送权限**：没开全量但开了主动的群全程无需刷新按钮；只开全量却没开主动的群仍按被动配额走
  - 私信侧暂无对应权限位，可在插件配置「私信直推用户」里手动指定（`all` = 默认全员有主动私信权限）
  - **授权后秒级生效**：收到全量消息或判定为「无权限」时，主动拉一次 `bot_state` 刷新（每群 60s 节流），拉完立即失效缓存
- **主动消息额度守护** —— 按 QQ 官方接口限制统计**每群 / 每用户**每日主动消息量（上限可配，默认 1000）
  - 撞上每分钟频控（`40034100`）不丢消息：退避后重发，等待中有新消息 ID 就改走被动回复

### 🛡️ 对局与数据安全

- **对局安全保护** —— 「计划重启」维护模式，保证重启不吃掉进行中的对局
  - 重启 / 退出时进行中对局拒绝释放引擎；状态跨热重载保持、真重启后自动恢复
- **对局跟踪** —— 仪表盘实时列出所有对局（群 / 私聊、游戏名、已进行时长）
- **重启前通知等待中的房间** —— 已建房但**未开局**的群会在重启前收到一条简要提示（房间将被清空）
- **群管中断授权** —— 群主 / 群管理员可用 `%中断` 强制中断本群卡死的对局
  - **且仅有这一条管理能力** —— 群管无法执行其他 `%` 管理指令（清除战绩 / 荣誉等）
- **数据库备份** —— 每日自动 + 手动按钮 + 一键恢复 + 下载到本地 + 自动轮转保留 7 份
  - zip 存主框架 `data/backup/lgtbot/`；SQLite 在线备份用 `Connection.backup()` API，引擎运行中也安全

### 🖥️ Web 面板与运维

- **Web 面板拓展页** —— 侧边栏「LGTBot 机器人」单页多标签
  - 仪表盘 / 指标面板 / 配置管理 / 预编译部署 / 引擎编译 / 头衔系统 / 数据备份 / 消息日志 / 操作审计 / 昵称审核 / 崩溃转储 / 用户数据
  - 含一键更新、缓存清理、运行环境自检、在线编辑配置（可视化）与整进程重启
  - 明暗主题：**自动**（默认跟随主框架面板的夜间模式）/ 浅色 / 深色
- **运维观测** —— 指标 / 审计 / 崩溃三条观测线
  - **指标面板**：数据统计、图床成功率、配额压力与异常发送失败、发送失败总计、重启累计、@全体次数、游戏数据、主动消息、排行榜、10 日趋势
  - **操作审计**：编译 / 备份 / 清缓存 / 头衔 / 更新 / 配置 / 重启 / 换绑等状态变更
  - **昵称审核**：连接「AI LLM 服务」判定用户昵称是否违规，违规昵称统一显示为匿名（默认关闭，接口与模型在该标签页选择）
  - **崩溃转储**：查看引擎 SIGSEGV / SIGBUS / SIGABRT 的 backtrace dump；另列**游戏子进程 core 文件**（游戏崩溃不影响主进程），读 ELF note 解析出信号 / 出错地址 / 是哪个游戏 / 崩溃模块
- **机器人绑定** —— 多 bot 部署时在仪表盘可视化选择本插件服务的机器人，其他 bot 的事件静默忽略
- **自动化 API** —— 面向框架内其他插件的 HTTP 接口，独立 token 认证（面板一键复制）
  - 编译：`POST /api/ext/lgtbot/build/compile`（`{"target": "<名称>", "new": bool}`，同步等待并返回用时）与 `.../build/terminate`（强制中断）；完整增量编译不开放
  - 状态：`GET .../build/status` —— 只读探测编译服务此刻是否可用
  - 重启：`POST .../restart`（立即重启，可选 `{"reason": "更新内容"}` 随重启提示发给仍有等待中房间的群）与 `POST .../planned-restart`（`{"enable", "auto", "reason"}` —— 开关计划重启维护模式，`auto: true` 时全部对局结束后自动重启）
  - @全体成员：`POST .../at-all`（`{"group_openid", "content", "send_at"}`）—— 机器人须为群管理员，每群每天 1 次，计入主动消息额度；`send_at` 为预留的定时发送参数，暂不生效

## QQ 协议相关限制（已知）

QQ 官方机器人协议层面的限制，**所有 QQ Bot 都会遇到**，与 LGTBot 无关：

| 限制                                 | 影响                           | 当前应对                                                                                                      |
|------------------------------------|------------------------------|-----------------------------------------------------------------------------------------------------------|
| 被动消息需 `msg_id` / `event_id` 引用     | 倒计时类被动推送可能失败                 | 按场景缓存消息 ID（群 5 分钟 / 私信 60 分钟），多条各自计次、先用快过期的；即将耗尽时挂「刷新会话」按钮续额度。**开了主动权限**的群可绕过被动配额走真主动消息，私信侧官方已默认允许向好友主动推送 |
| 主动频控：单群/好友 20 条/分，bot 总量 60 条/分    | 超频返回 `40034100`，消息被拒         | 能被动回复就不走主动；被拒后按 5 / 10 / 15 / 15 / 20 秒退避重发（上浮 0~20%），等待中来了新引用立即改走被动；累计约 65 秒仍被拒才丢弃                       |
| @ 旧写法即将弃用；@全体成员要求机器人是群管理员，每群每天 1 次 | 旧写法失效后 @ 不再生效；@全体受管理员身份与次数限制 | 群聊 @ 成员默认沿用旧写法，开启 `new_at_format` 后换成新标签（主框架适配新版接口前保持关闭）；@全体只能经 API 发出，发送前校验管理员身份、当日次数与主动消息额度             |
| 媒体消息不解析 @、没有按钮、只能带一个媒体             | @ 无效、不能带按钮、无法自定义排版           | 转为可读的 `@昵称`（不真正 @）；需启用图床模块发送 Markdown 图文混合消息（支持原生 @ 和按钮）                                                  |
| Linux only（Boost.Python + C++20）   | Windows 编译复杂度极高              | 当前仅支持在 Linux/WSL 上构建                                                                                      |

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

## 文件结构

```
plugins/LGTBot_ElainaBot/
├── main.py                  ElainaBot 插件入口（元数据 + 生命周期）
├── LGTBot_ElainaBot.cc      C++ ↔ Python 桥接层（Boost.Python 模块）
├── CMakeLists.txt           构建配置（自动探测 Python / Boost.Python 版本）
├── build.sh                 一键编译脚本（依赖自检 + 多种编译选项）
├── CLAUDE.md                AI 协作约定
├── DEPLOY.md                部署指南
├── DETAILS.md               详细说明（本文档）
├── README.md                项目介绍
├── LICENSE                  LGPLv2 许可证
│
├── mod/                     插件功能模块（按功能分子包）
│   ├── __init__.py
│   ├── boot.py              C++ 扩展加载（chdir + lib*.so 预加载 + RTLD_GLOBAL）
│   ├── state.py             共享运行时状态容器（含跨重载持久化）
│   ├── config.py            data/config.yaml 读写
│   ├── helpers.py           通用工具（sender / mention / target_key / 主动推送权限）
│   ├── callbacks.py         C++ 引擎回调（cb_* 入口 + 异步发送实现）
│   ├── dispatcher.py        @handler 注册（消息派发 + INTERACTION 处理）
│   ├── game_switch.py       游戏开关：停用名单 + 引擎启动前拼出只含启用游戏的视图目录 + 开局拦截
│   ├── messaging/           消息发送链路
│   │   ├── quota.py         被动消息引用池（每条引用按场景：群 5 次/5 分钟，私信 4 次/60 分钟）
│   │   ├── buttons.py       按钮模板 + 命令触发正则
│   │   ├── uploader.py      图床上传调度 + 图片尺寸解析
│   │   ├── log_attribution.py  类级 monkey-patch，把本插件 push 的消息在 Web 面板正确归类
│   │   └── at_all.py        @全体成员：当日次数 / 主动消息额度 / 管理员身份校验 + 发送 + 记账
│   ├── users/               用户数据
│   │   ├── userinfo.py      主框架用户数据只读门面（昵称缓存 + 变化时写回最新昵称）
│   │   ├── nickname_review.py  昵称 AI 审核：结论库（SQLite，按昵称文本永久缓存）+ 送审队列 + 批量扫描
│   │   └── titles.py        玩家头衔：头衔库 + 当前装备 + 头衔等级的读写校验，KaTeX 上色
│   ├── stats/               统计
│   │   ├── metrics.py       运行指标计数（上传 / 崩溃 / 配额 / @全体）+ lgtbot.db 统计查询 + 不计分对局账本
│   │   └── stats_image.py   「数据统计」指令的统计卡片渲染（PIL，缺字体自动回退文本）
│   ├── announce/            公告
│   │   ├── notice.py        更新公告分段：按日期行切分，最新一段 + 更早的日期区间列表与查询
│   │   └── urgent.py        紧急公告：文案 + 总开关 / 已通知群记录（落盘，重启仍生效）
│   ├── ops/                 运维
│   │   ├── audit.py         状态变更操作审计（持久化最近 30 天）
│   │   ├── backup.py        数据备份（创建 / 恢复 / 删除 / 轮转 + 启动自动检查）
│   │   ├── prebuilt.py      预编译包下载 / 校验 / 原子安装 / 本地·预编译切换 + 依赖自检
│   │   ├── _prebuilt_swap.py  build_prebuilt 暂存换入：目录被占用时暂存 pending，boot 重启换入
│   │   └── corefile.py      游戏子进程 core 文件的 ELF note 解析（信号 / 游戏名 / 崩溃模块）
│   └── webui/               Web 面板拓展页（侧边栏「LGTBot 机器人」/ 多标签）
│       ├── __init__.py
│       ├── main.py          入口：页面注册 + 主页面拼装（读 templates/ 并填充占位）+ 隐藏 action 端点路由
│       ├── api/             对外 HTTP API（供其他插件调用，共用一枚 token）
│       │   ├── build.py     编译 API：token 认证 + 单目标同步编译 / 强制中断 / 状态探测
│       │   ├── restart.py   重启 / 计划重启 API：立即重启 + enable/auto/reason 维护模式开关
│       │   └── at_all.py    @全体成员 API：群 openid + 消息内容（send_at 预留）+ 统一错误码
│       ├── pages/           各标签页
│       │   ├── dashboard.py 「仪表盘」标签：版本 / 机器人绑定 / 运行环境自检 / 进行中对局 / 缓存清理 + 检查更新 / git pull / 子模块 update
│       │   ├── metrics.py   「指标面板」标签：数据统计 + 运行指标 + 游戏数据
│       │   ├── config.py    「配置管理」标签：插件和引擎全部配置的内置编辑器 + 热重载
│       │   ├── build.py     「引擎编译」标签：子进程 + state.json + build.log + ANSI 解析成结构化段 + 编译动作
│       │   ├── prebuilt.py  「预编译部署」标签：镜像测速·选择 / 包列表 / 下载进度 / 本地·预编译切换
│       │   ├── backup.py    「数据备份」标签：列出 / 创建 / 恢复 / 下载 / 删除备份 zip
│       │   ├── logs.py      「消息日志」标签 + 日志缓冲数据层
│       │   ├── audit.py     「操作审计」标签：只读展示审计记录
│       │   ├── review.py    「昵称审核」标签：总开关 / 存量批量扫描 / 违规记录与白名单
│       │   ├── crash.py     「崩溃转储」标签：崩溃重启概况 + dump 列表 + 游戏 core 列表
│       │   ├── users.py     「用户数据」标签：读主框架数据库（昵称 / 消息数 / 最后活跃日）
│       │   └── titles.py    「头衔系统」标签：头衔等级新增 / 编辑，全部玩家的头衔库与当前装备
│       └── templates/       前端模板（纯 HTML / CSS / JS，按功能分子目录）
│           ├── main/        主骨架 / 全局 + 通用 CSS / 公共 JS / 图标 SVG
│           ├── dashboard/   「仪表盘」标签
│           ├── metrics/     「指标面板」标签
│           ├── config/      「配置管理」标签
│           ├── build/       「引擎编译」标签
│           ├── prebuilt/    「预编译部署」标签
│           ├── backup/      「数据备份」标签
│           ├── logs/        「消息日志」标签
│           ├── audit/       「操作审计」标签
│           ├── review/      「昵称审核」标签
│           ├── crash/       「崩溃转储」标签
│           ├── users/       「用户数据」标签
│           └── titles/      「头衔系统」标签
│
├── _images/                 仓库内置静态资源
│   ├── logo_transparent_colorful.png   欢迎菜单顶部 logo
│   └── lgtbot_icon.png      站标 64×64，Web 面板顶栏内联成 data URI
│
├── tools/
│   ├── pack_prebuilt.sh     CI 打包脚本：收运行时子集 + 编译树里链接的共享库 + manifest.json → dist/*.zip
│   ├── check_prebuilt.py    CI 打包后自检：按面板方式解压，查依赖都在包里 + 照 boot 方式导入桥接层，不过不发布
│   └── trim_prebuilt_release.py  CI 发布前清理：滚动 release 仅保留最新 + 上一批两个版本
│
├── .github/workflows/       GitHub Actions CI
│   ├── cmake.yml            Ubuntu 编译 + ctest + 桥接冒烟
│   ├── pytest.yml           Python 单元测试
│   ├── prebuilt.yml         三发行版矩阵预编译 → 打包 → 自检 → 发滚动 prebuilt release
│   └── update-lgtbot.yml    每周自动 bump lgtbot 子模块（变更则触发 prebuilt）
│
├── lgtbot/                  ⬇ git submodule（LGTBot 上游源码）
│
├── build/                   ⚙️ CMake 本地编译产物（运行时不可删）
│   ├── libbot_core.so       引擎核心库（运行时由 boot.py 用 ctypes 预加载）
│   ├── markdown2image       游戏图片渲染器
│   ├── plugins/<game>/libgame.so   各游戏插件
│   └── plugins_enabled/     游戏开关的视图目录：有停用游戏时每次引擎启动前重建（预编译包 build_prebuilt/build/）
│
├── build_prebuilt/          📦 下载的预编译包解压目录（与 build/ 并存）
│   ├── LGTBot_ElainaBot.so  预编译的桥接层（预编译模式下 boot 从此 import）
│   ├── build/               编译产物（同顶层 build/ 布局：libbot_core.so / runner / plugins/…）
│   └── manifest.json        包元数据（平台 / Python / sha，用于对比更新）
│
└── data/                    🗂 运行时数据（自动创建）
    ├── config.yaml          插件配置（Web 面板支持可视化编辑）
    ├── urgent_notice.json   紧急公告：启用状态 + 已通知群记录（面板按钮维护）
    ├── games_disabled.json  游戏开关：停用的游戏（面板「配置管理」维护）
    ├── match_runner_cwd.sh  自动生成：游戏子进程启动 wrapper（保证赛况图能渲染）
    ├── user_cache.db        *旧版昵称缓存遗留文件（已完全停用，可删除）*
    ├── build/               引擎编译状态 + 日志（面板「引擎编译」）
    │   ├── state.json       当前 / 上次编译的 PID + 命令 + 时间
    │   ├── build.log        子进程 stdout/stderr（含 ANSI 颜色码）
    │   └── build_target_input.json  自定义目标名临时参数（前端 POST 写入）
    ├── audit/               操作审计（面板「操作审计」）
    │   └── audit.json       最近 30 天的状态变更记录
    ├── metrics/             运行指标（面板「指标面板」）
    │   ├── metrics.json     上传 / 崩溃 / 配额 / 频控计数器
    │   └── unranked.json    不计分对局的临时账本（今天与昨天）
    ├── review/              昵称审核（面板「昵称审核」）
    │   ├── settings.json    总开关 + 接口 / 模型选择 + 批量条数（面板维护）
    │   └── nickname.db      SQLite：昵称文本 → 审核结论（永久缓存）
    ├── titles/              玩家头衔（面板「头衔系统」）
    │   ├── titles.json      头衔库与当前装备
    │   └── levels.json      头衔等级：名称 + HEX 颜色
    ├── prebuilt/            预编译部署状态（面板「预编译部署」）
    │   ├── active           构建来源开关：build | build_prebuilt（boot 启动时读）
    │   └── state.json       下载进度（前端轮询）
    └── engine/              引擎内部数据
        ├── lgtbot.json      LGTBot 引擎全局选项（首次启动写入空 JSON）
        ├── lgtbot.db        SQLite（用户 / 对局 / 排行榜）
        └── images/          引擎临时渲染图片（可清理）
```
