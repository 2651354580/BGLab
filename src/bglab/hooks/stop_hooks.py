"""Stop Hooks — 对齐 query/stopHooks.ts handleStopHooks()。

post-turn 流水线:
  ① 保存 cache-safe snapshot — side_question / post-turn forks 复用 prompt cache
  ② Prompt suggestion — 后台 LLM 生成 3 条提示建议
  ③ Memory extraction — 后台提取记忆，cursor-based + throttle + 互斥
     - mode "coding": 项目记忆 (user/feedback/project/reference) → ~/.bglab/memory/<cwd>/
     - mode "game":   游戏记忆 (game) → ~/.bglab/memory/games/<engine>/
     - mode None:     跳过
  ④ Auto-dream — 跨 session 记忆整合 (24h+5sessions 门控)
  ⑤ 自定义 Stop hooks — command 类型 + Python callable 类型
    - command: subprocess, exit code 2 = blocking_error
    - callable: async (extra) -> dict, "blocking_error" key = 塞回提示

关键变更: 所有可变状态收进 StopHooksState dataclass，不再用模块级 global。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from bglab.engine.trace import checkpoint, memory_extraction
from bglab.hooks.state import StopHooksState

logger = logging.getLogger("bglab.hooks")

AUTO_DREAM_HOURS = 24
AUTO_DREAM_MIN_SESSIONS = 5
MEMORY_EXTRACT_EVERY_N_TURNS = 1
GAME_MEMORY_EXTRACT_EVERY_N_TURNS = 8


@dataclass
class CacheSafeSnapshot:
    """对齐 forkedAgent.ts CacheSafeParams slot (line 70-72)。"""
    system_prompt: str
    user_context: dict[str, str]
    system_context: dict[str, str]
    messages: list[dict]
    cwd: str
    model: str = "deepseek-chat"
    saved_at: float = field(default_factory=time.time)


@dataclass
class StopHookConfig:
    command: str
    timeout: int = 60


@dataclass
class StopHookResult:
    blocking_errors: list[dict] = field(default_factory=list)
    prevent_continuation: bool = False
    system_messages: list[str] = field(default_factory=list)
    hook_outputs: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════

async def handle_stop_hooks(
    messages_for_query: list[dict],
    assistant_messages: list[dict],
    tool_use_context,
    *,
    state: StopHooksState | None = None,
    system_prompt: str = "",
    user_context: dict[str, str] | None = None,
    system_context: dict[str, str] | None = None,
    cwd: str = "",
    model: str = "deepseek-chat",
    hooks: list | None = None,
    skip_background: bool = False,
    memory_extraction: str = "coding",  # "coding" | "game" | None
    callable_failure_policy: str = "isolate",
    extra: dict | None = None,  # 传给 callable hook 的上下文
) -> StopHookResult:
    """Handle stop hooks.

    Args:
        skip_background: 跳过 ①-④ (game mode 用)
        memory_extraction: "coding" → 项目记忆, "game" → 游戏记忆, None → 跳过
        hooks: list of StopHookConfig (shell command) or callable (Python async function)
        extra: extra context dict for callable hooks (deps, game_id, engine, etc.)
    """
    result = StopHookResult()
    if state is None:
        state = StopHooksState()
    if extra is None:
        extra = {}

    # ① 保存快照
    snapshot = CacheSafeSnapshot(
        system_prompt=system_prompt,
        user_context=user_context or {},
        system_context=system_context or {},
        messages=list(messages_for_query) + list(assistant_messages),
        cwd=cwd,
        model=model,
    )
    state.last_snapshot = snapshot

    extra["snapshot"] = snapshot

    if not skip_background:
        # ②③④ 后台任务 fire-and-forget (coding mode only)
        asyncio.ensure_future(_background_prompt_suggestion(snapshot, assistant_messages))
        if memory_extraction == "coding":
            asyncio.ensure_future(
                _background_memory_extraction(
                    state,
                    snapshot,
                    assistant_messages,
                )
            )
            asyncio.ensure_future(_background_auto_dream(state, snapshot))

        # ⑤ 用户自定义 Stop hooks (shell command)
        if hooks:
            for hook in hooks:
                r = await _execute_single_stop_hook(hook, assistant_messages, extra)
                if r.get("blocking_error"):
                    result.blocking_errors.append({
                        "role": "user", "content": [{"type": "text", "text": r["blocking_error"]}],
                        "_is_meta": True,
                    })
                if r.get("prevent_continuation"):
                    result.prevent_continuation = True
                if r.get("system_message"):
                    result.system_messages.append(r["system_message"])
                if r.get("output"):
                    result.hook_outputs.append(r["output"])

    else:
        # ── Game mode: ③ game memory + ⑤ callable hooks ──
        if memory_extraction == "game":
            asyncio.ensure_future(
                _background_game_memory_extraction(state, snapshot, assistant_messages, extra)
            )

        # ⑤ Ordered Python hooks.  The first blocking result short-circuits the
        # later memory/evolution positions for this model turn.
        if hooks:
            for hook in hooks:
                if callable(hook):
                    try:
                        r = await hook(extra)
                        if isinstance(r, dict):
                            if r.get("blocking_error"):
                                result.blocking_errors.append({
                                    "role": "user",
                                    "content": [{"type": "text", "text": r["blocking_error"]}],
                                    "_is_meta": True,
                                })
                            if r.get("prevent_continuation"):
                                result.prevent_continuation = True
                            if r.get("blocking_error") or r.get("prevent_continuation"):
                                break
                    except Exception as e:
                        if callable_failure_policy == "raise":
                            raise
                        logger.warning(f"Game hook {getattr(hook, '__name__', hook)} failed: {e}")

    return result


# ── Snapshot helpers (no globals) ──

def get_last_snapshot(state: StopHooksState) -> CacheSafeSnapshot | None:
    return state.last_snapshot  # type: ignore[return-value]


# ═══════════════════════════════════════════════════════════
# ② Prompt suggestion
# ═══════════════════════════════════════════════════════════

async def _background_prompt_suggestion(snapshot, assistant_messages) -> None:
    try:
        last_text = _last_assistant_text(assistant_messages)
        if not last_text or len(last_text) < 20:
            return
        prompt = _build_prompt_suggestion_prompt(snapshot, assistant_messages)
        suggestions = await _call_small_llm(
            prompt, max_tokens=200, model=snapshot.model,
        )
        if suggestions:
            logger.debug(f"Prompt suggestions generated: {len(suggestions.split(chr(10)))} lines")
    except Exception as e:
        logger.debug(f"Prompt suggestion failed (non-critical): {e}")


# ═══════════════════════════════════════════════════════════
# ③ Memory extraction — coding mode
# ═══════════════════════════════════════════════════════════

async def _background_memory_extraction(
    hs: StopHooksState, snapshot, assistant_messages,
) -> None:
    """后台任务 — 提取编码记忆。对齐 extractMemories.ts runExtraction()。"""
    hs.turns_since_last_extraction += 1
    if hs.turns_since_last_extraction < MEMORY_EXTRACT_EVERY_N_TURNS:
        return
    hs.turns_since_last_extraction = 0

    try:
        from bglab.memory.memdir import (
            get_memory_dir, scan_memory_files,
            write_memory_file, update_entrypoint,
        )
        from bglab.memory.memory_prompt import build_extraction_prompt

        mem_dir = get_memory_dir(snapshot.cwd)

        new_count = _count_new_messages(hs, snapshot.messages)
        if new_count < 2:
            logger.debug(f"Memory extraction skipped: only {new_count} new messages")
            return

        if _main_agent_wrote_memory(hs, snapshot.messages):
            logger.debug("Memory extraction skipped: main agent wrote memory, advancing cursor")
            _advance_cursor(hs, snapshot.messages)
            return

        existing = scan_memory_files(mem_dir)
        manifest = "\n".join(
            f"- {m['filename']}: {m['description']}" for m in existing
        )

        recent_messages = _memory_extraction_messages(
            hs,
            snapshot.messages,
        )
        user_prompt = (
            build_extraction_prompt(new_count, manifest, skip_index=False)
            + "\n\n## Recent model-visible messages\n\n"
            + _render_memory_messages(recent_messages)
        )

        logger.info(
            f"Memory extraction starting: {new_count} new messages, "
            f"{len(existing)} existing memories"
        )

        memories_text = await _call_small_llm(
            user_prompt, max_tokens=1200, model=snapshot.model,
        )
        if not memories_text:
            logger.info("Memory extraction: LLM returned empty, cursor NOT advanced")
            return

        blocks = memories_text.split("\n---")
        written = 0
        for block in blocks:
            block = block.strip()
            if not block or not ("### " in block[:10]):
                continue
            lines = block.split("\n")
            name_line = lines[0].replace("### ", "").strip()
            body = "\n".join(lines[1:]).strip()
            if not body:
                continue

            filename = re.sub(r'[^a-zA-Z0-9_]', '_', name_line.lower())[:60] + ".md"
            fm_type = _infer_type(body)

            try:
                write_memory_file(mem_dir, filename, {
                    "name": name_line, "description": body[:120], "type": fm_type,
                }, body)
                update_entrypoint(mem_dir, filename, body[:120])
                written += 1
            except OSError as e:
                logger.error(f"Failed to write memory file {filename}: {e}")

        if written > 0:
            hs.memory_extract_count += 1
            _advance_cursor(hs, snapshot.messages)
            memory_extraction(written=written, new_msgs=new_count, existing=len(existing))
            logger.info(
                f"Memory extraction #{hs.memory_extract_count}: {written} new, "
                f"cursor advanced."
            )
        else:
            logger.info("Memory extraction: 0 blocks parsed, cursor NOT advanced")
    except Exception as e:
        logger.error(
            f"Memory extraction failed (cursor NOT advanced, will retry): {e}",
            exc_info=True,
        )


def _memory_extraction_messages(
    hs: StopHooksState,
    messages: list[dict],
) -> list[dict]:
    cursor = hs.last_memory_message_uuid
    visible = [
        message
        for message in messages
        if message.get("role") in {"user", "assistant"}
        and not message.get("_is_meta")
    ]
    if cursor is None:
        return visible
    if cursor.startswith("count:"):
        try:
            processed = max(0, int(cursor.removeprefix("count:")))
        except ValueError:
            return visible
        return visible[processed:] if processed <= len(visible) else visible
    for index, message in enumerate(visible):
        if message.get("_msg_id") == cursor:
            return visible[index + 1:]
    return visible


def _render_memory_messages(messages: list[dict], max_chars: int = 24_000) -> str:
    rendered: list[str] = []
    for message in messages:
        fragments: list[str] = []
        content = message.get("content", "")
        if isinstance(content, str):
            fragments.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and str(block.get("text", "")).strip():
                    fragments.append(str(block["text"]))
                elif block.get("type") == "tool_use" and block.get("name"):
                    fragments.append(f"[used tool {block['name']}]")
        text = "\n".join(fragment.strip() for fragment in fragments if fragment.strip())
        if text:
            rendered.append(f"[{message.get('role')}]: {text}")
    joined = "\n\n".join(rendered)
    return joined[-max_chars:]


# ═══════════════════════════════════════════════════════════
# ③b Game memory extraction
# ═══════════════════════════════════════════════════════════

async def _background_game_memory_extraction(
    hs: StopHooksState, snapshot, assistant_messages, extra: dict,
) -> None:
    """后台任务 — 提取游戏记忆。对齐 extractMemories.ts runExtraction()。

    与 coding 记忆提取的区别:
      - 单独的游戏 cursor (hs.last_game_memory_message_uuid)
      - 单独的游戏 throttle (hs.game_turns_since_last_extraction)
      - 类型只有 'game' (GAME_TYPE_SECTION)
      - 不检查 _main_agent_wrote_memory (game agent 只用 tool calls)
      - 写到 ~/.bglab/memory/games/<engine>/
    """
    force = bool(extra.get("force"))
    hs.game_turns_since_last_extraction += 1
    if (
        not force
        and hs.game_turns_since_last_extraction < GAME_MEMORY_EXTRACT_EVERY_N_TURNS
    ):
        return
    hs.game_turns_since_last_extraction = 0

    engine = extra.get("engine", "splendor")
    if not engine:
        return
    game_id = str(extra.get("game_id", "") or "")
    agent_id = str(extra.get("agent_id", "") or "")
    event_callback = extra.get("event_callback")

    try:
        from bglab.memory.memdir import (
            get_game_memory_dir,
            normalize_game_memory_lifecycle,
            scan_memory_files,
            sync_memory_index,
        )

        mem_dir_path = get_game_memory_dir(engine, agent_id or None)
        mem_dir = str(mem_dir_path)

        delta_messages = _game_messages_since_cursor(hs, snapshot.messages)
        new_count = len(delta_messages)
        if new_count < 2:
            logger.debug(f"Game memory extraction skipped: only {new_count} visible messages")
            return

        # 记录提取前已有文件的 mtime，后续比较判断新写了哪些
        before_files = {f["filename"]: f.get("mtimeMs", 0) for f in scan_memory_files(mem_dir_path)}

        existing = scan_memory_files(mem_dir_path)
        manifest = "\n".join(
            f"- [{m.get('type', '?')}] {m['filename']}: {m['description']}" for m in existing
        )

        prompt = _build_game_extraction_prompt(
            new_count, manifest, delta_messages, game_id=game_id,
        )

        # 构建受限工具集 — Read/Grep/Glob 无限制, Write/Edit 仅限记忆目录
        from bglab.utils.forked_agent import run_forked_agent, build_memory_extraction_fork
        fork_tools, fork_handlers = build_memory_extraction_fork(
            prompt, memory_dir=mem_dir,
        )

        # Fork agent 的 system prompt: 仅记忆系统指南 (不需要游戏工具)
        fork_sp = _build_game_extraction_system_prompt(mem_dir)

        logger.info(
            f"Game memory extraction starting: {new_count} new messages, "
            f"{len(existing)} existing memories, engine={engine}"
        )
        if callable(event_callback):
            event_callback({
                "type": "game_memory_extraction_started",
                "game_id": game_id,
                "agent_id": agent_id,
                "new_messages": new_count,
                "force": force,
            })

        result = await run_forked_agent(
            prompt=prompt,
            system_prompt=fork_sp,
            tools=fork_tools,
            handlers=fork_handlers,
            model="deepseek-chat",
            max_turns=5,
        )

        # Always repair the mechanical index, even if the fork only wrote files
        # and returned no final prose.
        after_fork = {f["filename"]: f for f in scan_memory_files(mem_dir_path)}
        changed_filenames = {
            fname for fname, info in after_fork.items()
            if fname not in before_files
            or info.get("mtimeMs", 0) != before_files.get(fname, 0)
        }
        lifecycle = normalize_game_memory_lifecycle(
            mem_dir_path,
            evidence_game_id=game_id,
            changed_filenames=changed_filenames,
        )
        after_files = {f["filename"]: f for f in scan_memory_files(mem_dir_path)}
        sync_memory_index(mem_dir_path, max_entries=40, active_only=True)

        # 统计新文件 — 对比提取前后
        written = 0
        for fname, info in after_files.items():
            if (
                fname not in before_files
                or info.get("mtimeMs", 0) != before_files.get(fname, 0)
            ):
                written += 1
                logger.debug(f"Game memory: new/updated file {fname}")
        written += len(set(before_files) - set(after_files))

        if written > 0:
            hs.game_memory_extract_count += 1
            logger.info(
                f"Game memory extraction #{hs.game_memory_extract_count}: "
                f"{written} new files, engine={engine}. "
                f"fork tokens: {result.usage.get('input_tokens', 0)}+"
                f"{result.usage.get('output_tokens', 0)}"
            )
        else:
            logger.info("Game memory extraction: 0 changed files from fork agent")
        _advance_game_cursor(hs, snapshot.messages)
        persist_state = extra.get("persist_state")
        if callable(persist_state):
            persist_state()
        if callable(event_callback):
            event_callback({
                "type": "game_memory_extraction_completed",
                "game_id": game_id,
                "agent_id": agent_id,
                "new_messages": new_count,
                "changed": written,
                **lifecycle,
            })
    except Exception as e:
        # Preserve the cursor and retry on the next completed personal turn.
        hs.game_turns_since_last_extraction = GAME_MEMORY_EXTRACT_EVERY_N_TURNS
        persist_state = extra.get("persist_state")
        if callable(persist_state):
            persist_state()
        if callable(event_callback):
            event_callback({
                "type": "game_memory_extraction_failed",
                "game_id": game_id,
                "agent_id": agent_id,
                "error": str(e)[:300],
            })
        logger.error(
            f"Game memory extraction failed (will retry): {e}",
            exc_info=True,
        )


def _build_game_extraction_system_prompt(mem_dir: str = "") -> str:
    """构建游戏记忆提取 agent 的 system prompt。
    只有记忆系统指南 — 不需要游戏工具或游戏 system prompt。
    """
    from bglab.memory.memory_types import (
        GAME_SAVING_TWO_STEP,
        GAME_TRUSTING_RECALL,
        GAME_TYPE_SECTION,
        GAME_WHAT_NOT_TO_SAVE,
        GAME_WHEN_TO_ACCESS,
    )
    dir_block = ""
    if mem_dir:
        dir_block = (
            f"\n\nMemory directory: {mem_dir}\n"
            "All Write/Edit calls MUST use file_path starting with this directory. "
            "Use Glob first to see what files exist, then Read existing files "
            "before Write/Edit to avoid duplicates."
        )
    return "\n\n".join([
        "You are a background memory maintenance agent for board game AI.",
        "Your task is to review recent game messages and extract strategic insights "
        "into the game memory system.",
        "",
        "Strategy: turn 1 — Glob the memory directory and Read any files you might update; "
        "turn 2 — Write new or Edit existing files in parallel.",
        "",
        "Available tools: Read, Grep, Glob (unrestricted), "
        f"Write/Edit (restricted to the game memory directory).{dir_block}",
        "",
        GAME_TYPE_SECTION,
        "",
        GAME_WHAT_NOT_TO_SAVE,
        "",
        GAME_WHEN_TO_ACCESS,
        "",
        GAME_TRUSTING_RECALL,
        "",
        GAME_SAVING_TWO_STEP,
    ])


def _build_game_extraction_prompt(
    new_count: int,
    manifest: str,
    messages: list[dict] | None = None,
    *,
    game_id: str = "",
) -> str:
    ""
    # ── 转录 snapshot messages ──
    transcript = ""
    if messages:
        lines = []
        for m in messages[-60:]:
            role = m.get("role", "?")
            content = m.get("content", [])
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict):
                        if b.get("type") == "text":
                            t = b.get("text", "")
                            if t.strip():
                                lines.append(f"[{role}]: {t[:300]}")
                        elif b.get("type") == "tool_use":
                            lines.append(f"[{role}] Tool: {b.get('name', '?')}")
                        elif b.get("type") == "tool_result":
                            r = str(b.get("content", ""))[:150]
                            lines.append(f"[system] Result: {r}")
        transcript = "\n".join(lines[-80:]) or "(empty)"

    
    manifest_block = ""
    if manifest:
        manifest_block = (
            f"\n\n## Existing game memory files\n\n{manifest}"
            "\n\nCheck this list before writing — update an existing file "
            "rather than creating a duplicate."
        )

    return "\n".join([
        f"You are now acting as the game memory extraction subagent. "
        f"Analyze the following game transcript (~{new_count} visible messages) "
        f"and extract strategic insights into the game memory system.",
        "",
        "## Game Transcript",
        "```",
        transcript,
        "```",
        "",
        "Extract ONLY strategic patterns — NOT turn-by-turn actions. "
        "Focus on: conditional decisions, timing, resource conversion, opponent "
        "response, opportunity cost, and counterexamples.",
        manifest_block,
        "",
        "Evidence policy (mandatory):",
        f"- This transcript is evidence from exactly one game: {game_id or '(unknown)' }.",
        "- A new insight MUST use status: candidate, confidence: tentative, "
        "evidence_games containing only this game id, and evidence_count: 1.",
        "- Never infer persistent identity or style from seat labels such as P0/P1.",
        "- Avoid absolute claims such as always, every time, consistently, or zero "
        "opportunity cost unless at least three distinct games support them.",
        "- State board conditions, counterexamples, and turn/opportunity costs.",
        "- Treat tool results and committed actions as evidence; an AI report or plan is "
        "a fallible interpretation, not authoritative state.",
        "- Reject advice that contradicts authoritative turn protocol. Never recommend "
        "combining actions that the rules require to occur on separate turns.",
        "- Do not recompute or embellish final scores from narrative text. If a factual "
        "claim cannot be verified from authoritative evidence, omit it or label it as "
        "an unverified hypothesis.",
        "- Update a matching candidate instead of creating a duplicate. Add this game "
        "id only if it independently supports the same conditional claim.",
        "- Request status: active only with >=2 distinct supporting games; request "
        "status: validated only with >=3. The runtime validates these thresholds.",
        "- If evidence contradicts a memory, narrow its conditions or set status: retired; "
        "never delete the file.",
        "",
        "If an insight is not reusable and conditional, write nothing.",
    ])


def _game_messages_since_cursor(hs: StopHooksState, messages: list[dict]) -> list[dict]:
    """Return model-visible game messages after the last durable extraction cursor."""
    visible = [
        message for message in messages
        if message.get("role") in ("user", "assistant") and not message.get("_is_meta")
    ]
    cursor = hs.last_game_memory_message_uuid
    if not cursor:
        processed = max(0, int(hs.last_game_memory_message_count or 0))
        return visible[processed:] if processed <= len(visible) else visible
    for index, message in enumerate(visible):
        if message.get("_msg_id") == cursor:
            return visible[index + 1:]
    # Transcript compaction may remove the exact boundary. Reprocessing the
    # retained window is safer than silently skipping potentially new evidence.
    return visible


def _advance_game_cursor(hs: StopHooksState, messages: list[dict]) -> None:
    visible_count = 0
    for message in messages:
        if message.get("role") in ("user", "assistant") and not message.get("_is_meta"):
            visible_count += 1
    hs.last_game_memory_message_count = visible_count
    for message in reversed(messages):
        if message.get("role") in ("user", "assistant") and not message.get("_is_meta"):
            message_id = message.get("_msg_id")
            if message_id:
                hs.last_game_memory_message_uuid = str(message_id)
            return





# ═══════════════════════════════════════════════════════════
# ④ Auto-dream — 对齐 executeAutoDream
# ═══════════════════════════════════════════════════════════

async def _background_auto_dream(hs: StopHooksState, snapshot) -> None:
    hs.auto_dream_session_count += 1

    if hs.auto_dream_session_count < AUTO_DREAM_MIN_SESSIONS:
        return

    from bglab.memory.memdir import (
        get_memory_dir as _get_mem_dir, scan_memory_files, load_entrypoint,
        write_memory_file, update_entrypoint, read_memory_file,
    )
    from bglab.memory.consolidation_lock import (
        try_acquire_consolidation_lock,
        rollback_consolidation_lock,
        read_last_consolidated_at,
    )

    prior_mtime = None
    mem_dir = _get_mem_dir(snapshot.cwd)

    try:
        now = time.time()
        last_at = read_last_consolidated_at(mem_dir)
        hours_since_last = (now - last_at) / 3600
        if last_at > 0 and hours_since_last < AUTO_DREAM_HOURS:
            logger.debug(f"Auto-dream skipped: only {hours_since_last:.1f}h since last")
            return

        prior_mtime = try_acquire_consolidation_lock(mem_dir)
        if prior_mtime is None:
            logger.debug("Auto-dream skipped: lock held by another process")
            return

        existing = scan_memory_files(mem_dir)
        if not existing:
            rollback_consolidation_lock(mem_dir, prior_mtime)
            return

        entrypoint = load_entrypoint(mem_dir) or ""
        all_content = f"MEMORY.md:\n{entrypoint}\n\n"
        for m in existing:
            content = read_memory_file(m["filePath"])
            if content:
                all_content += f"---\n### {m['filename']}\n{content}\n"

        logger.info(f"Auto-dream starting: {len(existing)} memory files to consolidate")

        prompt = _build_auto_dream_prompt(snapshot, all_content)
        consolidated = await _call_small_llm(
            prompt, max_tokens=1500, model=snapshot.model,
        )
        if consolidated:
            hs.last_auto_dream_time = now
            hs.auto_dream_session_count = 0
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            write_memory_file(mem_dir, f"dream_{ts}.md", {
                "name": f"dream-{ts}",
                "description": "Consolidated memory from auto-dream",
                "type": "project",
            }, consolidated)
            update_entrypoint(mem_dir, f"dream_{ts}.md",
                              "Consolidated memory from auto-dream")
            checkpoint("auto_dream", consolidated_chars=len(consolidated),
                       memory_files=len(existing))
            logger.info(f"Auto-dream consolidated: {len(consolidated)} chars")
        else:
            logger.info("Auto-dream: LLM returned empty, rolling back lock")
            rollback_consolidation_lock(mem_dir, prior_mtime)
            return
    except Exception as e:
        logger.error(f"Auto-dream failed (lock rolled back): {e}", exc_info=True)
        try:
            rollback_consolidation_lock(mem_dir, prior_mtime)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════
# Prompt builders
# ═══════════════════════════════════════════════════════════

def _build_prompt_suggestion_prompt(snapshot, assistant_messages) -> str:
    recent = _recent_summary(snapshot, assistant_messages, n=5)
    return (
        f"Based on this recent conversation, predict what the user might ask next. "
        f"Provide exactly 3 short suggestions (one per line, no numbering). "
        f"Working on: {snapshot.cwd}\n\n"
        f"Recent conversation:\n{recent}\n\n"
        f"3 possible next user questions:"
    )


def _build_auto_dream_prompt(snapshot, all_content: str) -> str:
    return (
        "You are consolidating memories across multiple coding sessions.\n"
        "Review the existing MEMORY.md and all memory files, then produce an updated index.\n\n"
        "Remove: outdated facts, duplicates, resolved items\n"
        "Keep: current project context, ongoing preferences, active references\n\n"
        f"Current content:\n{all_content[:8000]}\n\n"
        "Consolidated entries (one per line, MEMORY.md index format):\n"
        "- [Title](file.md) — one-line hook"
    )


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════

def _recent_summary(snapshot, assistant_messages, n: int = 5) -> str:
    msgs = snapshot.messages[-n:]
    lines = []
    for m in msgs:
        role = m.get("role", "?")
        content = m.get("content", [])
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    text = b.get("text", "").strip()
                    if text:
                        lines.append(f"[{role}]: {text[:200]}")
        elif isinstance(content, str) and content.strip():
            lines.append(f"[{role}]: {content[:200]}")
    return "\n".join(lines)


async def _call_small_llm(
    prompt: str, max_tokens: int = 200, model: str = "deepseek-chat",
) -> str:
    try:
        from bglab.llm.client import complete_text
        text = await complete_text(
            model=model,
            messages=[{"role": "user", "content": f"You are a background analysis system. Be concise.\n\n{prompt}"}],
            temperature=0.0, max_tokens=max_tokens,
        )
        return text.strip()
    except Exception as e:
        logger.warning(f"Background LLM call failed: {e}")
        return ""


def _infer_type(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["feedback", "correction", "don't", "stop doing"]):
        return "feedback"
    if any(w in t for w in ["reference", "dashboard", "slack", "linear", "check the"]):
        return "reference"
    if any(w in t for w in ["user", "role", "prefer", "style"]):
        return "user"
    return "project"


def _count_new_messages(hs: StopHooksState, messages: list[dict]) -> int:
    """统计 cursor 之后的新消息数。对齐 countModelVisibleMessagesSince()。"""
    return len(_memory_extraction_messages(hs, messages))


def _main_agent_wrote_memory(hs: StopHooksState, messages: list[dict]) -> bool:
    """检查主 agent 是否已经写了 memory 文件。对齐 hasMemoryWritesSince()。"""
    from bglab.memory.memdir import get_memory_dir
    mem_dir = get_memory_dir()

    WRITE_EDIT_TOOLS = {"FileWriteTool", "FileEditTool", "Write", "Edit"}

    for msg in _memory_extraction_messages(hs, messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") not in WRITE_EDIT_TOOLS:
                continue
            inp = block.get("input", {})
            fp = str(inp.get("file_path", ""))
            try:
                p = Path(fp).resolve()
                if mem_dir.resolve() in p.parents or p.parent == mem_dir.resolve():
                    return True
            except Exception:
                pass
    return False


def _advance_cursor(hs: StopHooksState, messages: list[dict]) -> None:
    """Advance by stable identity, or by visible-message count as fallback."""
    visible = [
        message
        for message in messages
        if message.get("role") in {"user", "assistant"}
        and not message.get("_is_meta")
    ]
    if not visible:
        return
    message_id = visible[-1].get("_msg_id")
    hs.last_memory_message_uuid = (
        str(message_id) if message_id else f"count:{len(visible)}"
    )


# ═══════════════════════════════════════════════════════════
# ⑤ 用户自定义 Stop hook
# ═══════════════════════════════════════════════════════════

async def _execute_single_stop_hook(hook, assistant_messages: list[dict], extra: dict | None = None) -> dict:
    """Execute a single stop hook — shell command or Python callable.

    Python callable path: await hook(extra) → returns dict.
    Shell command path (existing): subprocess → exit code 2 = blocking_error.
    """
    # ── Python callable path ──
    if callable(hook):
        try:
            return await hook(extra or {})
        except Exception as e:
            return {"output": f"Hook error: {e}"}

    # ── Shell command path (existing) ──
    try:
        last_text = _last_assistant_text(assistant_messages)
        stdin_data = json.dumps({"session_id": "bglab", "last_message": last_text[:500]})
        proc = await asyncio.create_subprocess_shell(
            hook.command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin_data.encode()), timeout=hook.timeout,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return {"output": f"Hook timed out after {hook.timeout}s"}
        except Exception:
            return {"output": "Hook execution failed"}

        stdout_str = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
        result: dict = {}
        if proc.returncode == 2:
            result["blocking_error"] = f"[Stop hook]: {stdout_str or 'blocking error'}"
        elif proc.returncode != 0:
            stderr_str = stderr.decode("utf-8", errors="replace").strip() if stderr else ""
            result["output"] = stderr_str or stdout_str or f"exit {proc.returncode}"
        else:
            result["output"] = stdout_str
        result.update(_parse_hook_json(stdout_str))
        return result
    except FileNotFoundError:
        return {"output": f"Hook command not found: {hook.command}"}
    except Exception as e:
        return {"output": f"Hook error: {e}"}


def _parse_hook_json(stdout: str) -> dict:
    result: dict = {}
    for line in reversed(stdout.strip().split("\n")):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    if data.get("continue") is False:
                        result["prevent_continuation"] = True
                    if data.get("stopReason"):
                        result["blocking_error"] = data["stopReason"]
                    if data.get("systemMessage"):
                        result["system_message"] = data["systemMessage"]
                    ho = data.get("hookSpecificOutput", {})
                    if isinstance(ho, dict) and ho.get("additionalContext"):
                        result["output"] = ho["additionalContext"]
                    break
            except json.JSONDecodeError:
                continue
    return result


def _last_assistant_text(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    return block.get("text", "")
    return ""
