""

from bglab.skills.base import register_bundled_skill

CODE_REVIEW_PROMPT = """# Code Review: Correctness Bugs in Current Git Diff

Review the current git diff for **correctness** issues — bugs that would cause
wrong behavior, errors, or data loss. Quality, reuse, and efficiency issues
are the domain of /simplify; do not raise them here.

## Step 1: List the diff

Run:
```
git diff
```

If there are staged changes, also run `git diff --cached`. If neither shows
anything, ask the user for the commit range to review.

## Step 2: For each changed hunk, evaluate

### Correctness (raise only if true)
- **Off-by-one / boundary**: loop indices, range ends, slice boundaries,
  comparisons using `<` vs `<=`.
- **Null/None dereference**: dereferencing a possibly-None value without a guard.
- **Type confusion**: passing wrong-but-coerced type that silently misbehaves
  (bytes vs str, int vs float, list vs tuple unpacking).
- **Resource leak**: opened file/socket/lock that is never released
  (no `with`, no `finally`).
- **Mutation of shared state**: function mutates an input list/dict instead of
  returning a copy — caller observes side effects.
- **Race / async ordering**: `await` forgotten; or two coroutines writing to
  the same shared structure without a lock.
- **Wrong default**: mutable default argument (`def f(x=[])`); incorrect
  truthy check on falsy-but-valid values (empty string, `0`, `[]`).
- **Exception swallowing**: `except Exception: pass` hides the error path.
- **Sign / direction error**: reversed comparison, inverted branch, off-by-one
  enum mapping.

### Security (raise if obvious and small)
- SQL string concatenation (use parameterized queries).
- Shell injection: `subprocess.run(..., shell=True)` with user input.
- Hardcoded secrets, API keys, or passwords in source.

### Migration / data loss
- Dropping or duplicating rows when a schema migration reorders columns.
- Writing to a path that did not exist before without creating it.
- Overwriting an existing file without confirming that is intended.

## Step 3: Aggregate into findings

For each finding, write a single block:

```
### Finding: <short title>
- File: <path>:<line>
- Severity: critical | high | medium | low
- Why: <one sentence on the bug>
- Suggested fix: <one-line diff or pseudocode>
```

Skip everything tagged "nitpick" — those are quality, not correctness, issues.

## Step 4: Apply fixes

Apply fixes inline. If fixing changes more than 30 lines, stop and ask the
user which findings to apply — do not auto-mass-edit.

End with a one-line summary: `<N> findings, <M> fixed, <K> skipped`.
"""


def _code_review_prompt(args: str) -> str:
    if args:
        return CODE_REVIEW_PROMPT + f"\n## User focus\n\n{args}\n"
    return CODE_REVIEW_PROMPT


register_bundled_skill(
    name="code-review",
    description="Review the current git diff for correctness bugs and apply fixes. Quality and efficiency are /simplify's domain.",
    get_prompt=_code_review_prompt,
    argument_hint="[file or area to focus on]",
)