""

from bglab.skills.base import register_bundled_skill

SECURITY_REVIEW_PROMPT = """# Security Review

Audit the named code paths (or the whole repo if no scope given) for common,
concrete security issues. Do not raise theoretical concerns — only findings
with a reproducible failure mode.

## Scope

If args are given, restrict the audit to those paths. If args are empty, scan
the files that the user has edited in this session (per transcript) plus the
staged diff (`git diff --cached`); if neither exists, ask the user for paths.

If args contain the word "fix", apply non-destructive fixes inline.

## Checks

### Injection
- Shell injection: `subprocess.run(..., shell=True)` with user-controlled input.
  Use `shell=False` + argv list, or `shlex.quote`.
- SQL string concatenation with `f"..."`/`%`/`+` instead of parameterized
  queries.
- Template / SSTI in Jinja2/Mako with `{{ user_input }}` not escaped.
- Path traversal: user-controlled path joined to a base dir without
  rejecting `..`.
- YAML / pickle deserialization of untrusted data (`yaml.safe_load` is fine;
  `yaml.load` without `Loader=SafeLoader` is not; `pickle.loads` on network
  input is critical).

### Authentication / authorization
- Routes or handlers that mutate state without an auth check.
- Missing CSRF token on a state-changing endpoint.
- IDOR: grabbing an object by id from request without checking the owner.
- Hardcoded API keys, JWT signing keys, db passwords in source.

### Cryptography
- Hardcoded password / token / SMTP password.
- `random.random` or `MD5`/`SHA1` used for security-sensitive purposes (use
  `secrets` / `hashlib.sha256` or stronger).
- Comparing secrets with `==` instead of `hmac.compare_digest`.

### Secrets / leakage
- API keys / passwords printed to logs or returned in error responses.
- Git history containing committed secrets
  (`git log -p | grep -iE 'password|secret|token|key='` near HEAD).

### Resource exhaustion
- Unbounded input: regex without timeout, no max length on user-supplied
  file or dataset size, no pagination on listing endpoints.
- `eval` / `exec` / `pickle.loads` on raw request body.

### Filesystem
- Writing temp files to predictable paths in `/tmp` without `0600` modes.
- Deserialization of files whose path was supplied by the user.

## Step 1: catalog

Run `git diff --cached` and read the named paths. For each finding, note
`file:line` and quote the vulnerable line.

## Step 2: write findings

For each issue:

```
### <title>
- File: `<path>:<line>`
- Severity: critical | high | medium | low
- Failure mode: how to trigger the bug
- Suggested fix: one-line code change
```

Severity rules:
- **critical**: code-execution, secret leak, authentication bypass
- **high**: data exfiltration, IDOR, resource exhaustion leading to DoS
- **medium**: weak hashing, missing rate-limit, missing CSRF on mutation
- **low**: hardening (HTTP security headers, predictable tmp paths)

Skip everything that needs runtime verification the user has not confirmed.

## Step 3: apply fixes (only if "fix" in args)

Apply non-destructive fixes inline (parameterize the SQL call, switch to
`shell=False`, use `hmac.compare_digest`). If a fix changes behavior or might
break tests, only describe the fix — do not apply it.

End with `<N> findings, <M> fixed, <K> needs confirmation`.

Do not omit critical findings even if you are unsure whether the call site is
reachable — flag it and let the user confirm. Do not guess filenames you have
not opened.
"""


def _security_review_prompt(args: str) -> str:
    if args:
        return SECURITY_REVIEW_PROMPT + f"\n## Scope\n\n{args}\n"
    return SECURITY_REVIEW_PROMPT


register_bundled_skill(
    name="security-review",
    description="Audit the codebase (or staged diff) for common security issues: injection, auth bypass, weak hashing, secret leakage, resource exhaustion.",
    get_prompt=_security_review_prompt,
    argument_hint="[paths or 'fix' to apply fixes]",
)