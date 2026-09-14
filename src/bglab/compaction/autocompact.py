"""AutoCompact — 对齐 autoCompact.ts + compact.ts compactConversation()。

决策器 + 全文摘要（LLM）。

流程:
  1. estimate tokens
  2. check threshold → 是否需要压缩
  3. circuit breaker check
  4. try session memory compact（桩）→ fallback full compact
  5. full compact: 调 LLM 生成摘要
  6. 构造 post-compact messages（boundary + summary + recent）

对齐关系:
  autoCompact.ts:241 autoCompactIfNeeded()
  compact.ts:387 compactConversation()
  compact/prompt.ts:61 BASE_COMPACT_PROMPT
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Sequence

from bglab.compaction.token_counter import estimate_tokens
from bglab.llm.types import ProviderFailureInfo
from bglab.llm.providers import get_model_context_window


RESERVED_OUTPUT_TOKENS = 20_000        
COMPACT_TARGET_RATIO = 0.40            # 目标压缩到 40%
MAX_CONSECUTIVE_FAILURES = 3           # 熔断上限
AUTOCOMPACT_BUFFER_TOKENS = 13_000      
COLLAPSE_COMMIT_RATIO = 0.90            
COLLAPSE_BLOCKING_RATIO = 0.95          
POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000  
POST_COMPACT_MAX_FILES_TO_RESTORE = 5     

# ── Compact 状态追踪 ──

class CompactTracker:
    """追踪 compact 状态 — 对齐 autoCompactTracking。"""
    def __init__(self):
        self.consecutive_failures = 0
        self.total_compacts = 0
        self.last_compact_turn: int | None = None

    def on_success(self, turn: int) -> None:
        self.consecutive_failures = 0
        self.total_compacts += 1
        self.last_compact_turn = turn

    def on_failure(self) -> None:
        self.consecutive_failures += 1

    def is_circuit_broken(self) -> bool:
        return self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES


# ── Compact 结果 ──

class CompactionProviderStopped(Exception):
    """A provider-requested wait exceeds this request's retry window."""

    def __init__(self, provider_failure: ProviderFailureInfo):
        super().__init__("Compaction provider wait exceeds the request retry window")
        self.provider_failure = provider_failure


class CompactResult:
    def __init__(
        self,
        messages: list[dict],
        boundary_marker: dict | None = None,
        summary: str = "",
        pre_tokens: int = 0,
        post_tokens: int = 0,
    ):
        self.messages = messages
        self.boundary_marker = boundary_marker
        self.summary = summary
        self.pre_compact_tokens = pre_tokens
        self.post_compact_tokens = post_tokens


# ═══════════════════════════════════════════════════════════
# 决策器
# ═══════════════════════════════════════════════════════════

def should_autocompact(
    messages: list[dict],
    model: str = "deepseek-chat",
    *,
    request_tokens: int | None = None,
    max_output_tokens: int | None = None,
) -> tuple[bool, int, int]:
    ""
    # The caller supplies the current, already-shaped history. Released tokens
    # are shaping telemetry, not another discount on this input.
    current = estimate_tokens(messages) if request_tokens is None else request_tokens

    
    # effectiveWindow = contextWindow - min(maxOutput, RESERVED_OUTPUT_TOKENS)
    effective_window = (_get_effective_context_window(model) if max_output_tokens is None
                        else _get_context_window(model) - max_output_tokens)
    threshold = effective_window - AUTOCOMPACT_BUFFER_TOKENS
    threshold = max(0, threshold)

    return current > threshold, current, threshold


def _get_effective_context_window(model: str) -> int:
    ""
    raw = _get_context_window(model)
    max_output = _get_max_output_tokens(model)
    reserved = min(max_output, RESERVED_OUTPUT_TOKENS)
    return raw - reserved


def _get_context_window(model: str) -> int:
    return get_model_context_window(model)


def _get_max_output_tokens(model: str) -> int:
    """Existing local output reserve, not the provider's maximum generation.

    Raising input capacity must not raise the requested output budget.
    """
    return 8_192


# ═══════════════════════════════════════════════════════════
# Full Compact — 调 LLM 生成摘要
# ═══════════════════════════════════════════════════════════

