"""Typed Orrery input hand-over: the emitter's existing graph publication queue.

The emitter hashes the selected preserved files, durably queues them and
delivers them; this package only names the files and their roles. The shapes
equal ``microcosm_emitter.orrery.contracts``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

type PublicationFileRole = Literal[
    "graph",
    "schema",
    "index",
    "declaration",
    "manifest",
    "binding",
    "summaries",
    "upstream",
]


class PublicationFile(TypedDict):
    """One preserved file, identified by its exact bytes and allowed role."""

    name: str
    role: PublicationFileRole
    bytes: int
    sha256: str


class PublicationInventory(TypedDict):
    """The versioned file inventory the emitter computed and will deliver."""

    version: Literal[1]
    publication_id: str
    files: list[PublicationFile]


class PublicationReceipt(TypedDict):
    """The emitter's latest delivery result for one publication."""

    version: Literal[1]
    publication_id: str | None
    status: Literal["pending", "published", "skipped", "failed", "no_evidence"]
    error_code: str | None
    url: NotRequired[str | None]
    attempts: NotRequired[int]
    inventory_sha256: NotRequired[str]
    preserved_directory: NotRequired[str]
    recorded_execution: NotRequired[bool]


@dataclass(frozen=True)
class OrrerySettings:
    """Configuration for the emitter's Orrery module; defaults are production."""

    development_collector_url: str | None = None
    publication_origin: str | None = None

    def configuration(self, spool_path: Path) -> dict[str, Any]:
        configuration: dict[str, Any] = {"spool_path": str(spool_path)}
        if self.development_collector_url is not None:
            configuration["development_collector_url"] = self.development_collector_url
        if self.publication_origin is not None:
            configuration["publication_origin"] = self.publication_origin
        return configuration


def unavailable_receipt(publication_id: str | None) -> PublicationReceipt:
    """No acknowledgement: no claim that the emitter queued anything."""
    return {
        "version": 1,
        "publication_id": publication_id,
        "status": "failed",
        "error_code": "local_service_unavailable",
    }
