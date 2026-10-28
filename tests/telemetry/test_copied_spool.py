"""`status --spool` and `deliver --spool`: a spool copied from a build elsewhere.

The Modal runner copies a build's spool database to the runs volume when
events are left undelivered there; the operator reads and delivers the copy
on the workstation later, with their own Hugging Face login.
"""

import hashlib
import json
import shutil
import socket
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args

import pytest
from fake_collector import FakeCollector
from given_token import given_token_variables, set_variables
from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from microcosm_emitter import cli
from microcosm_emitter.core import auth as auth_module
from microcosm_emitter.core.auth import (
    CollectorSession,
    exchange_huggingface_credential,
)
from microcosm_emitter.core.constants import TELEMETRY_SCOPE
from microcosm_emitter.core.database import create_spool_engine
from microcosm_emitter.core.models import TelemetryEventRecord
from microcosm_emitter.telemetry.client import TelemetryRun
from microcosm_emitter.telemetry.service import spool_copy as spool_copy_module
from microcosm_emitter.telemetry.service.collector import CollectorDelivery
from microcosm_emitter.telemetry.service.spool import EventSpool
from microcosm_emitter.telemetry.service.spool_copy import LoginFailure, spool_status

OPERATOR_LOGIN = "hf_synthetic_operator"
OTHER_OPERATOR_LOGIN = "hf_synthetic_other_operator"
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


def leave_an_unfinished_write(spool_path: Path) -> Path:
    """Leave the journal of a write that never finished beside a spool
    database, as an emitter stopped during a commit does, and return it."""
    journal = spool_path.with_name(f"{spool_path.name}-journal")
    engine = create_spool_engine(spool_path)
    try:
        with engine.connect() as connection:
            # Without syncing, SQLite marks the journal as one to roll back
            # from its first write, as it does just before a commit changes
            # the database file.
            connection.exec_driver_sql("PRAGMA synchronous = OFF")
            connection.commit()
            connection.execute(delete(TelemetryEventRecord))
            unfinished = journal.read_bytes()
            connection.rollback()
    finally:
        engine.dispose()
    journal.write_bytes(unfinished)
    return journal


