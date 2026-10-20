"""Bundles reach the collector after their Hugging Face commit, when switched on."""

import hashlib
import time

import pytest
from fake_hugging_face import PRIVATE_REPOSITORY

from microcosm_emitter_interface.bundles import BundleFile, StageDatasetBundle
from microcosm_emitter_interface.submissions import CollectorProvenance

PROVENANCE = CollectorProvenance(
    country_code="UK", build_id="uk-frs-calibration-attempt-1", licensed_data=True
)
CONTENTS = {
    "microcosm_uk_2024_25.h5": ("dataset", b"synthetic h5 bytes"),
    "calibration_diagnostics.json": ("calibration_diagnostics", b'{"schema": 8}'),
    "build_manifest.json": ("build_manifest", b'{"outputs": {}}'),
    "staged_manifest.json": ("staged_manifest", b'{"files": {}}'),
    "sha256sums.txt": ("checksums", b"digests\n"),
}
ORRERY_CONTENTS = {
    "graph.schema.json": ("orrery_schema", b'{"schema": true}'),
    "graph.execution.json": ("execution_overlay", b'{"protocol": "overlay"}'),
}


def bundle(
    controls,
    *,
    collector=PROVENANCE,
    contents=CONTENTS,
    run_id="uk-run-1",
    prefix="staged",
):
    directory = controls.tmp_path / run_id
    directory.mkdir(exist_ok=True)
    files = []
    for name, (role, content) in contents.items():
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
    return StageDatasetBundle(
        repository=PRIVATE_REPOSITORY,
        run_id=run_id,
        files=files,
        collector=collector,
        prefix=prefix,
    )


def stage(controls, request):
    return controls.module.query(request.message())["receipt"]


def cycle(controls):
    controls.module.delivery.run_cycle()
    if controls.module.collector_delivery is not None:
        controls.module.collector_delivery.run_cycle()


def collector_receipt(controls, request):
    return controls.module.query(
        {
            "protocol_version": 1,
            "command": "collector_submission_receipt",
            "handover_id": request.handover_id(),
        }
    )["receipt"]


def test_a_described_bundle_reaches_the_collector_after_its_commit(
    submitting_module,
):
    controls = submitting_module
    request = bundle(controls)
    stage(controls, request)

    cycle(controls)

    (commit,) = controls.hub.commits(PRIVATE_REPOSITORY)
    (created,) = controls.collector.created
    assert created["kind"] == "calibration-diagnostics"
    assert created["provenance"]["huggingface"] == {
        "repo": PRIVATE_REPOSITORY,
        "commit": commit.oid,
        "folder": "staged/uk-run-1",
    }
    assert [file["name"] for file in created["files"]] == [
        "build_manifest.json",
        "calibration_diagnostics.json",
    ]
    assert sorted(controls.collector.objects.values()) == [
        b'{"outputs": {}}',
        b'{"schema": 8}',
    ]
    receipt = collector_receipt(controls, request)
    assert receipt["status"] == "submitted"
    assert receipt["error_code"] is None
    [submitted] = receipt["submissions"]
    assert submitted["kind"] == "calibration-diagnostics"
    assert submitted["status"] == "accepted"
    assert submitted["submission_id"] in controls.collector.submissions


def test_a_bundle_naming_orrery_inputs_submits_both_kinds(submitting_module):
    controls = submitting_module
    request = bundle(controls, contents={**CONTENTS, **ORRERY_CONTENTS})
    stage(controls, request)

    cycle(controls)

    assert sorted(created["kind"] for created in controls.collector.created) == [
        "calibration-diagnostics",
        "orrery-inputs",
    ]
    assert collector_receipt(controls, request)["status"] == "submitted"


def test_collector_submissions_are_off_by_default(default_module):
    controls = default_module
    request = bundle(controls)
    stage(controls, request)

    cycle(controls)

    assert len(controls.hub.commits(PRIVATE_REPOSITORY)) == 1
    assert controls.module.collector_delivery is None
    assert collector_receipt(controls, request) is None
    assert controls.collector.exchange_requests == []


def test_a_bundle_without_provenance_goes_only_to_hugging_face(submitting_module):
    controls = submitting_module
    request = bundle(controls, collector=None)
    stage(controls, request)

    cycle(controls)

    assert len(controls.hub.commits(PRIVATE_REPOSITORY)) == 1
    assert collector_receipt(controls, request) is None
    assert controls.collector.created == []