# ── Compact Prompt — 对齐 compact/prompt.ts BASE_COMPACT_PROMPT ──

_NO_TOOLS_PREAMBLE = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- Do NOT use Read, Bash, Grep, Glob, Edit, Write, or ANY other tool.
- You already have all the context you need in the conversation above.
- Tool calls will be REJECTED and will waste your only turn — you will fail the task.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.

"""

_DETAILED_ANALYSIS_INSTRUCTION = """Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:

1. Chronologically analyze each message and section of the conversation. For each section thoroughly identify:
   - The user's explicit requests and intents
   - Your approach to addressing the user's requests
   - Key decisions, technical concepts and code patterns
   - Specific details like:
     - file names
     - full code snippets
     - function signatures
     - file edits
   - Errors that you ran into and how you fixed them
   - Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
2. Double-check for technical accuracy and completeness, addressing each required element thoroughly."""

COMPACT_SYSTEM_PROMPT = _NO_TOOLS_PREAMBLE

COMPACT_USER_PROMPT = """Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

""" + _DETAILED_ANALYSIS_INSTRUCTION + """

Your summary should include the following sections:

1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail
2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Pay special attention to the most recent messages and include full code snippets where applicable and include a summary of why this file read or edit is important.
4. Errors and fixes: List all errors that you ran into, and how you fixed them. Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All user messages: List ALL user messages that are not tool results. These are critical for understanding the users' feedback and changing intent.
7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.
8. Current Work: Describe in detail precisely what was being worked on immediately before this summary request, paying special attention to the most recent messages from both user and assistant. Include file names and code snippets where applicable.
9. Optional Next Step: List the next step that you will take that is related to the most recent work you were doing. IMPORTANT: ensure that this step is DIRECTLY in line with the user's most recent explicit requests, and the task you were working on immediately before this summary request. If your last task was concluded, then only list next steps if they are explicitly in line with the users request. Do not start on tangential requests or really old requests that were already completed without confirming with the user first.
                       If there is a next step, include direct quotes from the most recent conversation showing exactly what task you were working on and where you left off. This should be verbatim to ensure there's no drift in task interpretation.

Here's an example of how your output should be structured:

<example>
<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Primary Request and Intent:
   [Detailed description]

2. Key Technical Concepts:
   - [Concept 1]
   - [Concept 2]
   - [...]

3. Files and Code Sections:
   - [File Name 1]
      - [Summary of why this file is important]
      - [Summary of the changes made to this file, if any]
      - [Important Code Snippet]
   - [File Name 2]
      - [Important Code Snippet]
   - [...]

4. Errors and fixes:
    - [Detailed description of error 1]:
      - [How you fixed the error]
      - [User feedback on the error if any]
    - [...]

5. Problem Solving:
   [Description of solved problems and ongoing troubleshooting]

6. All user messages:
    - [Detailed non tool use user message]
    - [...]

7. Pending Tasks:
   - [Task 1]
   - [Task 2]
   - [...]

8. Current Work:
   [Precise description of current work]

9. Optional Next Step:
   [Optional Next step to take]

</summary>
</example>

Please provide your summary based on the conversation so far, following this structure and ensuring precision and thoroughness in your response.

There may be additional summarization instructions provided in the included context. If so, remember to follow these instructions when creating the above summary. Examples of instructions include:
<example>
## Compact Instructions
When summarizing the conversation focus on typescript code changes and also remember the mistakes you made and how you fixed them.
</example>

<example>
# Summary instructions
When you are using compact - please focus on test output and code changes. Include file reads verbatim.
</example>

