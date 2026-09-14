""

from bglab.skills.base import register_bundled_skill

DEBUG_PROMPT = """# Debug: Diagnose a Reported Issue

Read this session's transcript and debug log to investigate the reported issue.

## Step 1: Locate the transcript

BGLab stores session transcripts at `~/.bglab/transcripts/<cwd-slug>/<session-id>.jsonl`.
Each line is one message (user, assistant, tool_use, tool_result, system-reminder).

Read the last few hundred lines of the most recent transcript. If a transcript path was
given as args, use that; otherwise find the most recent session directory with:

```
ls -lt ~/.bglab/transcripts/
```

## Step 2: Summarize recent activity

Identify the last intersection of tool calls + tool results. Note:
- last user message and what was requested
- last assistant turn: tool calls + reasoning
- last tool result (success / error message / empty string)
- any error messages or tracebacks in the last few turns
- any `loop_intervention` system-reminders (indicates LoopDetector escalated)

## Step 3: Form hypotheses

List 2-3 candidate root causes ranked by likelihood:
1. tool-call failure (permission denied / API error / missing file)
2. infinite loop detected by LoopDetector
3. context budget exhausted (check the final block of the transcript for `tokenUsage`)
4. wrong cwd / wrong file path / wrong model id

## Step 4: Recommend one next step

Write a single actionable next step:
- "retry X with Y"
- "kill PID Z and restart cleanly"
- "increase --max-turns"
- "remove corrupted ~/.bglab/transcripts/<sid>.jsonl"

## Output format

End with:
- Root cause: <one line>
- Evidence: 1-3 cited transcript excerpts as `file:line` + the line content
- Next step: <one line>

Do not kill processes, do not delete transcripts. This is diagnostic, not destructive.
"""


def _debug_prompt(args: str) -> str:
    if args:
        return DEBUG_PROMPT + f"\n## Reported issue\n\n{args}\n"
    return DEBUG_PROMPT


register_bundled_skill(
    name="debug",
    description="Read this session's transcript and debug log to investigate a reported issue.",
    get_prompt=_debug_prompt,
    argument_hint="[issue description or absolute transcript path]",
)
