"""Explicit data contracts for immutable graph publication.

The request and receipt shapes are the interface package's, so the emitter's
service, its in-process client and ``microcosm_emitter_interface`` share one
definition.
"""

from dataclasses import dataclass
from pathlib import Path

from microcosm_emitter_interface.orrery import PublicationFile as PublicationFile
from microcosm_emitter_interface.orrery import (
    PublicationFileRole as PublicationFileRole,
)
from microcosm_emitter_interface.orrery import (
    PublicationInventory as PublicationInventory,
)
from microcosm_emitter_interface.orrery import (
    PublicationReceipt as PublicationReceipt,
)


@dataclass(frozen=True)
class ClaimedGraphPublication:
    """A job reserved until lease_until; the timestamp also identifies its lease."""

    publication_id: str
    directory: Path
    inventory: PublicationInventory
    lease_until: float