Conversation to summarize:
---
{conversation}
---"""


@dataclass(frozen=True)
class CompactionProfile:
    """Prompt and recovery policy used at the fixed compaction position."""

    name: str
    system_prompt: str
    user_prompt: str
    keep_recent: int = 5
    max_output_tokens: int = 4096
    timeout_seconds: float | None = None
    disable_thinking: bool = False
    deterministic_fallback_summary: str | Callable[[str], str] | None = None
    render_history: Callable[[list[dict]], str] | None = None
    select_summary: Callable[[str, str], str | None] | None = None
    allow_session_memory_compact: bool = True
    select_recent_indexes: Callable[
        [list[dict], int, str], Sequence[int]
    ] | None = None
    render_recent_context: Callable[[list[dict]], str] | None = None
    thinking_effort: str | None = None
    build_summary: Callable[[str], str] | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("compaction profile name must be non-empty")
        if self.keep_recent < 0:
            raise ValueError("compaction keep_recent must be non-negative")
        if type(self.max_output_tokens) is not int or self.max_output_tokens <= 0:
            raise ValueError("compaction max_output_tokens must be a positive integer")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("compaction timeout must be positive")
        if self.thinking_effort not in {None, "low", "high", "max"}:
            raise ValueError("compaction thinking_effort must be low, high, max, or None")
        for field in ("render_history", "select_summary", "select_recent_indexes", "render_recent_context", "build_summary"):
            value = getattr(self, field)
            if value is not None and not callable(value):
                raise ValueError(f"compaction {field} must be callable")
        fallback = self.deterministic_fallback_summary
        if fallback is not None and not isinstance(fallback, str) and not callable(fallback):
            raise ValueError("compaction fallback must be text or callable")


CODE_COMPACTION_PROFILE = CompactionProfile(
    name="code",
    system_prompt=COMPACT_SYSTEM_PROMPT,
    user_prompt=COMPACT_USER_PROMPT,
)


async def full_compact(
    messages: list[dict],
    model: str = "deepseek-chat",
    cwd: str | None = None,
    keep_recent: int | None = None,
    profile: CompactionProfile = CODE_COMPACTION_PROFILE,
    provider_slot: Any = None,
    completion: Any = None,
    observation_callback: Any = None,
    request_session_id: str | None = None,
) -> CompactResult | None:
    """Compact older history with the selected profile, keeping recent exchanges.

    Args:
        messages: 完整对话历史
        model: 用哪个模型做摘要（和 coding 同一个）
        cwd: 工作目录
        keep_recent: optional caller floor for recent messages
        profile: prompt/recovery policy selected by the query-loop profile

    Returns:
        CompactResult or None (失败时)

    Raises:
        CompactionProviderStopped: provider Retry-After exceeds the retry window.
    """

    pre_tokens = estimate_tokens(messages)

    effective_keep_recent = max(
        profile.keep_recent,
        profile.keep_recent if keep_recent is None else keep_recent,
    )

    # 分离出需要摘要的 messages 和保留的 recent messages. Profiles may
    # preserve typed authority anchors instead of a blind tail count.
    if profile.select_recent_indexes is not None:
        selected = tuple(profile.select_recent_indexes(
            messages,
            effective_keep_recent,
            model,
        ))
        if (
            any(
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < len(messages)
                for index in selected
            )
            or tuple(sorted(set(selected))) != selected
        ):
            raise ValueError("compaction recent selector returned invalid indexes")
        selected_set = set(selected)
        recent = [messages[index] for index in selected]
        to_compact = [
            message for index, message in enumerate(messages)
            if index not in selected_set
        ]
    elif len(messages) > effective_keep_recent:
        to_compact = messages[:-effective_keep_recent]
        recent = messages[-effective_keep_recent:]
    else:
        to_compact = messages
        recent = []

    if len(to_compact) < 2:
        return None  # 太少内容，不值得 compact

    # 把 to_compact 转成文本
    conv_text = (profile.render_history or _messages_to_text)(to_compact)
    if profile.render_history is None and len(conv_text) < 500:
        return None

    # The selected profile owns fallback content. Game retains public evidence
    # without reviving old board snapshots. Provider waits beyond the allowed
    # retry window still stop at the existing provider-recovery boundary;
    # normal/code 其余失败保持原来的 None 语义。
    summary_source = "profile" if profile.build_summary is not None else "llm"
    if profile.build_summary is not None:
        summary = profile.build_summary(conv_text)
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("compaction summary builder must return non-empty text")
    else:
        try:
            summary = await _call_compact_llm(
                conv_text, model, profile=profile, provider_slot=provider_slot,
                completion=completion, observation_callback=observation_callback,
                request_session_id=request_session_id,
                recent_context=(
                    profile.render_recent_context(recent)
                    if profile.render_recent_context is not None else ""
                ),
            )
        except CompactionProviderStopped:
            raise
        except Exception:
            if profile.deterministic_fallback_summary is None:
                raise
            summary = None
    if not summary or not summary.strip():
        if profile.deterministic_fallback_summary is None:
            return None
        fallback = profile.deterministic_fallback_summary
        summary = fallback(conv_text) if callable(fallback) else fallback
        summary_source = "deterministic_fallback"

    # 构造 boundary marker（对齐源码 boundaryMarker）
    boundary = {
        "type": "compact_boundary",
        "timestamp": datetime.now().isoformat(),
        "pre_compact_tokens": pre_tokens,
        "messages_compacted": len(to_compact),
        "messages_kept": len(recent),
        "summary": summary,
        # Preserved segment: index-based relink since no UUID system
        "preserved_segment": {
            "anchor_idx": 0,          # boundary itself is anchor (idx 0 in post_messages)
            "head_idx": 1,            # first keept message (idx 1 in post_messages)
            "tail_idx": len(recent),  # last keept message
        },
    }
    if summary_source != "llm":
        boundary["compact_type"] = f"{profile.name}_{summary_source}"
        boundary["summary_source"] = summary_source

    # 构造 post-compact messages
    post_messages = [
        {
            "role": "user",
            "content": [{
                "type": "text",
                "text": (
                    "<system-reminder>\n"
                    "[CONVERSATION COMPRESSED]\n"
                    f"Previous {len(to_compact)} messages ({pre_tokens} tokens) "
                    "have been compressed into the summary below.\n\n"
                    f"{summary}\n"
                    "</system-reminder>"
                ),
            }],
            "_is_meta": True,
            "_compact_boundary": boundary,
        },
        *recent,
    ]

    post_tokens = estimate_tokens(post_messages)

    return CompactResult(
        messages=post_messages,
        boundary_marker=boundary,
        summary=summary,
        pre_tokens=pre_tokens,
        post_tokens=post_tokens,
    )


# ═══════════════════════════════════════════════════════════
# 主入口 — autoCompactIfNeeded
# ═══════════════════════════════════════════════════════════

async def auto_compact_if_needed(
    messages: list[dict],
    tracker: CompactTracker,
    *,
    model: str = "deepseek-chat",
    cwd: str | None = None,
    turn_count: int = 1,
    profile: CompactionProfile = CODE_COMPACTION_PROFILE,
    provider_slot: Any = None,
    completion: Any = None,
    observation_callback: Any = None,
    request_session_id: str | None = None,
    request_tokens: int | None = None,
    max_output_tokens: int | None = None,
) -> tuple[list[dict], CompactTracker, CompactResult | None]:
    """主入口 — 对齐 autoCompact.ts:241 autoCompactIfNeeded()。

    Args:
        messages: current history after context shaping, without the request head
        profile: content/recovery policy selected at the shared compact slot

    Returns:
        (compacted_messages_or_original, tracker, result_or_None)

    Raises:
        CompactionProviderStopped: provider Retry-After exceeds the retry window.
    """
    # Circuit breaker
    if tracker.is_circuit_broken():
        return messages, tracker, None

    needs_compact, current_tokens, threshold = should_autocompact(
        messages, model, request_tokens=request_tokens, max_output_tokens=max_output_tokens,
    )
    if not needs_compact:
        return messages, tracker, None

    print(f"[autocompact] triggered: {current_tokens} > {threshold} tokens")
    try:
        from bglab.engine.trace import checkpoint as _tcheck
        _tcheck("autocompact_decision", triggered=True, current=current_tokens, threshold=threshold)
    except Exception:
        pass

    # ① Profile-selected session-memory shortcut.
    if profile.allow_session_memory_compact:
        sm_result = await try_session_memory_compact(messages)
        if sm_result:
            tracker.on_success(turn_count)
            return sm_result.messages, tracker, sm_result

    # ② Fallback: full compact
    try:
        result = await full_compact(
            messages, model=model, cwd=cwd, profile=profile,
            provider_slot=provider_slot, completion=completion,
            observation_callback=observation_callback,
            request_session_id=request_session_id,
        )
        if result:
            tracker.on_success(turn_count)
            print(f"[autocompact] full compact done: {result.pre_compact_tokens} -> {result.post_compact_tokens}")
            return result.messages, tracker, result
        else:
            tracker.on_failure()
            print(f"[autocompact] full compact failed, failures={tracker.consecutive_failures}")
    except CompactionProviderStopped:
        raise
    except Exception as e:
        tracker.on_failure()
        print(f"[autocompact] error: {e}, failures={tracker.consecutive_failures}")

    # 熔断
    if tracker.is_circuit_broken():
        print(f"[autocompact] CIRCUIT BREAKER ({MAX_CONSECUTIVE_FAILURES} failures)")

    return messages, tracker, None


# ═══════════════════════════════════════════════════════════
# 内部 helpers
# ═══════════════════════════════════════════════════════════

def _messages_to_text(messages: list[dict]) -> str:
    """把 messages 转成可读文本，供 compact LLM 阅读。"""
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")

        # A compact boundary stores durable history even though it is rendered
        # as a meta message. Preserve it for the next generation of summaries;
        # transient system attachments still do not belong to the conversation.
        if msg.get("_is_meta") and not isinstance(msg.get("_compact_boundary"), dict):
            continue

        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    btype = block.get("type", "")
                    if btype == "text":
                        t = block.get("text", "")
                        if t.strip():
                            parts.append(t)
                    elif btype == "tool_use":
                        parts.append(f"[tool: {block.get('name', '?')}({_brief(block.get('input', {}))})]")
                    elif btype == "tool_result":
                        t = str(block.get("content", ""))
                        parts.append(f"[tool_result: {t[:200]}...]" if len(t) > 200 else f"[tool_result: {t}]")
            text = "\n".join(parts) if parts else ""
        elif isinstance(content, str):
            text = content
        else:
            text = str(content)

        if text.strip():
            lines.append(f"[{role}]: {text}")

    return "\n\n".join(lines)


def _brief(d: dict) -> str:
    """Keep small JSON inputs intact; label bounded prefixes as incomplete."""
    if isinstance(d, dict) and not d:
        return ""

    def is_json_value(value: Any) -> bool:
        if isinstance(value, dict):
            return all(isinstance(k, str) and is_json_value(v) for k, v in value.items())
        if isinstance(value, list):
            return all(is_json_value(v) for v in value)
        return value is None or isinstance(value, (str, bool, int, float))

    unavailable = "[tool input unavailable: not JSON-serializable]"
    try:
        if not is_json_value(d):
            return unavailable
        text = json.dumps(d, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return unavailable
    limit = 512
    if len(text) <= limit:
        return text
    prefix = "[incomplete tool input; JSON prefix only] "
    return prefix + text[:limit - len(prefix)]


def _select_compact_summary(text: str) -> str | None:
    """Select public summary text; malformed reserved tags never become history."""
    import re

    text = text.strip()
    reserved = {"analysis", "summary"}
    tags = []
    # Only these two protocol tags have meaning. Other markup remains text.
    for match in re.finditer(r"<\s*/?\s*([a-z][a-z0-9:_-]*)[^<>]*(?:>|(?=<)|$)", text, re.I):
        name = match.group(1).lower()
        fragment = match.group(0)
        partial_name = not fragment.endswith(">") and any(
            tag.startswith(name) for tag in reserved
        )
        if name not in reserved and not partial_name:
            continue
        canonical = re.fullmatch(r"<(/?)(analysis|summary)>", fragment, re.I)
        if canonical is None:
            return None
        tags.append((match.start(), match.end(), canonical.group(2).lower(),
                     bool(canonical.group(1))))

    if not tags:
        return text

    active = None
    seen = set()
    summary = None
    summary_start = 0
    for start, end, name, closing in tags:
        if not closing:
            if active is not None or name in seen:
                return None  # Nested or repeated protocol blocks are ambiguous.
            seen.add(name)
            active = name
            if name == "summary":
                summary_start = end
        else:
            if active != name:
                return None
            if name == "summary":
                summary = text[summary_start:start].strip()
            active = None
    return summary if active is None and summary else None


async def _call_compact_llm(
    conversation_text: str,
    model: str,
    *,
    profile: CompactionProfile = CODE_COMPACTION_PROFILE,
    provider_slot: Any = None,
    completion: Any = None,
    observation_callback: Any = None,
    request_session_id: str | None = None,
    recent_context: str = "",
) -> str | None:
    """调 LLM 做摘要 — 使用和主 agent 相同的 API。

    用非流式调用。通常失败返回 None；超过重试窗口的 Provider 等待
    抛出 CompactionProviderStopped，避免摘要回退后立即再次请求。
    """
    try:
        if completion is None:
            from bglab.llm.client import complete_text
            completion = complete_text

        sys_prompt = profile.system_prompt
        usr_prompt = profile.user_prompt
        timeout_kwargs = (
            {"timeout_seconds": profile.timeout_seconds}
            if profile.timeout_seconds is not None
            else {}
        )
        text = await completion(
            provider_slot=provider_slot,
            observation_callback=observation_callback,
            request_session_id=request_session_id,
            system_prompt=sys_prompt,
            model=model,
            messages=[
                {"role": "user", "content": usr_prompt.format(
                    conversation=conversation_text,
                    recent_context=recent_context,
                )},
            ],
            temperature=0.0,
            max_tokens=profile.max_output_tokens,
            require_complete_response=True,
            # Summary content must be completed public output. This setting is
            # a provider request option, not proof that reasoning was disabled.
            disable_thinking=profile.disable_thinking,
            **({"thinking_effort": profile.thinking_effort} if profile.thinking_effort is not None else {}),
            **timeout_kwargs,
        )
        if profile.select_summary is not None:
            return profile.select_summary(text, conversation_text)
        return _select_compact_summary(text)

    except Exception as e:
        from bglab.llm.client import _provider_failure_info
        from bglab.llm.retry import MAX_RETRY_DELAY_SECONDS, classify_provider_error

        decision = classify_provider_error(e)
        if (
            decision.retry_after_seconds is not None
            and decision.retry_after_seconds > MAX_RETRY_DELAY_SECONDS
        ):
            # complete_text has already delivered its observation in finally.
            # An injected completion may omit that callback; classify its
            # exception too, without inventing usage or another observation.
            raise CompactionProviderStopped(_provider_failure_info(e, decision)) from None
        if decision.retry_after_seconds is not None and decision.retry_after_seconds > 0:
            # Fallback can issue a decision to the same provider immediately.
            # Honor its minimum delay once, with cancellation, before fallback.
            await asyncio.sleep(decision.retry_after_seconds)
        print(f"[autocompact] LLM call failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════
# Session Memory Compact — 对齐 sessionMemoryCompact.ts
# ═══════════════════════════════════════════════════════════

SM_COMPACT_MIN_TOKENS = 10_000
SM_COMPACT_MIN_MESSAGES = 5
SM_COMPACT_MAX_TOKENS = 40_000
SM_COMPACT_MIN_CONTENT = 200  # 最少 session memory 内容量才触发


async def try_session_memory_compact(
    messages: list[dict],
    session_id: str | None = None,
) -> CompactResult | None:
    """对齐 sessionMemoryCompact.ts trySessionMemoryCompaction()。

    用 stop hooks 提取的 session memory 替代 LLM 摘要，
    避免 compact API 调用，降低成本和延迟。

    流程:
      1. 读取 session memory 文件
      2. 检查内容是否足够（≥200 chars）
      3. 计算保留消息索引（min tokens + min messages）
      4. 构建 CompactResult（不调 LLM）
    """
    memory_content = _read_session_memory(session_id)
    if not memory_content or len(memory_content) < SM_COMPACT_MIN_CONTENT:
        return None

    pre_tokens = estimate_tokens(messages)

    # 计算保留消息索引 — 对齐 calculateMessagesToKeepIndex
    keep_from = _calculate_keep_index(messages, min_tokens=SM_COMPACT_MIN_TOKENS,
                                      min_messages=SM_COMPACT_MIN_MESSAGES,
                                      max_tokens=SM_COMPACT_MAX_TOKENS)

    to_summarize = messages[:keep_from]
    recent = messages[keep_from:]

    if len(to_summarize) < 2:
        return None

    boundary = {
        "type": "compact_boundary",
        "timestamp": datetime.now().isoformat(),
        "pre_compact_tokens": pre_tokens,
        "messages_compacted": len(to_summarize),
        "messages_kept": len(recent),
        "compact_type": "session_memory",
        "preserved_segment": {
            "anchor_idx": 0,
            "head_idx": 1,
            "tail_idx": len(recent),
        },
    }

    # 用 session memory 作为摘要
    summary_message = {
        "role": "user",
        "content": [{
            "type": "text",
            "text": (
                "<system-reminder>\n"
                "[SESSION MEMORY — CONVERSATION COMPRESSED]\n"
                f"Previous {len(to_summarize)} messages ({pre_tokens} tokens) "
                "have been compressed using session memory.\n\n"
                f"{memory_content}\n"
                "</system-reminder>"
            ),
        }],
        "_is_meta": True,
        "_compact_boundary": boundary,
    }

    post_messages = [summary_message, *recent]
    post_tokens = estimate_tokens(post_messages)

    return CompactResult(
        messages=post_messages,
        boundary_marker=boundary,
        summary=memory_content,
        pre_tokens=pre_tokens,
        post_tokens=post_tokens,
    )


def _read_session_memory(session_id: str | None = None) -> str | None:
    """读取 session memory 文件。

    查找顺序:
      1. 指定 session_id → ~/.bglab/sessions/{session_id}/memory.md
      2. 当前 cwd → <cwd>/.bglab/sessions/current/memory.md
      3. 全局 → ~/.bglab/memory/session.md
    """
    from pathlib import Path

    candidates = []
    home = Path.home()

    if session_id:
        candidates.append(home / ".bglab" / "sessions" / session_id / "memory.md")

    try:
        cwd_name = Path.cwd().name
        candidates.append(home / ".bglab" / "sessions" / cwd_name / "memory.md")
    except Exception:
        pass

    candidates.append(home / ".bglab" / "memory" / "session.md")

    for path in candidates:
        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    return content
            except Exception:
                continue
    return None


def _calculate_keep_index(
    messages: list[dict],
    min_tokens: int = SM_COMPACT_MIN_TOKENS,
    min_messages: int = SM_COMPACT_MIN_MESSAGES,
    max_tokens: int = SM_COMPACT_MAX_TOKENS,
) -> int:
    """计算保留消息的起始索引 — 对齐 calculateMessagesToKeepIndex()。

    从尾部向前扩展，直到达到 min_tokens/min_messages 或超过 max_tokens。
    保留的消息 = messages[return_value:]
    """
    if not messages:
        return 0

    total = len(messages)
    acc_tokens = 0
    text_msg_count = 0
    keep_from = total

    for i in range(total - 1, -1, -1):
        msg = messages[i]
        # 跳过 meta/system 消息
        if msg.get("_is_meta"):
            continue

        content = msg.get("content", "")
        if isinstance(content, list):
            text = "".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        elif isinstance(content, str):
            text = content
        else:
            text = ""

        if text.strip():
            text_msg_count += 1

        msg_tokens = estimate_tokens([msg])
        acc_tokens += msg_tokens
        keep_from = i

        if acc_tokens >= min_tokens and text_msg_count >= min_messages:
            break
        if acc_tokens >= max_tokens:
            break

    # 至少保留 2 条
    while keep_from > total - 2 and keep_from > 0:
        keep_from -= 1

    return keep_from


# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════

COLLAPSE_COLLAPSED_STUB = "[Collapsed: {n} identical tool results]"


def _is_collapse_enabled() -> bool:
    ""
    import os
    val = os.environ.get("NANO_CONTEXT_COLLAPSE", "")
    return val in ("1", "true", "yes")


def should_collapse(
    messages: list[dict],
    model: str = "deepseek-chat",
) -> tuple[bool, int, int]:
    ""
    current = estimate_tokens(messages)
    effective_window = _get_effective_context_window(model)
    commit_threshold = int(effective_window * COLLAPSE_COMMIT_RATIO)
    return current > commit_threshold, current, commit_threshold


def apply_context_collapse(
    messages: list[dict],
    model: str = "deepseek-chat",
) -> tuple[list[dict], int]:
    ""
    if not _is_collapse_enabled():
        return list(messages), 0

    pre_tokens = estimate_tokens(messages)
    collapsed = _collapse_repetitive_results(list(messages))
    post_tokens = estimate_tokens(collapsed)
    saved = max(0, pre_tokens - post_tokens)

    if saved > 0:
        print(f"[context_collapse] collapsed {saved} tokens")

    return collapsed, saved


def _collapse_repetitive_results(messages: list[dict]) -> list[dict]:
    """Collapse repetitive tool_result blocks in place.
    Strategy: group consecutive user messages with tool_results by tool_use name.
    If same tool appears >=3 times with same-ish output, collapse."""
    from collections import Counter as _Counter

    # Build a fingerprint for each tool_result
    tool_fingerprints: list[tuple[int, str, str]] = []  # (idx, tool_name, fingerprint)
    for i, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id", "")
                text = str(block.get("content", ""))
                # Find preceding assistant message with tool_use of same id
                for j in range(i - 1, max(i - 5, -1), -1):
                    prev = messages[j] if j >= 0 else {}
                    if prev.get("role") != "assistant":
                        continue
                    pcontent = prev.get("content", [])
                    if not isinstance(pcontent, list):
                        continue
                    for pb in pcontent:
                        if isinstance(pb, dict) and pb.get("type") == "tool_use":
                            if pb.get("id", "") == tid:
                                tname = pb.get("name", "?")
                                # Fingerprint: tool name + first 80 chars of result
                                fp = f"{tname}:{text[:80]}"
                                tool_fingerprints.append((i, tname, fp))
                                break
                    break

    # Count fingerprints and find repetitive ones
    fingerprint_counts = _Counter(fp for _, _, fp in tool_fingerprints)
    repetitive_fps = {fp for fp, count in fingerprint_counts.items() if count >= 3}

    if not repetitive_fps:
        return messages

    result = list(messages)
    for msg in result:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        # Build fingerprint for this message's tool_result
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id", "")
                text = str(block.get("content", ""))
                # Find matching tool name
                for i in range(len(messages)):
                    if messages[i].get("role") != "assistant":
                        continue
                    pcontent = messages[i].get("content", [])
                    if not isinstance(pcontent, list):
                        continue
                    for pb in pcontent:
                        if isinstance(pb, dict) and pb.get("type") == "tool_use":
                            if pb.get("id", "") == tid:
                                fp = f"{pb.get('name', '?')}:{text[:80]}"
                                if fp in repetitive_fps:
                                    # Collapse this content
                                    block["content"] = COLLAPSE_COLLAPSED_STUB.format(n=fingerprint_counts[fp])
                                break
        new_content = []
        prev_stub = None
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                this_content = block.get("content", "")
                if this_content == prev_stub:
                    continue  # skip duplicate
                prev_stub = this_content
            else:
                prev_stub = None
            new_content.append(block)
        msg["content"] = new_content

    return result


# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════

def drain_collapses(
    messages: list[dict],
    model: str = "deepseek-chat",
) -> tuple[list[dict], int]:
    ""
    if not _is_collapse_enabled():
        return list(messages), 0

    from collections import Counter as _Counter

    pre_tokens = estimate_tokens(messages)
    result = list(messages)

    # First pass: collapse >= 2 patterns (more aggressive)
    try:
        # Re-run with lower threshold by modifying our approach
        tool_counts = _Counter()
        for msg in result:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    text = str(block.get("content", ""))
                    tool_counts[text[:80]] += 1

        for msg in result:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    text = str(block.get("content", ""))
                    fp = text[:80]
                    count = tool_counts.get(fp, 0)
                    if count >= 2 and COLLAPSE_COLLAPSED_STUB.format(n=1) not in str(block.get("content", "")):
                        block["content"] = COLLAPSE_COLLAPSED_STUB.format(n=count)
    except Exception:
        pass

    post_tokens = estimate_tokens(result)
    saved = max(0, pre_tokens - post_tokens)
    return result, saved
