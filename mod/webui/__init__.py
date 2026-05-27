"""LGTBot Web 面板拓展 —— 「LGTBot 机器人」侧边栏页面(单页多标签)。

Python 模块(仅做逻辑 + 模板加载):
    main           页面注册入口 + 主页面拼装(读 templates/main.{html,css,js}
                   并填充各标签的 HTML/JS 片段与数据 JSON)。`@on_load` 时
                   通过 ``webui.register()`` 把 PAGE_KEY 挂进框架的
                   ``web_pages._registry``,卸载时 ``webui.unregister()`` 摘除。
    page_dashboard 「仪表盘」标签(默认最左):版本/统计/引擎配置/缓存,
                   提供检查更新、git pull、JSON 配置编辑、缓存清理等 action
    page_logs      「消息日志」标签:加载 ``templates/logs.{html,js}`` + 数据生成
    page_users     「用户数据」标签:加载 ``templates/users.{html,js}`` + 数据查询
    message_log    日志缓冲(纯数据层):log_incoming / log_outgoing /
                   get_logs / clear_logs;被 callbacks 与 dispatcher 直接调用

前端静态模板(纯 HTML/CSS/JS,按功能分子目录):
    templates/main/        主骨架 / 全局 CSS / 公共 JS
    templates/dashboard/   「仪表盘」标签 HTML+JS
    templates/logs/        「消息日志」标签 HTML+JS
    templates/users/       「用户数据」标签 HTML+JS

action 端点(隐藏的 web_pages._registry key,被 get_pages wrap 过滤掉,不出
现在侧边栏列表,仅供 JS fetch 触发):
    __lgtbot_restart                    整页通用「🔁 重启 LGTBot」按钮
    __lgtbot_dash_check_update          Dashboard「检查更新」(GET GitHub tags)
    __lgtbot_dash_do_update             Dashboard「立即更新」(git pull --ff-only)
    __lgtbot_dash_clear_avatar          Dashboard 头像缓存「清理全部」
    __lgtbot_dash_clear_gen             Dashboard 图片缓存「清理全部」
    __lgtbot_dash_clear_match_all       Dashboard 赛况缓存「清理全部」
    __lgtbot_dash_clear_match_7d        Dashboard 赛况缓存「保留 7 天」

「重启」端点返回 ``<div id="msg">…</div>`` 片段太小,直接在 ``main.py::
_render_restart`` 里 inline 字符串;Dashboard 端点返回的 ``<pre id="result">
JSON</pre>`` 也在 ``page_dashboard._fragment`` inline,无模板文件。

「保存引擎配置」复用主框架 ``/api/config-file/save`` 端点(接受 plugins/
下绝对路径),不在插件 webui 自建端点。

模板由 Python 在 import 时一次性读入并缓存(模块常量);插件热重载会重
新执行 import → 重新读盘,所以改完模板存盘后下次热重载就能看到新版本,
无须重启进程。

如新增标签(房间监控、排行榜等):
  1. 新建 ``page_xxx.py`` + ``templates/xxx/{html,js}``
  2. 在 ``templates/main/main.html`` 加 tab nav 与 tab-pane 容器,并在
     ``_render_html`` 里把新模块的 HTML/JS/data 拼进去
"""
