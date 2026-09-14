"""Fixed-phase attachment assembly for the BGLab query engine."""

from bglab.engine.attachments.collector import (
    AttachmentCollectionError,
    collect_providers,
)
from bglab.engine.attachments.lifecycle import AttachmentIteration, AttachmentTurn
from bglab.engine.attachments.messages import attachment_to_message
from bglab.engine.attachments.types import (
    AttachmentBuild,
    AttachmentPrefetch,
    AttachmentProfile,
    AttachmentProvider,
    AttachmentValue,
    ProviderContext,
)

__all__ = [
    "AttachmentBuild",
    "AttachmentCollectionError",
    "AttachmentIteration",
    "AttachmentPrefetch",
    "AttachmentProfile",
    "AttachmentProvider",
    "AttachmentTurn",
    "AttachmentValue",
    "ProviderContext",
    "attachment_to_message",
    "collect_providers",
]
