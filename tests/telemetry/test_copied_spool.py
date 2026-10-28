"""`status --spool` and `deliver --spool`: a spool copied from a build elsewhere.

The Modal runner copies a build's spool database to the runs volume when
events are left undelivered there; the operator reads and delivers the copy
on the workstation later, with their own Hugging Face login.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.orm import Session

from microcosm_emitter import cli
from microcosm_emitter.core import auth as auth_module
from microcosm_emitter.core.database import create_spool_engine
from microcosm_emitter.core.models import TelemetryEventRecord
from microcosm_emitter.telemetry.client import TelemetryRun
from microcosm_emitter.telemetry.service.spool import EventSpool


def registration(run_id: str) -> dict[str, object]:
    return TelemetryRun(
        run_id=run_id, country_code="US", pipeline="us_acs", producer_id="modal"
    ).as_registration()


def stage_event(stage_id: str) -> dict[str, object]:
    return {"event_type": "stage", "stage_id": stage_id, "status": "started"}


def age_events(spool_path: Path, days: int) -> None:
    """Make every queued event older than the spool's retention period."""
    created_at = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    engine = create_spool_engine(spool_path)
    try:
        with Session(engine) as session, session.begin():
            session.execute(update(TelemetryEventRecord).values(created_at=created_at))
    finally:
        engine.dispose()


def test_status_reads_a_copy_without_changing_it_or_the_network(
    tmp_path, monkeypatch, capsys
) -> None:
    spool_path = tmp_path / "copied" / "events.sqlite3"
    spool = EventSpool(spool_path)
    for run_id, events in (("waiting-run", 2), ("refused-run", 1), ("done-run", 0)):
        spool.register(registration(run_id))
        for index in range(events):
            spool.append(registration(run_id), stage_event(f"stage_{index}"))
    spool.record_pending_reason("waiting-run", "modal", "collector_token_expired")
    spool.make_local_only("refused-run", "modal", "run_registration_rejected")
    spool.close()
    # Older than the 7-day retention: opening the copy with `EventSpool`
    # would delete them.
    age_events(spool_path, days=8)
    copied = sorted(spool_path.parent.iterdir())
    digest = hashlib.sha256(spool_path.read_bytes()).hexdigest()

    def no_network(*arguments, **options):
        raise AssertionError("network request")

    monkeypatch.setattr(auth_module, "_http_post", no_network)

    assert cli.main(["status", "--spool", str(spool_path)]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "runs": [
            {
                "run_id": "done-run",
                "producer_id": "modal",
                "upload_state": "pending",
                "reason": None,
                "undelivered_events": 0,
            },
            {
                "run_id": "refused-run",
                "producer_id": "modal",
                "upload_state": "local_only",
                "reason": "run_registration_rejected",
                "undelivered_events": 1,
            },
            {
                "run_id": "waiting-run",
                "producer_id": "modal",
                "upload_state": "pending",
                "reason": "collector_token_expired",
                "undelivered_events": 2,
            },
        ]
    }
    assert hashlib.sha256(spool_path.read_bytes()).hexdigest() == digest
    assert sorted(spool_path.parent.iterdir()) == copied

    not_a_spool = tmp_path / "notes.sqlite3"
    not_a_spool.write_bytes(b"not a database")
    assert cli.main(["status", "--spool", str(not_a_spool)]) == 7
    assert not_a_spool.read_bytes() == b"not a database"