def test_the_collector_waits_for_the_hugging_face_commit(submitting_module):
    controls = submitting_module
    request = bundle(controls)
    stage(controls, request)
    controls.remove_credential()

    cycle(controls)
    waiting = collector_receipt(controls, request)
    controls.restore_credential()
    controls.clock.advance(600)
    cycle(controls)

    assert waiting["status"] == "pending"
    assert waiting["error_code"] == "WAITING_FOR_HUGGING_FACE"
    assert controls.collector.created
    assert collector_receipt(controls, request)["status"] == "submitted"


def test_a_bundle_hugging_face_refused_is_not_submitted(submitting_module, monkeypatch):
    from microcosm_emitter.huggingface.service import requests as request_module

    # Too large to copy: the changed file is uploaded from its path and refused.
    monkeypatch.setattr(request_module, "MAX_COPIED_FILE_BYTES", 8)
    controls = submitting_module
    request = bundle(controls)
    stage(controls, request)
    (controls.tmp_path / "uk-run-1" / "calibration_diagnostics.json").write_bytes(
        b'{"schema": 9}'
    )

    cycle(controls)

    receipt = collector_receipt(controls, request)
    assert receipt["status"] == "skipped"
    assert receipt["error_code"] == "NOT_STAGED_ON_HUGGING_FACE"
    assert controls.collector.created == []


@pytest.mark.parametrize("intake", ["off", "absent"])
def test_without_intake_the_submission_is_retried_about_hourly(
    submitting_module, intake
):
    controls = submitting_module
    request = bundle(controls)
    stage(controls, request)
    controls.collector.intake = intake

    cycle(controls)
    pending = collector_receipt(controls, request)
    controls.collector.intake = "on"
    controls.clock.advance(600)
    cycle(controls)
    still_pending = collector_receipt(controls, request)
    controls.clock.advance(3000)
    cycle(controls)

    assert pending["status"] == "pending"
    assert pending["error_code"] == "COLLECTOR_INTAKE_NOT_ENABLED"
    # Not attempted again within the hour, and nothing was submitted.
    assert still_pending["attempts"] == pending["attempts"]
    assert len(controls.collector.created) == 1
    assert collector_receipt(controls, request)["status"] == "submitted"


def test_a_refused_submission_fails_its_collector_hand_over(submitting_module):
    controls = submitting_module
    request = bundle(controls)
    stage(controls, request)
    controls.collector.create_statuses = [422]

    cycle(controls)

    receipt = collector_receipt(controls, request)
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "COLLECTOR_REFUSED_422"


def test_an_invalid_collector_description_refuses_the_bundle(submitting_module):
    controls = submitting_module
    message = bundle(controls).message()
    message["collector"] = {**message["collector"], "country_code": "uk"}

    receipt = controls.module.query(message)["receipt"]

    assert receipt["status"] == "refused"
    assert receipt["error_code"] == "INVALID_REQUEST"


def test_the_module_tick_delivers_to_both_destinations(submitting_module):
    controls = submitting_module
    request = bundle(controls)
    stage(controls, request)

    controls.module.tick(time.monotonic())

    assert len(controls.hub.commits(PRIVATE_REPOSITORY)) == 1
    assert collector_receipt(controls, request)["status"] == "submitted"


def test_files_microcosm_rewrites_after_staging_are_submitted_as_handed_over(
    submitting_module,
):
    """Microcosm rewrites its build manifest right after ``stage_dataset``."""
    controls = submitting_module
    request = bundle(controls)
    receipt = stage(controls, request)
    assert "build_manifest.json" in receipt["copied_files"]
    manifest = controls.tmp_path / "uk-run-1" / "build_manifest.json"
    rewritten = manifest.with_name(".build_manifest.json.rewrite")
    rewritten.write_bytes(b'{"outputs": {}, "staging_delivery": {}}')
    rewritten.replace(manifest)

    cycle(controls)

    assert collector_receipt(controls, request)["status"] == "submitted"
    assert sorted(controls.collector.objects.values()) == [
        b'{"outputs": {}}',
        b'{"schema": 8}',
    ]


def test_a_folder_longer_than_the_collector_accepts_is_not_submitted(
    submitting_module,
):
    """The contract allows a folder of at most 512 characters; a longer one
    would be refused with a final 422."""
    controls = submitting_module
    long_prefix = "/".join(["p" * 128] * 3)
    request = bundle(controls, run_id="r" * 128, prefix=long_prefix)
    assert len(request.remote_prefix) > 512
    stage(controls, request)

    cycle(controls)

    receipt = collector_receipt(controls, request)
    assert (receipt["status"], receipt["error_code"]) == (
        "skipped",
        "COLLECTOR_FOLDER_TOO_LONG",
    )
    assert controls.collector.created == []
    assert len(controls.hub.commits(PRIVATE_REPOSITORY)) == 1
