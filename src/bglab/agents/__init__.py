"""Reusable agent runtimes shared by Code Agent and configured modes."""

from .in_process import (
    InProcessTeammateConfig,
    InProcessTeammateRunner,
    TeammateRunResult,
)

__all__ = [
    "InProcessTeammateConfig",
    "InProcessTeammateRunner",
    "TeammateRunResult",
]
