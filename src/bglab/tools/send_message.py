""

from __future__ import annotations

import json

from bglab.owned_path import OwnedPathError
from bglab.tools.base import Tool
from bglab.tools.team_mailbox import write_message


def send_message(to: str, message: str, team_name: str = "",
                  summary: str = "", sender: str = "") -> dict:
    if not to or to == "*":
        return {"error": "broadcast not yet implemented; specify a teammate name"}

    text = message
    msg = {"text": text}
    try:
        write_message(to, msg, team_name, sender=sender, summary=summary)
    except OwnedPathError as exc:
        return {"error": f"Invalid team or teammate name: {exc}"}
    return {"to": to, "sent": True, "summary": summary}


def _send_message_call(args: dict) -> str:
    to = args.get("to", "")
    message = args.get("message", "")
    team_name = args.get("team_name", "")
    summary = args.get("summary", "")
    sender = args.get("sender", "leader")
    if not to:
        return "Error: 'to' is required"
    if not message:
        return "Error: 'message' is required"
    result = send_message(to, message, team_name, summary, sender)
    if "error" in result:
        return result["error"]
    return json.dumps(result, ensure_ascii=False)


SendMessageTool = Tool(
    name="SendMessage",
    searchHint="send message to teammate",
    description="Send a message to a teammate in the current agent team.",
    prompt=(
        "Sends a message to a teammate's mailbox. "
        "Use this to assign tasks, provide context, or request results from teammates.\n"
        "The recipient will see the message on their next turn.\n"
        "Use 'to' to specify the teammate name (e.g. 'ai-p1')."
    ),
    parameters={
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Teammate name receiving the message"},
            "message": {
                "type": "string",
                "description": "Message content (JSON for structured messages, plain text otherwise)",
            },
            "team_name": {"type": "string", "description": "Team name", "default": ""},
            "summary": {"type": "string", "description": "5-10 word summary shown in UI", "default": ""},
        },
        "required": ["to", "message"],
    },
    call=_send_message_call,
    is_read_only=False,
)
