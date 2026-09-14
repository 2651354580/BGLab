"""One package-runtime identity shared by play, replay and certification."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


_RUNTIME_DIRECTORIES = (
    "data",
    "skills",
    "semantic",
    "src/core",
    "src/adapter",
)


class PackageIdentityError(ValueError):
    """Stable package error that never renders a host workspace path."""

    def __init__(self, code: str, relative_path: str) -> None:
        safe_path = str(relative_path).replace("\\", "/").strip() or "."
        if Path(safe_path).is_absolute() or ".." in Path(safe_path).parts:
            safe_path = "<outside-package>"
        self.code = str(code)
        self.relative_path = safe_path
        super().__init__(f"{self.code}: {self.relative_path}")


def _package_relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        raise PackageIdentityError(
            "PACKAGE_RUNTIME_PATH_ESCAPE",
            "<outside-package>",
        ) from None


def package_runtime_files(definition: Any) -> tuple[Path, ...]:
    root = Path(getattr(definition, "root", "")).resolve()
    if not root.is_dir():
        raise PackageIdentityError("PACKAGE_ROOT_MISSING", ".")
    paths: set[Path] = {root / "game.manifest.json"}
    for attribute in (
        "rules_path",
        "session_head_path",
        "action_schema",
        "model_actions_path",
        "engine_mapping_path",
        "adapter_script",
    ):
        value = getattr(definition, attribute, None)
        if value is not None:
            candidate = Path(value)
            _package_relative(root, candidate)
            paths.add(candidate)
    for relative in _RUNTIME_DIRECTORIES:
        directory = root / relative
        if not directory.is_dir():
            continue
        try:
            paths.update(path for path in directory.rglob("*") if path.is_file())
        except OSError:
            raise PackageIdentityError(
                "PACKAGE_RUNTIME_SCAN_FAILED",
                relative,
            ) from None
    resolved: list[Path] = []
    for path in paths:
        candidate = path.resolve()
        relative = _package_relative(root, candidate)
        if not candidate.is_file():
            raise PackageIdentityError(
                "PACKAGE_RUNTIME_FILE_MISSING",
                relative,
            )
        resolved.append(candidate)
    return tuple(
        sorted(
            set(resolved),
            key=lambda path: path.relative_to(root).as_posix(),
        )
    )


def package_runtime_fingerprint(definition: Any) -> str:
    root = Path(getattr(definition, "root", "")).resolve()
    digest = hashlib.sha256()
    for path in package_runtime_files(definition):
        relative_text = _package_relative(root, path)
        relative = relative_text.encode("utf-8")
        try:
            payload = path.read_bytes()
        except OSError:
            raise PackageIdentityError(
                "PACKAGE_RUNTIME_FILE_UNREADABLE",
                relative_text,
            ) from None
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()[:16]


class ResumePackageIdentityError(ValueError):
    """Default continuation needs a known, unchanged package identity."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def require_resume_package_identity(definition: Any, manifest: dict[str, Any]) -> None:
    """Read-only preflight; never infer compatibility from snapshot support."""
    recorded = manifest.get("game_package_fingerprint")
    if not isinstance(recorded, str) or not recorded.strip():
        raise ResumePackageIdentityError(
            "PACKAGE_FINGERPRINT_UNKNOWN",
            "legacy or unknown package identity; default resume requires the recorded package",
        )
    if recorded != package_runtime_fingerprint(definition):
        raise ResumePackageIdentityError(
            "PACKAGE_FINGERPRINT_MISMATCH",
            "current package differs from the saved game; use the recorded package to resume",
        )


__all__ = [
    "PackageIdentityError",
    "package_runtime_files",
    "package_runtime_fingerprint",
    "ResumePackageIdentityError",
    "require_resume_package_identity",
]
