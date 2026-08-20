"""LGTBot Web 面板拓展 —— 「LGTBot 机器人」侧边栏页面(单页多标签)。

Python 模块(仅做逻辑 + 模板加载):
    main           页面注册入口 + 主页面拼装(读 templates/main.{html,css,js}
                   并填充各标签的 HTML/JS/CSS 片段与数据 JSON)。`@on_load` 时
                   通过 ``webui.register()`` 把 PAGE_KEY 挂进框架的
                   ``web_pages._registry``,卸载时 ``webui.unregister()`` 摘除。
    page_dashboard 「仪表盘」标签(默认最左):版本/统计/缓存清理,提供检查更新、
                   git pull、子模块 update、缓存清理等 action
    page_config    「配置管理」标签:7 块编辑器(config.yaml / important_update.txt
                   / update_notice.txt / urgent_notice.txt / troubleshooting.txt
                   / sponsors.txt / lgtbot.json)+ 热重载 action
                   + 紧急公告的「启用 / 关闭」与「重置已通知群」两个 action
    page_build     「引擎编译」标签:子进程跑 bash build.sh,跨 WebUI 进程
                   重启仍能续看(state.json + build.log),支持中途终止;
                   ANSI 颜色转 HTML 渲染
    page_logs      「消息日志」标签 + 日志缓冲数据层。除了渲染消息日志页面,还
                   暴露 log_incoming / log_outgoing / get_logs / clear_logs,被
                   callbacks 与 dispatcher 在收发消息路径直接调用 —— deque 与
                   锁挂在 ``boot._get_persistent`` 上跨重载共享。
    page_users     「用户数据」标签:加载 ``templates/users/`` + 数据查询

前端静态模板(纯 HTML / CSS / JS,按功能分子目录):
    templates/main/        主骨架 / 全局 + 通用 CSS / 公共 JS
    templates/dashboard/   「仪表盘」标签 HTML / CSS / JS
    templates/config/      「配置管理」标签 HTML / CSS / JS
    templates/build/       「引擎编译」标签 HTML / CSS / JS
    templates/logs/        「消息日志」标签 HTML / CSS / JS
    templates/users/       「用户数据」标签 HTML / CSS / JS

action 端点(隐藏的 web_pages._registry key,被 get_pages wrap 过滤掉,不出现在侧边栏列表,仅供 JS fetch 触发):
    __lgtbot_restart                    整页通用「🔁 重启 LGTBot」按钮
    __lgtbot_dash_check_update          Dashboard「检查更新」(同时查桥接层 / 子模块上游)
    __lgtbot_dash_do_update             Dashboard「更新桥接层」(git pull --ff-only)
    __lgtbot_dash_update_submodule      Dashboard「更新 / 初始化 lgtbot 子模块」
    __lgtbot_dash_clear_avatar / _7d    Dashboard 头像缓存「清理全部 / 保留 7 天」
    __lgtbot_dash_clear_gen    / _7d    Dashboard 图片缓存「清理全部 / 保留 7 天」
    __lgtbot_dash_clear_match_all / _7d Dashboard 赛况缓存「清理全部 / 保留 7 天」
    __lgtbot_dash_build_full / incr / bridge / list / custom
                                        引擎编译 启动入口(完整 / 增量 / 桥接层 /
                                        列出目标 / 自定义目标)
    __lgtbot_dash_build_kill            引擎编译 终止当前进程
    __lgtbot_dash_build_clean / remove  引擎编译 清理重编 / 删 build/ 目录
    __lgtbot_dash_build_log             引擎编译 轮询拉日志 + 状态

「重启」端点返回 ``<div id="msg">…</div>`` 片段太小,直接在 ``main.py::
_render_restart`` 里 inline 字符串;Dashboard 端点返回的 ``<pre id="result">
JSON</pre>`` 也在 ``page_dashboard._fragment`` inline,无模板文件。

「保存引擎配置」复用主框架 ``/api/config-file/save`` 端点(接受 plugins/
下绝对路径),不在插件 webui 自建端点。

模板由 Python 在 import 时一次性读入并缓存(模块常量);插件热重载会重
新执行 import → 重新读盘,所以改完模板存盘后下次热重载就能看到新版本,无须重启进程。

如新增标签(房间监控、排行榜等):
  1. 新建 ``page_xxx.py`` + ``templates/xxx/{html,js}``
  2. 在 ``templates/main/main.html`` 加 tab nav 与 tab-pane 容器,并在
     ``_render_html`` 里把新模块的 HTML/JS/data 拼进去
"""
