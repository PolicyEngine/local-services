"""Verify the single installed distribution away from the source checkout."""

import sys
from importlib.metadata import distribution
from importlib.util import find_spec
from pathlib import Path


def host_smoke():
    from microcosm_emitter.host.client import SocketClient
    from microcosm_emitter.host.launcher import start_service

    assert SocketClient and start_service
    assert find_spec("microcosm") is None
    emitter = distribution("microcosm-emitter")
    assert emitter.version
    assert emitter.metadata.get_all("Project-URL") == [
        "Source, https://github.com/PolicyEngine/microcosm-emitter"
    ]
    for name in ("sqlalchemy", "alembic", "huggingface_hub"):
        assert find_spec(name) is not None, name
        assert name not in sys.modules, name
    assert "microcosm_emitter.telemetry" not in sys.modules
    from microcosm_emitter.orrery.client import GraphPublicationClient

    assert GraphPublicationClient
    assert "microcosm_emitter.core" not in sys.modules
    assert "microcosm_emitter.orrery.service" not in sys.modules


def telemetry_smoke():
    from microcosm_emitter.telemetry.client import LocalTelemetryEmitter, TelemetryRun

    assert "sqlalchemy" not in sys.modules
    assert "huggingface_hub" not in sys.modules
    assert find_spec("microcosm") is None
    run = TelemetryRun("wheel-test", "UK", "installed")
    path = Path.cwd() / "spool" / "events.sqlite3"
    emitter = LocalTelemetryEmitter.start(
        run=run,
        spool_path=path,
        development_collector_url="http://127.0.0.1:1",
        # A clean wheel install must compile dependency bytecode on first use.
        # Test packaging independently of the production startup-time limit.
        startup_timeout_seconds=10,
    )
    assert emitter.available
    try:
        emitter.stage("compile")
        emitter.complete()
        assert emitter._handle.process.wait(timeout=10) == 0
    finally:
        emitter.close()
    from microcosm_emitter.core.migrations import (
        current_database_revision,
    )
    from microcosm_emitter.telemetry.service.spool import EventSpool

    assert current_database_revision(path) == "20261008_02"
    spool = EventSpool(path)
    try:
        assert not spool.has_deliverable()
        assert [
            event["sequence"] for event in spool.batch(run.run_id, run.producer_id)
        ] == [1, 2, 3]
    finally:
        spool.close()


def graph_only_smoke():
    from microcosm_emitter.orrery.client import GraphPublicationClient, publish_graph
    from microcosm_emitter.orrery.service.queue import GraphPublicationQueue

    path = Path.cwd() / "graph-only.sqlite3"
    directory = Path.cwd() / "graph-only"
    directory.mkdir()
    (directory / "graph.orrery.json").write_text('{"synthetic":true}')
    client = GraphPublicationClient.start(
        spool_path=path,
        development_collector_url="http://127.0.0.1:1",
        publication_origin="http://127.0.0.1:1",
        startup_timeout=10,
    )
    assert client.available
    try:
        inventory = client.inventory(directory, {"graph.orrery.json": "graph"})
        assert inventory is not None and "run_id" not in inventory
        receipt = publish_graph(directory, inventory, client=client, wait_seconds=0)
        assert receipt["status"] == "pending"
    finally:
        handle = client._handle
        client.close()
        assert handle.process.wait(timeout=20) == 0
    queue = GraphPublicationQueue(path)
    try:
        assert queue.receipt(inventory["publication_id"])["status"] == "pending"
    finally:
        queue.close()


def combined_smoke():
    from microcosm_emitter.host.launcher import start_services
    from microcosm_emitter.orrery.client import GraphPublicationClient, publish_graph
    from microcosm_emitter.telemetry.client import LocalTelemetryEmitter, TelemetryRun

    path = Path.cwd() / "combined.sqlite3"
    run = TelemetryRun("combined-test", "UK", "installed")
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
        },
        startup_timeout=10,
    )
    client = GraphPublicationClient(handle.client.for_module("orrery"))
    directory = Path.cwd() / "combined-graph"
    directory.mkdir()
    (directory / "graph.orrery.json").write_text('{"synthetic":true}')
    try:
        emitter = LocalTelemetryEmitter(
            run=run, transport=handle.client.for_module("telemetry")
        )
        emitter.stage("hf_download")
        emitter.complete()
        handle.client.ping()
        inventory = client.inventory(directory, {"graph.orrery.json": "graph"})
        assert inventory is not None
        assert (
            publish_graph(directory, inventory, client=client, wait_seconds=0)["status"]
            == "pending"
        )
    finally:
        client.close()
        handle.close()
        assert handle.process.wait(timeout=20) == 0


if __name__ == "__main__":
    host_smoke()
    telemetry_smoke()
    graph_only_smoke()
    combined_smoke()
