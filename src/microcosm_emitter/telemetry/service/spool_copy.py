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

A copy taken after its emitter stopped during a commit has a journal beside
it holding a write that did not finish. SQLite rolls that write back the
first time a connection that may write reads the file, and refuses a
read-only one. So ``spool_status`` refuses such a copy, saying why, and
``deliver_spool`` checks a recovered copy of both files elsewhere before it
opens the file itself, which rolls the write back.
"""

from __future__ import annotations

import errno
import os
import shutil
import tempfile
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

from microcosm_emitter.core.auth import (
    CollectorSession,
    SessionResult,
    huggingface_token,
)
from microcosm_emitter.core.collector_origin import resolve_collector_origin
from microcosm_emitter.core.constants import DRAIN_RETRY_SECONDS
from microcosm_emitter.core.database import (
    create_read_only_spool_engine,
    create_spool_engine,
)
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
    permission (``permission_denied``), a journal beside it holds a write
    that did not finish, which a read-only connection cannot roll back
    (``unfinished_write``), or it is not a spool database this emitter knows.
    The message is the underlying error's type, never its details."""

    def __init__(self, error: Exception) -> None:
        super().__init__(type(error).__name__)
        self.permission_denied = isinstance(error, PermissionError)
        # By the name of SQLite's error code, which the driver's exception
        # carries; the database modules do not import the driver.
        driver_error = getattr(error, "orig", None)
        self.unfinished_write = (
            getattr(driver_error, "sqlite_errorname", None)
            == "SQLITE_READONLY_ROLLBACK"
        )


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


class UnreadableLoginError(RuntimeError):
    """huggingface_hub could not read the login `hf auth login` saved on this
    machine, such as a file this user may not read, and neither HF_TOKEN nor
    HUGGINGFACE_TOKEN is set. Nothing was sent. The message is the underlying
    error's type, never its details."""

    def __init__(self, error: Exception) -> None:
        super().__init__(type(error).__name__)


def _this_machines_login() -> str | None:
    """This machine's Hugging Face login, raising ``UnreadableLoginError``
    when the saved one cannot be read."""

    try:
        return huggingface_token()
    except (OSError, UnicodeDecodeError) as error:
        raise UnreadableLoginError(error) from None


#: Why this machine's login gave ``deliver_spool`` no collector token:
#: ``missing`` (no Hugging Face login), ``unsendable`` (it contains
#: characters that cannot be sent as a bearer token, so it was not sent),
#: ``rejected`` (the collector refused it), or ``unavailable`` (the collector
#: could not be reached or gave no usable token; only before delivery starts,
#: as delivery retries it). A saved login that cannot be read raises
#: ``UnreadableLoginError`` instead.
LoginFailure = Literal["missing", "unsendable", "rejected", "unavailable"]
#: The failure for each answer of the session other than ``ok``. Its session
#: has no scope and ignores given tokens, so it gives no other answer; any
#: other would count as ``unavailable``.
_LOGIN_FAILURES: dict[SessionResult, LoginFailure] = {
    "missing": "missing",
    "unsendable": "unsendable",
    "rejected": "rejected",
    "unavailable": "unavailable",
}


def _login_failure(result: SessionResult) -> LoginFailure:
    return _LOGIN_FAILURES.get(result, "unavailable")


def _journal(path: Path) -> Path:
    """SQLite's rollback journal for the database at ``path``."""
    return path.with_name(f"{path.name}-journal")


def _check_recovered_copy(path: Path) -> None:
    """Refuse, as ``_read_only_spool`` does, a spool database whose journal
    holds a write that did not finish, once SQLite has rolled that write back
    in a copy of both files. The copy is in a temporary directory only this
    user may open, and is deleted afterwards; the file itself is not
    written."""

    with _unreadable_spool_errors(), tempfile.TemporaryDirectory() as directory:
        recovered = Path(directory) / path.name
        shutil.copyfile(path, recovered)
        shutil.copyfile(_journal(path), _journal(recovered))
        engine = create_spool_engine(recovered)
        try:
            with engine.connect() as connection:
                require_known_revision(connection)
        finally:
            engine.dispose()


