""

from bglab.skills.base import register_bundled_skill

SIMPLIFY_PROMPT = """# Simplify: Code Review and Cleanup

Review all changed files for reuse, quality, and efficiency. Fix any issues found.

## Phase 1: Identify Changes

Run `git diff` (or `git diff HEAD` if there are staged changes) to see what changed. If there are no git changes, review the most recently modified files that the user mentioned or that you edited earlier in this conversation.

## Phase 2: Review in Three Dimensions

Read each changed file and evaluate it across these three areas:

### 1. Code Reuse
For each change:
- **Search for existing utilities and helpers** that could replace newly written code. Look for similar patterns elsewhere in the codebase.
- **Flag any new function that duplicates existing functionality.** Suggest the existing function to use instead.
- **Flag any inline logic that could use an existing utility** — hand-rolled string manipulation, manual path handling, custom environment checks, ad-hoc type guards, and similar patterns are common candidates.

### 2. Code Quality
Review for hacky patterns:
- **Redundant state**: state that duplicates existing state, cached values that could be derived
- **Parameter sprawl**: adding new parameters to a function instead of generalizing
- **Copy-paste with slight variation**: near-duplicate code blocks that should be unified
- **Leaky abstractions**: exposing internal details that should be encapsulated
- **Stringly-typed code**: using raw strings where constants or enums already exist
- **Unnecessary comments**: comments explaining WHAT the code does (well-named identifiers already do that) — delete them; keep only non-obvious WHY (hidden constraints, subtle invariants, workarounds)

### 3. Efficiency
- **Unnecessary work**: redundant computations, repeated file reads, duplicate API calls, N+1 patterns
- **Missed concurrency**: independent operations run sequentially when they could run in parallel
- **Hot-path bloat**: new blocking work added to startup or per-request hot paths
- **TOCTOU anti-pattern**: pre-checking file/resource existence before operating — operate directly and handle the error
- **Overly broad operations**: reading entire files when only a portion is needed

## Phase 3: Fix Issues

Aggregate findings and fix each issue directly. If a finding is a false positive or not worth addressing, note it and move on — do not argue with the finding, just skip it.

When done, briefly summarize what was fixed (or confirm the code was already clean).
"""


def _simplify_prompt(args: str) -> str:
    if args:
        return SIMPLIFY_PROMPT + f"\n\n## Additional Focus\n\n{args}\n"
    return SIMPLIFY_PROMPT


register_bundled_skill(
    name="simplify",
    description="Review changed code for reuse, quality, and efficiency, then fix any issues found.",
    get_prompt=_simplify_prompt,
    argument_hint="[area to focus on]",
)
