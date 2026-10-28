"""Report on and deliver a spool database copied from a build elsewhere.

A build on a machine without a Hugging Face credential, such as Modal, sends
telemetry with a collector token it is given. Events it could not deliver stay
in its spool database, which the Modal runner copies to the runs volume.
`microcosm-emitter status --spool` and `deliver --spool` read and deliver that
copy on the operator's workstation.

Opening a spool with ``EventSpool`` migrates it and prunes it: events older
than the retention period, and the oldest beyond the size limit, are deleted.
It also makes the file and its directory private to this user. So
``spool_status`` reads a copy in SQLite's read-only mode, changing nothing,
and ``deliver_spool`` opens it as a copy, which changes no permissions and
prunes nothing: an event leaves the copy only when the collector acknowledges
it. A copy may be delivered long after it was made, from any directory: the
spool records no paths for telemetry.
"""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

from alembic.util import CommandError
from sqlalchemy import func, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from microcosm_emitter.core.auth import CollectorSession, SessionResult
from microcosm_emitter.core.collector_origin import resolve_collector_origin
from microcosm_emitter.core.constants import DRAIN_RETRY_SECONDS
from microcosm_emitter.core.database import create_read_only_spool_engine
from microcosm_emitter.core.migrations import require_known_revision
from microcosm_emitter.core.models import TelemetryEventRecord, TelemetryRunRecord
from microcosm_emitter.telemetry.service.collector import CollectorDelivery
from microcosm_emitter.telemetry.service.spool import EventSpool


class RunStatus(TypedDict):
    """One producer's run in a spool, as `status --spool` prints it.

    ``reason`` is why a ``local_only`` run is excluded from upload, or why a
    ``pending`` one waits (a given collector token expired or was refused).
    """

    run_id: str
    producer_id: str
    upload_state: str
    reason: str | None
    undelivered_events: int


class UnreadableSpoolError(RuntimeError):
    """The file cannot be used as a spool database: this user lacks
    permission (``permission_denied``), or it is not a spool database this
    emitter knows. The message is the underlying error's type, never its
    details."""

    def __init__(self, error: Exception) -> None:
        super().__init__(type(error).__name__)
        self.permission_denied = isinstance(error, PermissionError)


@contextmanager
def _unreadable_spool_errors() -> Iterator[None]:
    """Report a failure to open or read a spool database as
    ``UnreadableSpoolError``."""

    try:
        yield
    except (SQLAlchemyError, CommandError, RuntimeError, OSError) as error:
        raise UnreadableSpoolError(error) from None


def _require_permissions(path: Path, *, write: bool) -> None:
    """Raise ``PermissionError`` unless this user may read the file, and for
    ``write`` also write it and create files in its directory, as SQLite's
    rollback journal needs. SQLite itself reports neither as a permission
    error, only as a file it cannot open."""

    if write:
        permitted = os.access(path, os.R_OK | os.W_OK) and os.access(
            path.parent, os.W_OK | os.X_OK
        )
    else:
        permitted = os.access(path, os.R_OK)
    if not permitted:
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))


@contextmanager
def _read_only_spool(path: Path) -> Iterator[Connection]:
    """A connection that cannot write to the spool database at ``path``,
    once its revision is one this emitter knows."""

    with _unreadable_spool_errors():
        _require_permissions(path, write=False)
        engine = create_read_only_spool_engine(path)
        try:
            with engine.connect() as connection:
                require_known_revision(connection)
                yield connection
        finally:
            engine.dispose()


@dataclass(frozen=True)
class SpoolDelivery:
    """What ``deliver_spool`` did with a spool database.

    ``outcome`` is ``delivered`` once nothing this login may deliver is
    pending, ``time_limit_passed`` when the collector stayed unavailable until
    the time limit, or ``login_failed`` when it stopped before it opened the
    file. ``login`` is the collector session's answer for this machine's
    login: ``ok``, or with ``login_failed`` ``missing`` (no Hugging Face
    login), ``rejected`` or ``unavailable`` (the collector could not be
    reached or gave no usable token). ``runs_of_other_users`` lists the runs
    the collector refused this login with 403, because another Hugging Face
    user registered them; they stay pending in the file.
    """

    outcome: Literal["delivered", "time_limit_passed", "login_failed"]
    login: SessionResult = "ok"
    runs_of_other_users: tuple[str, ...] = ()


