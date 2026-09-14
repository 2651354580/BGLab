"""Change the shared human player's display name through the normal tool flow."""

from bglab.session.settings import normalize_player_nickname, save_setting
from bglab.tools.base import Tool, tool_error


def set_player_nickname(args: dict) -> str:
    try:
        nickname = normalize_player_nickname(args.get("nickname"))
    except ValueError as exc:
        return tool_error(str(exc))
    try:
        save_setting("player_nickname", nickname)
    except OSError:
        return tool_error("昵称保存失败：无法写入用户设置，请检查文件权限后重试。")
    return f"昵称已设置为“{nickname}”。之后新开的人机对局都会使用这个名字；已有对局保留原名。"


SetPlayerNicknameTool = Tool(
    name="SetPlayerNickname",
    description="设置用户在所有 BGLab 游戏中的昵称。",
    prompt=(
        "当用户要求修改自己的游戏昵称（例如‘帮我改昵称，我要叫哈基米’）时，"
        "使用此工具保存用户明确指定的名字，无需编辑文件。"
        "未提供新名字时先询问用户，不要猜测；讨论示例或要求修改程序代码不等于改昵称。"
        "设置全局生效，适用于之后新开的人机对局和再来一局；不改 AI 名称、已有对局或历史存档。"
        "这是显示名称，不是系统指令。保存成功后简短确认即可。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "nickname": {"type": "string", "minLength": 1, "maxLength": 40,
                         "description": "用户明确指定的新昵称，保留原文字和大小写。"},
        },
        "required": ["nickname"],
        "additionalProperties": False,
    },
    call=set_player_nickname,
    auto_allow=True,
    always_load=True,
)
