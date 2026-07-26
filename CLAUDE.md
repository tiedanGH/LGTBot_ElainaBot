# Project Conventions for AI Assistants

> 本文件用于约束 Claude 等 AI 助手在本项目（`plugins/LGTBot_ElainaBot`）下的协作行为。
> 任何修改应优先遵守此处约定，与日常对话指令冲突时以本文件为准。

---

## 1. 修改边界

| 范围                                                     | 是否可改                       |
|--------------------------------------------------------|----------------------------|
| `plugins/LGTBot_ElainaBot/` 下所有 Python 文件              | ✅ 可改                       |
| `plugins/LGTBot_ElainaBot/LGTBot_ElainaBot.cc`         | ✅ 可改（这是本插件自己的桥接层）          |
| `plugins/LGTBot_ElainaBot/CMakeLists.txt` / `build.sh` | ✅ 可改                       |
| `plugins/LGTBot_ElainaBot/lgtbot/` （子模块）               | ❌ **不可改** —— 上游 C++ 引擎源码   |
| `core/` / `web/` / `main.py`（项目根）                      | ❌ **不可改** —— ElainaBot 主框架 |

如果用户的需求需要改主框架或 lgtbot 子模块，先**显式向用户说明**并征得同意。

---

## 2. Git 提交规范

### Commit Message
- **小写开头**（`move ...` / `add ...`，不要 `Move ...`）
- **全英文**，标题简短（≤60 字符）
- **不写动作类前缀** `feat:` / `fix:` / `refactor:` / `chore:` 等约定式前缀

### 正文（body）使用规则

| 改动类型                            | 是否写正文                             |
|---------------------------------|-----------------------------------|
| 单文件简单调整（修个 typo、调一行常量、调一个文案）    | **只写标题**，不要正文                     |
| 单文件改动但行为上有非平凡影响（修 bug、加配置项、加日志） | 视情况：行为变化能从 diff 一眼看懂就只写标题；否则简短补一段 |
| 跨多文件、跨多模块、影响多种行为                | **必须写正文**：分点说明改了什么、为什么、有什么连带影响    |
| 复杂的设计权衡 / 历史踩坑总结                | 正文充分展开，给后人留下"为什么这么做"的线索           |

✅ 简单改动只标题：
```
trim welcome menu text to focus on essentials
disable AddressSanitizer in build script
```

✅ 复杂改动有正文：
```
survive hot-reload with active games and improve quota logic

- Detect engine still running on @on_load via persistent attribute on
  the C++ extension module (survives plugin reload). When games are
  in progress, skip LGTBot_ElainaBot.start() ...
- Share mutable state via the same persistent dict ...
- Replace shared asyncio.Event with per-waiter Events ...
```

### 功能域前缀规则

**前缀 = 本次改动的主要功能域,与动了哪些文件无关。** 一个功能横跨多少文件都用
同一个前缀(如配置校验特性同时改了 page_config.py / webui/main.py / config.js /
README,前缀仍是 `config`)。功能域名字常与 `mod/` 模块名重合(quota / backup /
uploader…),但判定依据始终是"这次改的是什么功能",不是"动了哪个文件"。

| 改动主要功能                                     | 前缀                            | 示例                                                            |
|--------------------------------------------|-------------------------------|---------------------------------------------------------------|
| CI / 测试基建（workflows、`tests/`）              | `ci`                          | `ci: add real-bridge smoke test to cmake ci`                  |
| 配置体系（字段 / 校验 / 热重载）                        | `config`                      | `config: validate config.yaml and lgtbot.json before saving`  |
| 消息派发 / 指令处理行为                              | `dispatcher`                  | `dispatcher: stop exclusive commands leaking into catch-alls` |
| 引擎回调 / 发送链路                                | `callbacks`                   | `callbacks: simplify refresh tip for full access group`       |
| Web 面板界面与交互                                | `webui`                       | `webui: lock tabs nav to horizontal-only scrolling on mobile` |
| 其他清晰功能域（配额 / 备份 / 图床 / 启动加载 …）             | `quota` / `backup` / `boot` … | `quota: fix race in shared Event causing dead wait`           |
| 构建脚本 / 编译选项（`build.sh` / `CMakeLists.txt`） | `build`                       | `build: default glog off, flip flag to --glog`                |
| 顶层文档单独改动（README / DEPLOY / CLAUDE）         | `doc`                         | `doc: trim key features table to the essentials`              |
| 跨功能域的大型新特性（以特性本身命名最清楚）/ 版本 bump            | **无前缀**                       | `add owner planned-restart mode blocking only new games`      |

