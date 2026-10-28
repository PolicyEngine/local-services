"""`status --spool` and `deliver --spool`: a spool copied from a build elsewhere.

The Modal runner copies a build's spool database to the runs volume when
events are left undelivered there; the operator reads and delivers the copy
on the workstation later, with their own Hugging Face login.
"""

import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fake_collector import FakeCollector
from sqlalchemy import update
from sqlalchemy.orm import Session

from microcosm_emitter import cli
from microcosm_emitter.core import auth as auth_module
from microcosm_emitter.core.auth import CollectorSession, exchange_telemetry_token
from microcosm_emitter.core.database import create_spool_engine
from microcosm_emitter.core.models import TelemetryEventRecord
from microcosm_emitter.telemetry.client import TelemetryRun
from microcosm_emitter.telemetry.service.collector import CollectorDelivery
from microcosm_emitter.telemetry.service.spool import EventSpool
from microcosm_emitter.telemetry.service.spool_copy import spool_status

OPERATOR_LOGIN = "hf_synthetic_operator"
UNREACHABLE_COLLECTOR_URL = "http://127.0.0.1:1"


@pytest.fixture
def collector():
    fake = FakeCollector().start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture
def operator_login(monkeypatch):
    """The operator's Hugging Face login on the workstation, nothing ambient."""
    monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)
    monkeypatch.setenv("HF_TOKEN", OPERATOR_LOGIN)
    monkeypatch.setattr(auth_module, "get_token", lambda: None)


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


def build_on_modal(collector: FakeCollector, spool_path: Path) -> None:
    """A build that sends its first event with a given token, which the
    collector then refuses, leaving two events pending in its spool."""
    obtained = exchange_telemetry_token(
        collector.origin, 18 * 3600, credential=lambda: OPERATOR_LOGIN
    )
    given_token = {
        "MICROCOSM_COLLECTOR_TOKEN": obtained.access_token,
        "MICROCOSM_COLLECTOR_TOKEN_EXPIRES_AT": obtained.expires_at,
        "MICROCOSM_COLLECTOR_ORIGIN": collector.origin,
    }
    spool = EventSpool(spool_path)
    try:
        run = registration("modal-run")
        spool.register(run)
        spool.append(run, stage_event("stage_inputs"))
        delivery = CollectorDelivery(
            spool,
            session=CollectorSession(collector.origin, environment=given_token),
            development_collector_url=collector.origin,
        )
        assert delivery.flush_once()
        spool.append(run, stage_event("calibrate"))
        spool.append(run, stage_event("write_outputs"))
        collector.refuse_issued_tokens()
        delivery.flush_once()
        delivery.flush_once()
        assert delivery.waiting_for_credential
    finally:
        spool.close()


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


def test_deliver_sends_a_copy_from_another_machine_with_the_operators_login(
    collector, operator_login, tmp_path, monkeypatch, capsys
) -> None:
    container_spool = tmp_path / "container" / "root" / "events.sqlite3"
    build_on_modal(collector, container_spool)
    copy = tmp_path / "runs" / "modal-run" / "telemetry" / "events.sqlite3"
    copy.parent.mkdir(parents=True)
    shutil.copyfile(container_spool, copy)
    # Delivery may come after the spool's 7-day retention period.
    age_events(copy, days=8)
    # A token given to builds is not the operator's login, even if it is
    # still in the shell.
    monkeypatch.setenv("MICROCOSM_COLLECTOR_TOKEN", "a-refused-build-token")
    monkeypatch.setenv(
        "MICROCOSM_COLLECTOR_TOKEN_EXPIRES_AT", datetime.now(UTC).isoformat()
    )
    monkeypatch.setenv("MICROCOSM_COLLECTOR_ORIGIN", collector.origin)
    capsys.readouterr()

    exit_code = cli.main(
        [
            "deliver",
            "--spool",
            str(copy),
            "--development-collector-url",
            collector.origin,
        ]
    )

    output = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(output.out) == {
        "runs": [
            {
                "run_id": "modal-run",
                "producer_id": "modal",
                "upload_state": "pending",
                "reason": None,
                "undelivered_events": 0,
            }
        ]
    }
    assert sorted(event["stage_id"] for event in collector.events.values()) == [
        "calibrate",
        "stage_inputs",
        "write_outputs",
    ]
    assert collector.runs["modal-run"]["owner"] == OPERATOR_LOGIN
    assert OPERATOR_LOGIN not in output.err
    # Only the copy changed.
    assert spool_status(container_spool)[0]["undelivered_events"] == 2


@pytest.mark.parametrize(
    ("failure", "expected_exit_code"),
    [("no login", 3), ("login refused", 4), ("collector unreachable", 5)],
)
def test_deliver_without_a_usable_login_leaves_the_copy_unchanged(
    collector,
    operator_login,
    tmp_path,
    monkeypatch,
    capsys,
    failure,
    expected_exit_code,
) -> None:
    copy = tmp_path / "copied" / "events.sqlite3"
    build_on_modal(collector, copy)
    digest = hashlib.sha256(copy.read_bytes()).hexdigest()
    collector_url = collector.origin
    if failure == "no login":
        monkeypatch.delenv("HF_TOKEN")
    elif failure == "login refused":
        collector.exchange_status = 403
    else:
        collector_url = UNREACHABLE_COLLECTOR_URL
    capsys.readouterr()

    exit_code = cli.main(
        ["deliver", "--spool", str(copy), "--development-collector-url", collector_url]
    )

    output = capsys.readouterr()
    assert exit_code == expected_exit_code
    assert output.out == ""
    assert output.err.strip()
    # A missing or refused login must not make the copy's runs local-only.
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == digest