def _run(registration: Mapping[str, Any]) -> tuple[str, str]:
    return str(registration["run_id"]), str(registration["producer_id"])


class _CopiedSpoolDelivery(CollectorDelivery):
    """Delivery that leaves a run the collector refuses this login pending.

    The collector answers 403 for a run another Hugging Face user registered,
    normally the operator who launched the build. A build makes its own run
    local-only then; a copy's run is that operator's to deliver, so it stays
    pending, and this delivery sends nothing more for it.
    """

    def __init__(self, *arguments: Any, **options: Any) -> None:
        super().__init__(*arguments, **options)
        #: Each refused run, as (run ID, producer ID).
        self.runs_of_other_users: set[tuple[str, str]] = set()

    def _forbidden(self, registration: Mapping[str, Any], reason: str) -> None:
        self.runs_of_other_users.add(_run(registration))

    def _collector_token(self, registration: Mapping[str, Any]) -> str | None:
        if _run(registration) in self.runs_of_other_users:
            return None
        return super()._collector_token(registration)

    def has_deliverable(self) -> bool:
        """Whether a pending run this login may deliver has events left."""

        return any(
            _run(registration) not in self.runs_of_other_users
            for registration in self.spool.pending_runs()
        )


def spool_status(path: Path) -> list[RunStatus]:
    """Every run in the spool database at ``path``, read without writing."""

    with _read_only_spool(path) as connection, Session(connection) as session:
        rows = session.execute(
            select(TelemetryRunRecord, func.count(TelemetryEventRecord.event_id))
            .outerjoin(TelemetryRunRecord.events)
            .group_by(TelemetryRunRecord.run_id, TelemetryRunRecord.producer_id)
            .order_by(TelemetryRunRecord.run_id, TelemetryRunRecord.producer_id)
        ).all()
        return [
            {
                "run_id": run.run_id,
                "producer_id": run.producer_id,
                "upload_state": run.upload_state,
                "reason": run.local_only_reason,
                "undelivered_events": undelivered_events,
            }
            for run, undelivered_events in rows
        ]


def deliver_spool(
    path: Path,
    *,
    development_collector_url: str | None = None,
    time_limit_seconds: float,
) -> SpoolDelivery:
    """Deliver the pending runs of the spool database at ``path``.

    It sends to the collector origin the emitter resolves, or to a loopback
    ``development_collector_url``. In order:

    1. It refuses, unchanged, a file this user may not read and write (or
       whose directory it may not write), or that is not a spool database of
       a revision this emitter knows, by raising ``UnreadableSpoolError``.
    2. It exchanges this machine's Hugging Face login, never a collector token
       given to builds in the environment. A missing, refused or
       unexchangeable login returns at once and leaves the file unchanged, so
       its runs never become local-only for want of a login here.
    3. It opens the file as a copy, which changes no permissions and prunes
       nothing, and delivers until nothing it may deliver is pending, or
       ``time_limit_seconds`` passes.

    The collector accepts a run registered on another machine again from the
    same Hugging Face user, and counts events it already has as duplicates. A
    run another Hugging Face user registered stays pending, for that user.
    """

    # Refuse a file this user may not write, or that is not a spool database,
    # before anything else.
    with _unreadable_spool_errors():
        _require_permissions(path, write=True)
    with _read_only_spool(path):
        pass
    origin = resolve_collector_origin(
        development_collector_url=development_collector_url
    )
    session = CollectorSession(origin, use_given_token=False)
    login = session.credential()
    if login.result != "ok":
        return SpoolDelivery("login_failed", login.result)
    with _unreadable_spool_errors():
        spool = EventSpool(path, as_copy=True)
    try:
        delivery = _CopiedSpoolDelivery(
            spool, session=session, development_collector_url=development_collector_url
        )
        deadline = time.monotonic() + time_limit_seconds
        outcome: Literal["delivered", "time_limit_passed"] = "delivered"
        while delivery.has_deliverable():
            if time.monotonic() >= deadline:
                outcome = "time_limit_passed"
                break
            if not delivery.flush_once():
                time.sleep(
                    min(DRAIN_RETRY_SECONDS, max(0, deadline - time.monotonic()))
                )
        return SpoolDelivery(
            outcome,
            runs_of_other_users=tuple(
                sorted({run_id for run_id, _ in delivery.runs_of_other_users})
            ),
        )
    finally:
        spool.close()
