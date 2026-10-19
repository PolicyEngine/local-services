"""Check hand-over requests with the interface's own rules, then read their files.

A request that breaks a rule is refused as a whole with a fixed code; nothing
is queued, and no exception text is returned.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from microcosm_emitter.compatibility import SUPPORTED_PROTOCOL_VERSIONS
from microcosm_emitter_interface.identifiers import (
    relative_file_path,
    repository_identifier,
    repository_prefix,
    safe_identifier,
)

#: Transport bounds for one staging run file and one hand-over. Microcosm's
#: staging contract keeps its own, smaller limits.
MAX_RUN_FILE_BYTES = 32 * 1024 * 1024
MAX_HAND_OVER_BYTES = 64 * 1024 * 1024
#: Staging contract version 1's pointer to the latest run, at the root.
LATEST_STAGING_POINTER_PATH = "latest_staging.json"


class RequestRefusedError(ValueError):
    """A request the emitter will not queue; ``code`` says why."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RunFilesHandOver:
    repository: str
    run_id: str
    files: list[tuple[str, bytes]]
    run_index_entry: dict[str, Any] | None
    flush: bool
    interval: float


def require_supported_version(message: Mapping[str, Any]) -> None:
    version = message.get("protocol_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in SUPPORTED_PROTOCOL_VERSIONS
    ):
        raise RequestRefusedError("UNSUPPORTED_PROTOCOL_VERSION")


def _valid(check, *arguments, **keywords):
    try:
        return check(*arguments, **keywords)
    except ValueError:
        raise RequestRefusedError("INVALID_REQUEST") from None


def run_reference(message: Mapping[str, Any]) -> tuple[str, str]:
    require_supported_version(message)
    return (
        _valid(repository_identifier, message.get("repository")),
        _valid(safe_identifier, message.get("run_id"), label="run_id"),
    )


def _read_local_file(value: object) -> bytes:
    if not isinstance(value, str) or not value:
        raise RequestRefusedError("INVALID_REQUEST")
    path = Path(value)
    if not path.is_absolute():
        raise RequestRefusedError("INVALID_REQUEST")
    try:
        if not path.is_file():
            raise RequestRefusedError("LOCAL_FILE_MISSING")
        if path.stat().st_size > MAX_RUN_FILE_BYTES:
            raise RequestRefusedError("FILE_TOO_LARGE")
        content = path.read_bytes()
    except OSError:
        raise RequestRefusedError("LOCAL_FILE_MISSING") from None
    if len(content) > MAX_RUN_FILE_BYTES:
        raise RequestRefusedError("FILE_TOO_LARGE")
    return content


def parse_stage_run_files(message: Mapping[str, Any]) -> RunFilesHandOver:
    """Validate a ``stage_run_files`` request and read every named file."""
    repository, run_id = run_reference(message)
    prefix = _valid(repository_prefix, message.get("prefix"))
    flush = message.get("flush")
    interval = message.get("commit_interval_seconds")
    if not isinstance(flush, bool) or (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not math.isfinite(interval)
        or interval < 0
    ):
        raise RequestRefusedError("INVALID_REQUEST")
    entries = message.get("files")
    if not isinstance(entries, list):
        raise RequestRefusedError("INVALID_REQUEST")
    targets: list[tuple[str, object]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise RequestRefusedError("INVALID_REQUEST")
        relative = _valid(
            relative_file_path, entry.get("relative_path"), label="relative_path"
        )
        targets.append((f"{prefix}/{run_id}/{relative}", entry.get("local_path")))
    pointer = message.get("latest_pointer")
    if pointer is not None:
        targets.append((LATEST_STAGING_POINTER_PATH, pointer))
    if len({path_in_repo for path_in_repo, _ in targets}) != len(targets):
        raise RequestRefusedError("INVALID_REQUEST")
    run_index_entry = message.get("run_index_entry")
    if run_index_entry is not None and (
        not isinstance(run_index_entry, Mapping)
        or run_index_entry.get("run_id") != run_id
    ):
        raise RequestRefusedError("INVALID_REQUEST")
    files = []
    total = 0
    for path_in_repo, local_path in targets:
        content = _read_local_file(local_path)
        total += len(content)
        if total > MAX_HAND_OVER_BYTES:
            raise RequestRefusedError("FILE_TOO_LARGE")
        files.append((path_in_repo, content))
    return RunFilesHandOver(
        repository=repository,
        run_id=run_id,
        files=files,
        run_index_entry=None if run_index_entry is None else dict(run_index_entry),
        flush=flush,
        interval=float(interval),
    )