@dataclass(frozen=True)
class SpoolDelivery:
    """What ``deliver_spool`` did with a spool database.

    ``outcome`` is ``delivered`` once nothing this login may deliver is
    pending, ``time_limit_passed`` when the time limit passed first, or
    ``login_failed`` when this machine's login gave no collector token
    (``login_failure`` says why), before it opened the file or partway
    through delivery; every run not yet delivered then stays pending.
    ``runs_of_other_users`` lists the runs the collector refused this login
    with 403, because another Hugging Face user registered them; they stay
    pending in the file.
    """

    outcome: Literal["delivered", "time_limit_passed", "login_failed"]
    login_failure: LoginFailure | None = None
    runs_of_other_users: tuple[str, ...] = ()


def _run(registration: Mapping[str, Any]) -> tuple[str, str]:
    return str(registration["run_id"]), str(registration["producer_id"])


class _CopiedSpoolDelivery(CollectorDelivery):
    """Delivery that leaves a copy's runs pending whenever the collector
    refuses this login.

    The collector answers 403 for a run another Hugging Face user registered,
    normally the operator who launched the build. A build makes its own run
    local-only then; a copy's run is that operator's to deliver, so it stays
    pending, and this delivery sends nothing more for it.

    A build also makes its runs local-only when its login is missing or
    refused. Here, the login can fail partway through delivery: after a 401,
    the session exchanges it again, and the operator may have logged out or
    lost access since. That says nothing about the copy's runs, so delivery
    stops at once, writing nothing more, and ``login_failure`` says why.
    """

    def __init__(self, *arguments: Any, **options: Any) -> None:
        super().__init__(*arguments, **options)
        #: Each refused run, as (run ID, producer ID).
        self.runs_of_other_users: set[tuple[str, str]] = set()
        self.login_failure: LoginFailure | None = None

    def _forbidden(self, registration: Mapping[str, Any], reason: str) -> None:
        self.runs_of_other_users.add(_run(registration))

    def _collector_token(self, registration: Mapping[str, Any]) -> str | None:
        if (
            self.login_failure is not None
            or _run(registration) in self.runs_of_other_users
        ):
            return None
        result, token = self.session.credential()
        if result == "unavailable":
            self._defer_retry()
        elif result != "ok":
            self.login_failure = _login_failure(result)
        return token

    def has_deliverable(self) -> bool:
        """Whether a pending run this login may deliver has events left."""

        return self.login_failure is None and any(
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
       When a journal beside the file holds a write that did not finish, it
       checks a recovered copy of both files instead (``_check_recovered_copy``).
    2. It exchanges this machine's Hugging Face login, never a collector token
       given to builds in the environment. A missing, refused or
       unexchangeable login returns at once and leaves the file unchanged, so
       its runs never become local-only for want of a login here. A saved
       login it cannot read raises ``UnreadableLoginError``, here or when the
       session reads it again partway through delivery, which then stops
       without writing anything more.
    3. It opens the file as a copy, which changes no permissions and prunes
       nothing, and delivers until nothing it may deliver is pending, or
       ``time_limit_seconds`` passes. If the login is missing or refused
       when the session exchanges it again, it stops, and the runs not yet
       delivered stay pending.

    The collector accepts a run registered on another machine again from the
    same Hugging Face user, and counts events it already has as duplicates. A
    run another Hugging Face user registered stays pending, for that user.
    """

    # Refuse a file this user may not write, or that is not a spool database,
    # before anything else.
    with _unreadable_spool_errors():
        _require_permissions(path, write=True)
    try:
        with _read_only_spool(path):
            pass
    except UnreadableSpoolError as refusal:
        if not refusal.unfinished_write:
            raise
        # Opening the file to deliver it rolls the write back. Check what
        # that leaves first, so the file is written only once it is known to
        # be a spool database, and a login works.
        _check_recovered_copy(path)
    origin = resolve_collector_origin(
        development_collector_url=development_collector_url
    )
    session = CollectorSession(
        origin, use_given_token=False, credential=_this_machines_login
    )
    login = session.credential()
    if login.result != "ok":
        return SpoolDelivery("login_failed", _login_failure(login.result))
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
        runs_of_other_users = tuple(
            sorted({run_id for run_id, _ in delivery.runs_of_other_users})
        )
        if delivery.login_failure is not None:
            return SpoolDelivery(
                "login_failed", delivery.login_failure, runs_of_other_users
            )
        return SpoolDelivery(outcome, runs_of_other_users=runs_of_other_users)
    finally:
        spool.close()