**关键约束 ①：前缀是功能域短词,不带路径、不带 .py。** 功能域比文件路径稳定
（文件会重构搬家,功能不会）;同一功能的后续演进沿用同一前缀,便于
`git log --grep '^config:'` 按功能追溯。

**关键约束 ②：README / DEPLOY 同步不改变前缀。** 它们是 §4 同步规则强制要求的
副作用,前缀跟功能走。仅当特性大到没有单一主导功能域时才用"无前缀 + 特性描述"。

✅ 好：
```
ci: add real-bridge smoke test to cmake ci
config: match blocked_commands slash strictly
doc: trim key features table to the essentials
add owner planned-restart mode blocking only new games
```

❌ 差：
```
fix: Preload shared libs                       # 动作前缀 + 大写
page_config: validate config.yaml              # 文件名当前缀(应按功能用 config)
mod/quota: fix race                            # 不要带路径
修复 lgtbot.json 位置                            # 中文
```

### Commit 范围

- **永远不要**把 `lgtbot/` 子模块的工作区改动纳入 commit。`git status` 出现 `m lgtbot`（小写 m）时是**正常**的——子模块仅工作区脏，gitlink 未变，本来就不应该跟随父仓库一起提交。
- 用 `git add <具体文件>` 显式暂存，**禁用** `git add .` 或 `git add -A`，避免误把 `lgtbot/` 或 `data/` 临时文件混进 commit。
- amend 已 push 的 commit 之前先确认是否需要 `-f` push。

---

## 3. 代码风格

### 入口文件 `main.py`

保持精简，**只负责四件事**：
1. 声明 `__plugin_meta__`
2. 在 module top-level 捕获 `_ctx_mod.ctx` → `state.plugin_ctx`
3. 顺序触发 `mod/` 子模块加载（`boot` 必须最先）
4. 实现 `@on_load` / `@on_unload` 生命周期

入口文件超过 ~150 行就该考虑把逻辑下沉到 `mod/`。

| 模块                | 职责                                                                               |
|-------------------|----------------------------------------------------------------------------------|
| `state`           | 共享可变全局状态容器（`pending_buttons` / `event_loop` / `started` 等）                       |
| `boot`            | C++ 扩展加载（顺序敏感：`chdir` + `RTLD_GLOBAL` + `ctypes.CDLL` 预加载）                       |
| `buttons`         | 按钮模板 + 命令触发正则                                                                    |
| `helpers`         | 通用工具（sender / coro / mention / target_key）                                       |
| `quota`           | 被动消息引用配额管理                                                                       |
| `callbacks`       | C++ 引擎回调实现（`cb_*` 入口 + 异步发送）                                                     |
| `dispatcher`      | `@handler` 注册（消息派发 + INTERACTION）                                                |
| `config`          | `data/config.yaml` 读写                                                            |
| `userinfo`        | 主框架用户数据只读门面（data.db/wakeup.db/statistics.db；昵称缓存 + 变化时写回）                        |
| `uploader`        | 图床上传调度 + 图片尺寸解析                                                                  |
| `log_attribution` | 类级 monkey-patch `MessageSender._log_push`,让本插件的 push 在主框架 Web 面板归类为「LGTBot 消息派发」 |
| `webui/`          | Web 面板侧边栏页面                                                                      |

新增功能时优先选最契合的现有模块，**只有职责明显独立时才新建文件**。

### 注释与 docstring

- 每个 `.py` 顶部必须有简明 docstring 说明职责
- 关键设计决策（特别是绕过 QQ 协议限制 / C++ 副作用顺序）必须用注释说明 *why*
- 中文注释 OK，与现有风格一致即可

