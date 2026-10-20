"""Hugging Face dataset-repository calls, ported from Microcosm's staging storage.

``DatasetRepository`` keeps the behaviour of Microcosm's
``HuggingFaceDatasetStorage`` (``staging_storage.py``) for the calls the
emitter makes, with an injected ``HfApi``-shaped client so tests use an
in-memory Hub. The client carries the build's own credential, found exactly as
``microcosm_emitter.core.auth`` finds it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from microcosm_emitter.core.auth import _huggingface_token

#: HTTP statuses the Hub returns when ``parent_commit`` is no longer the head.
PARENT_CONFLICT_STATUSES = frozenset({409, 412})
#: Errors huggingface_hub raises when the Hub answered that a file is missing
#: (``RemoteEntryNotFoundError`` from 1.0, ``EntryNotFoundError`` before).
_MISSING_FILE_ERROR_NAMES = frozenset(
    {"EntryNotFoundError", "RemoteEntryNotFoundError"}
)
#: Raised when the Hub did not answer (a connection error, a timeout, or a 429
#: or 5xx response for an uncached file). It subclasses ``EntryNotFoundError``
#: and ``FileNotFoundError``, so a check by class alone mistakes it for absence.
_NO_ANSWER_ERROR_NAME = "LocalEntryNotFoundError"


def hugging_face_api() -> Any | None:
    """An ``HfApi`` with the build's credential, or ``None`` without one.

    The credential is ``HF_TOKEN``, then ``HUGGINGFACE_TOKEN``, then the token
    saved by ``hf auth login``: the same order telemetry uses. It is read again
    for every cycle, so a login during a long build is picked up.
    """
    token = _huggingface_token()
    if not token:
        return None
    from huggingface_hub import HfApi

    return HfApi(token=token)


def add_operation(path_in_repo: str, content: bytes | Path) -> Any:
    """One file write in a commit, from bytes or a preserved local file."""
    from huggingface_hub import CommitOperationAdd

    return CommitOperationAdd(
        path_in_repo=path_in_repo,
        path_or_fileobj=content if isinstance(content, bytes) else str(content),
    )


def status_code(error: BaseException) -> int | None:
    response = getattr(error, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def is_parent_conflict(error: BaseException) -> bool:
    """Another commit landed after the head this commit was built on."""
    return status_code(error) in PARENT_CONFLICT_STATUSES


def is_absent(error: BaseException) -> bool:
    """The Hub answered HTTP 404 because the file does not exist, nothing else.

    Every other error, including a missing revision or repository and every
    error raised without the Hub's answer, means the file's state is unknown.
    """
    names = {cls.__name__ for cls in type(error).__mro__}
    return (
        _NO_ANSWER_ERROR_NAME not in names
        and bool(names & _MISSING_FILE_ERROR_NAMES)
        and status_code(error) == 404
    )


class DatasetRepository:
    """Read and write files in one Hugging Face dataset repository."""

    def __init__(self, repo_id: str, api: Any) -> None:
        if not isinstance(repo_id, str) or not repo_id.strip():
            raise ValueError("A Hugging Face hand-over requires a repository.")
        self.repo_id = repo_id.strip()
        self.api = api

    def head_revision(self) -> str:
        info = self.api.repo_info(
            repo_id=self.repo_id, repo_type="dataset", revision="main"
        )
        sha = (
            info.get("sha") if isinstance(info, Mapping) else getattr(info, "sha", None)
        )
        if not sha:
            raise RuntimeError("The Hub reported no head revision.")
        return str(sha)

    def commit(
        self,
        operations: Sequence[Any],
        *,
        message: str,
        parent_commit: str | None = None,
    ) -> str:
        """Write several files in one commit and return the new revision.

        Pinning ``parent_commit`` makes a concurrent write fail with a
        conflict (see ``is_parent_conflict``) instead of interleaving.
        """
        info = self.api.create_commit(
            repo_id=self.repo_id,
            operations=list(operations),
            commit_message=message,
            repo_type="dataset",
            parent_commit=parent_commit,
        )
        oid = (
            info.get("oid") if isinstance(info, Mapping) else getattr(info, "oid", None)
        )
        if not isinstance(oid, str) or not oid.strip():
            raise RuntimeError("The Hub commit reported no revision.")
        return oid.strip()

    def download(self, path_in_repo: str, *, revision: str | None = None) -> bytes:
        local = self.api.hf_hub_download(
            repo_id=self.repo_id,
            filename=path_in_repo,
            repo_type="dataset",
            revision=revision,
            force_download=revision is None,
        )
        return Path(local).read_bytes()

    def file_exists(self, path_in_repo: str, *, revision: str | None = None) -> bool:
        return bool(
            self.api.file_exists(
                repo_id=self.repo_id,
                filename=path_in_repo,
                repo_type="dataset",
                revision=revision,
            )
        )

    def list_files(self, *, revision: str) -> list[str]:
        return sorted(
            self.api.list_repo_files(
                repo_id=self.repo_id, repo_type="dataset", revision=revision
            )
        )

    def last_commit(self, path_in_repo: str) -> str | None:
        """The commit that last changed ``path_in_repo``, when the Hub reports it."""
        entries = self.api.get_paths_info(
            repo_id=self.repo_id,
            paths=[path_in_repo],
            expand=True,
            repo_type="dataset",
        )
        for entry in entries or []:
            commit = (
                entry.get("last_commit")
                if isinstance(entry, Mapping)
                else getattr(entry, "last_commit", None)
            )
            oid = (
                commit.get("oid")
                if isinstance(commit, Mapping)
                else getattr(commit, "oid", None)
            )
            if oid:
                return str(oid)
        return None

    def _access_token(self) -> Mapping[str, Any] | None:
        info = self.api.whoami()
        auth = info.get("auth") if isinstance(info, Mapping) else None
        token = auth.get("accessToken") if isinstance(auth, Mapping) else None
        return token if isinstance(token, Mapping) else None

    def credential_role(self) -> str | None:
        """``read``, ``write`` or ``fineGrained``, as the Hub reports the token."""
        token = self._access_token()
        role = token.get("role") if token else None
        return str(role) if role else None

    def credential_can_write(self) -> bool | None:
        """Whether the credential may write this repository; ``None`` if unknown.

        ``True`` for a classic write token or a fine-grained token whose scopes
        grant ``repo.write`` on the repository, its owner or globally.
        """
        token = self._access_token()
        if token is None:
            return None
        role = str(token.get("role") or "")
        if role == "read":
            return False
        if role == "write":
            return True
        if role != "fineGrained":
            return None
        fine = token.get("fineGrained")
        fine = fine if isinstance(fine, Mapping) else {}
        if "repo.write" in (fine.get("global") or []):
            return True
        owner = self.repo_id.split("/", 1)[0]
        for scope in fine.get("scoped") or []:
            if not isinstance(scope, Mapping):
                continue
            entity = scope.get("entity")
            entity = entity if isinstance(entity, Mapping) else {}
            name = str(entity.get("name") or "")
            kind = str(entity.get("type") or "")
            covers = name == self.repo_id or (kind in {"user", "org"} and name == owner)
            if covers and "repo.write" in (scope.get("permissions") or []):
                return True
        return False
