#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按钮模板 + 组装函数。

设计要点：本插件的命令按钮**不使用 `enter` 字段** —— bot.yaml 开了
`message.button_enter_to_send` 时，框架 keyboard.py 会把 `type=2 + enter=True`
转成 `type=1`，而 type=1 只触发 INTERACTION、永远不回填输入框。不带 enter 的
type=2 点击后文字回填到输入框，由用户手动发送。
"""

from __future__ import annotations

import random


# ──────── 按钮构造函数 ──────────────────────────────────────────────────────

def btn(text: str, data: str = '', *, type: int = 2, style: int = 0,
        link: str = '', admin: bool = False) -> dict:
    """构造单个按钮 dict(本插件唯一按钮入口,统一不带 ``enter``,见模块 docstring)。

    Args:
        text:   按钮文案(可含 emoji)
        data:   点击行为数据 —— ``type=2`` 回填输入框 / ``type=1`` 纯 callback
        type:   QQ 按钮 action type,默认 2(回填);``link`` 非空时忽略
        style:  视觉样式 0-4(框架 render_data.style)
        link:   非空则生成链接按钮(仅 ``{'text','link'}``,QQ 侧按 type=0 跳转处理,不带 style)
        admin:  True = **仅群主 / 群管理员可点**(框架 keyboard.py 据此下发
                ``permission {'type': 1}``;默认所有人可点 ``{'type': 2}``)
    """
    if link:
        return {'text': text, 'link': link}
    b = {'text': text, 'data': data, 'type': type, 'style': style}
    if admin:
        b['admin'] = True
    return b


# ──────── 外部链接常量 ──────────────────────────────────────────────────────

_OFFICIAL_GROUP_LINK = 'https://qm.qq.com/q/R3GXMpMU2m'
_QUESTIONNAIRE_LINK = 'https://docs.qq.com/form/page/DY1JJTkZZeVh4TXZJ'
_NAV_HOME_LINK = 'https://tiedan.site/'
_REPO_ADAPTER_LINK = 'https://github.com/tiedanGH/LGTBot_ElainaBot'
_REPO_LGTBOT_LINK = 'https://github.com/Slontia/lgtbot'
# 赞助支持:导航站的赞助页 + 爱发电直达
_SPONSOR_PAGE_LINK = 'https://tiedan.site/pages/support/'
_AFDIAN_LINK = 'https://afdian.com/a/tiedan-LGTBot/plan'


# ──────── 赞助功能总开关 ────────────────────────────────────────────────────
# 由 config.py::_apply_runtime_tunables 按 ``sponsor_enabled`` 覆写,**默认关闭**:插件市场里的第三方部署方不该看到本作者的收款引导。
# 关闭时不展示任何赞助入口,「赞助支持」指令也直接转发给引擎。
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
BTN_FULL_VOLUME_APPLY = btn('⚡ 全量消息授权', '全量申请', style=4)
# 群管专用:中断投票广播上的「强制中断游戏」,admin=True 只对群主/群管理员放行点击。
BTN_FORCE_INTERRUPT = btn('🛑 强制中断游戏', '%中断', style=3, admin=True)
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
    `include_rule=True` 且游戏名已知时,追加一行 `/规则 <游戏名>` 按钮。
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


def build_force_interrupt_buttons() -> list[list[dict]]:
    """「强制中断游戏」单按钮 —— 只挂在群管发起 ``/中断`` 后的引擎广播上。

    普通玩家发 ``/中断`` 时**整组不出现**(挂载判定见 ``callbacks._force_interrupt_buttons_for``):
    这颗按钮的作用是给群管一条"投票太慢就直接强制"的近路,对没有权限的人只是噪声。
    """
    return [[BTN_FORCE_INTERRUPT]]


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


# 候选游戏按钮最多 4 排:按钮上限 5 排,末排留给「游戏列表」
_SUGGEST_ROWS_MAX = 4
_SUGGEST_BUTTONS_MAX = _SUGGEST_ROWS_MAX * 3


def _suggest_row_sizes(n: int) -> list[int]:
    """候选按钮每排几个:优先 2 个一排,多出来的从最后一排往前补成 3 个。排数封顶后多出的继续往前补;再多由调用方截断。"""
    if n <= 2:
        return [n] if n > 0 else []
    rows = min(n // 2, _SUGGEST_ROWS_MAX)
    extra = n - rows * 2
    return [2] * (rows - extra) + [3] * extra


def build_game_suggest_buttons(names) -> list[list[dict]]:
    """「数据统计<游戏名>」对不上游戏名时:每个候选游戏一颗(type=1,点击直接查)+ 末排「🎲 游戏列表」。

    只有一个候选时带 emoji,多个时省掉给游戏名留地方;超过 ``_SUGGEST_BUTTONS_MAX`` 的截掉。
    """
    names = list(names or [])[:_SUGGEST_BUTTONS_MAX]
    rows: list[list[dict]] = []
    i = 0
    for k in _suggest_row_sizes(len(names)):
        rows.append([btn(f'📈 {n}' if len(names) == 1 else n, f'数据统计 {n}', type=1, style=1)
                     for n in names[i:i + k]])
        i += k
    rows.append([BTN_GAME_LIST])
    return rows


def build_full_volume_apply_button() -> list[list[dict]]:
    """单按钮一行:「全量申请」(type=2,回填到输入框,用户自行补群号再发送)。

    挂在「消息回复限制」教学提示底部。实际处理「全量申请」命令的是另一个插件,本插件只提供 UI 入口。
    """
    return [[BTN_FULL_VOLUME_APPLY]]


def build_about_buttons() -> list[list[dict]]:
    """/关于 回执底部附:左 适配层仓库,右 LGT-Bot 上游仓库。两个都是链接按钮
    (type=0,QQ 协议下点击直接跳转,无 style)。

    ``SPONSOR_ENABLED`` 时「赞助支持」单独放**第一行**,开关关闭时这一行不出现。
    """
    rows: list[list[dict]] = []
    if SPONSOR_ENABLED:
        rows.append([BTN_SPONSOR])
    rows.append([BTN_REPO_ADAPTER, BTN_REPO_LGTBOT])
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

    都是 link 按钮,点击直接跳转外部 URL,**不依赖 bot 进程存活** —— 崩溃道歉等
    进程即将 execv 重启的消息也能安全挂(callback 按钮在 execv 后无法 ack)。
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
# 「游戏快捷开局」部分按 ``MENU_GAMES`` 渲染(由 ``data/config.yaml`` 的 ``menu_game_buttons`` 下发,见 config.py),其余部分固定。
# 做成函数而非常量:config 改后 dispatcher 下次 reply 立刻拿到新布局。

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

    用在没有 event 上下文的发送路径(如 callbacks._send_dm_warning)。
    """
    from . import helpers as _helpers
    appid = _helpers.get_bound_appid()
    uin = _helpers.get_bot_uin(appid)
    return _build_robot_invite_link(uin, appid)


