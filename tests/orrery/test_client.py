"""Graph adapters only request service-side work and never claim lost data is queued."""

import ast
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from microcosm_emitter.core.auth import shared_session
from microcosm_emitter.core.database import create_spool_engine
from microcosm_emitter.core.models import TelemetryRunRecord
from microcosm_emitter.host.errors import LocalServiceError
from microcosm_emitter.host.launcher import start_services
from microcosm_emitter.orrery import client as client_module
from microcosm_emitter.orrery.client import GraphPublicationClient, publish_graph
from microcosm_emitter.orrery.service.queue import GraphPublicationQueue
from microcosm_emitter.telemetry.client import LocalTelemetryEmitter, TelemetryRun


def graph_files(tmp_path):
    directory = tmp_path / "graph"
    directory.mkdir()
    (directory / "graph.orrery.json").write_text('{"nodes":[]}')
    return directory


def test_public_client_enqueues_through_graph_only_service_without_run(tmp_path):
    directory = graph_files(tmp_path)
    path = tmp_path / "jobs.sqlite3"
    client = GraphPublicationClient.start(
        spool_path=path,
        development_collector_url="http://127.0.0.1:1",
        publication_origin="http://127.0.0.1:1",
    )
    assert client.available
    try:
        inventory = client.inventory(
            directory,
            {"graph.orrery.json": "graph"},
            publication_id="independent-graph",
        )
        assert inventory is not None and "run_id" not in inventory
        receipt = publish_graph(directory, inventory, client=client, wait_seconds=0)
        assert receipt["status"] == "pending"
        assert receipt["publication_id"] == "independent-graph"
        assert client.receipt("independent-graph")["status"] == "pending"
        assert client.retry("independent-graph")["status"] == "pending"
    finally:
        handle = client._handle
        client.close()
        assert handle.process.wait(timeout=20) == 0
    queue = GraphPublicationQueue(path)
    try:
        assert queue.receipt("independent-graph")["status"] == "pending"
    finally:
        queue.close()
    engine = create_spool_engine(path)
    try:
        with Session(engine) as session:
            assert (
                session.scalar(select(func.count()).select_from(TelemetryRunRecord))
                == 0
            )
    finally:
        engine.dispose()


def test_combined_host_keeps_graph_service_after_telemetry_completion(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    run = TelemetryRun("combined", "UK", "test")
    handle = start_services(
        {
            "telemetry": {
                "registration": run.as_registration(),
                "spool_path": str(path),
                "development_collector_url": "http://127.0.0.1:1",
            },
            "orrery": {
                "spool_path": str(path),
                "development_collector_url": "http://127.0.0.1:1",
                "publication_origin": "http://127.0.0.1:1",
            },
        }
    )
    graph = GraphPublicationClient(handle.client.for_module("orrery"))
    emitter = LocalTelemetryEmitter(
        run=run, transport=handle.client.for_module("telemetry")
    )
    try:
        emitter.transition_stage("hf_download")
        emitter.complete()
        handle.client.ping()
        directory = graph_files(tmp_path)
        inventory = graph.inventory(directory, {"graph.orrery.json": "graph"})
        assert inventory is not None
        assert graph.enqueue(directory, inventory)["status"] == "pending"
        assert graph.receipt(inventory["publication_id"])["status"] == "pending"
    finally:
        graph.close()
        handle.client.ping()  # A module adapter cannot close the shared host.
        handle.close()
        assert handle.process.wait(timeout=20) == 0


def test_graph_adapter_never_imports_persistence_or_auth():
    tree = ast.parse(Path(client_module.__file__).read_text())
    imports = [
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    ]
    assert not any(".service" in name or ".core" in name for name in imports)


def test_startup_failure_returns_unavailable_without_direct_queue(
    tmp_path, monkeypatch
):
    path = tmp_path / "jobs.sqlite3"
    monkeypatch.setattr(
        client_module, "start_service", Mock(side_effect=LocalServiceError("synthetic"))
    )
    client = GraphPublicationClient.start(spool_path=path)
    assert not client.available
    inventory = {"version": 1, "publication_id": "not-persisted", "files": []}
    assert client.enqueue(tmp_path, inventory) == {
        "version": 1,
        "publication_id": "not-persisted",
        "status": "failed",
        "error_code": "local_service_unavailable",
    }
    assert client.inventory(tmp_path, {}) is None
    assert not path.exists()


def test_request_failure_is_contained_without_claiming_pending(tmp_path):
    transport = Mock()
    transport.request.side_effect = OSError("synthetic private error")
    client = GraphPublicationClient(transport)
    inventory = {"version": 1, "publication_id": "not-acknowledged", "files": []}
    receipt = publish_graph(tmp_path, inventory, client=client, wait_seconds=0)
    assert receipt["status"] == "failed"
    assert "synthetic" not in receipt["error_code"]


def test_graph_timeout_failure_does_not_claim_durable_enqueue(tmp_path):
    from microcosm_emitter.orrery.constants import GRAPH_REQUEST_TIMEOUT_SECONDS

    transport = Mock()
    transport.request.side_effect = LocalServiceError("TimeoutError")
    client = GraphPublicationClient(transport)
    inventory = {"version": 1, "publication_id": "timed-out", "files": []}
    receipt = client.enqueue(tmp_path, inventory)
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "local_service_unavailable"
    assert transport.request.call_args.kwargs == {
        "timeout": GRAPH_REQUEST_TIMEOUT_SECONDS
    }


def test_modules_share_one_session_and_reject_conflicting_origins():
    services = {}
    first = shared_session(services, "https://collector.example")
    assert shared_session(services, "https://collector.example") is first
    with pytest.raises(ValueError, match="same collector origin"):
        shared_session(services, "https://different.example")


def test_retry_waits_for_a_new_attempt_via_receipt_requests(monkeypatch):
    pending = {
        "version": 1,
        "publication_id": "retry",
        "status": "pending",
        "error_code": "credential_missing",
        "attempts": 1,
    }
    tried = {**pending, "attempts": 2}
    transport = Mock()
    transport.request.side_effect = [{"receipt": pending}, {"receipt": tried}]
    monkeypatch.setattr(client_module.time, "sleep", lambda _: None)
    client = GraphPublicationClient(transport)
    assert client.retry("retry", wait_seconds=1) == tried
    assert [call.args[0]["command"] for call in transport.request.call_args_list] == [
        "retry",
        "receipt",
    ]
