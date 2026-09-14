"""Resolve BGLab runtime resources in source and frozen distributions."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def runtime_root() -> Path:
    """Return the directory containing bundled ``games`` and ``scripts``."""

    configured = os.environ.get("BGLAB_RUNTIME_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def runtime_path(*parts: str) -> Path:
    return runtime_root().joinpath(*parts)


def node_executable() -> str:
    """Find the bundled Node runtime before consulting the host PATH."""

    configured = os.environ.get("BGLAB_NODE", "").strip()
    candidates = [
        Path(configured).expanduser() if configured else None,
        runtime_path("runtime", "node", "node.exe"),
        runtime_path("node", "node.exe"),
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return str(candidate.resolve())
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        raise RuntimeError(
            "BGLab bundled Node runtime is missing; reinstall the application",
        )
    discovered = shutil.which("node")
    if discovered:
        return discovered
    raise RuntimeError("BGLab Node runtime is missing; reinstall the application")

