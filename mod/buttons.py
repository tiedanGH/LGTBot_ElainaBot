#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按钮模板 + 触发命令正则。

设计要点：本插件的命令按钮**不使用 `enter` 字段**。
  原因：当 bot.yaml 配置 `message.button_enter_to_send: true` 时，框架
        keyboard.py 会把 `type=2 + enter=True` 强制转成 `type=1`（纯
        callback 按钮）。type=1 在 QQ 协议层永远不会回填输入框，仅触发
        INTERACTION_CREATE → bot ACK → 客户端弹"操作成功"，与"点按钮 →
        文字进输入框"的本意冲突。
  去掉 enter 后保持 type=2 不被转换：点击 → 文字回填到输入框 → 用户手动
  点发送。如果想要"自动发送"，用户需在 bot.yaml 把 button_enter_to_send
  设为 false 并在按钮里加回 enter=True。
"""

from __future__ import annotations

import random


# ──────── 按钮构造函数 ──────────────────────────────────────────────────────

def btn(text: str, data: str = '', *, type: int = 2, style: int = 0,
        link: str = '') -> dict:
    """构造单个按钮 dict(本插件唯一按钮入口,统一不带 ``enter``,见模块 docstring)。

    Args:
        text:   按钮文案(可含 emoji)
        data:   点击行为数据 —— ``type=2`` 回填输入框 / ``type=1`` 纯 callback
        type:   QQ 按钮 action type,默认 2(回填);``link`` 非空时忽略
        style:  视觉样式 0-4(框架 render_data.style)
        link:   非空则生成链接按钮(仅 ``{'text','link'}``,QQ 侧按 type=0
                跳转处理,不带 style)
    """
    if link:
        return {'text': text, 'link': link}
    return {'text': text, 'data': data, 'type': type, 'style': style}


# ──────── 外部链接常量 ──────────────────────────────────────────────────────

_OFFICIAL_GROUP_LINK = 'https://qm.qq.com/q/R3GXMpMU2m'
# 问题反馈问卷链接 —— 默认指向腾讯文档表单,方便统一收集反馈
_QUESTIONNAIRE_LINK = 'https://docs.qq.com/form/page/DY1JJTkZZeVh4TXZJ'
_NAV_HOME_LINK = 'https://tiedan.site/'
_REPO_ADAPTER_LINK = 'https://github.com/tiedanGH/LGTBot_ElainaBot'
_REPO_LGTBOT_LINK = 'https://github.com/Slontia/lgtbot'
# 赞助支持:导航站的赞助页 + 爱发电直达
_SPONSOR_PAGE_LINK = 'https://tiedan.site/pages/support/'
_AFDIAN_LINK = 'https://afdian.com/a/tiedan-LGTBot/plan'


# ──────── 赞助功能总开关 ────────────────────────────────────────────────────
# 由 config.py::_apply_runtime_tunables 按 ``sponsor_enabled`` 覆写,**默认关闭**。
# 关闭时本插件不展示任何赞助入口(下面三个 build_* 都不会带赞助按钮),「赞助支持」指令也直接转发给引擎
# 插件市场里的第三方部署方看不到任何与本作者相关的收款引导。
SPONSOR_ENABLED: bool = False


# ──────── 静态按钮常量 ──────────────────────────────────────────────────────
# 房间动作(挂在建房 / 加入退出广播上)
BTN_JOIN            = btn('🟢 加入', '/加入', style=1)
BTN_LEAVE           = btn('🔴 退出', '/退出', style=3)
# 引导与查询(type=2 回填版,用于解散引导 / 误输游戏名 / 结算)
BTN_GAME_LIST       = btn('🎲 游戏列表', '/游戏列表', style=4)
BTN_CREATE_ROOM     = btn('🎮 创建房间', '/新游戏', style=1)
BTN_RECORD          = btn('📊 查看战绩', '/战绩', style=1)
# 帮助类。「配置/游戏帮助」发不带斜杠的「帮助」;「元指令帮助」发 `/帮助`。
BTN_META_HELP       = btn('❓ 元指令帮助', '/帮助', style=1)
BTN_CONFIG_HELP     = btn('⚙️ 配置帮助', '帮助', type=1, style=4)
BTN_GAME_HELP       = btn('🎮 游戏帮助', '帮助', type=1, style=4)
# 「全量申请」入口(type=2 回填,用户自行补群号;实际命令由另一插件实现)
BTN_FULL_VOLUME_APPLY = btn('全量消息授权', '全量申请', style=4)
# 欢迎菜单固定区(type=1 callback 版帮助 / 列表 —— 菜单上点击即触发不回填)
BTN_MENU_HELP       = btn('📖 查看帮助', '/帮助', type=1, style=4)
BTN_MENU_GAME_LIST  = btn('🎲 游戏列表', '/游戏列表', type=1, style=4)
BTN_MORE_FEATURES   = btn('🧩 更多功能', '更多功能', type=1, style=1)
# 「更多功能」子菜单
BTN_ANNOUNCEMENT    = btn('📢 更新公告', '更新公告', type=1, style=4)
BTN_DATA_STATS      = btn('📈 数据统计', '数据统计', type=1, style=4)
BTN_TROUBLESHOOT    = btn('❓ 疑难解答', '疑难解答', type=1, style=0)
BTN_ABOUT           = btn('ℹ️ 关于框架', '/关于', style=1)
# 赞助入口(type=1 callback:点击直接触发「赞助支持」)
BTN_SPONSOR         = btn('❤️ 赞助支持', '赞助支持', type=1, style=1)
# 链接按钮(点击跳外部 URL,不依赖 bot 进程存活)
BTN_OFFICIAL_GROUP  = btn('💬 官方群聊', link=_OFFICIAL_GROUP_LINK)
BTN_FEEDBACK        = btn('🛠️ 问题反馈', link=_QUESTIONNAIRE_LINK)
BTN_NAV_HOME        = btn('🌠 导航网站主页', link=_NAV_HOME_LINK)
BTN_REPO_ADAPTER    = btn('适配层 仓库', link=_REPO_ADAPTER_LINK)
BTN_REPO_LGTBOT     = btn('LGTBot 仓库', link=_REPO_LGTBOT_LINK)
BTN_SPONSOR_PAGE    = btn('🍚 投喂入口', link=_SPONSOR_PAGE_LINK)
BTN_AFDIAN          = btn('⚡ 爱发电', link=_AFDIAN_LINK)


# ──────── 组装函数(挂载点见各 docstring) ────────────────────────────────────

# 玩家在 LGTBot 房间里常用动作（C++ 桥接层 ClassifyMatchEvent 决定挂在哪条上）
def build_game_action_buttons(game_name: str | None = None,
                              include_rule: bool = False,
                              include_join_leave: bool = True) -> list[list[dict]]:
    """构造房间相关按钮组。

    `include_join_leave=True`(群聊默认)时,第一行是「加入 / 退出」;私信
    场景调用方传 False 跳过这一行,因为 DM 里玩家通常自己就是房主或经
    match_id 加入,/加入 这种群内简写并不适用。
    `include_rule=True`(仅新建房间消息)且游戏名已知时,追加一行
    `/规则 <游戏名>` 按钮。
    两个开关都关掉且无游戏名时返回空列表,调用方负责跳过 pending_buttons
    的写入。
    """
    rows: list[list[dict]] = []
    if include_join_leave:
        rows.append([BTN_JOIN, BTN_LEAVE])
    if include_rule and game_name:
        rows.append([
            btn(f'📖 《{game_name}》规则', f'/规则 {game_name}', type=1, style=4),
        ])
    return rows


def build_dissolve_buttons() -> list[list[dict]]:
    """房间因全员退出而解散时建议的两个引导按钮:看看别的游戏 / 直接再开一局。

    仅在「所有玩家都退出了游戏」/「所有玩家都强制退出了游戏」这两条解散
    广播上附加（见 LGTBot_ElainaBot.cc::ClassifyMatchEvent 的 ``all_left``
    分支）。/新游戏 时引擎前置发出的「游戏已解散，谢谢大家参与」(Terminate)
    不附,因为紧跟着会有真正的新建房间消息覆盖。
    """
    return [[BTN_GAME_LIST, BTN_CREATE_ROOM]]


def build_game_over_buttons(game_name: str | None = None,
                            include_record: bool = True) -> list[list[dict]]:
    """游戏自然结束(结算广播)时的引导按钮:查看战绩 / 重开一局。

    挂在「游戏结束，公布分数：」结算消息上(``ClassifyMatchEvent`` 的
    ``game_over`` / ``game_over_unrecorded`` 分支)。结算广播不带 brief,
    「重开一局」的游戏名由调用方从 ``state.current_game`` 回查后传入。
    ``include_record=False``(结算带「游戏结果不记录」:单机 / 非正式局 /
    未连接数据库)时不给「查看战绩」—— 本局没进战绩,按钮只会误导。
    两个按钮都凑不齐时返回空列表,调用方跳过 pending_buttons 写入。
    """
    row: list[dict] = []
    if include_record:
        row.append(BTN_RECORD)
    if game_name:
        row.append(btn('🔄 重开一局', f'/新游戏 {game_name}', style=4))
    return [row] if row else []


# ──────── 未知指令引导(LGTBot_ElainaBot.cc::ClassifyMatchEvent 的 unknown_* 分支)──

def build_unknown_meta_buttons() -> list[list[dict]]:
    """场景 1:用户没参与游戏 / 已加入但不在本群 —— 只给元指令帮助。"""
    return [[BTN_META_HELP]]


def build_unknown_config_buttons() -> list[list[dict]]:
    """场景 2:已在等待中的房间但用了未知的游戏配置 —— 配置帮助 + 元指令帮助。"""
    return [[BTN_CONFIG_HELP, BTN_META_HELP]]


def build_unknown_game_buttons() -> list[list[dict]]:
    """场景 3:游戏进行中,但用了未知的游戏指令 —— 游戏帮助 + 元指令帮助。"""
    return [[BTN_GAME_HELP, BTN_META_HELP]]


def build_game_help_buttons() -> list[list[dict]]:
    """单按钮一行:「🎮 游戏帮助」。

    挂在引擎「游戏开始，您可以使用「帮助」命令…」这条开局广播上
    (``ClassifyMatchEvent`` 的 ``game_started`` 分支)—— 广播本身就在教玩家发
    「帮助」,给一颗按钮省掉手输。
    """
    return [[BTN_GAME_HELP]]


def build_game_list_buttons() -> list[list[dict]]:
    """单按钮一行:「🎲 游戏列表」——与欢迎菜单同款。
    用于 `/新游戏 X` / `/规则 X` / `/设置 X` 等误输游戏名时,引导用户查正确名字。
    """
    return [[BTN_GAME_LIST]]


def build_full_volume_apply_button() -> list[list[dict]]:
    """单按钮一行:「全量申请」(type=2,回填到输入框,用户自行补群号再发送)。

    挂在非全量群的「消息回复限制」教学提示底部,与文案里给出的命令格式
    ``全量申请 <本群群号>`` 对齐 —— 用户点完按钮后输入框出现「全量申请」,
    再手动补群号即可。``type=2`` 不带 ``enter``,符合本插件按钮约定。
    实际处理「全量申请」命令的是另一个插件,本插件只提供 UI 入口。
    """
    return [[BTN_FULL_VOLUME_APPLY]]


def build_about_buttons() -> list[list[dict]]:
    """/关于 回执底部附:左 适配层仓库,右 LGT-Bot 上游仓库。两个都是链接按钮
    (type=0,QQ 协议下点击直接跳转,无 style)。

    ``SPONSOR_ENABLED`` 时在下方追加一行「赞助支持」—— 关于页是介绍项目本身的
    地方,赞助引导在这里最不违和;开关关闭时这一行完全不出现。
    """
    rows = [[BTN_REPO_ADAPTER, BTN_REPO_LGTBOT]]
    if SPONSOR_ENABLED:
        rows.append([BTN_SPONSOR])
    return rows


def build_sponsor_entry_buttons() -> list[list[dict]]:
    """单独一行「赞助支持」入口;赞助功能关闭时返回空列表。

    给本身没有按钮组的回执用(目前是「更新公告」)—— 调用方需自行处理空列表
    (``event.reply`` 不传 buttons),不要把空列表当键盘传下去。
    """
    return [[BTN_SPONSOR]] if SPONSOR_ENABLED else []


def build_sponsor_buttons() -> list[list[dict]]:
    """「赞助支持」回执底部:赞助页面 + 爱发电,两个都是 link 按钮。

    赞助页面(导航站 /pages/support/)上才有收款码 —— 收款码图片不进 QQ 消息
    (markdown 图片需报备域名,且平台对收款码敏感),机器人侧一律只给链接。
    """
    return [
        [BTN_SPONSOR_PAGE, BTN_AFDIAN],
        [BTN_OFFICIAL_GROUP, BTN_FEEDBACK]
    ]


def build_support_buttons() -> list[list[dict]]:
    """官方群聊 + 问题反馈按钮组 —— 求助 / 反馈类消息底部统一引导。

    都是 link 按钮(默认 ``type=0``),点击直接跳转外部 URL,**不依赖 bot 进程
    存活**;因此在崩溃道歉等"进程即将 execv 重启"的场景下也安全可挂(callback
    按钮 type=1/2 在 execv 后无法 ack,但 link 按钮跟客户端打开浏览器一样不受
    影响)。

    当前调用方:
      · dispatcher.lgtbot_troubleshooting —— 疑难解答 Q&A 末尾
      · dispatcher 两个 catch-all 的「计划重启」维护提示 —— 即将 execv 重启
      · callbacks._try_send_crash_apology —— 引擎崩溃道歉末尾
    """
    return [[BTN_OFFICIAL_GROUP, BTN_FEEDBACK]]


def build_more_features_buttons() -> list[list[dict]]:
    """「🧩 更多功能」子菜单 —— dispatcher 的 ``lgtbot_more_features`` handler
    在用户点击「更多功能」按钮 / 直接发送「更多功能」文本时回复。

    ``SPONSOR_ENABLED`` 时底部追加一行「赞助支持」(共 5 行,正好是 QQ 键盘上限)。
    """
    rows = [
        [BTN_ANNOUNCEMENT, BTN_DATA_STATS],
        [BTN_TROUBLESHOOT, BTN_FEEDBACK],
        [BTN_ABOUT],
        [BTN_NAV_HOME],
    ]
    if SPONSOR_ENABLED:
        rows.append([BTN_SPONSOR])
    return rows


# ──────── 欢迎菜单按钮组 ────────────────────────────────────────────────────
# 「游戏快捷开局」部分按 ``MENU_GAMES`` 渲染,这个列表由 ``data/config.yaml``
# 的 ``menu_game_buttons`` 字段在 @on_load 时下发 (见 config.py)。其他部分
# (帮助 / 游戏列表 / 创建房间 / 战绩 / 仓库链接) 是固定的。
#
# 之所以拆成函数而非常量,是为了让 config 改后 dispatcher 下次 reply 立刻拿到
# 新布局,不用重启进程;调用方一律走 ``build_menu_buttons()``。

DEFAULT_MENU_GAMES: list[str] = [
    '数字蜂巢', '天赋云巢', '炼金术士',
    '差值投标', '决胜五子', '彩虹奇兵',
]
# 由 config.py::_apply_runtime_tunables 覆盖;默认 6 个游戏 → 2 行 × 3 列。
MENU_GAMES: list[str] = list(DEFAULT_MENU_GAMES)
# 每行最多几个游戏按钮;QQ 客户端单行最多 5 个,3 排版上最舒服。
MENU_GAMES_PER_ROW: int = 3


def _build_robot_invite_link(uin: str, appid: str) -> str:
    """拼 QQ 群机器人添加链接 —— 接收方在 QQ 客户端打开后能看到「邀请到我的群」按钮。

    uin/appid 由调用方从框架(``helpers.get_bot_uin`` / ``event.appid``)获取;
    任一为空时也照常生成 URL —— QQ 点击后会自己拒绝并提示无效 robot,这样
    用户能立刻发现 bot.yaml 里 ``robot_qq`` 没配。
    """
    return (f'https://qun.qq.com/qunpro/robot/qunshare'
            f'?robot_uin={uin}&robot_appid={appid}&biz_type=0')


def _auto_robot_invite_link() -> str:
    """用**绑定 bot** 的 (uin, appid) 拼邀请链接。

    用在没有 event 上下文的发送路径(如 callbacks._send_dm_warning,跑在
    C++ 工作线程上拿不到具体 event.appid)。绑定解析见
    ``helpers.get_bound_appid``(配置的 bind_bot_appid,回退第一个 bot)。
    """
    from . import helpers as _helpers
    appid = _helpers.get_bound_appid()
    uin = _helpers.get_bot_uin(appid)
    return _build_robot_invite_link(uin, appid)


def build_dm_warning_buttons() -> list[list[dict]]:
    """「私信消息限制」提示底部的单按钮一行 ——「💫 添加好友」link 跳转。

    点击后 QQ 客户端打开 bot 分享页(同欢迎菜单「邀我进群」用同一个
    ``_build_robot_invite_link``),用户可选「添加为好友」或「邀请到群」。
    uin / appid 自动从 BotManager 抓任一活跃 bot 凭据 —— callbacks 触发点是
    C++ 工作线程,拿不到具体的 event.appid。链接随绑定 bot 变化,不能做常量。
    """
    return [[btn('💫 添加好友', link=_auto_robot_invite_link())]]


def build_menu_buttons(appid: str = '') -> list[list[dict]]:
    """组装欢迎菜单完整按钮组(每次调用都按当前 ``MENU_GAMES`` 重新渲染)。

    游戏快捷部分被切分为每行 ``MENU_GAMES_PER_ROW`` 个;``MENU_GAMES`` 为空
    列表时跳过整个游戏分区,菜单仍包含帮助/创建房间等固定按钮和底部链接。

    ``appid`` 用于拼「邀我进群」按钮的链接(需要 bot 的 robot_qq + appid);
    调用方建议传 ``event.appid``,空则从框架任一已加载 bot 取。
    """
    # 从 helpers 拿 bot 的 QQ uin 拼邀请链接(避免 circular import)
    from . import helpers as _helpers
    uin = _helpers.get_bot_uin(appid)
    invite_link = _build_robot_invite_link(uin, appid)

    # 游戏快捷区:固定显示 2 行 × MENU_GAMES_PER_ROW 个 = 6 个。
    #   · 配置 ≤ 6 个:按原序全部展示(每次完全一致,不洗牌)
    #   · 配置 > 6 个:每次调用都用 ``random.sample`` 随机抽 6 个,顺序也是随机的
    #     —— 让用户每次 @bot 都能看到不同游戏组合,提升发现感
    display_max = MENU_GAMES_PER_ROW * 2
    if len(MENU_GAMES) > display_max:
        display_games = random.sample(MENU_GAMES, display_max)
    else:
        display_games = list(MENU_GAMES)

    game_rows: list[list[dict]] = []
    for i in range(0, len(display_games), MENU_GAMES_PER_ROW):
        chunk = display_games[i:i + MENU_GAMES_PER_ROW]
        game_rows.append([btn(name, f'/新游戏 {name}') for name in chunk])
    return [
        [BTN_MENU_HELP, BTN_MENU_GAME_LIST],
        [BTN_CREATE_ROOM, BTN_MORE_FEATURES],
        # 游戏快捷开局按钮 (可配置 —— data/config.yaml 的 menu_game_buttons)
        *game_rows,
        # 底部链接按钮
        [BTN_OFFICIAL_GROUP, btn('🚀 邀我进群', link=invite_link)],
    ]


# 单独 @ bot（content 为空）时回复的欢迎语
MENU_TEXT_HEADER = (
    '## 🎮 LGT-Bot 机器人\n'
    '\n'
    '---\n'
    '\n'
)
MENU_TEXT_BODY = (
    ''
)
# 兼容旧引用：拼接版
MENU_TEXT = MENU_TEXT_HEADER + MENU_TEXT_BODY


# ──────── markdown 内联指令链接(<qqbot-cmd-input>)生成工具 ─────────────────
# QQ 官方机器人 markdown 支持 ``<qqbot-cmd-input>`` 自定义标签:
# 点击后客户端显示 ``show`` 文案,把 ``text`` 回填到输入框

def cmd_input(text: str, show: str, reference: bool = False) -> str:
    """生成 markdown 行内 ``<qqbot-cmd-input>`` 标签。

    Args:
        text:       点击后回填给输入框的指令文本(如 ``/排行大图 本群``)
        show:       客户端上显示的按钮文案,可含 emoji(如 ``🏆 本群排行``)
        reference:  发送时是否引用原消息;本插件默认 False
    """
    ref = 'true' if reference else 'false'
    return f'<qqbot-cmd-input text="{text}" show="{show}" reference="{ref}"/>'


# ──────── 欢迎菜单「logo / 标题下方」可扩展区块 ─────────────────────────────
# dispatcher 在 logo 渲染成功 / 失败两个分支都会把本字符串拼到 markdown 末尾,
# 所以即便图床没启用、logo 没拿到 URL,这里的内容也会照常显示。

MENU_HEADER_EXTRA_MD = (
    cmd_input('更新公告', '✨ 点击查看最近更新') + '\n'
)

# 非全量群的菜单追加行:引导发起「全量申请」(免刷新授权)。
# 是否拼接由 dispatcher 按事件判定 —— 仅群聊且 **非全量群** 显示;
# 实际命令由另一插件实现,本插件只提供入口(与 _REFRESH_TIP_GROUP_TAIL / 全量申请按钮同源引导)。
MENU_FULL_VOLUME_CMD_MD = (
    cmd_input('全量申请', '⚡ 免刷新授权（大幅改善体验）') + '\n'
)
