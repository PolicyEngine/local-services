"""A long upload to one repository never holds up commits to another.

The module runs inside a real host runtime with the in-memory Hub. A bundle
commit to the private repository blocks until the test releases it; run-file
commits to the staging repository, the commit at shutdown, and a read-back
must all complete meanwhile.
"""

import hashlib
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_hugging_face import (
    PRIVATE_REPOSITORY,
    STAGING_REPOSITORY,
    FakeHuggingFaceApi,
)

from microcosm_emitter_interface.bundles import BundleFile, StageDatasetBundle
from microcosm_emitter_interface.client import EmitterClient
from microcosm_emitter_interface.staging import RunFile, StageRunFiles
from microcosm_emitter_interface.transport import SocketTransport

SHUTDOWN_SECONDS = 2.0
BLOCKED_COMMIT_LIMIT_SECONDS = 30.0


@dataclass
class InThreadEmitter:
    """The client's view of an emitter host running in a test thread."""

    transport: object
    description: dict = field(default_factory=dict)

    def close(self):
        self.transport.close()


def wait_until(condition, *, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


@pytest.fixture
def running_module(tmp_path):
    """The Hugging Face module in a host runtime; its next commit blocks."""
    from microcosm_emitter.host.contracts import ModuleContext
    from microcosm_emitter.host.runtime import ServiceRuntime
    from microcosm_emitter.huggingface.service.module import HuggingFaceModule

    hub = FakeHuggingFaceApi(whoami={"auth": {"accessToken": {"role": "write"}}})
    hub.create_repository(STAGING_REPOSITORY, {"README.md": b"staging fixture"})
    hub.create_repository(PRIVATE_REPOSITORY, {"README.md": b"private fixture"})
    commit_entered = threading.Event()
    commit_released = threading.Event()

    def block_the_next_commit():
        commit_entered.set()
        commit_released.wait(BLOCKED_COMMIT_LIMIT_SECONDS)

    hub.run_before_next_commit(block_the_next_commit)
    runtime_directory = Path(tempfile.mkdtemp(prefix="pe-workers-", dir="/tmp"))
    socket_path = runtime_directory / "service.sock"
    module = HuggingFaceModule(
        {"handover_database_path": str(tmp_path / "events.handovers.sqlite3")},
        ModuleContext(os.getpid()),
        api_factory=lambda: hub,
    )
    runtime = ServiceRuntime(
        socket_path,
        modules={"huggingface": module},
        is_parent_alive=lambda: True,
        tick_seconds=0.01,
        drain_seconds=SHUTDOWN_SECONDS,
    )
    runtime_errors: list[BaseException] = []

    def run_runtime():
        try:
            runtime.run()
        except BaseException as error:
            runtime_errors.append(error)

    runtime_thread = threading.Thread(target=run_runtime, daemon=True)
    runtime_thread.start()
    transport = SocketTransport(socket_path)
    assert wait_until(lambda: _answers_ping(transport), seconds=5)
    client = EmitterClient(
        InThreadEmitter(
            transport, {"modules": ["huggingface"], "protocol_versions": [1]}
        )
    )
    controls = SimpleNamespace(
        hub=hub,
        client=client,
        tmp_path=tmp_path,
        commit_entered=commit_entered,
        commit_released=commit_released,
        runtime_thread=runtime_thread,
        runtime_errors=runtime_errors,
    )
    try:
        yield controls
    finally:
        commit_released.set()
        client.close()
        runtime_thread.join(timeout=SHUTDOWN_SECONDS + 10)
        shutil.rmtree(runtime_directory, ignore_errors=True)


def _answers_ping(transport) -> bool:
    try:
        transport.ping()
    except Exception:
        return False
    return True


def bundle_file(directory: Path, name: str, role: str, content: bytes) -> BundleFile:
    path = directory / name
    path.write_bytes(content)
    return BundleFile(
        name=name,
        role=role,
        local_path=path,
        sha256=hashlib.sha256(content).hexdigest(),
        bytes=len(content),
    )


def start_a_blocked_bundle_upload(controls) -> None:
    """Hand over a bundle; return once its commit is in progress and blocked."""
    directory = controls.tmp_path / "bundle"
    directory.mkdir()
    request = StageDatasetBundle(
        repository=PRIVATE_REPOSITORY,
        run_id="uk-run-1",
        files=(
            bundle_file(directory, "dataset.h5", "dataset", b"rows"),
            bundle_file(
                directory,
                "calibration_diagnostics.json",
                "calibration_diagnostics",
                b"{}",
            ),
            bundle_file(
                directory, "staged_manifest.json", "staged_manifest", b'{"files": {}}'
            ),
        ),
    )
    receipt = controls.client.stage_dataset_bundle(request)
    assert receipt["status"] == "pending", receipt
    assert controls.commit_entered.wait(10), "the bundle commit never started"


def stage_progress(controls, *, flush: bool) -> None:
    progress = controls.tmp_path / "progress.json"
    progress.write_bytes(b'{"stage":"completed"}')
    receipt = controls.client.stage_run_files(
        StageRunFiles(
            repository=STAGING_REPOSITORY,
            run_id="staging-run-1",
            files=(RunFile("progress.json", progress),),
            flush=flush,
            commit_interval_seconds=3600,
        )
    )
    assert receipt["status"] == "recorded", receipt


def staging_holds_the_progress_file(controls) -> bool:
    return any(
        "runs/staging-run-1/progress.json" in commit.added
        for commit in controls.hub.commits(STAGING_REPOSITORY)
    )


def test_run_files_reach_another_repository_during_a_bundle_upload(running_module):
    controls = running_module
    start_a_blocked_bundle_upload(controls)

    stage_progress(controls, flush=True)

    assert wait_until(lambda: staging_holds_the_progress_file(controls), seconds=5)
    assert controls.hub.commits(PRIVATE_REPOSITORY) == []


def test_the_shutdown_commit_runs_during_a_bundle_upload(running_module):
    controls = running_module
    start_a_blocked_bundle_upload(controls)
    # Not due for an hour: only the commit at shutdown can deliver it.
    stage_progress(controls, flush=False)
    assert not staging_holds_the_progress_file(controls)

    controls.client.close()
    controls.runtime_thread.join(timeout=SHUTDOWN_SECONDS + 2)

    assert not controls.runtime_thread.is_alive()
    assert controls.runtime_errors == []
    assert staging_holds_the_progress_file(controls)
    assert controls.hub.commits(PRIVATE_REPOSITORY) == []


def test_a_read_back_passes_during_a_bundle_upload(running_module):
    controls = running_module
    start_a_blocked_bundle_upload(controls)
    stage_progress(controls, flush=False)

    result = controls.client.read_back_run_files(
        STAGING_REPOSITORY, "staging-run-1", wait_seconds=10
    )

    assert result["status"] == "passed", result
    assert result["files"] == 1
    assert controls.hub.commits(PRIVATE_REPOSITORY) == []


def test_the_bundle_commit_lands_once_its_upload_finishes(running_module):
    controls = running_module
    start_a_blocked_bundle_upload(controls)

    controls.commit_released.set()

    assert wait_until(
        lambda: len(controls.hub.commits(PRIVATE_REPOSITORY)) == 1, seconds=10
    )


def bundle_message(directory: Path, repository: str, run_id: str) -> dict:
    directory.mkdir(parents=True)
    return StageDatasetBundle(
        repository=repository,
        run_id=run_id,
        files=(
            bundle_file(directory, "dataset.h5", "dataset", b"rows"),
            bundle_file(
                directory, "staged_manifest.json", "staged_manifest", b'{"files": {}}'
            ),
        ),
    ).message()


def queue_bundles_before_the_workers_start(
    database_path: Path, hub, directory: Path, repositories: list[str]
) -> None:
    """Queue one bundle per repository, so the worker's first cycle lists all."""
    from microcosm_emitter.host.contracts import ModuleContext
    from microcosm_emitter.huggingface.service.module import HuggingFaceModule

    queueing_module = HuggingFaceModule(
        {"handover_database_path": str(database_path)},
        ModuleContext(os.getpid()),
        api_factory=lambda: hub,
        background_workers=False,
    )
    queueing_module.initialize()
    for position, repository in enumerate(repositories):
        receipt = queueing_module.query(
            bundle_message(
                directory / f"bundle-{position}", repository, f"run-{position}"
            )
        )["receipt"]
        assert receipt["status"] == "pending", receipt
    queueing_module.close(time.monotonic() + SHUTDOWN_SECONDS)


def test_no_bundle_upload_starts_once_shutdown_begins(tmp_path):
    """Shutdown during one bundle upload: the worker starts no other upload.

    The worker's cycle lists two repositories. Shutdown begins while the
    upload to the first is held; that upload then finishes within the
    shutdown period. No upload to the second repository may start, and the
    first repository's run files, which the first shutdown commit skipped
    because the upload held that repository, get the second shutdown commit.
    """
    from microcosm_emitter.host.contracts import ModuleContext
    from microcosm_emitter.huggingface.service.module import HuggingFaceModule

    hub = FakeHuggingFaceApi(whoami={"auth": {"accessToken": {"role": "write"}}})
    hub.create_repository(PRIVATE_REPOSITORY, {"README.md": b"private fixture"})
    hub.create_repository(STAGING_REPOSITORY, {"README.md": b"staging fixture"})
    # The cycle visits repositories in name order: the private one first.
    assert PRIVATE_REPOSITORY < STAGING_REPOSITORY
    database_path = tmp_path / "events.handovers.sqlite3"
    queue_bundles_before_the_workers_start(
        database_path, hub, tmp_path, [PRIVATE_REPOSITORY, STAGING_REPOSITORY]
    )

    first_upload_entered = threading.Event()
    first_upload_released = threading.Event()
    second_upload_started = threading.Event()
    test_finished = threading.Event()

    def hold_the_first_upload():
        first_upload_entered.set()
        first_upload_released.wait(BLOCKED_COMMIT_LIMIT_SECONDS)

    hub.run_before_next_commit(hold_the_first_upload)
    create_commit_on_the_hub = hub.create_commit

    def create_commit_holding_the_second_repository(repo_id, operations, **options):
        if repo_id == STAGING_REPOSITORY:
            # An upload that outlasts the shutdown period.
            second_upload_started.set()
            test_finished.wait(BLOCKED_COMMIT_LIMIT_SECONDS)
        return create_commit_on_the_hub(repo_id, operations, **options)

    hub.create_commit = create_commit_holding_the_second_repository
    module = HuggingFaceModule(
        {"handover_database_path": str(database_path)},
        ModuleContext(os.getpid()),
        api_factory=lambda: hub,
        worker_interval_seconds=0.05,
    )
    module.initialize()
    try:
        assert first_upload_entered.wait(10), "the first bundle upload never started"
        progress = tmp_path / "progress.json"
        progress.write_bytes(b'{"stage":"completed"}')
        # Not due for an hour: only a commit at shutdown can deliver it.
        run_files_receipt = module.query(
            StageRunFiles(
                repository=PRIVATE_REPOSITORY,
                run_id="staging-run-1",
                files=(RunFile("progress.json", progress),),
                flush=False,
                commit_interval_seconds=3600,
            ).message()
        )["receipt"]
        assert run_files_receipt["status"] == "recorded", run_files_receipt
        releaser = threading.Timer(0.5, first_upload_released.set)
        releaser.start()

        module.close(time.monotonic() + SHUTDOWN_SECONDS)

        releaser.join()
        assert not second_upload_started.is_set(), (
            "an upload to the second repository started after shutdown began"
        )
        assert any(
            "runs/staging-run-1/progress.json" in commit.added
            for commit in hub.commits(PRIVATE_REPOSITORY)
        ), "the first repository's run files missed the second shutdown commit"
    finally:
        first_upload_released.set()
        test_finished.set()
