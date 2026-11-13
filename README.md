<div align="center">

![Logo](https://github.com/Slontia/lgtbot/blob/master/images/logo_transparent_colorful.svg)

# LGTBot × ElainaBot

**QQ 官方机器人版 LGTBot 适配插件**

![lang](https://img.shields.io/badge/language-Python%20%2F%20C%2B%2B20-green.svg)
![platform](https://img.shields.io/badge/platform-QQ%E5%AE%98%E6%96%B9%E6%9C%BA%E5%99%A8%E4%BA%BA-blue.svg)
![license](https://img.shields.io/badge/license-LGPLv2-orange.svg)
[![CMake](https://github.com/tiedanGH/LGTBot_ElainaBot/actions/workflows/cmake.yml/badge.svg)](https://github.com/tiedanGH/LGTBot_ElainaBot/actions/workflows/cmake.yml)
[![pytest](https://github.com/tiedanGH/LGTBot_ElainaBot/actions/workflows/pytest.yml/badge.svg)](https://github.com/tiedanGH/LGTBot_ElainaBot/actions/workflows/pytest.yml)
[![QQ Group](https://img.shields.io/badge/QQ%20Group-1059834024-0085F0.svg)](https://qm.qq.com/q/R3GXMpMU2m)

</div>

---

## 简介

把 LGTBot 的 60+ 种游戏通过 [ElainaBot_v2](https://github.com/ElainaCore/ElainaBot_v2) 框架接入到 **QQ 官方机器人**，核心游戏引擎来自上游项目 [LGTBot](https://github.com/Slontia/lgtbot)（© [@Slontia](https://github.com/Slontia)）。

**作为 ElainaBot 插件零配置启动**：安装插件 → 启动主框架 → 面板下载预编译包 → 重启机器人 → 在群里 @ 机器人即可游玩，全程无需编译。

## 快速开始

预编译包覆盖 **Ubuntu 22.04 / 24.04、Debian 12**，无需安装编译工具链：

1. **安装插件**，任选一种：
   - **插件市场（推荐）**：在 ElainaBot Web 面板的插件市场搜索 LGTBot_ElainaBot 并安装
   - **手动部署**：下载本项目源代码，放入主框架的 `plugins/LGTBot_ElainaBot/` 目录（目录名需保持一致）
2. **启动主框架**：插件自动加载，在 Web 面板侧边栏「LGTBot 机器人」→ 仪表盘「运行环境」检查依赖，运行时缺少的库按自检提示安装，编译依赖可忽略
3. **下载预编译包**：切到「预编译部署」，点「测速」选择镜像，在预编译包列表里下载与本机匹配的包
4. **切换并重启**：在「构建来源」点「用预编译包」，再点右上角「重启 LGTBot」
5. 在 QQ 群里 @ 机器人即可开始游玩

> 自行部署（本地编译、预编译未覆盖的系统或 Python 版本）请查看 [DEPLOY.md](./DEPLOY.md)。

## 关键特性

- **部署与加载**：项目目录自包含、零配置加载；CI 预编译包覆盖 Ubuntu 22.04 / 24.04、Debian 12，免本地工具链；面板可按游戏停用
- **消息与交互**：图文消息聚合与图床、交互按钮与欢迎菜单、数据统计卡片、玩家头衔、更新与紧急公告；全量群与主动推送权限分开判定，主动消息额度与频控自动守护
- **对局与数据安全**：计划重启不丢进行中的对局、实时对局跟踪、群管可 `%中断` 卡死的对局、数据库每日自动备份与一键恢复
- **Web 面板与运维**：侧边栏单页多标签，涵盖运行指标、配置编辑、预编译部署、引擎编译、数据备份、操作审计与崩溃转储；另有供其他插件调用的自动化 API

> 每项特性的完整说明，以及 QQ 协议限制与文件结构，见 [DETAILS.md](./DETAILS.md)。

## 工作原理

```
┌─────────────────────┐    @handler               ┌──────────────────────────────┐
│ ElainaBot 主框架    │ ─────────────────────►    │ plugins/LGTBot_ElainaBot/    │
│  (QQ Webhook / WS)  │                           │  main.py                     │
│  MessageSender      │ ◄──── send_to_xxx ─────── │   ↓ Boost.Python             │
└─────────────────────┘     run_coroutine_        │  LGTBot_ElainaBot.so         │
                            threadsafe            │   ↓ FFI                      │
                                                  │  libbot_core (C++)           │
                                                  │  + 60+ games                 │
                                                  └──────────────────────────────┘
```

## 开发计划（TODO）

规划中的改进方向，欢迎在 [QQ 群](https://qm.qq.com/q/R3GXMpMU2m) 或 issue 中讨论：

- [ ] **定时任务小框架** —— 项目内建轻量定时调度，替掉现在的 ad-hoc 定时器。**固定时间**的每日备份、每日排行榜推送、过期渲染图定时清理。跨热重载不重复注册、进程重启后能补跑漏掉的任务、单个任务失败不拖垮其他任务
- [x] **Web 面板图标 SVG 化** —— 把面板内**全部 emoji 图标**换成 SVG，摆脱各平台 emoji 字形不一致（Windows / macOS / 移动端差异明显）、无法随主题换色的问题
- [x] **CI 预编译产物** —— GitHub Actions 三发行版矩阵编译引擎核心 + 游戏插件，发布到滚动 `prebuilt` release；面板「预编译部署」下载切换，用户无需本地搭 Boost.Python / C++20 工具链
- [x] **复用主框架用户数据** —— 昵称 / 头像 / 活跃 / 消息统计全部改读 ElainaBot 主框架数据库（data.db / wakeup.db / statistics.db），插件私有的 `data/user_cache.db` 完全停用；昵称变化由插件比对后经框架写队列回写保持最新

---

## 致谢与项目来源

本项目是一个**适配层 / 集成包**，核心游戏引擎完全来自上游项目：

> **[LGTBot](https://github.com/Slontia/lgtbot)** — © [@Slontia](https://github.com/Slontia)
>
> *「LGT」源自日本漫画家甲斐谷忍《Liar Game》中的虚构组织「**L**iar **G**ame **T**ournament 事务所」*
>
> 一个基于 C++20 的多人文字推理游戏裁判机器人库，包含 60+ 种不同风格的游戏。游戏逻辑、引擎核心、图片渲染均由原作者 Slontia 设计实现。

本项目并未对 LGTBot 引擎做任何功能性修改，仅做：
1. 把 [LGTBot](https://github.com/Slontia/lgtbot) 适配 [ElainaBot_v2](https://github.com/ElainaCore/ElainaBot_v2) QQ 官方机器人框架
2. 处理 QQ 协议特有的限制（媒体消息合并、@mention 格式、按钮交互等）

**所有荣誉归原作者所有 —— 强烈建议先去 [LGTBot 主仓库](https://github.com/Slontia/lgtbot) 给原项目点 Star。**

| 上游项目                                                                  | 作者                                     | 协议     |
|-----------------------------------------------------------------------|----------------------------------------|--------|
| [LGTBot 引擎](https://github.com/Slontia/lgtbot)                        | [@Slontia](https://github.com/Slontia) | LGPLv2 |
| [lgtbot-khl](https://github.com/Slontia/lgtbot-khl) (KOOK 适配，本项目参考实现) | [@Slontia](https://github.com/Slontia) | LGPLv2 |
| [ElainaBot_v2 框架](https://github.com/ElainaCore/ElainaBot_v2)         | [@冷曦](https://github.com/lengxi-root)  | MIT    |
| 本适配层                                                                  | 铁蛋                                     | LGPLv2 |

## 赞助支持

运营的公开 bot 的服务器、图床与域名开销均为自费。如果这个项目帮到了你，欢迎请我喝杯咖啡：

- ❤️ 赞助页面：https://tiedan.site/pages/support/
- ⚡ 爱发电：https://afdian.com/a/tiedan-LGTBot/plan

> 支持**本适配层的开发与公开运行开销**，与上游 LGTBot 引擎无关。赞助完全自愿，**不存在任何「付费解锁」内容**。

**自行部署者请注意**：赞助功能由 `config.yaml` 的 `sponsor_enabled` 控制，**默认 `false`**。保持关闭时不会展示任何赞助入口，你的用户不会看到与本作者相关的收款引导。

---

## 许可证

本适配层与 LGTBot 引擎保持一致，使用 **LGPLv2** 协议。

游戏逻辑、引擎核心、图片渲染等核心实现的著作权归 [@Slontia](https://github.com/Slontia) 所有，请遵守上游项目的 [LICENSE](https://github.com/Slontia/lgtbot/blob/master/LICENSE)。

## 链接

- 🎮 LGTBot 上游仓库：https://github.com/Slontia/lgtbot
- 🟢 KOOK 版（本项目参考实现）：https://github.com/Slontia/lgtbot-khl
- 🤖 ElainaBot_v2 主框架：[https://github.com/ElainaCore/ElainaBot_v2](https://github.com/ElainaCore/ElainaBot_v2)
- 📖 部署指南：[DEPLOY.md](./DEPLOY.md)
- 📑 详细说明：[DETAILS.md](./DETAILS.md)
- ❤️ 赞助支持：https://tiedan.site/pages/support/
