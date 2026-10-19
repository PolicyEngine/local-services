"""Recorded protocol messages, shipped so every emitter release can test them.

``fixtures/protocol-v<N>/`` holds one JSON file per message this package sends
under protocol version ``N``. The interface's tests check that each typed call
sends exactly its fixture; the emitter's contract tests check that it accepts
every fixture of every version it supports. A released fixture never changes:
a new message shape gets a new fixture, and an incompatible one a new version.

Each file holds:

- ``protocol_version`` and ``module`` (``telemetry``, ``orrery`` or ``host``);
- ``kind``: ``message`` (acknowledged), ``query`` (answered), ``configuration``
  (a module's startup settings) or ``hello`` (the host's description);
- ``sequence``: the order within a module, for queries that build on earlier
  ones;
- ``body``: the message, with the placeholders ``{directory}`` and
  ``{spool_path}`` in string values;
- ``files``: text files to create in ``{directory}`` before sending;
- ``response_keys``: the keys an answer must contain.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Literal

FIXTURE_ROOT = "fixtures"
DIRECTORY_PLACEHOLDER = "{directory}"
SPOOL_PATH_PLACEHOLDER = "{spool_path}"

type FixtureKind = Literal["message", "query", "configuration", "hello"]


@dataclass(frozen=True)
class ProtocolFixture:
    name: str
    path: str
    protocol_version: int
    module: str
    kind: FixtureKind
    body: dict[str, Any]
    sequence: int = 0
    files: dict[str, str] = field(default_factory=dict)
    response_keys: tuple[str, ...] = ()


def _fixture_files() -> list[tuple[str, Traversable]]:
    root = files("microcosm_emitter_interface").joinpath(FIXTURE_ROOT)
    found = []
    for version_directory in root.iterdir():
        if not version_directory.is_dir() or not version_directory.name.startswith(
            "protocol-v"
        ):
            continue
        for entry in version_directory.iterdir():
            if entry.name.endswith(".json"):
                found.append((f"{version_directory.name}/{entry.name}", entry))
    return sorted(found, key=lambda item: item[0])


def protocol_fixtures(version: int | None = None) -> list[ProtocolFixture]:
    """Every shipped fixture, or those of one protocol version."""
    fixtures = []
    for relative_path, entry in _fixture_files():
        document = json.loads(entry.read_text(encoding="utf-8"))
        fixture = ProtocolFixture(
            name=entry.name.removesuffix(".json"),
            path=relative_path,
            protocol_version=document["protocol_version"],
            module=document["module"],
            kind=document["kind"],
            body=document["body"],
            sequence=document.get("sequence", 0),
            files=dict(document.get("files", {})),
            response_keys=tuple(document.get("response_keys", ())),
        )
        if relative_path.split("/", 1)[0] != f"protocol-v{fixture.protocol_version}":
            raise ValueError(f"{relative_path} is filed under the wrong version.")
        if version is None or fixture.protocol_version == version:
            fixtures.append(fixture)
    return fixtures


def fixture_digests() -> dict[str, str]:
    """SHA-256 of every fixture file, keyed by its path below ``fixtures/``."""
    return {
        relative_path: hashlib.sha256(entry.read_bytes()).hexdigest()
        for relative_path, entry in _fixture_files()
    }


def materialize(
    fixture: ProtocolFixture, *, directory: Path, spool_path: Path
) -> dict[str, Any]:
    """The fixture's body with its placeholders replaced."""

    def replace(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace(DIRECTORY_PLACEHOLDER, str(directory)).replace(
                SPOOL_PATH_PLACEHOLDER, str(spool_path)
            )
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        return value

    return replace(fixture.body)
