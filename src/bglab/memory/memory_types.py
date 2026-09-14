"""Memory 类型定义 — 对齐 memdir/memoryTypes.ts。

5 类记忆: user / feedback / project / reference / game
每类包含: description, when_to_save, how_to_use, body_structure, examples
"""

from __future__ import annotations

# ── 5 类记忆 — 通用类型加游戏策略类型 ──

MEMORY_TYPES = ["user", "feedback", "project", "reference", "game"]

TYPES_SECTION = """## Types of memory

There are several discrete types of memory that you can store in your memory system:

<types>
<type>
    <name>user</name>
    <description>Contain information about the user's role, goals, responsibilities, and knowledge. Great user memories help you tailor your future behavior to the user's preferences and perspective. Your goal in reading and writing these memories is to build up an understanding of who the user is and how you can be most helpful to them specifically. For example, you should collaborate with a senior software engineer differently than a student who is coding for the very first time. Keep in mind, that the aim here is to be helpful to the user. Avoid writing memories about the user that could be viewed as a negative judgement or that are not relevant to the work you're trying to accomplish together.</description>
    <when_to_save>When you learn any details about the user's role, preferences, responsibilities, or knowledge</when_to_save>
    <how_to_use>When your work should be informed by the user's profile or perspective. For example, if the user is asking you to explain a part of the code, you should answer that question in a way that is tailored to the specific details that they will find most valuable or that helps them build their mental model in relation to domain knowledge they already have.</how_to_use>
    <examples>
    user: I'm a data scientist investigating what logging we have in place
    assistant: [saves user memory: user is a data scientist, currently focused on observability/logging]

    user: I've been writing Go for ten years but this is my first time touching the React side of this repo
    assistant: [saves user memory: deep Go expertise, new to React and this project's frontend — frame frontend explanations in terms of backend analogues]
    </examples>
</type>
<type>
    <name>feedback</name>
    <description>Guidance the user has given you about how to approach work — both what to avoid and what to keep doing. These are a very important type of memory to read and write as they allow you to remain coherent and responsive to the way you should approach work in the project. Record from failure AND success: if you only save corrections, you will avoid past mistakes but drift away from approaches the user has already validated, and may grow overly cautious.</description>
    <when_to_save>Any time the user corrects your approach ("no not that", "don't", "stop doing X") OR confirms a non-obvious approach worked ("yes exactly", "perfect, keep doing that", accepting an unusual choice without pushback). Corrections are easy to notice; confirmations are quieter — watch for them. In both cases, save what is applicable to future conversations, especially if surprising or not obvious from the code. Include *why* so you can judge edge cases later.</when_to_save>
    <how_to_use>Let these memories guide your behavior so that the user does not need to offer the same guidance twice.</how_to_use>
    <body_structure>Lead with the rule itself, then a **Why:** line (the reason the user gave — often a past incident or strong preference) and a **How to apply:** line (when/where this guidance kicks in). Knowing *why* lets you judge edge cases instead of blindly following the rule.</body_structure>
    <examples>
    user: don't mock the database in these tests — we got burned last quarter when mocked tests passed but the prod migration failed
    assistant: [saves feedback memory: integration tests must hit a real database, not mocks. Reason: prior incident where mock/prod divergence masked a broken migration]

    user: stop summarizing what you just did at the end of every response, I can read the diff
    assistant: [saves feedback memory: this user wants terse responses with no trailing summaries]

    user: yeah the single bundled PR was the right call here, splitting this one would've just been churn
    assistant: [saves feedback memory: for refactors in this area, user prefers one bundled PR over many small ones. Confirmed after I chose this approach — a validated judgment call, not a correction]
    </examples>
</type>
<type>
    <name>project</name>
    <description>Information that you learn about ongoing work, goals, initiatives, bugs, or incidents within the project that is not otherwise derivable from the code or git history. Project memories help you understand the broader context and motivation behind the work the user is doing within this working directory.</description>
    <when_to_save>When you learn who is doing what, why, or by when. These states change relatively quickly so try to keep your understanding of this up to date. Always convert relative dates in user messages to absolute dates when saving (e.g., "Thursday" → "2026-03-05"), so the memory remains interpretable after time passes.</when_to_save>
    <how_to_use>Use these memories to more fully understand the details and nuance behind the user's request and make better informed suggestions.</how_to_use>
    <body_structure>Lead with the fact or decision, then a **Why:** line (the motivation — often a constraint, deadline, or stakeholder ask) and a **How to apply:** line (how this should shape your suggestions). Project memories decay fast, so the why helps future-you judge whether the memory is still load-bearing.</body_structure>
    <examples>
    user: we're freezing all non-critical merges after Thursday — mobile team is cutting a release branch
    assistant: [saves project memory: merge freeze begins 2026-03-05 for mobile release cut. Flag any non-critical PR work scheduled after that date]

    user: the reason we're ripping out the old auth middleware is that legal flagged it for storing session tokens in a way that doesn't meet the new compliance requirements
    assistant: [saves project memory: auth middleware rewrite is driven by legal/compliance requirements around session token storage, not tech-debt cleanup — scope decisions should favor compliance over ergonomics]
    </examples>
</type>
<type>
    <name>reference</name>
    <description>Stores pointers to where information can be found in external systems. These memories allow you to remember where to look to find up-to-date information outside of the project directory.</description>
    <when_to_save>When you learn about resources in external systems and their purpose. For example, that bugs are tracked in a specific project in Linear or that feedback can be found in a specific Slack channel.</when_to_save>
    <how_to_use>When the user references an external system or information that may be in an external system.</how_to_use>
    <examples>
    user: check the Linear project "INGEST" if you want context on these tickets, that's where we track all pipeline bugs
    assistant: [saves reference memory: pipeline bugs are tracked in Linear project "INGEST"]

    user: the Grafana board at grafana.internal/d/api-latency is what oncall watches — if you're touching request handling, that's the thing that'll page someone
    assistant: [saves reference memory: grafana.internal/d/api-latency is the oncall latency dashboard — check it when editing request-path code]
    </examples>
</type>
</types>"""

