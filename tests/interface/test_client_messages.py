"""Each typed call sends exactly the message its protocol fixture records."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from microcosm_emitter_interface.client import EmitterClient
from microcosm_emitter_interface.launcher import EmitterProcess
from microcosm_emitter_interface.orrery import OrrerySettings
from microcosm_emitter_interface.protocol_fixtures import (
    ProtocolFixture,
    materialize,
    protocol_fixtures,
)
from microcosm_emitter_interface.telemetry import TelemetryRun, TelemetrySettings


class RecordingTransport:
    """Records requests and answers queries like an emitter that queued nothing."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def send(self, module, message, *, timeout=None):
        self.requests.append(("message", module, dict(message)))

    def query(self, module, message, *, timeout=None):
        self.requests.append(("query", module, dict(message)))
        if message.get("command") == "inventory":
            return {"inventory": None}
        return {
            "receipt": {
                "version": 1,
                "publication_id": message.get("publication_id")
                or message["inventory"]["publication_id"],
                "status": "pending",
                "error_code": None,
                "attempts": 1,
            }
        }

    def close(self):
        pass


@pytest.fixture
def recording_client():
    transport = RecordingTransport()
    process = EmitterProcess(
        process=None,
        transport=transport,
        runtime_directory=Path("/nonexistent"),
        description={"modules": ["orrery", "telemetry"], "protocol_versions": [1]},
    )
    return EmitterClient(process), transport


def fixture_run() -> TelemetryRun:
    return TelemetryRun(
        "fixture-run", "UK", "fixture-pipeline", producer_id="fixture-producer"
    )


def fixture_inventory(directory: Path) -> dict[str, Any]:
    fixture = fixtures_by_name()["orrery-enqueue"]
    return materialize(fixture, directory=directory, spool_path=Path("unused"))[
        "inventory"
    ]


def fixtures_by_name() -> dict[str, ProtocolFixture]:
    return {fixture.name: fixture for fixture in protocol_fixtures()}


SENT_BY: dict[str, Callable[[EmitterClient, Path], Any]] = {
    "telemetry-emit": lambda client, directory: client.telemetry.emit(
        event_type="progress",
        status="progress",
        stage_id="compile",
        message="Compiling.",
        details={"step": 1},
    ),
    "telemetry-stage": lambda client, directory: client.telemetry.stage(
        "compile", rows=3
    ),
    "telemetry-transition-stage": lambda client, directory: (
        client.telemetry.transition_stage("calibrating", message="Calibrating.")
    ),
    "telemetry-progress": lambda client, directory: client.telemetry.progress(
        "calibrating", done=2, total=10, unit="epochs", phase="solve"
    ),
    "telemetry-calibration-progress": lambda client, directory: (
        client.telemetry.calibration_progress(
            {"kind": "calibration_epoch", "epoch": 10, "epochs": 100, "loss": 0.25}
        )
    ),
    "telemetry-transition-calibration-progress": lambda client, directory: (
        client.telemetry.transition_calibration_progress(
            {"kind": "calibration_epoch", "epoch": 20, "epochs": 100, "loss": 0.125}
        )
    ),
    "telemetry-fail": lambda client, directory: client.telemetry.fail(
        RuntimeError("Synthetic failure."),
        failed_during="calibrating",
        error_code="BUILD_FAILED",
    ),
    "telemetry-fail-without-code": lambda client, directory: client.telemetry.fail(
        MemoryError("Synthetic failure."), failure_class="oom"
    ),
    "telemetry-refusal": lambda client, directory: client.telemetry.refuse(
        phase="preflight",
        refusing_check_ids=["fixture-check"],
        refusing_check_count=1,
        check_statuses={"fixture-check": "failed"},
    ),
    "telemetry-complete": lambda client, directory: client.telemetry.complete(),
    "orrery-inventory": lambda client, directory: client.orrery_inventory(
        directory,
        {"graph.orrery.json": "graph", "graph.schema.json": "schema"},
        publication_id="fixture-publication",
    ),
    "orrery-enqueue": lambda client, directory: client.hand_over_orrery_inputs(
        directory, fixture_inventory(directory), wait_seconds=0
    ),
    "orrery-receipt": lambda client, directory: client.orrery_receipt(
        "fixture-publication"
    ),
    "orrery-retry": lambda client, directory: client.retry_orrery_inputs(
        "fixture-publication"
    ),
}

CONFIGURED_BY: dict[str, Callable[[Path], dict[str, Any]]] = {
    "telemetry-configuration": lambda spool_path: TelemetrySettings(
        run=fixture_run(),
        identity={"host": "fixture"},
        development_collector_url="http://127.0.0.1:1",
    ).configuration(spool_path),
    "orrery-configuration": lambda spool_path: OrrerySettings(
        development_collector_url="http://127.0.0.1:1",
        publication_origin="http://127.0.0.1:1",
    ).configuration(spool_path),
}


#: Host fixtures: the launch and each socket envelope, sent in
#: test_host_protocol.py.
HOST_FIXTURES = {
    "host-close",
    "host-hello",
    "host-launch",
    "host-message-envelope",
    "host-ping",
    "host-query-envelope",
}


def test_every_fixture_has_a_typed_call_that_produces_it():
    covered = set(SENT_BY) | set(CONFIGURED_BY) | HOST_FIXTURES
    assert {fixture.name for fixture in protocol_fixtures()} == covered


@pytest.mark.parametrize("name", sorted(SENT_BY))
def test_typed_call_sends_the_recorded_message(recording_client, tmp_path, name):
    client, transport = recording_client
    fixture = fixtures_by_name()[name]
    directory = tmp_path / "inputs"
    SENT_BY[name](client, directory)
    expected = materialize(fixture, directory=directory.resolve(), spool_path=tmp_path)
    assert transport.requests[0] == (fixture.kind, fixture.module, expected)


@pytest.mark.parametrize("name", sorted(CONFIGURED_BY))
def test_module_settings_produce_the_recorded_configuration(tmp_path, name):
    fixture = fixtures_by_name()[name]
    spool_path = tmp_path / "events.sqlite3"
    assert CONFIGURED_BY[name](spool_path) == materialize(
        fixture, directory=tmp_path, spool_path=spool_path
    )


def test_calls_for_a_module_the_emitter_did_not_load_are_not_sent(tmp_path):
    transport = RecordingTransport()
    client = EmitterClient(
        EmitterProcess(
            process=None,
            transport=transport,
            runtime_directory=Path("/nonexistent"),
            description={"modules": ["orrery"], "protocol_versions": [1]},
        )
    )
    assert client.telemetry.complete() is False
    assert transport.requests == []
    receipt = client.orrery_receipt("fixture-publication")
    assert receipt["status"] == "pending"
