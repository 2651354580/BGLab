"""Immutable meaning of one input at the shared history-append position."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class TurnInput:
    text: str
    kind: Literal["message", "authority_snapshot"] = "message"
    is_meta: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("turn input text must be a string")
        if self.kind not in {"message", "authority_snapshot"}:
            raise ValueError("unsupported turn input kind")
        if type(self.is_meta) is not bool:
            raise TypeError("turn input is_meta must be a boolean")


def normalize_turn_input(value: TurnInput | str) -> TurnInput:
    if isinstance(value, TurnInput):
        return value
    if isinstance(value, str):
        return TurnInput(value)
    raise TypeError("turn input must be a string or TurnInput")
