"""No interface call raises into the build, whatever arguments it is given.

Arguments are converted to JSON inside each call's error handling: a value that
cannot be converted makes the call report failure, with a reason, both with and
without a running emitter.
"""

from pathlib import Path
from typing import Any

import pytest

from microcosm_emitter_interface.client import EmitterClient
from microcosm_emitter_interface.launcher import EmitterProcess
from microcosm_emitter_interface.telemetry import TelemetryRun, TelemetrySettings


class MultiElementArray:
    """Behaves like a NumPy array or tensor with more than one element."""

    def item(self):
        raise ValueError("can only convert an array of size 1 to a Python scalar")


def self_referencing_mapping() -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    mapping["itself"] = mapping
    return mapping


def deeply_nested_list(depth: int = 100_000) -> list[Any]:
    nested: list[Any] = []
    for _ in range(depth):
        nested = [nested]
    return nested


class UnresolvablePath:
    """A path-like argument whose conversion fails."""

    def __fspath__(self):
        raise OSError("synthetic unresolvable path")


class RecordingTransport:
    def __init__(self):
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def send(self, module, message, *, timeout=None):
        self.requests.append(("message", module, dict(message)))

    def query(self, module, message, *, timeout=None):
        self.requests.append(("query", module, dict(message)))
        return {}

    def close(self):
        pass


def running_client() -> tuple[EmitterClient, RecordingTransport]:
    transport = RecordingTransport()
    process = EmitterProcess(
        process=None,
        transport=transport,
        runtime_directory=Path("/nonexistent"),
        description={"modules": ["orrery", "telemetry"], "protocol_versions": [1]},
    )
    return EmitterClient(process), transport


UNCONVERTIBLE_DETAILS = {
    "multi-element array": lambda: MultiElementArray(),
    "self-referencing mapping": self_referencing_mapping,
    "deep nesting": deeply_nested_list,
}


@pytest.mark.parametrize(
    "make_value", UNCONVERTIBLE_DETAILS.values(), ids=list(UNCONVERTIBLE_DETAILS)
)
def test_unconvertible_telemetry_arguments_report_failure_with_an_emitter(
    make_value, capsys
):
    client, transport = running_client()
    assert client.telemetry.stage("compile", value=make_value()) is False
    assert client.telemetry.calibration_progress({"loss": make_value()}) is False
    assert transport.requests == []
    from microcosm_emitter_interface.client import RequestFailure

    failure = client.last_failure
    assert isinstance(failure, RequestFailure)
    assert (failure.kind, failure.reason) == ("telemetry", "invalid_arguments")
    assert failure.error_type in {"ValueError", "RecursionError"}
    warning = capsys.readouterr().err
    assert warning.count("did not accept a telemetry request") == 1
    assert "invalid_arguments" in warning
    assert "synthetic" not in warning and "scalar" not in warning


@pytest.mark.parametrize(
    "make_value", UNCONVERTIBLE_DETAILS.values(), ids=list(UNCONVERTIBLE_DETAILS)
)
def test_unconvertible_telemetry_arguments_report_failure_without_an_emitter(
    make_value,
):
    client = EmitterClient()
    assert client.telemetry.stage("compile", value=make_value()) is False
    assert (
        client.telemetry.emit(
            event_type="progress", status="progress", details={"value": make_value()}
        )
        is False
    )
    assert client.last_failure is not None
    assert client.last_failure.reason == "emitter_unavailable"


def test_unconvertible_refusal_and_failure_arguments_report_failure():
    client, transport = running_client()
    assert (
        client.telemetry.refuse(
            phase="preflight", refusing_check_ids=MultiElementArray()
        )
        is False
    )
    assert (
        client.telemetry.fail(RuntimeError("x"), failed_during=MultiElementArray())
        is False
    )
    assert transport.requests == []


def test_an_emitter_that_raises_an_unexpected_error_is_a_failure_not_a_raise():
    client, transport = running_client()

    def unexpected(*args, **kwargs):
        raise RecursionError("synthetic")

    transport.send = unexpected
    transport.query = unexpected
    assert client.telemetry.complete() is False
    assert client.last_failure.reason == "not_accepted"
    assert client.orrery_receipt("fixture-publication")["status"] == "failed"


def test_successful_call_after_a_failure_keeps_the_last_failure_until_the_next():
    client, transport = running_client()
    assert client.telemetry.stage("compile", value=MultiElementArray()) is False
    assert client.telemetry.complete() is True
    assert transport.requests[-1] == ("message", "telemetry", {"command": "complete"})


def test_unconvertible_orrery_arguments_report_failure():
    client, transport = running_client()
    assert client.orrery_inventory(UnresolvablePath(), {"graph.json": "graph"}) is None
    receipt = client.hand_over_orrery_inputs(
        UnresolvablePath(),
        {"version": 1, "publication_id": "fixture-publication", "files": []},
        wait_seconds=0,
    )
    assert receipt["status"] == "failed"
    assert receipt["publication_id"] == "fixture-publication"
    assert receipt["error_code"] == "invalid_arguments"
    assert client.last_failure.reason == "invalid_arguments"
    missing_identifier = client.hand_over_orrery_inputs(
        Path("/tmp"), {"version": 1, "files": []}, wait_seconds=0
    )
    assert missing_identifier["status"] == "failed"
    assert missing_identifier["publication_id"] is None
    assert all(
        request[2].get("command") != "inventory" for request in transport.requests
    )


def test_unconvertible_startup_settings_start_an_unavailable_client(tmp_path, capsys):
    settings = TelemetrySettings(
        run=TelemetryRun("bad-settings", "UK", "test"),
        identity={"accelerator": MultiElementArray()},
    )
    client = EmitterClient.start(
        telemetry=settings, spool_path=tmp_path / "events.sqlite3"
    )
    assert not client.available
    assert client.startup_error is not None
    assert client.startup_error.reason == "invalid_configuration"
    assert client.telemetry.complete() is False
    warning = capsys.readouterr().err
    assert "invalid_configuration" in warning
    assert "scalar" not in warning


def test_unconvertible_spool_path_starts_an_unavailable_client():
    client = EmitterClient.start(
        telemetry=TelemetrySettings(run=TelemetryRun("bad-path", "UK", "test")),
        spool_path=UnresolvablePath(),
    )
    assert not client.available
    assert client.startup_error.reason == "invalid_configuration"