def build_dm_warning_buttons() -> list[list[dict]]:
    """「私信消息限制」提示底部的单按钮一行 ——「💫 添加好友」link 跳转。

    点击后 QQ 客户端打开 bot 分享页,用户可选「添加为好友」或「邀请到群」。
    callbacks 侧拿不到 event.appid,链接按绑定 bot 现拼,随换绑变化,不能做常量。
    """
    return [[btn('💫 添加好友', link=_auto_robot_invite_link())]]


def build_menu_buttons(appid: str = '') -> list[list[dict]]:
    """组装欢迎菜单完整按钮组(每次调用都按当前 ``MENU_GAMES`` 重新渲染)。

    游戏快捷部分被切分为每行 ``MENU_GAMES_PER_ROW`` 个;``MENU_GAMES`` 为空
    列表时跳过整个游戏分区,菜单仍包含帮助/创建房间等固定按钮和底部链接。

    ``appid`` 用于拼「邀我进群」按钮的链接(需要 bot 的 robot_qq + appid),
    调用方建议传 ``event.appid``。
    """
    # 局部导入避免 circular import
    from . import helpers as _helpers
    uin = _helpers.get_bot_uin(appid)
    invite_link = _build_robot_invite_link(uin, appid)

    # 游戏快捷区最多 2 行;配置超出时每次随机抽,让用户每次 @bot 都能看到不同的游戏组合
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
        # 游戏快捷开局按钮
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
# dispatcher 在 logo 渲染成功 / 失败两个分支都会拼上本字符串,图床没启用时也照常显示。

MENU_HEADER_EXTRA_MD = (
    cmd_input('更新公告', '✨ 点击查看最近更新') + '\n'
)

# 非全量群的菜单追加行:引导发起「全量申请」(免刷新授权),是否拼接由 dispatcher 判定。
# 实际命令由另一插件实现,本插件只提供入口。
MENU_FULL_VOLUME_CMD_MD = (
    cmd_input('全量申请', '⚡ 免刷新授权（大幅改善体验）') + '\n'
)
