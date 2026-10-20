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

**Order.** A queued change can wait (an outage, a missing credential, backoff)
while newer changes land, from this machine or another one. So:

- A change still pending ``RELEASE_CHANGE_EXPIRY_SECONDS`` after it was handed
  over is final: ``refused`` with ``REQUEST_EXPIRED``. It never lands days
  later, for example after ``hf auth login``.
- An ordered change (a promotion, a withdrawal, or a publication that makes
  its build current) is refused with ``NEWER_CHANGE_LANDED`` when an ordered
  change requested after it has already landed on the repository's record.
  The writer knows that from two places: this machine's queue (its own later
  requests that landed, including ones that left the record unchanged), and
  the title of the commit that last changed ``releases.json``, which ends
  ``[newest request <time>]``: the newest request time of any ordered change
  applied to the record so far.
- A publication that only adds a build is neither refused for order nor
  counted as a newer change: it undoes nothing, and two concurrent
  publications must both land (plan item 5). Its commit carries the recorded
  time forward.

What this cannot see: a newer change on another machine that left
``releases.json`` unchanged (promoting the build that is already current)
makes no commit, so it leaves no time in a commit title; only the expiry
bounds how late an older change can still land after it. Request times come
from each machine's clock.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
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
#: A change requested after this one has already landed on the record.
NEWER_CHANGE_LANDED = "NEWER_CHANGE_LANDED"
#: The change was still pending ``RELEASE_CHANGE_EXPIRY_SECONDS`` after it
#: was handed over.
REQUEST_EXPIRED = "REQUEST_EXPIRED"
#: How long a release change may stay pending: long enough for a Hub outage
#: or a few backoffs (at most five minutes apart), short enough that an old
#: request never lands silently after the operator has moved on.
RELEASE_CHANGE_EXPIRY_SECONDS = 3600.0
#: Ends the title of every commit the writer makes.
_NEWEST_REQUEST = re.compile(r" \[newest request (?P<time>[^\]]+)\]$")
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


def expired_change(requested_at: float, now: float) -> ReleaseOutcome | None:
    """``REQUEST_EXPIRED`` once a change has waited too long; else ``None``."""
    if now - requested_at <= RELEASE_CHANGE_EXPIRY_SECONDS:
        return None
    return _refused(
        REQUEST_EXPIRED,
        "The change did not land within an hour of its request; request it "
        "again if it is still wanted.",
    )


def _request_time_text(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="microseconds")


def _request_time(text: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo is not None else None


def _as_written(seconds: float) -> float:
    """``seconds`` as a commit title records it (to the microsecond)."""
    written = _request_time(_request_time_text(seconds))
    return seconds if written is None else written


def _newest_request_on_record(
    repository: DatasetRepository, head: str, files: list[str]
) -> float | None:
    """The newest request time in the title of ``releases.json``'s last commit.

    ``None`` without a record, or when that commit was not the writer's
    (P1's one-time migration, for example).
    """
    if RELEASES_RECORD_PATH not in files:
        return None
    title = repository.last_commit_title(RELEASES_RECORD_PATH, revision=head)
    match = _NEWEST_REQUEST.search(title or "")
    return None if match is None else _request_time(match["time"])


def is_ordered(change: ReleaseChange) -> bool:
    """Whether the change sets ``current`` or withdraws a build.

    Only such changes can undo each other, so only they are refused when a
    newer one landed first, and only they count as newer changes.
    """
    return not (isinstance(change, PublishRelease) and not change.make_current)


def _commit_message(change: ReleaseChange, newest_request: float | None) -> str:
    release, build = change.release, change.build
    if isinstance(change, PublishRelease):
        suffix = " and make it current" if change.make_current else ""
        title = f"Publish build {build} of {release}{suffix}"
    elif isinstance(change, PromoteBuild):
        title = f"Make build {build} the current build of {release}"
    else:
        title = f"Withdraw build {build} of {release}"
    if newest_request is None:
        return title
    return f"{title} [newest request {_request_time_text(newest_request)}]"


def _first_violation(error: ReleaseRecordError) -> ReleaseOutcome:
    first = error.violations[0]
    return _refused(first.code, "; ".join(item.message for item in error.violations))


def apply_release_change(
    repository: DatasetRepository,
    change: ReleaseChange,
    *,
    published_at: str,
    requested_at: float | None = None,
    newest_local_request: float | None = None,
) -> ReleaseOutcome:
    """Apply one validated release change, or report why it did not land.

    ``requested_at`` is when the change was handed over (now, by default).
    ``newest_local_request`` is the newest request time of this machine's
    ordered changes (see ``is_ordered``) to the repository that have landed.
    """
    requested = _as_written(time.time() if requested_at is None else requested_at)
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
            record_bytes = (
                repository.download(RELEASES_RECORD_PATH, revision=head)
                if RELEASES_RECORD_PATH in files
                else None
            )
            before = (
                None if record_bytes is None else ReleasesRecord.parse(record_bytes)
            )
        except ReleaseRecordError as error:
            return _first_violation(error)
        except Exception:
            return _pending(REPOSITORY_UNAVAILABLE)
        record = before or ReleasesRecord.empty()
        if isinstance(change, PublishRelease) and any(
            path.startswith(folder + "/") for path in files
        ):
            return _already_published(repository, change, record, head)
        try:
            on_record = _newest_request_on_record(repository, head, files)
        except Exception:
            return _pending(REPOSITORY_UNAVAILABLE)
        landed = [
            seconds
            for seconds in (on_record, newest_local_request)
            if seconds is not None
        ]
        newest_landed = max(landed, default=None)
        ordered = is_ordered(change)
        if ordered and newest_landed is not None and newest_landed > requested:
            return _refused(
                NEWER_CHANGE_LANDED,
                f"A change to {RELEASES_RECORD_PATH} requested after this one "
                "has already landed; request this change again if it is "
                "still wanted.",
            )
        try:
            if isinstance(change, PublishRelease):
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
        if not new_files and record_bytes == after.serialize():
            # Nothing to write (the build is already current, for example):
            # the Hub would skip an empty commit and answer with its head.
            return ReleaseOutcome("published", commit=head)
        operations = [add_operation(path, local) for path, local in new_files.items()]
        operations.append(add_operation(RELEASES_RECORD_PATH, after.serialize()))
        message = _commit_message(
            change, max([requested, *landed]) if ordered else newest_landed
        )
        try:
            commit = repository.commit(operations, message=message, parent_commit=head)
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
