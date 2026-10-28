"""Report on and deliver a spool database copied from a build elsewhere.

A build on a machine without a Hugging Face credential, such as Modal, sends
telemetry with a collector token it is given. Events it could not deliver stay
in its spool database, which the Modal runner copies to the runs volume.
`microcosm-emitter status --spool` and `deliver --spool` read and deliver that
copy on the operator's workstation.

Opening a spool with ``EventSpool`` migrates it and prunes it: events older
than the retention period, and the oldest beyond the size limit, are deleted.
So ``spool_status`` reads a copy in SQLite's read-only mode, changing nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from alembic.util import CommandError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from microcosm_emitter.core.database import create_read_only_spool_engine
from microcosm_emitter.core.migrations import require_known_revision
from microcosm_emitter.core.models import TelemetryEventRecord, TelemetryRunRecord


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
    """The file is not a spool database this emitter can read. The message is
    the underlying error's type, never its details."""


def spool_status(path: Path) -> list[RunStatus]:
    """Every run in the spool database at ``path``, read without writing."""

    engine = create_read_only_spool_engine(path)
    try:
        with engine.connect() as connection:
            require_known_revision(connection)
            with Session(connection) as session:
                rows = session.execute(
                    select(
                        TelemetryRunRecord,
                        func.count(TelemetryEventRecord.event_id),
                    )
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
    except (SQLAlchemyError, CommandError, RuntimeError, OSError) as error:
        raise UnreadableSpoolError(type(error).__name__) from None
    finally:
        engine.dispose()
