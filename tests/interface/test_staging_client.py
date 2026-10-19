"""Typed staging-run-file hand-overs, through a real socket to the module."""

import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fake_hugging_face import STAGING_REPOSITORY, FakeHuggingFaceApi

from microcosm_emitter.host.contracts import ModuleContext
from microcosm_emitter.host.runtime import ServiceRuntime
from microcosm_emitter.huggingface.service.module import HuggingFaceModule
from microcosm_emitter_interface.client import EmitterClient
from microcosm_emitter_interface.staging import RunFile, StageRunFiles
from microcosm_emitter_interface.transport import SocketTransport


@dataclass
class InThreadEmitter:
    """An emitter host running in a thread of the test process."""

    transport: SocketTransport
    description: dict = field(default_factory=dict)

    def close(self):
        self.transport.close()


@pytest.fixture
def emitter_with_hub(tmp_path):
    hub = FakeHuggingFaceApi()
    hub.create_repository(STAGING_REPOSITORY)
    runtime_directory = Path(tempfile.mkdtemp(prefix="pe-stage-", dir="/tmp"))
    socket_path = runtime_directory / "service.sock"
    module = HuggingFaceModule(
        {"spool_path": str(tmp_path / "events.sqlite3")},
        ModuleContext(os.getpid()),
        api_factory=lambda: hub,
    )
    runtime = ServiceRuntime(
        socket_path,
        modules={"huggingface": module},
        is_parent_alive=lambda: True,
        tick_seconds=0.01,
    )
    thread = threading.Thread(target=runtime.run)
    thread.start()
    transport = SocketTransport(socket_path)
    deadline = time.monotonic() + 5
    while True:
        try:
            transport.ping()
            break
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.01)
    client = EmitterClient(
        InThreadEmitter(
            transport, {"modules": ["huggingface"], "protocol_versions": [1]}
        )
    )
    try:
        yield client, hub
    finally:
        client.close()
        thread.join(timeout=20)
        shutil.rmtree(runtime_directory, ignore_errors=True)


@pytest.mark.parametrize(
    "build_request",
    [
        lambda local: StageRunFiles(
            repository=STAGING_REPOSITORY,
            run_id="run-1",
            files=(RunFile("../escape.json", local),),
        ),
        lambda local: StageRunFiles(
            repository=STAGING_REPOSITORY,
            run_id="run-1",
            files=(RunFile("/absolute.json", local),),
        ),
        lambda local: StageRunFiles(
            repository=STAGING_REPOSITORY, run_id="run-1", prefix="releases"
        ),
        lambda local: StageRunFiles(repository=STAGING_REPOSITORY, run_id="../run"),
        lambda local: StageRunFiles(repository="no-owner", run_id="run-1"),
        lambda local: StageRunFiles(
            repository=STAGING_REPOSITORY, run_id="run-1", commit_interval_seconds=-1
        ),
        lambda local: StageRunFiles(
            repository=STAGING_REPOSITORY,
            run_id="run-1",
            run_index_entry={"run_id": "another-run"},
        ),
    ],
    ids=[
        "parent-directory",
        "absolute",
        "release-records",
        "run-identifier",
        "repository",
        "interval",
        "index-entry-of-another-run",
    ],
)
def test_requests_that_could_leave_the_run_folder_cannot_be_built(
    tmp_path, build_request
):
    with pytest.raises(ValueError):
        build_request(tmp_path / "progress.json")


def test_message_names_each_file_by_its_absolute_local_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    request = StageRunFiles(
        repository=STAGING_REPOSITORY,
        run_id="run-1",
        files=(RunFile("artifacts/fit_summary.json", Path("fit.json")),),
        flush=True,
        commit_interval_seconds=300,
    )
    assert request.message() == {
        "protocol_version": 1,
        "command": "stage_run_files",
        "repository": STAGING_REPOSITORY,
        "run_id": "run-1",
        "prefix": "runs",
        "files": [
            {
                "relative_path": "artifacts/fit_summary.json",
                "local_path": str((tmp_path / "fit.json").resolve()),
            }
        ],
        "latest_pointer": None,
        "run_index_entry": None,
        "flush": True,
        "commit_interval_seconds": 300.0,
    }


def test_client_without_the_hugging_face_module_reports_unavailable(tmp_path):
    client = EmitterClient(
        InThreadEmitter(
            SocketTransport(tmp_path / "unused.sock"),
            {"modules": ["telemetry"], "protocol_versions": [1]},
        )
    )
    receipt = client.stage_run_files(
        StageRunFiles(repository=STAGING_REPOSITORY, run_id="run-1")
    )
    assert receipt["status"] == "unavailable"
    assert client.read_back_run_files(STAGING_REPOSITORY, "run-1", wait_seconds=0)[
        "status"
    ] == ("unavailable")


def test_unreachable_emitter_never_raises(tmp_path):
    client = EmitterClient(
        InThreadEmitter(
            SocketTransport(tmp_path / "nothing-listens.sock"),
            {"modules": ["huggingface"], "protocol_versions": [1]},
        )
    )
    receipt = client.stage_run_files(
        StageRunFiles(repository=STAGING_REPOSITORY, run_id="run-1", flush=True)
    )
    assert receipt["status"] == "unavailable"


def test_hand_over_reaches_the_hub_in_one_commit_and_reads_back(
    tmp_path, emitter_with_hub
):
    client, hub = emitter_with_hub
    progress = tmp_path / "progress.json"
    progress.write_bytes(b'{"stage":"complete"}')
    manifest = tmp_path / "run_manifest.json"
    manifest.write_bytes(b'{"run_id":"run-1"}')
    receipt = client.stage_run_files(
        StageRunFiles(
            repository=STAGING_REPOSITORY,
            run_id="run-1",
            files=(
                RunFile("progress.json", progress),
                RunFile("run_manifest.json", manifest),
            ),
            flush=True,
        )
    )
    assert receipt["status"] == "recorded"
    read_back = client.read_back_run_files(STAGING_REPOSITORY, "run-1", wait_seconds=20)
    assert read_back["status"] == "passed", read_back
    assert read_back["files"] == 2
    (commit,) = hub.commits(STAGING_REPOSITORY)
    assert commit.added == {
        "runs/run-1/progress.json": b'{"stage":"complete"}',
        "runs/run-1/run_manifest.json": b'{"run_id":"run-1"}',
    }
    receipt = client.run_files_receipt(STAGING_REPOSITORY, "run-1")
    assert receipt["commits"] == 1 and receipt["last_commit"] == commit.oid
