"""Pure canonical identities for semantic engine transactions."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence


def canonical_steps_fingerprint(steps: Sequence[dict[str, Any]]) -> str:
    """Return the stable SHA-256 identity for canonical engine steps."""

    encoded = json.dumps(
        steps,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["canonical_steps_fingerprint"]
