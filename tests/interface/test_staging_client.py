"""Typed staging-run-file hand-overs, through a real socket to the module."""

from pathlib import Path

import pytest
from fake_hugging_face import STAGING_REPOSITORY

from microcosm_emitter_interface.client import EmitterClient
from microcosm_emitter_interface.staging import RunFile, StageRunFiles
from microcosm_emitter_interface.transport import SocketTransport


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


def test_client_without_the_hugging_face_module_reports_unavailable(
    tmp_path, in_thread_emitter
):
    client = EmitterClient(
        in_thread_emitter(
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


def test_unreachable_emitter_never_raises(tmp_path, in_thread_emitter):
    client = EmitterClient(
        in_thread_emitter(
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
