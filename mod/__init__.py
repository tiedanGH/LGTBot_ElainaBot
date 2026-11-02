"""LGTBot 插件内部子模块包。

用 ``mod/`` 而非 ``app/``:主框架 ``web/tools/_plugin_mgr/scan.py`` 会把插件根的 ``app/`` 扫成
Web 面板「插件管理」里可独立 toggle 的子模块,而这里**关闭任一个都会让整个插件崩溃**;
两个名字对加载器(``core/plugin/_loader.py::_import_plugin``)等价。

根目录只放引擎桥接与共享基础模块,其余按功能分子包:
    boot              C++ 扩展加载 + 全局路径常量 + 跨重载持久 dict(顺序敏感,须最先加载)
    state             共享运行时状态容器(跨重载持久;via boot._get_persistent)
    config            ``data/config.yaml`` 读写 + 运行时下发
    helpers           通用工具(sender / target_key / mention 改写 / 主动推送权限)
    callbacks         C++ 引擎回调(cb_* 入口 + 异步发送);模块路径被桥接层写死,不能挪
    dispatcher        ``@handler`` 注册(消息派发 + INTERACTION + 管理指令)
    game_switch       游戏开关:启动前拼出只含启用游戏的视图目录 + 运行中拦截开局
    messaging/        发送链路:quota(被动配额)/ buttons(按钮模板)/ uploader(图床上传)/log_attribution(push 日志归属 + 发送失败计数)/ at_all(@全体成员)
    users/            userinfo(主框架用户数据只读门面)/ nickname_review(昵称 AI 审核)
    stats/            metrics(运行指标 + lgtbot.db 统计查询)/ stats_image(统计卡片渲染)
    announce/         notice(更新公告分段)/ urgent(紧急公告)
    ops/              audit(操作审计)/ backup(数据备份)/ prebuilt + _prebuilt_swap(预编译包)/corefile(游戏 core 文件解析)
    webui/            Web 面板:main(页面注册 + 路由)/ api/(对外 HTTP API)/ pages/(各标签页)/templates/(前端模板)
"""
