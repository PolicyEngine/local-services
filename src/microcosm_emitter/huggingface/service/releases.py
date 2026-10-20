"""The release writer: the only code that changes a dataset repository's
``releases.json``.

Each change (publish a build, make a build current, withdraw a build) is one
commit:

1. read ``releases.json`` and the file list at ``main``'s head;
2. apply the change to the record;
3. run the shared checks in ``microcosm_emitter_interface.releases``;
4. commit the new build folder (for a publication) and ``releases.json``
   with ``parent_commit`` set to that head.

If another commit landed first, the Hub refuses the stale parent; the writer
then reads the new head and applies the change again, so concurrent
publications never lose each other's changes. The writer creates no Hugging
Face tags or branches, no ``current/`` folder and no ``latest*.json`` file,
and never touches existing tags or files outside the new build folder.

Who may make a change is not settled; ``ReleaseChangePolicy`` is where that
decision plugs in. The module applies it when a change is handed over, with
the caller's identity, and the default allows every change.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from microcosm_emitter.huggingface.service.hub import (
    MAX_PARENT_CONFLICT_ATTEMPTS,
    PRESERVED_BYTES_CHANGED,
    PRESERVED_FILES_MISSING,
    REPOSITORY_UNAVAILABLE,
    UPLOAD_FAILED,
    DatasetRepository,
    add_operation,
    is_parent_conflict,
)
from microcosm_emitter.huggingface.service.queue import sha256_file
from microcosm_emitter_interface.publication import (
    PromoteBuild,
    PublishRelease,
    ReleaseChange,
)
from microcosm_emitter_interface.releases import (
    RELEASE_MANIFEST_NAME,
    RELEASES_RECORD_PATH,
    ReleaseRecordError,
    ReleasesRecord,
    add_build,
    build_folder,
    check_change,
    check_commit,
    check_repository,
    make_current,
    withdraw_build,
)

PARENT_CONFLICT = "PARENT_CONFLICT"
type ReleaseStatus = Literal["pending", "published", "refused"]


@dataclass(frozen=True)
class ReleaseCaller:
    """Who handed a release change over, as far as the emitter can tell.

    ``user`` is the account the emitter runs as, which is the build's;
    ``parent_pid`` is the build process that started this emitter host. Who
    may promote or withdraw is not settled, so this may gain fields once it
    is.
    """

    user: str
    parent_pid: int


#: Returns ``None`` to allow a change, or a reason to refuse it
#: (``NOT_AUTHORIZED``). It runs when the change is handed over, so a refused
#: change is never queued.
type ReleaseChangePolicy = Callable[[ReleaseChange, ReleaseCaller], str | None]


def allow_every_change(change: ReleaseChange, caller: ReleaseCaller) -> str | None:
    """The default until who may change releases is decided."""
    return None


@dataclass(frozen=True)
class ReleaseOutcome:
    """One attempt's result: final unless ``retry``."""

    status: ReleaseStatus
    error_code: str | None = None
    message: str | None = None
    commit: str | None = None
    retry: bool = False
    committed: bool = False


def _refused(code: str, message: str) -> ReleaseOutcome:
    return ReleaseOutcome("refused", code, message)


def _pending(code: str) -> ReleaseOutcome:
    return ReleaseOutcome("pending", code, retry=True)


def _preserved_files(change: PublishRelease) -> ReleaseOutcome | None:
    for item in change.files:
        try:
            if not item.local_path.is_file():
                return _pending(PRESERVED_FILES_MISSING)
            if (
                item.local_path.stat().st_size != item.bytes
                or sha256_file(item.local_path) != item.sha256
            ):
                return _refused(
                    PRESERVED_BYTES_CHANGED,
                    f"{item.name} changed after it was handed over.",
                )
        except OSError:
            return _pending(PRESERVED_FILES_MISSING)
    return None


def _commit_message(change: ReleaseChange) -> str:
    release, build = change.release, change.build
    if isinstance(change, PublishRelease):
        suffix = " and make it current" if change.make_current else ""
        return f"Publish build {build} of {release}{suffix}"
    if isinstance(change, PromoteBuild):
        return f"Make build {build} the current build of {release}"
    return f"Withdraw build {build} of {release}"


def _first_violation(error: ReleaseRecordError) -> ReleaseOutcome:
    first = error.violations[0]
    return _refused(first.code, "; ".join(item.message for item in error.violations))


