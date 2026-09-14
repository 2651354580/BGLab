"""Small internal interfaces for the fixed attachment pipeline."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AttachmentValue:
    """Provider-independent attachment content retained in the transcript."""

    kind: str
    text: str
    attachment_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderContext:
    """Only the public query state an attachment builder may inspect."""

    profile: "AttachmentProfile"
    messages: list[dict[str, Any]]
    tool_uses: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    deps: Any = None
    state: Any = None
    tool_use_context: Any = None
    cwd: str = ""
    model: str = "deepseek-chat"
    permission_mode: str = "default"
    phase: str = "user_input"
    local: dict[str, Any] = field(default_factory=dict)


AttachmentBuild = Callable[
    [ProviderContext],
    Sequence[AttachmentValue] | Awaitable[Sequence[AttachmentValue]],
]
AttachmentPrefetch = Callable[
    [ProviderContext], Awaitable[Sequence[AttachmentValue]],
]


@dataclass(frozen=True)
class AttachmentProvider:
    """One isolated builder at a position owned by an AttachmentProfile."""

    id: str
    build: AttachmentBuild
    max_bytes: int | None = None
    required: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("attachment provider id must be non-empty")
        if self.max_bytes is not None and self.max_bytes <= 0:
            raise ValueError("attachment provider max_bytes must be positive")

    @classmethod
    def empty(cls, provider_id: str) -> "AttachmentProvider":
        async def build(_context: ProviderContext) -> tuple[AttachmentValue, ...]:
            return ()

        return cls(provider_id, build)


@dataclass(frozen=True)
class AttachmentProfile:
    """Fixed ordered provider lists; query control flow is not configurable."""

    name: str
    # Session-head providers are rendered immediately before every provider
    # request.  They are deliberately separate from transcript attachments:
    # the rendered messages are request-local and never accepted into the
    # durable message list.
    head: tuple[AttachmentProvider, ...] = ()
    user_input: tuple[AttachmentProvider, ...] = ()
    thread: tuple[AttachmentProvider, ...] = ()
    main: tuple[AttachmentProvider, ...] = ()
    # Typed values restored at a compaction boundary.  This seam is also
    # transcript-backed (unlike ``head``), so provider values must carry
    # stable IDs when the profile requires IDs.
    post_compact: tuple[AttachmentProvider, ...] = ()
    memory_prefetch: AttachmentPrefetch | None = None
    skill_prefetch: AttachmentPrefetch | None = None
    total_max_bytes: int | None = None
    require_ids: bool = False
    provider_failure_policy: str = "isolate"
    delivered_ids_attr: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("attachment profile name must be non-empty")
        ids = [
            provider.id
            for provider in (
                *self.head,
                *self.user_input,
                *self.thread,
                *self.main,
                *self.post_compact,
            )
        ]
        if len(ids) != len(set(ids)):
            raise ValueError("attachment provider ids must be unique within a profile")
        if self.total_max_bytes is not None and self.total_max_bytes <= 0:
            raise ValueError("attachment profile total_max_bytes must be positive")
        if self.provider_failure_policy not in {"isolate", "raise"}:
            raise ValueError("unknown attachment provider failure policy")