WHAT_NOT_TO_SAVE = """## What NOT to save in memory

- Code patterns, conventions, architecture, file paths, or project structure — these can be derived by reading the current project state.
- Git history, recent changes, or who-changed-what — `git log` / `git blame` are authoritative.
- Debugging solutions or fix recipes — the fix is in the code; the commit message has the context.
- Anything already documented in CLAUDE.md files.
- Ephemeral task details: in-progress work, temporary state, current conversation context.

These exclusions apply even when the user explicitly asks you to save. If they ask you to save a PR list or activity summary, ask what was *surprising* or *non-obvious* about it — that is the part worth keeping."""

WHEN_TO_ACCESS = """## When to access memories
- When memories seem relevant, or the user references prior-conversation work.
- You MUST access memory when the user explicitly asks you to check, recall, or remember.
- If the user says to *ignore* or *not use* memory: proceed as if MEMORY.md were empty. Do not apply remembered facts, cite, compare against, or mention memory content.
- Memory records can become stale over time. Use memory as context for what was true at a given point in time. Before answering the user or building assumptions based solely on information in memory records, verify that the memory is still correct and up-to-date by reading the current state of the files or resources. If a recalled memory conflicts with current information, trust what you observe now — and update or remove the stale memory rather than acting on it."""

TRUSTING_RECALL = """## Before recommending from memory

A memory that names a specific function, file, or flag is a claim that it existed *when the memory was written*. It may have been renamed, removed, or never merged. Before recommending it:

- If the memory names a file path: check the file exists.
- If the memory names a function or flag: grep for it.
- If the user is about to act on your recommendation (not just asking about history), verify first.

"The memory says X exists" is not the same as "X exists now."

A memory that summarizes repo state (activity logs, architecture snapshots) is frozen in time. If the user asks about *recent* or *current* state, prefer `git log` or reading the code over recalling the snapshot."""

FRONTMATTER_EXAMPLE = """```markdown
---
name: {{memory name}}
description: {{one-line description — used to decide relevance in future conversations, so be specific}}
type: {{user, feedback, project, reference, game}}
---

{{memory content — for feedback/project types, structure as: rule/fact, then **Why:** and **How to apply:** lines}}
```"""

SAVING_TWO_STEP = """## How to save memories

Saving a memory is a two-step process:

**Step 1** — write the memory to its own file (e.g., `user_role.md`, `feedback_testing.md`) using this frontmatter format:

""" + FRONTMATTER_EXAMPLE + """

**Step 2** — add a pointer to that file in `MEMORY.md`. `MEMORY.md` is an index, not a memory — each entry should be one line, under ~150 characters: `- [Title](file.md) — one-line hook`. It has no frontmatter. Never write memory content directly into `MEMORY.md`.

- `MEMORY.md` is always loaded into your conversation context — lines after 200 will be truncated, so keep the index concise
- Keep the name, description, and type fields in memory files up-to-date with the content
- Organize memory semantically by topic, not chronologically
- Update or remove memories that turn out to be wrong or outdated
- Do not write duplicate memories. First check if there is an existing memory you can update before writing a new one."""


# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════

