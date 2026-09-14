""

from bglab.skills.base import register_bundled_skill

STUCK_PROMPT = """# /stuck — diagnose frozen or slow bglab sessions

Investigate why this bglab session is frozen, stuck, or very slow.

## What to look for

Scan for Python processes related to bglab. Process names are typically `python`, `bglab`, or `python.exe`.

Signs of a stuck session:
- **High CPU (>=90%) sustained** — likely an infinite loop. Sample twice, 1-2s apart.
- **Stuck child process** — a hung subprocess (git, pip, etc.) can freeze the parent.
- **Very high RSS (>=4GB)** — possible memory leak.
- **Maxed-out token budget** — check if we hit max_turns or max_output_tokens repeatedly.

## Investigation steps

1. **List Python/BGLab processes** (macOS/Linux):
```
ps -axo pid=,pcpu=,rss=,etime=,state=,comm=,command= | grep -iE '(python|bglab)' | grep -v grep
```
Windows:
```
tasklist | findstr -i python
```

2. **For anything suspicious**, gather more context:
- Child processes: `pgrep -lP <pid>` (macOS/Linux) or `wmic process where (ParentProcessId=<pid>) get ProcessId,Name` (Windows)
- If high CPU: sample again after 1-2s to confirm sustained
- Check the transcript: read the last few hundred lines of `~/.bglab/transcripts/<cwd>/<session>.jsonl`

3. **Check our own state**:
- Run `/stats` to see turn count and token usage
- Run `/compact` to check compaction tracker state
- Run `/trace` to see recent checkpoints if tracing is enabled

## Report

Summarize what you found:
- Which process is stuck and why (most likely cause)
- What you recommend: kill the process? just wait? restart?
- If it's a known pattern (e.g., infinite loop, memory leak, blocking subprocess), mention that

Don't kill or signal any processes — this is diagnostic only. Prioritize the user's argument if they gave one.
"""


def _stuck_prompt(args: str) -> str:
    if args:
        return STUCK_PROMPT + f"\n\n## User-provided context\n\n{args}\n"
    return STUCK_PROMPT


register_bundled_skill(
    name="stuck",
    description="Investigate frozen/stuck/slow bglab sessions on this machine.",
    get_prompt=_stuck_prompt,
    argument_hint="[PID or symptom]",
)