def build_on_modal(collector: FakeCollector, spool_path: Path) -> None:
    """A build that sends its first event with a given token, which the
    collector then refuses, leaving two events pending in its spool."""
    obtained = exchange_huggingface_credential(
        collector.origin,
        OPERATOR_LOGIN,
        scope=TELEMETRY_SCOPE,
        lifetime_seconds=18 * 3600,
    )
    # Only the build's environment holds the given token, as on Modal.
    with pytest.MonkeyPatch.context() as build_environment:
        set_variables(
            build_environment,
            given_token_variables(
                collector.origin,
                expires_in=timedelta(seconds=obtained.expires_in),
                token=obtained.access_token,
            ),
        )
        session = CollectorSession(collector.origin)
    spool = EventSpool(spool_path)
    try:
        run = registration("modal-run")
        spool.register(run)
        spool.append(run, stage_event("stage_inputs"))
        delivery = CollectorDelivery(
            spool, session=session, development_collector_url=collector.origin
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


def copy_of_a_build_on_modal(collector: FakeCollector, tmp_path: Path) -> Path:
    """The spool a build on Modal left, copied to a directory of its own, as
    the Modal runner copies it to the runs volume, with ordinary modes."""
    container_spool = tmp_path / "container" / "root" / "events.sqlite3"
    build_on_modal(collector, container_spool)
    copy = tmp_path / "runs" / "modal-run" / "telemetry" / "events.sqlite3"
    copy.parent.mkdir(parents=True)
    shutil.copyfile(container_spool, copy)
    copy.parent.chmod(0o755)
    copy.chmod(0o644)
    return copy


def modes(spool_path: Path) -> tuple[int, int]:
    """The permission bits of a spool database's directory and of the file."""
    return (
        stat.S_IMODE(spool_path.parent.stat().st_mode),
        stat.S_IMODE(spool_path.stat().st_mode),
    )


def test_status_reads_a_copy_without_changing_it_or_the_network(
    operator_login, tmp_path, monkeypatch, capsys
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

    # With a login to deliver with, any request would be an attempt to.
    monkeypatch.setattr(socket.socket, "connect", no_network)

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


@pytest.mark.parametrize("command", ["status", "deliver"])
@pytest.mark.parametrize(
    ("content", "mode", "message"),
    [
        (b"not a database", 0o600, "not a spool database"),
        (b"", 0o600, "not a spool database"),
        # A spool database, which this user may not read.
        (None, 0o000, "permission denied"),
    ],
    ids=["other content", "zero bytes", "no read permission"],
)
def test_both_commands_refuse_a_file_they_cannot_use(
    collector, operator_login, tmp_path, capsys, command, content, mode, message
) -> None:
    spool_path = tmp_path / "copied" / "events.sqlite3"
    if content is None:
        build_on_modal(collector, spool_path)
    else:
        spool_path.parent.mkdir()
        spool_path.write_bytes(content)
    original = spool_path.read_bytes()
    listing = sorted(spool_path.parent.iterdir())
    spool_path.chmod(mode)
    arguments = [command, "--spool", str(spool_path)]
    if command == "deliver":
        arguments += ["--development-collector-url", collector.origin]
    capsys.readouterr()

    exit_code = cli.main(arguments)

    output = capsys.readouterr()
    # Not 7, which `telemetry-token` returns for an unexpected failure.
    assert exit_code == 9
    assert output.out == ""
    assert message in output.err
    assert modes(spool_path)[1] == mode
    spool_path.chmod(0o600)
    # `deliver` must not write the spool schema into it, or anything beside it.
    assert spool_path.read_bytes() == original
    assert sorted(spool_path.parent.iterdir()) == listing


def test_deliver_sends_a_copy_from_another_machine_only_with_the_owners_login(
    collector, operator_login, tmp_path, monkeypatch, capsys
) -> None:
    copy = copy_of_a_build_on_modal(collector, tmp_path)
    # Delivery may come after the spool's 7-day retention period.
    age_events(copy, days=8)
    # A token given to builds is not the operator's login, even if it is
    # still in the shell.
    set_variables(
        monkeypatch,
        given_token_variables(
            collector.origin, expires_in=timedelta(0), token="a-refused-build-token"
        ),
    )
    deliver = ["deliver", "--spool", str(copy), "--development-collector-url"]
    # Another operator's login: the collector refuses it the run (403), which
    # must stay pending in the copy for the operator who launched the build.
    monkeypatch.setenv("HF_TOKEN", OTHER_OPERATOR_LOGIN)
    digest = hashlib.sha256(copy.read_bytes()).hexdigest()
    capsys.readouterr()

    assert cli.main([*deliver, collector.origin]) == 8

    output = capsys.readouterr()
    assert json.loads(output.out)["runs"][0]["undelivered_events"] == 2
    assert "modal-run" in output.err
    assert "another Hugging Face user" in output.err
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == digest
    monkeypatch.setenv("HF_TOKEN", OPERATOR_LOGIN)

    exit_code = cli.main([*deliver, collector.origin])

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
    # Only the copy's events changed, not its permissions or the original.
    assert modes(copy) == (0o755, 0o644)
    container_spool = tmp_path / "container" / "root" / "events.sqlite3"
    assert spool_status(container_spool)[0]["undelivered_events"] == 2


@pytest.mark.parametrize(
    ("failure", "expected_exit_code"),
    [
        ("no login", 3),
        ("login refused", 4),
        ("unsendable login", 4),
        ("collector unreachable", 5),
    ],
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
    # Every login failure deliver_spool reports has an exit code and a message.
    assert set(get_args(LoginFailure)) <= set(cli.CREDENTIAL_FAILURES)
    copy = copy_of_a_build_on_modal(collector, tmp_path)
    digest = hashlib.sha256(copy.read_bytes()).hexdigest()
    listing = sorted(copy.parent.iterdir())
    collector_url = collector.origin
    if failure == "no login":
        monkeypatch.delenv("HF_TOKEN")
    elif failure == "login refused":
        collector.exchange_status = 403
    elif failure == "unsendable login":
        monkeypatch.setenv("HF_TOKEN", f"{OPERATOR_LOGIN} pasted twice")
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
    if failure == "unsendable login":
        # Never sent, so not refused by the collector.
        assert "cannot be sent as a bearer token" in output.err
        assert OPERATOR_LOGIN not in output.err
    # A missing or refused login must not make the copy's runs local-only.
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == digest
    # Nor is the copy opened for writing, which would leave a lock file beside
    # it, even where the collector is unreachable and nothing would be written.
    assert sorted(copy.parent.iterdir()) == listing
    assert modes(copy) == (0o755, 0o644)


def test_deliver_keeps_the_runs_pending_when_its_login_is_refused_partway(
    collector, operator_login, tmp_path, monkeypatch, capsys
) -> None:
    copy = copy_of_a_build_on_modal(collector, tmp_path)
    exchange = auth_module._http_post

    def login_revoked_after_the_first_exchange(url, *arguments, **options):
        answer = exchange(url, *arguments, **options)
        # Then the collector's signing key changes, so the token just issued
        # gets 401, and the operator's Hugging Face login is revoked, so
        # exchanging it again is refused.
        collector.refuse_issued_tokens()
        collector.exchange_status = 401
        return answer

    monkeypatch.setattr(
        auth_module, "_http_post", login_revoked_after_the_first_exchange
    )
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
    assert len(collector.exchange_requests) == 3  # the build's, then deliver's two
    assert exit_code == 4
    assert "refused the Hugging Face credential" in output.err
    # The refusal is about this machine's login, not the copy's run, which
    # stays pending for a login that works.
    assert [
        (run["upload_state"], run["undelivered_events"]) for run in spool_status(copy)
    ] == [("pending", 2)]


@pytest.mark.parametrize("command", ["status", "deliver"])
def test_both_commands_print_only_the_type_of_an_unexpected_error(
    collector, operator_login, tmp_path, monkeypatch, capsys, command
) -> None:
    copy = copy_of_a_build_on_modal(collector, tmp_path)

    def unexpected_error(*arguments, **options):
        raise ValueError(f"an error quoting {OPERATOR_LOGIN}")

    monkeypatch.setattr(
        spool_copy_module,
        "spool_status" if command == "status" else "deliver_spool",
        unexpected_error,
    )
    arguments = [command, "--spool", str(copy)]
    if command == "deliver":
        arguments += ["--development-collector-url", collector.origin]
    capsys.readouterr()

    exit_code = cli.main(arguments)

    output = capsys.readouterr()
    # As `telemetry-token`: exit 1 is only for a host that could not start.
    assert exit_code == 7
    assert output.out == ""
    assert "unexpected ValueError" in output.err
    assert OPERATOR_LOGIN not in output.err


def test_only_deliver_recovers_a_copy_left_with_an_unfinished_write(
    collector, operator_login, tmp_path, monkeypatch, capsys
) -> None:
    copy = copy_of_a_build_on_modal(collector, tmp_path)
    journal = leave_an_unfinished_write(copy)
    left_behind = {path: path.read_bytes() for path in (copy, journal)}
    deliver = [
        "deliver",
        "--spool",
        str(copy),
        "--development-collector-url",
        collector.origin,
    ]
    capsys.readouterr()

    # Only a connection that may write can roll the write back.
    assert cli.main(["status", "--spool", str(copy)]) == 9
    output = capsys.readouterr()
    assert output.out == ""
    assert "did not finish" in output.err
    # `deliver` checks the file without writing to it, then stops for the
    # missing login.
    monkeypatch.delenv("HF_TOKEN")
    assert cli.main(deliver) == 3
    assert {path: path.read_bytes() for path in left_behind} == left_behind
    monkeypatch.setenv("HF_TOKEN", OPERATOR_LOGIN)
    capsys.readouterr()

    exit_code = cli.main(deliver)

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["runs"][0]["undelivered_events"] == 0
    assert not journal.exists()
    assert sorted(event["stage_id"] for event in collector.events.values()) == [
        "calibrate",
        "stage_inputs",
        "write_outputs",
    ]


def test_deliver_stops_at_its_time_limit_with_the_events_pending(
    collector, operator_login, tmp_path, capsys
) -> None:
    copy = copy_of_a_build_on_modal(collector, tmp_path)
    collector.telemetry_status = 503
    capsys.readouterr()

    exit_code = cli.main(
        [
            "deliver",
            "--spool",
            str(copy),
            "--time-limit-seconds",
            "1",
            "--development-collector-url",
            collector.origin,
        ]
    )

    output = capsys.readouterr()
    assert exit_code == 5
    assert json.loads(output.out)["runs"][0]["undelivered_events"] == 2
    # The limit bounds the whole delivery, whether the collector answers or not.
    assert "at the time limit (--time-limit-seconds 1)" in output.err
