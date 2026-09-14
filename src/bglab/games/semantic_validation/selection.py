from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .closest import ClosestSearchResult, _freeze_json


_CANDIDATE_LABEL_RE = re.compile(r"C[1-5]\Z")


@dataclass(frozen=True)
class SelectedProgram:
    label: str
    program_id: str
    engine_steps: tuple[Mapping[str, Any], ...]
    binding_fingerprint: str


def select_candidate(
    result: ClosestSearchResult,
    label: str,
    *,
    decision_id: str,
    state_hash: str,
) -> SelectedProgram:
    if decision_id != result.decision_id:
        raise ValueError("stale semantic selection decision")
    if state_hash != result.state_hash:
        raise ValueError("stale semantic selection state")
    if not isinstance(label, str) or _CANDIDATE_LABEL_RE.fullmatch(label) is None:
        raise ValueError("invalid semantic candidate label")
    candidate = next((item for item in result.candidates if item.label == label), None)
    if candidate is None:
        raise ValueError("unknown semantic candidate label")
    return SelectedProgram(
        label=candidate.label,
        program_id=candidate.program.program_id,
        engine_steps=tuple(_freeze_json(step) for step in candidate.program.engine_steps),
        binding_fingerprint=candidate.binding_fingerprint,
    )