def apply_release_change(
    repository: DatasetRepository, change: ReleaseChange, *, published_at: str
) -> ReleaseOutcome:
    """Apply one validated release change, or report why it did not land."""
    if isinstance(change, PublishRelease):
        preserved = _preserved_files(change)
        if preserved is not None:
            return preserved
    release, build = change.release, change.build
    folder = build_folder(release, build)
    new_files = {
        f"{folder}/{item.name}": item.local_path
        for item in (change.files if isinstance(change, PublishRelease) else ())
    }
    for _ in range(MAX_PARENT_CONFLICT_ATTEMPTS):
        try:
            head = repository.head_revision()
            files = repository.list_files(revision=head)
            before = (
                ReleasesRecord.parse(
                    repository.download(RELEASES_RECORD_PATH, revision=head)
                )
                if RELEASES_RECORD_PATH in files
                else None
            )
        except ReleaseRecordError as error:
            return _first_violation(error)
        except Exception:
            return _pending(REPOSITORY_UNAVAILABLE)
        record = before or ReleasesRecord.empty()
        try:
            if isinstance(change, PublishRelease):
                if any(path.startswith(folder + "/") for path in files):
                    return _already_published(repository, change, record, head)
                after = add_build(
                    record,
                    release=release,
                    build=build,
                    release_role=change.release_role,
                    published_at=published_at,
                    make_current=change.make_current,
                )
            elif isinstance(change, PromoteBuild):
                after = make_current(record, release=release, build=build)
            else:
                after = withdraw_build(record, release=release, build=build)
        except ReleaseRecordError as error:
            return _first_violation(error)
        added_paths = [*new_files, RELEASES_RECORD_PATH]

        def read_file(path: str, revision: str = head) -> bytes:
            local = new_files.get(path)
            if local is not None:
                return local.read_bytes()
            return repository.download(path, revision=revision)

        try:
            violations = [
                *check_change(before, after),
                *check_commit(
                    before_files=files,
                    added_paths=added_paths,
                    deleted_paths=[],
                    before=before,
                    after=after,
                ),
                *check_repository(after, [*files, *added_paths], read_file),
            ]
        except Exception:
            return _pending(REPOSITORY_UNAVAILABLE)
        if violations:
            return _refused(
                violations[0].code, "; ".join(item.message for item in violations)
            )
        operations = [add_operation(path, local) for path, local in new_files.items()]
        operations.append(add_operation(RELEASES_RECORD_PATH, after.serialize()))
        try:
            commit = repository.commit(
                operations, message=_commit_message(change), parent_commit=head
            )
        except Exception as error:
            if is_parent_conflict(error):
                continue
            return _pending(UPLOAD_FAILED)
        return ReleaseOutcome("published", commit=commit, committed=True)
    return _pending(PARENT_CONFLICT)


def _already_published(
    repository: DatasetRepository,
    change: PublishRelease,
    record: ReleasesRecord,
    head: str,
) -> ReleaseOutcome:
    """A build folder that exists is refused, unless this build landed.

    A retried publication whose earlier attempt landed (its commit raised, or
    the receipt was lost) finds its build listed with a byte-identical
    manifest: that is ``published``, not refused, even if another build has
    been made current since. Publishing never promotes an already published
    build; ``promote_build`` does that.
    """
    release, build = change.release, change.build
    entry = record.releases.get(release)
    manifest_path = f"{build_folder(release, build)}/{RELEASE_MANIFEST_NAME}"
    local_manifest = next(
        item.local_path for item in change.files if item.name == RELEASE_MANIFEST_NAME
    )
    exists_message = f"{build_folder(release, build)}/ already exists."
    if entry is None or build not in entry.builds:
        return _refused("BUILD_FOLDER_EXISTS", exists_message)
    try:
        same = repository.download(manifest_path, revision=head) == (
            local_manifest.read_bytes()
        )
        commit = repository.last_commit(manifest_path) if same else None
    except Exception:
        return _pending(REPOSITORY_UNAVAILABLE)
    if not same:
        return _refused("BUILD_FOLDER_EXISTS", exists_message)
    message = None
    if change.make_current and entry.current != build:
        message = (
            f"Build {build} of {release} was already published; the current "
            f"build is {entry.current}."
        )
    return ReleaseOutcome("published", message=message, commit=commit or head)
