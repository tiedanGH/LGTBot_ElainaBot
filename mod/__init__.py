"""LGTBot 插件内部子模块包。

为什么用 ``mod/`` 而非 ``app/``:主框架 ``web/tools/_plugin_mgr/scan.py`` 只把
插件根的 ``app/`` 子目录递归扫成 Web 面板「插件管理」里可独立 toggle 的子模块,
``mod/`` 不进扫描 —— 本目录下每个文件都是协作实现 LGTBot 集成的内部模块,
**关闭任一个都会让整个插件崩溃**(boot 关了 C++ 不加载、dispatcher 关了消息
不派发等)。放 ``mod/`` 对 Web UI 隐身,杜绝误触。两个名字对加载器
(``core/plugin/_loader.py::_import_plugin`` 扫所有非 ``_/.`` 开头的子目录注册
成 sub-package)等价。

模块划分:
    state             共享运行时状态容器(跨重载持久;via boot._get_persistent)
    boot              C++ 扩展加载 + 全局路径常量(顺序敏感,须最先加载)
    buttons           按钮模板(欢迎菜单 / 加入退出 / 全量申请 / 未知指令引导等)
    helpers           通用工具(sender / target_key / mention 美化 / 全量群判定)
    quota             被动消息引用配额管理(`msg_id`/`event_id` × 5 条 × 5 分钟)
    callbacks         C++ 引擎回调实现:
                      · cb_get_user_name / cb_get_user_avatar_url
                      · cb_send_text_message / cb_send_image_message
                        (fire-and-forget,跑 per-target Lock 串行化)
                      · cb_match_event(按 kind 决定按钮 + 触发开局教学)
                      · cb_lgtbot_crashed(SIGSEGV 恢复善后:日志 + 道歉 +
                        通知群推送 + 30s 后 os.execv 自启)
    dispatcher        ``@handler`` 注册(消息派发 + INTERACTION + ``/重启``);
                      接收侧适配全量群 GROUP_MESSAGE_CREATE
    config            ``data/config.yaml`` 读写 + 运行时下发(refresh_wait_timeout
                      / image_hosting / menu_game_buttons / notify_groups)
    urgent            紧急公告:文案(urgent_notice.txt)+ 总开关与已通知群记录
                      (urgent_notice.json,落盘所以重启仍生效);出口是欢迎菜单
                      尾部的引用块与新群首次建房的额外通知
    userinfo          主框架用户数据只读门面(data.db/wakeup.db/statistics.db;
                      昵称缓存 + 变化时经 db_queue 写回最新昵称)
    uploader          图床上传调度(名单随主框架模块动态发现) + 图片尺寸解析
    stats_image       「数据统计」指令的 PIL 统计卡片渲染(缺 PIL/字体回退文本)
    log_attribution   ``MessageSender._log_push`` 类级 monkey-patch + ContextVar,
                      让本插件 push 在主框架 Web 面板归类到「LGTBot 消息派发」
                      而不是默认的 'proactive'
    webui/            Web 面板拓展页(侧边栏「LGTBot 机器人」单页多标签):
                      ├─ main.py         页面注册 + 主骨架渲染
                      ├─ page_dashboard  「仪表盘」标签
                      ├─ page_config     「配置管理」标签 + 热重载 action
                      ├─ page_build      「引擎编译」标签
                      ├─ page_logs       「消息日志」标签 + 日志缓冲数据层
                      │                  (log_incoming / log_outgoing / get_logs)
                      ├─ page_users      「用户数据」标签
                      └─ templates/      纯前端资源,按 main / dashboard / config /
                                         build / logs / users 分子目录
"""
