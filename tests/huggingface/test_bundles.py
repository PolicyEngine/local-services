"""Staged dataset bundles: Microcosm's paths, idempotency and error codes."""

import hashlib
import json

import pytest
from fake_hugging_face import (
    PRIVATE_REPOSITORY,
    FakeHubHTTPError,
    FakeHuggingFaceApi,
)

from microcosm_emitter.host.contracts import ModuleContext
from microcosm_emitter.huggingface.service.module import HuggingFaceModule
from microcosm_emitter_interface.bundles import BundleFile, StageDatasetBundle

DATASET = b"synthetic h5 bytes"
DIAGNOSTICS = b'{"schema_version":8}'


def bundle_files(directory, *, dataset=DATASET, manifest_note="first"):
    """A bundle laid out as Microcosm's write_sidecars leaves it."""
    directory.mkdir(parents=True, exist_ok=True)
    outputs = {
        "microcosm_uk_2024_25.h5": ("dataset", dataset),
        "microcosm_uk_2024_25.diagnostics.json": (
            "calibration_diagnostics",
            DIAGNOSTICS,
        ),
    }
    digests = {
        name: {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
        for name, (_, content) in outputs.items()
    }
    manifest = json.dumps({"outputs": digests, "note": manifest_note}).encode()
    staged_manifest = json.dumps(
        {"schema_name": "microcosm.staged-dataset.manifest", "files": digests},
        indent=2,
        sort_keys=True,
    ).encode()
    sums = "".join(
        f"{entry['sha256']}  {name}\n" for name, entry in sorted(digests.items())
    ).encode()
    files = {
        **outputs,
        "rowwise_candidate_manifest.json": ("build_manifest", manifest),
        "staged_manifest.json": ("staged_manifest", staged_manifest),
        "sha256sums.txt": ("checksums", sums),
    }
    bundle = []
    for name, (role, content) in files.items():
        (directory / name).write_bytes(content)
        bundle.append(
            BundleFile(
                name=name,
                role=role,
                local_path=directory / name,
                sha256=hashlib.sha256(content).hexdigest(),
                bytes=len(content),
            )
        )
    return tuple(bundle), {name: content for name, (_, content) in files.items()}


def stage_bundle(controls, run_id="uk-run-1", **options):
    directory = controls.tmp_path / options.pop("directory", run_id)
    files, contents = bundle_files(directory, **options)
    request = StageDatasetBundle(
        repository=PRIVATE_REPOSITORY, run_id=run_id, files=files
    )
    receipt = controls.module.query(request.message())["receipt"]
    return request, receipt, contents


def receipt_of(controls, handover_id):
    return controls.module.query(
        {
            "protocol_version": 1,
            "command": "handover_receipt",
            "handover_id": handover_id,
        }
    )["receipt"]


def cycle(controls):
    controls.module.delivery.run_cycle()


def private_commits(controls):
    return controls.hub.commits(PRIVATE_REPOSITORY)


def test_bundle_is_one_commit_at_microcosm_paths_with_its_message(
    hugging_face_module,
):
    controls = hugging_face_module
    head_before = controls.hub.repositories[PRIVATE_REPOSITORY].head.oid
    request, receipt, contents = stage_bundle(controls)
    assert receipt["status"] == "pending"
    assert receipt["prefix"] == "staged/uk-run-1"
    cycle(controls)
    (commit,) = private_commits(controls)
    assert commit.added == {
        f"staged/uk-run-1/{name}": content for name, content in contents.items()
    }
    assert list(commit.added) == [
        f"staged/uk-run-1/{item.name}" for item in request.files
    ]
    assert commit.message == "Stage uk-run-1 under staged/uk-run-1"
    assert commit.parent == head_before
    (create_commit,) = [
        arguments
        for method, arguments in controls.hub.calls
        if method == "create_commit"
    ]
    assert create_commit["parent_commit"] == head_before
    receipt = receipt_of(controls, request.handover_id())
    assert receipt["status"] == "uploaded"
    assert receipt["revision"] == commit.oid
    assert receipt["error_code"] is None


def test_restaging_the_same_outputs_keeps_the_original_revision(hugging_face_module):
    controls = hugging_face_module
    first, _, _ = stage_bundle(controls)
    cycle(controls)
    (commit,) = private_commits(controls)
    controls.clock.advance(60)
    # The build appended evidence to its manifest; the outputs are unchanged.
    again, _, _ = stage_bundle(controls, directory="again", manifest_note="evidence")
    assert again.handover_id() != first.handover_id()
    cycle(controls)
    assert private_commits(controls) == [commit]
    receipt = receipt_of(controls, again.handover_id())
    assert (receipt["status"], receipt["revision"]) == ("already_staged", commit.oid)


def test_a_different_bundle_under_the_same_run_is_never_overwritten(
    hugging_face_module,
):
    controls = hugging_face_module
    stage_bundle(controls)
    cycle(controls)
    controls.clock.advance(60)
    other, _, _ = stage_bundle(controls, directory="other", dataset=b"other bytes")
    cycle(controls)
    assert len(private_commits(controls)) == 1
    receipt = receipt_of(controls, other.handover_id())
    assert (receipt["status"], receipt["error_code"]) == ("failed", "REMOTE_DIFFERS")
    controls.clock.advance(600)
    cycle(controls)
    assert receipt_of(controls, other.handover_id())["attempts"] == 1


@pytest.mark.parametrize(
    ("method", "error_code"),
    [("file_exists", "REPOSITORY_UNAVAILABLE"), ("create_commit", "UPLOAD_FAILED")],
)
def test_hub_failures_are_retried_with_microcosm_error_codes(
    hugging_face_module, method, error_code
):
    controls = hugging_face_module
    controls.hub.fail_next(method, FakeHubHTTPError(503))
    request, _, _ = stage_bundle(controls)
    cycle(controls)
    receipt = receipt_of(controls, request.handover_id())
    assert (receipt["status"], receipt["error_code"], receipt["attempts"]) == (
        "pending",
        error_code,
        1,
    )
    assert private_commits(controls) == []
    controls.clock.advance(2)
    cycle(controls)
    assert receipt_of(controls, request.handover_id())["status"] == "uploaded"


def test_a_commit_that_loses_a_race_is_retried_on_the_new_head(hugging_face_module):
    controls = hugging_face_module
    controls.hub.run_before_next_commit(
        lambda: controls.hub.write_directly(PRIVATE_REPOSITORY, {"other.json": b"{}"})
    )
    request, _, _ = stage_bundle(controls)
    cycle(controls)
    assert receipt_of(controls, request.handover_id())["status"] == "uploaded"
    ours = [
        commit
        for commit in private_commits(controls)
        if commit.message.startswith("Stage uk-run-1")
    ]
    assert len(ours) == 1


def test_local_bytes_changed_after_the_hand_over_are_never_uploaded(
    hugging_face_module,
):
    controls = hugging_face_module
    request, _, _ = stage_bundle(controls)
    dataset = next(item for item in request.files if item.role == "dataset")
    dataset.local_path.write_bytes(b"X" * dataset.bytes)
    cycle(controls)
    assert private_commits(controls) == []
    receipt = receipt_of(controls, request.handover_id())
    assert (receipt["status"], receipt["error_code"]) == (
        "failed",
        "PRESERVED_BYTES_CHANGED",
    )


def test_missing_local_files_stay_queued_until_restored(hugging_face_module):
    controls = hugging_face_module
    request, _, contents = stage_bundle(controls)
    dataset = next(item for item in request.files if item.role == "dataset")
    dataset.local_path.unlink()
    cycle(controls)
    receipt = receipt_of(controls, request.handover_id())
    assert (receipt["status"], receipt["error_code"]) == (
        "pending",
        "PRESERVED_FILES_MISSING",
    )
    dataset.local_path.write_bytes(contents[dataset.name])
    controls.clock.advance(2)
    cycle(controls)
    assert receipt_of(controls, request.handover_id())["status"] == "uploaded"


def test_repeating_a_hand_over_queues_it_once(hugging_face_module):
    controls = hugging_face_module
    request, first, _ = stage_bundle(controls)
    second = controls.module.query(request.message())["receipt"]
    assert first == second
    cycle(controls)
    controls.clock.advance(60)
    cycle(controls)
    assert len(private_commits(controls)) == 1


def test_bundles_and_run_files_for_one_repository_take_separate_cycles(
    hugging_face_module,
):
    from microcosm_emitter_interface.staging import RunFile, StageRunFiles

    controls = hugging_face_module
    request, _, _ = stage_bundle(controls)
    progress = controls.tmp_path / "progress.json"
    progress.write_bytes(b"{}")
    controls.module.query(
        StageRunFiles(
            repository=PRIVATE_REPOSITORY,
            run_id="uk-run-1",
            files=(RunFile("progress.json", progress),),
            flush=True,
        ).message()
    )
    cycle(controls)
    assert len(private_commits(controls)) == 1
    assert receipt_of(controls, request.handover_id())["status"] == "uploaded"
    cycle(controls)
    assert len(private_commits(controls)) == 1
    controls.clock.advance(10)
    cycle(controls)
    assert [list(commit.added) for commit in private_commits(controls)][-1] == [
        "runs/uk-run-1/progress.json"
    ]


def test_without_a_credential_bundles_stay_queued(hugging_face_module):
    controls = hugging_face_module
    controls.remove_credential()
    request, _, _ = stage_bundle(controls)
    cycle(controls)
    receipt = receipt_of(controls, request.handover_id())
    assert (receipt["status"], receipt["error_code"]) == (
        "pending",
        "CREDENTIAL_MISSING",
    )
    controls.restore_credential()
    controls.clock.advance(2)
    cycle(controls)
    assert receipt_of(controls, request.handover_id())["status"] == "uploaded"


@pytest.mark.parametrize(
    ("edit", "refusal_code"),
    [
        (lambda message: message["files"].pop(3), "INVALID_REQUEST"),
        (
            lambda message: message["files"][0].update(role="Not A Role"),
            "INVALID_REQUEST",
        ),
        (
            lambda message: message["files"][1].update(
                name=message["files"][0]["name"]
            ),
            "INVALID_REQUEST",
        ),
        (lambda message: message["files"][0].update(sha256="abc"), "INVALID_REQUEST"),
        (lambda message: message["files"][0].update(bytes=1), "FILE_SIZE_DIFFERS"),
        (
            lambda message: message["files"][0].update(
                local_path="/nonexistent/fixture.h5"
            ),
            "LOCAL_FILE_MISSING",
        ),
        (lambda message: message.update(prefix="releases"), "INVALID_REQUEST"),
        (
            lambda message: message.update(protocol_version=99),
            "UNSUPPORTED_PROTOCOL_VERSION",
        ),
    ],
    ids=[
        "no-inventory",
        "role",
        "duplicate-name",
        "digest",
        "size",
        "missing-file",
        "release-records",
        "protocol-version",
    ],
)
def test_malformed_bundle_hand_overs_are_refused_and_not_queued(
    hugging_face_module, edit, refusal_code
):
    controls = hugging_face_module
    files, _ = bundle_files(controls.tmp_path / "bundle")
    message = StageDatasetBundle(
        repository=PRIVATE_REPOSITORY, run_id="uk-run-1", files=files
    ).message()
    edit(message)
    receipt = controls.module.query(message)["receipt"]
    assert (receipt["status"], receipt["error_code"]) == ("refused", refusal_code)
    cycle(controls)
    assert private_commits(controls) == []


@pytest.mark.parametrize(
    ("whoami", "fail_head", "expected"),
    [
        (
            {"auth": {"accessToken": {"role": "write"}}},
            False,
            {"credential": "present", "reachable": True, "can_write": True},
        ),
        (
            {"auth": {"accessToken": {"role": "read"}}},
            False,
            {"credential": "present", "reachable": True, "can_write": False},
        ),
        (
            {"name": "fixture"},
            False,
            {"credential": "present", "reachable": True, "can_write": None},
        ),
        (
            {"auth": {"accessToken": {"role": "write"}}},
            True,
            {"credential": "present", "reachable": False, "can_write": None},
        ),
    ],
    ids=["write", "read", "unknown-scope", "unreachable"],
)
def test_write_access_check_reports_reach_and_scope(
    tmp_path, whoami, fail_head, expected
):
    import os

    hub = FakeHuggingFaceApi(whoami=whoami)
    hub.create_repository(PRIVATE_REPOSITORY)
    if fail_head:
        hub.fail_next("repo_info", FakeHubHTTPError(503))
    module = HuggingFaceModule(
        {"spool_path": str(tmp_path / "events.sqlite3")},
        ModuleContext(os.getpid()),
        api_factory=lambda: hub,
    )
    module.initialize()
    try:
        access = module.query(
            {
                "protocol_version": 1,
                "command": "check_write_access",
                "repository": PRIVATE_REPOSITORY,
            }
        )["access"]
    finally:
        module.close(0)
    assert {key: access[key] for key in expected} == expected
    assert access["status"] == "checked"
    assert access["error_type"] == ("FakeHubHTTPError" if fail_head else None)


def test_write_access_check_without_a_credential(hugging_face_module):
    controls = hugging_face_module
    controls.remove_credential()
    access = controls.module.query(
        {
            "protocol_version": 1,
            "command": "check_write_access",
            "repository": PRIVATE_REPOSITORY,
        }
    )["access"]
    assert access == {
        "status": "checked",
        "credential": "missing",
        "reachable": None,
        "can_write": None,
        "error_type": None,
    }
