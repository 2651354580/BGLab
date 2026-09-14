"""AskUserQuestion 工具 — 对齐 AskUserQuestionTool.ts。

模型在需要用户决策时调用此工具。
"""

from __future__ import annotations

from bglab.tools.base import Tool


def ask_user(args: dict) -> str:
    """向用户提问。CLI 模式下同步返回第一个选项。"""
    questions = args.get("questions", [])
    if not questions:
        return "Error: questions array is required (1-4 questions)"

    lines = ["\n"]
    for i, q in enumerate(questions[:4]):
        question = q.get("question", "")
        options = q.get("options", [])
        multi = q.get("multiSelect", False)

        lines.append(f"**{i+1}. {question}**")
        if multi:
            lines.append("  (select multiple)")
        for j, opt in enumerate(options[:4]):
            label = opt.get("label", "")
            desc = opt.get("description", "")
            lines.append(f"  [{j+1}] {label} — {desc}")
        lines.append("")

    # CLI 模式下无法交互，返回选项让用户在终端输入
    lines.append("---")
    lines.append("To answer: type the question number and option number.")
    lines.append("Example: `1:2` for question 1, option 2.")
    lines.append("For multi-select: `1:2,3`")
    return "\n".join(lines)


AskUserQuestionTool = Tool(
    name="AskUserQuestion",
    searchHint="ask user a question for clarification",
    description="Ask the user clarifying questions when multiple valid approaches exist.",
    prompt="""Use this tool when you need to ask the user questions during execution.

Usage notes:
- Users will always be able to select "Other" to provide custom text input
- Use multiSelect: true to allow multiple answers
- 1-4 questions per call
- Each question: a header (label), 2-4 options, optional preview content""",
    parameters={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "description": "Questions to ask (1-4)",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "The question text"},
                        "header": {"type": "string", "description": "Short label (max 12 chars)"},
                        "options": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 4,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                                "required": ["label", "description"],
                            },
                        },
                        "multiSelect": {"type": "boolean", "default": False},
                    },
                    "required": ["question", "header", "options", "multiSelect"],
                },
            },
        },
        "required": ["questions"],
    },
    call=ask_user,
    is_read_only=True,
    auto_allow=True,
    plan_allowed=True,
)
