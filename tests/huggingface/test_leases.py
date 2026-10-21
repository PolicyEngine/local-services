"""Repository and job leases: renewed during long uploads, short after a crash."""

import hashlib
import time

from fake_hugging_face import PRIVATE_REPOSITORY, STAGING_REPOSITORY

from microcosm_emitter.huggingface.service import delivery as delivery_module
from microcosm_emitter_interface.bundles import BundleFile, StageDatasetBundle
from microcosm_emitter_interface.staging import RunFile, StageRunFiles


def hand_over_run_file(controls, run_id="run-1"):
    local = controls.tmp_path / run_id / "progress.json"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b'{"stage":"calibrating"}')
    return controls.module.query(
        StageRunFiles(
            repository=STAGING_REPOSITORY,
            run_id=run_id,
            files=(RunFile("progress.json", local),),
            flush=True,
        ).message()
    )["receipt"]


def hand_over_bundle(controls, run_id="uk-run-1"):
    directory = controls.tmp_path / run_id
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    for name, role, content in (
        ("dataset.h5", "dataset", b"rows"),
        ("staged_manifest.json", "staged_manifest", b'{"files": {}}'),
    ):
        (directory / name).write_bytes(content)
        files.append(
            BundleFile(
                name=name,
                role=role,
                local_path=directory / name,
                sha256=hashlib.sha256(content).hexdigest(),
                bytes=len(content),
            )
        )
    request = StageDatasetBundle(
        repository=PRIVATE_REPOSITORY, run_id=run_id, files=tuple(files)
    )
    controls.module.query(request.message())
    return request


def test_a_killed_emitter_holds_a_repository_for_at_most_one_short_lease(
    hugging_face_module,
):
    """The lease of an emitter that never released it expires within minutes."""
    controls = hugging_face_module
    queue = controls.module.queue
    lease_seconds = delivery_module.REPOSITORY_LEASE_SECONDS
    assert lease_seconds <= 120
    # The killed emitter's lease, never released or renewed.
    assert queue.claim_repository(
        STAGING_REPOSITORY, now=controls.clock(), lease_seconds=lease_seconds
    )
    hand_over_run_file(controls)
    controls.module.delivery.run_cycle()
    assert controls.hub.commits(STAGING_REPOSITORY) == []
    controls.clock.advance(121)
    controls.module.delivery.run_cycle()
    assert len(controls.hub.commits(STAGING_REPOSITORY)) == 1


def wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    return condition()


def test_a_long_run_file_upload_keeps_renewing_its_repository_lease(
    hugging_face_module,
):
    controls = hugging_face_module
    queue = controls.module.queue
    controls.module.delivery.lease_renewal_seconds = 0.01
    claimed_during_upload = []

    def slow_upload():
        # The upload outlasts several leases; renewal keeps the repository.
        for _ in range(3):
            controls.clock.advance(delivery_module.REPOSITORY_LEASE_SECONDS)
            renewed_until = controls.clock() + delivery_module.REPOSITORY_LEASE_SECONDS
            assert wait_until(
                lambda until=renewed_until: (
                    queue.repository_lease(STAGING_REPOSITORY) >= until
                )
            ), "the lease was not renewed during the upload"
            claimed_during_upload.append(
                queue.claim_repository(
                    STAGING_REPOSITORY, now=controls.clock(), lease_seconds=1
                )
            )

    controls.hub.run_before_next_commit(slow_upload)
    hand_over_run_file(controls)
    controls.module.delivery.run_cycle()
    assert len(controls.hub.commits(STAGING_REPOSITORY)) == 1
    assert claimed_during_upload == [None, None, None]
    assert queue.repository_lease(STAGING_REPOSITORY) == 0.0


def test_a_long_bundle_upload_keeps_renewing_its_job_lease(hugging_face_module):
    controls = hugging_face_module
    queue = controls.module.queue
    controls.module.delivery.lease_renewal_seconds = 0.01
    claimed_during_upload = []

    def slow_upload():
        controls.clock.advance(delivery_module.REPOSITORY_LEASE_SECONDS * 2)
        renewed_until = controls.clock() + delivery_module.REPOSITORY_LEASE_SECONDS
        assert wait_until(
            lambda: queue.repository_lease(PRIVATE_REPOSITORY) >= renewed_until
        ), "the lease was not renewed during the upload"
        claimed_during_upload.append(
            queue.claim_job(
                PRIVATE_REPOSITORY,
                now=controls.clock(),
                lease_seconds=1,
                kinds=delivery_module.SUPPORTED_JOB_KINDS,
            )
        )

    controls.hub.run_before_next_commit(slow_upload)
    request = hand_over_bundle(controls)
    controls.module.delivery.run_cycle()
    assert claimed_during_upload == [None]
    receipt = queue.job_receipt(request.handover_id())
    # The attempt was recorded under the renewed lease.
    assert (receipt["status"], receipt["attempts"]) == ("uploaded", 1)