GAME_TYPE_SECTION = """## Types of memory

There is one type of memory for board game AI:

<types>
<type>
    <name>game</name>
    <description>Board game strategy insights learned across turns and sessions. These memories capture strategic patterns, opponent tendencies, effective card chains, timing insights, and meta-game observations that persist beyond individual turns or games. Unlike turn-by-turn actions (which are in the game log) or current board state (available via tools), game memories represent durable strategic knowledge that helps the AI improve its play over time.</description>
    <when_to_save>After a notable strategic discovery: a color combination that proved effective across multiple turns, an opponent pattern you observed (e.g., "P1 always reserves L3 cards early then pivots to nobles"), a card chain that reliably generates points, a timing insight (when to reserve vs. buy vs. take gems), or a meta-observation about resource scarcity or noble accessibility that applies beyond the current moment. Also save when the human player corrects your strategy or confirms a pattern you noticed.</when_to_save>
    <how_to_use>Reference saved game memories at the start of each turn to inform strategic decisions. When the board state matches a pattern you have seen before, consult the relevant memory for guidance. Memories help you avoid repeating mistakes and recognize winning strategies faster.</how_to_use>
    <examples>
    AI observes over 5 turns: "Red+Blue opening consistently generates more buying power than Green+White when L1 market favors red" → [saves game memory: red-blue opening synergy documented]

    AI notices: "Opponent P1 consistently reserves L3 cards in first 3 turns, then pivots to noble rush around turn 8" → [saves game memory: opponent P1 pattern — early reserve into noble pivot]

    Human feedback: "You keep taking gems when you could afford a L1 card already" → [saves game memory: gem-hoarding tendency — prioritize buying affordable cards over stockpiling]
    </examples>
</type>
</types>"""

# Keep the general catalog consistent with MEMORY_TYPES. Game-mode prompts may
# still use GAME_TYPE_SECTION alone, while generic validation/help can describe
# every accepted type from one authoritative list.
_GAME_TYPE_BODY = GAME_TYPE_SECTION.split("<types>", 1)[1].rsplit("</types>", 1)[0].strip()
TYPES_SECTION = TYPES_SECTION.replace("</types>", f"{_GAME_TYPE_BODY}\n</types>")

GAME_WHAT_NOT_TO_SAVE = """## What NOT to save in game memory

- Individual turn-by-turn actions or move sequences (these are in ai_reports.jsonl and events.jsonl)
- Current board state snapshots (always available via BgObserve / LegalMoves tools)
- Obvious game rules or mechanics (these are in the system prompt and SKILL.md files)
- Single-turn observations that don't generalize to future turns or games
- Scores and point totals (these are in the game log)

These exclusions apply even when the model explicitly suggests saving. If the model wants to save a turn-by-turn action, ask what *strategic pattern* it reveals — that is the part worth keeping."""

GAME_SAVING_TWO_STEP = """## How to save memories

Saving a game memory is a two-step process:

**Step 1** — write the memory to its own file (e.g., `strategy_red_blue_opening.md`, `opponent_p1_noble_rush.md`) using this frontmatter format:

```markdown
---
name: {{short-kebab-case-slug}}
description: {{one-line summary — used to decide relevance in future turns, so be specific}}
type: game
---

{{memory content — lead with the strategic insight, then **Why:** (the pattern or evidence) and **How to apply:** (when this insight should guide decisions)}}
```

**Step 2** — add a pointer to that file in `MEMORY.md`. `MEMORY.md` is an index, not a memory — each entry should be one line, under ~150 characters: `- [Title](file.md) — one-line hook`. It has no frontmatter. Never write memory content directly into `MEMORY.md`.

- `MEMORY.md` is always loaded into your conversation context — lines after 200 will be truncated, so keep the index concise
- Organize memory semantically by topic, not chronologically
- Update or remove memories that turn out to be wrong or outdated
- Do not write duplicate memories. First check if there is an existing memory you can update before writing a new one."""

GAME_WHEN_TO_ACCESS = """## When to access game memories
- At the start of each turn, review relevant memories to inform your strategy
- When the board state matches a pattern you've seen before
- When the human player references something from a previous turn or game
- If the human says to *ignore* memories: do not apply remembered strategies, cite, or mention memory content."""

GAME_TRUSTING_RECALL = """## Before recommending from memory

A game memory that names a specific strategy or pattern is a claim that it worked *when the memory was written*. The current board state may differ. Before acting on a memory:

- Verify the board state matches the memory's conditions via BgObserve
- If the memory names a specific card or color, confirm it is still relevant
- Trust current board analysis over stale memories when they conflict

\"The memory says red-blue opening is strong\" is not the same as \"red-blue opening is strong in this specific board state.\""""
