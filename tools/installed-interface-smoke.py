"""Start the separately installed emitter from a build environment without it."""

import sys
from importlib.metadata import distribution
from importlib.util import find_spec
from pathlib import Path


def main(emitter_command: str) -> None:
    assert find_spec("microcosm_emitter") is None, "the build must not hold it"
    interface = distribution("microcosm-emitter-interface")
    assert not interface.requires, interface.requires
    from microcosm_emitter_interface import (
        EmitterClient,
        OrrerySettings,
        TelemetryRun,
        TelemetrySettings,
    )
    from microcosm_emitter_interface.protocol_fixtures import protocol_fixtures

    assert protocol_fixtures(1), "protocol fixtures must ship in the wheel"
    directory = Path.cwd() / "interface-graph"
    directory.mkdir()
    (directory / "graph.orrery.json").write_text('{"synthetic":true}')
    client = EmitterClient.start(
        telemetry=TelemetrySettings(
            run=TelemetryRun("interface-wheel", "UK", "installed"),
            development_collector_url="http://127.0.0.1:1",
        ),
        orrery=OrrerySettings(
            development_collector_url="http://127.0.0.1:1",
            publication_origin="http://127.0.0.1:1",
        ),
        spool_path=Path.cwd() / "interface.sqlite3",
        command=emitter_command,
        # A clean install compiles dependency bytecode on first use.
        startup_timeout=15,
    )
    assert client.available, client.startup_error
    try:
        assert client.modules == {"telemetry", "orrery"}
        assert client.emitter_version == interface.version
        assert client.telemetry.transition_stage("compile")
        assert client.telemetry.complete()
        inventory = client.orrery_inventory(directory, {"graph.orrery.json": "graph"})
        assert inventory is not None
        receipt = client.hand_over_orrery_inputs(directory, inventory, wait_seconds=0)
        assert receipt["status"] == "pending", receipt
    finally:
        process = client._process
        client.close()
    assert process is not None and process.process.wait(timeout=20) == 0


if __name__ == "__main__":
    main(sys.argv[1])
