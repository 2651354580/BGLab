"""Local credential environment discovery and atomic updates.

Only the environment variable name and a redacted status leave this module.
Credential values are used transiently for process/file operations and are
never returned, logged, hashed, or persisted in settings.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

CredentialSource = Literal["process_environment", "local_env", "missing"]

_VALID_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")


@dataclass(frozen=True)
class CredentialStatus:
    configured: bool
    source: CredentialSource


def _validate_env_name(env_name: str) -> str:
    if not isinstance(env_name, str) or not _VALID_ENV_NAME.fullmatch(env_name):
        raise ValueError("invalid credential environment name")
    return env_name


def _linked_worktree_env_path(start: Path) -> Path | None:
    """Return the main worktree's .env path for a linked worktree."""
    cur = start
    for _ in range(6):
        marker = cur / ".git"
        if marker.is_file():
            try:
                line = marker.read_text(encoding="utf-8").strip()
                if not line.lower().startswith("gitdir:"):
                    return None
                git_dir = Path(line.split(":", 1)[1].strip())
                if not git_dir.is_absolute():
                    git_dir = (cur / git_dir).resolve()
                common_file = git_dir / "commondir"
                if not common_file.is_file():
                    return None
                common_git = (git_dir / common_file.read_text(
                    encoding="utf-8",
                ).strip()).resolve()
                return common_git.parent / ".env"
            except OSError:
                return None
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _candidate_env_paths(start: Path) -> list[Path]:
    paths: list[Path] = []
    cur = start
    for _ in range(6):
        paths.append(cur / ".env")
        if cur.parent == cur:
            break
        cur = cur.parent
    linked = _linked_worktree_env_path(start)
    if linked is not None:
        paths.append(linked)
    try:
        import bglab as _pkg

        pkg_dir = Path(_pkg.__file__).resolve().parent
        for _ in range(4):
            paths.append(pkg_dir / ".env")
            pkg_dir = pkg_dir.parent
    except Exception:
        pass
    return paths


def find_secret_env(start: Path | str | None = None) -> Path | None:
    """Find an existing local .env, including the linked-worktree main repo."""
    root = Path(start) if start is not None else Path.cwd()
    for candidate in _candidate_env_paths(root):
        if candidate.is_file():
            return candidate
    return None


def _parse_assignment(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    name, value = stripped.split("=", 1)
    name = name.strip()
    if not _VALID_ENV_NAME.fullmatch(name):
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return name, value


def _read_assignments(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    assignments: dict[str, str] = {}
    for line in lines:
        parsed = _parse_assignment(line)
        if parsed is not None:
            assignments[parsed[0]] = parsed[1]
    return assignments


def _credential_value(env_name: str, start: Path | str | None = None) -> str | None:
    process_value = os.environ.get(env_name)
    if process_value:
        return process_value
    path = find_secret_env(start)
    if path is None:
        return None
    value = _read_assignments(path).get(env_name)
    return value or None


def credential_status(
    env_name: str,
    start: Path | str | None = None,
) -> CredentialStatus:
    """Return configured/source metadata without exposing the credential."""
    env_name = _validate_env_name(env_name)
    if os.environ.get(env_name):
        return CredentialStatus(True, "process_environment")
    path = find_secret_env(start)
    if path is not None and _read_assignments(path).get(env_name):
        return CredentialStatus(True, "local_env")
    return CredentialStatus(False, "missing")


def credentials_equal(
    first_env: str,
    second_env: str,
    start: Path | str | None = None,
) -> bool:
    """Compare two configured values and return only a boolean."""
    first_env = _validate_env_name(first_env)
    second_env = _validate_env_name(second_env)
    first = _credential_value(first_env, start)
    second = _credential_value(second_env, start)
    return bool(first and second and first == second)


def _target_for_save(start: Path | str | None = None) -> Path:
    root = Path(start) if start is not None else Path.cwd()
    existing = find_secret_env(root)
    if existing is not None:
        return existing
    linked = _linked_worktree_env_path(root)
    if linked is not None:
        return linked
    return root / ".env"


def _replace_assignment(path: Path, env_name: str, value: str | None) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    replaced = False
    output: list[str] = []
    for line in lines:
        parsed = _parse_assignment(line)
        if parsed is None or parsed[0] != env_name:
            output.append(line)
            continue
        replaced = True
        if value is not None:
            output.append(f"{env_name}={value}")
    if value is not None and not replaced:
        output.append(f"{env_name}={value}")
    return output


def _atomic_write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def save_credential(
    env_name: str,
    value: str,
    start: Path | str | None = None,
) -> CredentialStatus:
    """Atomically save one credential and update this process environment."""
    env_name = _validate_env_name(env_name)
    if not isinstance(value, str) or not value:
        raise ValueError("empty value does not clear a credential")
    if "\n" in value or "\r" in value:
        raise ValueError("credential value must not contain a line break")
    target = _target_for_save(start)
    _atomic_write(target, _replace_assignment(target, env_name, value))
    os.environ[env_name] = value
    return CredentialStatus(True, "local_env")


def clear_credential(
    env_name: str,
    start: Path | str | None = None,
) -> CredentialStatus:
    """Remove one credential from local .env and this process environment."""
    env_name = _validate_env_name(env_name)
    target = find_secret_env(start)
    if target is not None:
        _atomic_write(target, _replace_assignment(target, env_name, None))
    os.environ.pop(env_name, None)
    return CredentialStatus(False, "missing")
