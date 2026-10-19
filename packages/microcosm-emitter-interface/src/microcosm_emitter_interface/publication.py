"""Typed release publication: one build folder and one ``releases.json`` change.

``PublishRelease`` names the release, the build (the attempt ID Microcosm
already assigns), whether to make the build current, and each file with its
role. The emitter writes the files to ``releases/<release>/<build>/`` and the
updated ``releases.json`` in one commit, after the checks in
``microcosm_emitter_interface.releases``. ``PromoteBuild`` and
``WithdrawBuild`` change only ``releases.json``.

Publication is not best-effort: the receipt says whether the change was
``published``, is still ``pending`` (retried from the emitter's queue) or was
``refused`` by a rule. Constructing a request that breaks the shape rules
raises ``ValueError``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, TypedDict

from microcosm_emitter_interface.bundles import handover_identifier
from microcosm_emitter_interface.identifiers import (
    bare_file_name,
    repository_identifier,
    role_identifier,
    safe_identifier,
)
from microcosm_emitter_interface.protocol import PROTOCOL_VERSION
from microcosm_emitter_interface.releases import (
    RELEASE_MANIFEST_NAME,
    is_pointer_file,
)

RELEASE_MANIFEST_ROLE: Final = "release_manifest"
RELEASE_CHANGE: Final = "release_change"
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ReleaseFile:
    """One preserved file of the build and what it is (``dataset``, ...)."""

    name: str
    role: str
    local_path: Path
    sha256: str
    bytes: int

    def __post_init__(self) -> None:
        bare_file_name(self.name, label="release file name")
        role_identifier(self.role)
        if is_pointer_file(self.name):
            raise ValueError("A release file may not be a latest*.json pointer.")
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            raise ValueError("sha256 must be a lowercase hexadecimal digest.")
        if (
            isinstance(self.bytes, bool)
            or not isinstance(self.bytes, int)
            or self.bytes < 0
        ):
            raise ValueError("bytes must be a nonnegative integer.")


def _check_reference(repository: str, release: str, build: str) -> None:
    repository_identifier(repository)
    safe_identifier(release, label="release")
    safe_identifier(build, label="build")


@dataclass(frozen=True)
class PublishRelease:
    """Publish a new build of a release, optionally as its current build.

    ``release_role`` is the release's role in ``releases.json`` (for example
    ``national_default``); a release keeps the role it was first published
    with. The files must include ``release_manifest.json`` with the role
    ``release_manifest``, naming this release and build.
    """

    repository: str
    release: str
    build: str
    release_role: str
    make_current: bool
    files: Sequence[ReleaseFile]
    protocol_version: int = field(default=PROTOCOL_VERSION, repr=False)

    def __post_init__(self) -> None:
        _check_reference(self.repository, self.release, self.build)
        role_identifier(self.release_role, label="release_role")
        if not isinstance(self.make_current, bool):
            raise ValueError("make_current must be a boolean.")
        object.__setattr__(self, "files", tuple(self.files))
        if any(not isinstance(item, ReleaseFile) for item in self.files):
            raise ValueError("files must be ReleaseFile values.")
        names = [item.name for item in self.files]
        if len(set(names)) != len(names):
            raise ValueError("A build names each file once.")
        manifests = [item for item in self.files if item.name == RELEASE_MANIFEST_NAME]
        if len(manifests) != 1 or manifests[0].role != RELEASE_MANIFEST_ROLE:
            raise ValueError(
                f"A build has one {RELEASE_MANIFEST_NAME} with the role "
                f"{RELEASE_MANIFEST_ROLE}."
            )
        if [item.role for item in self.files].count(RELEASE_MANIFEST_ROLE) != 1:
            raise ValueError(f"Only {RELEASE_MANIFEST_NAME} has its role.")

    def message(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "command": "publish_release",
            "repository": self.repository,
            "release": self.release,
            "build": self.build,
            "release_role": self.release_role,
            "make_current": self.make_current,
            "files": [
                {
                    "name": item.name,
                    "role": item.role,
                    "local_path": str(Path(item.local_path).resolve()),
                    "sha256": item.sha256,
                    "bytes": item.bytes,
                }
                for item in self.files
            ],
        }

    def handover_id(self) -> str:
        return handover_identifier(RELEASE_CHANGE, self.message())


@dataclass(frozen=True)
class PromoteBuild:
    """Make a listed, non-withdrawn build the release's current build."""

    repository: str
    release: str
    build: str
    protocol_version: int = field(default=PROTOCOL_VERSION, repr=False)

    def __post_init__(self) -> None:
        _check_reference(self.repository, self.release, self.build)

    def message(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "command": "promote_build",
            "repository": self.repository,
            "release": self.release,
            "build": self.build,
        }

    def handover_id(self) -> str:
        return handover_identifier(RELEASE_CHANGE, self.message())


@dataclass(frozen=True)
class WithdrawBuild:
    """Mark a build withdrawn; its folder and files stay as published."""

    repository: str
    release: str
    build: str
    protocol_version: int = field(default=PROTOCOL_VERSION, repr=False)

    def __post_init__(self) -> None:
        _check_reference(self.repository, self.release, self.build)

    def message(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "command": "withdraw_build",
            "repository": self.repository,
            "release": self.release,
            "build": self.build,
        }

    def handover_id(self) -> str:
        return handover_identifier(RELEASE_CHANGE, self.message())


type ReleaseChange = PublishRelease | PromoteBuild | WithdrawBuild


class ReleaseReceipt(TypedDict):
    """The emitter's latest result for one release change.

    ``published`` and ``refused`` are final; ``pending`` is retried, with the
    last attempt's ``error_code`` (``REPOSITORY_UNAVAILABLE``,
    ``UPLOAD_FAILED``, ``PARENT_CONFLICT``, ``CREDENTIAL_MISSING``,
    ``PRESERVED_FILES_MISSING``). A refusal's ``error_code`` names the broken
    rule, for example ``BUILD_FOLDER_EXISTS`` or ``POINTER_FILES_PRESENT``.
    """

    handover_id: str
    status: Literal["pending", "published", "refused", "unavailable"]
    error_code: str | None
    message: str | None
    commit: str | None
    repository: str
    release: str
    build: str
    attempts: int


def unavailable_release_receipt(request: ReleaseChange) -> ReleaseReceipt:
    return {
        "handover_id": request.handover_id(),
        "status": "unavailable",
        "error_code": None,
        "message": None,
        "commit": None,
        "repository": request.repository,
        "release": request.release,
        "build": request.build,
        "attempts": 0,
    }