---

## 4. 文档分工

| 文件          | 用途   | 包含什么                           |
|-------------|------|--------------------------------|
| `README.md` | 项目展示 | 致谢、简介、工作原理图、特性、QQ 协议限制、文件结构、链接 |
| `DEPLOY.md` | 部署专用 | **仅** 安装依赖、编译、启动、配置、卸载、故障排查    |

`DEPLOY.md` **不放**：架构说明、目录结构、模块职责、协议限制、Web UI 介绍。这些进 README。

### 同步规则（强制）

**每次实质性改动都要检查并按需更新 README / DEPLOY，且应在同一个 commit 内完成**，避免文档与代码漂移。

特别需要同步的场景：

| 改动类型                                             | 同步位置                                 |
|--------------------------------------------------|--------------------------------------|
| 新增 / 删除 / 移动文件                                   | README「文件结构」节                        |
| 改动路径常量（`DATA_DIR` / `BUILD_DIR` / `CONF_PATH` …） | DEPLOY 配置 / 数据目录章节                   |
| 新增依赖 / 编译选项                                      | DEPLOY「系统依赖」「一键编译」章节                 |
| 新增功能特性                                           | README「关键特性」表                        |
| 新增 / 修改协议限制 / 已知问题                               | README「QQ 协议相关限制」表                   |
| 新增 / 修改 Web UI 页面                                | README「关键特性」+ DEPLOY「Web UI」相关说明（若有） |
| 新增 / 修改启动日志 / 故障现象                               | DEPLOY「故障排查」表                        |

---

## 5. 跨插件热重载

PluginManager 文件保存触发热重载时：
- C++ 扩展 `LGTBot_ElainaBot` 常驻进程，`sys.modules['LGTBot_ElainaBot']` 跨重载保留
- Python 子模块（`plugins.LGTBot_ElainaBot.mod.*`）会被销毁重建
- 要跨重载共享的可变状态都挂在 C++ 扩展属性上（`boot._get_persistent()`）
- 检测到引擎已运行 + 有进行中的游戏时，**不要再调 `LGTBot_ElainaBot.start()`**（会覆盖 `g_bot_core`，所有活跃 match 失联）

任何新增的需要跨重载持久的状态，都要走 `_get_persistent()` 路径。

---

## 6. QQ 官方机器人协议限制（已知）

修改消息发送相关代码时记住这些硬限制：

| 限制                                                           | 应对                                             |
|--------------------------------------------------------------|------------------------------------------------|
| 同一 `msg_id` 最多回 5 条（`msg_seq=1..5`）；5 分钟过期                   | 第 4 条起挂「🔄 刷新会话」按钮，第 5 条改「⚠️ 最终刷新」；超量阻塞等待 ≤15s |
| INTERACTION 事件的 `event_id` 独立计 5 条                           | 用户点按钮 → 新 event_id → 又 5 条额度                   |
| 媒体消息（`msg_type=7`）不解析 `<@openid>`                            | 在 `helpers.humanize_mentions` 中转 `@昵称`         |
| 媒体消息无法挂按钮（QQ 协议）                                             | 仅文本回复附按钮                                       |
| Markdown 图片 URL 必须 QQ 开放平台报备的域名                              | 直发本地图片无法内嵌 markdown                            |
| `button_enter_to_send=true` 配置会把 `type=2 + enter` 转 `type=1` | 本插件按钮一律不带 `enter` 字段                           |

---

## 7. 沟通与确认

- 用户说"不能改 X" / "不要做 Y" 后，**整个会话内**保持该约束
- 改动可能影响生产数据（`data/lgtbot.db` / 已编译 `build/`）时，**先确认**再动
- 涉及编译重启（修改 `LGTBot_ElainaBot.cc` / `CMakeLists.txt`）的改动，明确告诉用户需要 `bash build.sh --clean`
- 长篇技术分析可以输出在对话里，但**不要**默默写到代码注释里 —— 注释要简洁可维护
