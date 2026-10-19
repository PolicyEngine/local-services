"""The release writer: build folders side by side and one releases.json."""

import hashlib
import json
import os

import pytest
from fake_hugging_face import PRIVATE_REPOSITORY, FakeHubHTTPError

from microcosm_emitter.host.contracts import ModuleContext
from microcosm_emitter.huggingface.service.hub import DatasetRepository
from microcosm_emitter.huggingface.service.module import HuggingFaceModule
from microcosm_emitter.huggingface.service.releases import (
    allow_every_change,
    apply_release_change,
)
from microcosm_emitter_interface.publication import (
    PromoteBuild,
    PublishRelease,
    ReleaseFile,
    WithdrawBuild,
)
from microcosm_emitter_interface.releases import ReleasesRecord

NATIONAL = "microcosm-uk-2024-25-national"
FIRST_BUILD = "uk-frs-calibration-attempt-20261014T214443Z-d12ff9b3"
SECOND_BUILD = "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71"


def release_files(directory, release, build, *, manifest_build=None, dataset=b"rows"):
    directory.mkdir(parents=True, exist_ok=True)
    contents = {
        "release_manifest.json": (
            "release_manifest",
            json.dumps(
                {"release": release, "build": manifest_build or build}, indent=2
            ).encode(),
        ),
        "microcosm_uk_2024_25.h5": ("dataset", dataset),
        "calibration_diagnostics.json": ("calibration_diagnostics", b"{}"),
    }
    files = []
    for name, (role, content) in contents.items():
        (directory / name).write_bytes(content)
        files.append(
            ReleaseFile(
                name=name,
                role=role,
                local_path=directory / name,
                sha256=hashlib.sha256(content).hexdigest(),
                bytes=len(content),
            )
        )
    return tuple(files), {name: content for name, (_, content) in contents.items()}


def publish_request(tmp_path, build, *, make_current=False, **options):
    files, contents = release_files(tmp_path / build, NATIONAL, build, **options)
    return (
        PublishRelease(
            repository=PRIVATE_REPOSITORY,
            release=NATIONAL,
            build=build,
            release_role="national_default",
            make_current=make_current,
            files=files,
        ),
        contents,
    )


def apply(hub, request, *, published_at="2026-10-20T00:00:00+00:00", policy=None):
    return apply_release_change(
        DatasetRepository(PRIVATE_REPOSITORY, hub),
        request.message(),
        published_at=published_at,
        policy=policy or allow_every_change,
    )


def record_of(hub) -> ReleasesRecord:
    return ReleasesRecord.parse(hub.files(PRIVATE_REPOSITORY)["releases.json"])


def no_tags_or_branches(hub) -> bool:
    return (
        not any(method in {"create_tag", "create_branch"} for method, _ in hub.calls)
        and not hub.repositories[PRIVATE_REPOSITORY].tags
    )


def test_publication_writes_the_build_folder_and_releases_json_in_one_commit(
    hub, tmp_path
):
    head_before = hub.repositories[PRIVATE_REPOSITORY].head.oid
    request, contents = publish_request(tmp_path, FIRST_BUILD)
    outcome = apply(hub, request)
    assert outcome.status == "published"
    (commit,) = hub.commits(PRIVATE_REPOSITORY)
    assert outcome.commit == commit.oid and commit.parent == head_before
    folder = f"releases/{NATIONAL}/{FIRST_BUILD}"
    assert commit.added == {
        **{f"{folder}/{name}": content for name, content in contents.items()},
        "releases.json": commit.added["releases.json"],
    }
    assert json.loads(commit.added["releases.json"]) == {
        "schema_version": 1,
        "default": None,
        "releases": {
            NATIONAL: {
                "role": "national_default",
                "current": None,
                "builds": {
                    FIRST_BUILD: {
                        "published_at": "2026-10-20T00:00:00+00:00",
                        "withdrawn": False,
                    }
                },
            }
        },
    }
    assert commit.message == f"Publish build {FIRST_BUILD} of {NATIONAL}"
    (create_commit,) = [
        arguments for method, arguments in hub.calls if method == "create_commit"
    ]
    assert create_commit["parent_commit"] == head_before
    assert no_tags_or_branches(hub)


def test_a_published_build_can_become_current_in_the_same_commit(hub, tmp_path):
    request, _ = publish_request(tmp_path, FIRST_BUILD, make_current=True)
    assert apply(hub, request).status == "published"
    assert record_of(hub).releases[NATIONAL].current == FIRST_BUILD
    (commit,) = hub.commits(PRIVATE_REPOSITORY)
    assert commit.message.endswith("and make it current")


def test_publication_refuses_a_build_whose_folder_exists(hub, tmp_path):
    hub.write_directly(
        PRIVATE_REPOSITORY, {f"releases/{NATIONAL}/{FIRST_BUILD}/old.json": b"{}"}
    )
    request, _ = publish_request(tmp_path, FIRST_BUILD)
    outcome = apply(hub, request)
    assert (outcome.status, outcome.error_code) == ("refused", "BUILD_FOLDER_EXISTS")
    assert [commit.message for commit in hub.commits(PRIVATE_REPOSITORY)] == ["Direct"]


def test_repeating_a_publication_that_already_landed_reports_it(hub, tmp_path):
    request, _ = publish_request(tmp_path, FIRST_BUILD)
    first = apply(hub, request)
    again = apply(hub, request, published_at="2026-10-21T00:00:00+00:00")
    assert (again.status, again.commit) == ("published", first.commit)
    assert len(hub.commits(PRIVATE_REPOSITORY)) == 1


def test_concurrent_publications_of_one_release_both_land(hub, tmp_path):
    other, _ = publish_request(tmp_path, FIRST_BUILD)
    ours, _ = publish_request(tmp_path, SECOND_BUILD, make_current=True)
    hub.run_before_next_commit(lambda: apply(hub, other))
    outcome = apply(hub, ours)
    assert outcome.status == "published"
    record = record_of(hub)
    assert set(record.releases[NATIONAL].builds) == {FIRST_BUILD, SECOND_BUILD}
    assert record.releases[NATIONAL].current == SECOND_BUILD
    our_attempts = [
        arguments
        for method, arguments in hub.calls
        if method == "create_commit"
        and arguments["commit_message"].startswith(f"Publish build {SECOND_BUILD}")
    ]
    assert len(our_attempts) == 2  # one refused on its stale parent, one landed
    assert len(hub.commits(PRIVATE_REPOSITORY)) == 2


def test_current_cannot_change_while_pointer_files_exist(hub, tmp_path):
    hub.write_directly(PRIVATE_REPOSITORY, {"latest-national.json": b"{}"})
    promoting, _ = publish_request(tmp_path, FIRST_BUILD, make_current=True)
    refused = apply(hub, promoting)
    assert (refused.status, refused.error_code) == ("refused", "POINTER_FILES_PRESENT")
    listing, _ = publish_request(tmp_path / "again", FIRST_BUILD)
    assert apply(hub, listing).status == "published"
    promotion = apply(
        hub,
        PromoteBuild(
            repository=PRIVATE_REPOSITORY, release=NATIONAL, build=FIRST_BUILD
        ),
    )
    assert (promotion.status, promotion.error_code) == (
        "refused",
        "POINTER_FILES_PRESENT",
    )
    assert hub.files(PRIVATE_REPOSITORY)["latest-national.json"] == b"{}"


def test_promotion_and_withdrawal_change_only_releases_json(hub, tmp_path):
    for build in (FIRST_BUILD, SECOND_BUILD):
        request, _ = publish_request(tmp_path, build, make_current=True)
        assert apply(hub, request).status == "published"
    promoted = apply(
        hub,
        PromoteBuild(
            repository=PRIVATE_REPOSITORY, release=NATIONAL, build=FIRST_BUILD
        ),
    )
    assert promoted.status == "published"
    assert list(hub.commits(PRIVATE_REPOSITORY)[-1].added) == ["releases.json"]
    withdrawn = apply(
        hub,
        WithdrawBuild(
            repository=PRIVATE_REPOSITORY, release=NATIONAL, build=SECOND_BUILD
        ),
    )
    assert withdrawn.status == "published"
    assert list(hub.commits(PRIVATE_REPOSITORY)[-1].added) == ["releases.json"]
    record = record_of(hub)
    assert record.releases[NATIONAL].current == FIRST_BUILD
    assert record.releases[NATIONAL].builds[SECOND_BUILD].withdrawn is True
    assert f"releases/{NATIONAL}/{SECOND_BUILD}/release_manifest.json" in hub.files(
        PRIVATE_REPOSITORY
    )
    current = apply(
        hub,
        WithdrawBuild(
            repository=PRIVATE_REPOSITORY, release=NATIONAL, build=FIRST_BUILD
        ),
    )
    assert (current.status, current.error_code) == (
        "refused",
        "WITHDRAWING_CURRENT_BUILD",
    )
    assert no_tags_or_branches(hub)


def test_a_manifest_naming_another_build_is_refused(hub, tmp_path):
    request, _ = publish_request(tmp_path, FIRST_BUILD, manifest_build=SECOND_BUILD)
    outcome = apply(hub, request)
    assert (outcome.status, outcome.error_code) == ("refused", "MANIFEST_MISMATCH")
    assert hub.commits(PRIVATE_REPOSITORY) == []


def test_an_invalid_record_in_the_repository_is_refused_until_repaired(hub, tmp_path):
    hub.write_directly(PRIVATE_REPOSITORY, {"releases.json": b'{"schema_version": 2}'})
    request, _ = publish_request(tmp_path, FIRST_BUILD)
    outcome = apply(hub, request)
    assert (outcome.status, outcome.error_code) == ("refused", "INVALID_RECORD")


def test_a_listed_build_without_its_folder_blocks_every_write(hub, tmp_path):
    listed_only = {
        "schema_version": 1,
        "default": None,
        "releases": {
            NATIONAL: {
                "role": "national_default",
                "current": None,
                "builds": {
                    "lost-build": {"published_at": "2026-10-01", "withdrawn": False}
                },
            }
        },
    }
    hub.write_directly(
        PRIVATE_REPOSITORY, {"releases.json": json.dumps(listed_only).encode()}
    )
    request, _ = publish_request(tmp_path, FIRST_BUILD)
    outcome = apply(hub, request)
    assert (outcome.status, outcome.error_code) == ("refused", "BUILD_FOLDER_MISSING")


@pytest.mark.parametrize(
    ("method", "error_code"),
    [("repo_info", "REPOSITORY_UNAVAILABLE"), ("create_commit", "UPLOAD_FAILED")],
)
def test_hub_failures_leave_the_change_pending_for_a_retry(
    hub, tmp_path, method, error_code
):
    hub.fail_next(method, FakeHubHTTPError(503))
    request, _ = publish_request(tmp_path, FIRST_BUILD)
    outcome = apply(hub, request)
    assert (outcome.status, outcome.error_code, outcome.retry) == (
        "pending",
        error_code,
        True,
    )
    assert apply(hub, request).status == "published"


def test_local_bytes_changed_after_the_hand_over_are_refused(hub, tmp_path):
    request, _ = publish_request(tmp_path, FIRST_BUILD)
    dataset = next(item for item in request.files if item.role == "dataset")
    dataset.local_path.write_bytes(b"X" * dataset.bytes)
    outcome = apply(hub, request)
    assert (outcome.status, outcome.error_code) == (
        "refused",
        "PRESERVED_BYTES_CHANGED",
    )
    assert hub.commits(PRIVATE_REPOSITORY) == []


def test_a_change_policy_can_refuse_a_promotion(hub, tmp_path):
    request, _ = publish_request(tmp_path, FIRST_BUILD)
    assert apply(hub, request).status == "published"

    def only_listing(change):
        return None if change["command"] == "publish_release" else "not permitted"

    outcome = apply(
        hub,
        PromoteBuild(
            repository=PRIVATE_REPOSITORY, release=NATIONAL, build=FIRST_BUILD
        ),
        policy=only_listing,
    )
    assert (outcome.status, outcome.error_code) == ("refused", "NOT_AUTHORIZED")


def test_existing_tags_old_files_and_staged_bundles_are_untouched(hub, tmp_path):
    old_files = {
        f"releases/{NATIONAL}/release_manifest.json": b'{"old": true}',
        "staged/uk-run/staged_manifest.json": b"{}",
        "latest.json": b"{}",
    }
    tagged = hub.write_directly(PRIVATE_REPOSITORY, old_files)
    hub.repositories[PRIVATE_REPOSITORY].tags[
        "microcosm-uk-2024-25-national-20261014T214443Z-d12ff9b3"
    ] = tagged
    request, _ = publish_request(tmp_path, FIRST_BUILD)
    assert apply(hub, request).status == "published"
    files = hub.files(PRIVATE_REPOSITORY)
    for path, content in old_files.items():
        assert files[path] == content
    assert hub.repositories[PRIVATE_REPOSITORY].tags == {
        "microcosm-uk-2024-25-national-20261014T214443Z-d12ff9b3": tagged
    }
    assert not any(
        path.rsplit("/", 1)[-1].startswith("latest")
        for path in hub.commits(PRIVATE_REPOSITORY)[-1].added
    )


def test_module_queues_a_release_change_and_reports_its_receipt(
    hugging_face_module,
):
    controls = hugging_face_module
    request, _ = publish_request(controls.tmp_path, FIRST_BUILD, make_current=True)
    receipt = controls.module.query(request.message())["receipt"]
    assert (receipt["status"], receipt["handover_id"]) == (
        "pending",
        request.handover_id(),
    )
    controls.module.delivery.run_cycle()
    receipt = controls.module.query(
        {
            "protocol_version": 1,
            "command": "handover_receipt",
            "handover_id": request.handover_id(),
        }
    )["receipt"]
    assert receipt["status"] == "published"
    assert receipt["commit"] == controls.hub.commits(PRIVATE_REPOSITORY)[-1].oid
    assert (receipt["release"], receipt["build"]) == (NATIONAL, FIRST_BUILD)
    record = ReleasesRecord.parse(
        controls.hub.files(PRIVATE_REPOSITORY)["releases.json"]
    )
    assert record.releases[NATIONAL].current == FIRST_BUILD


def test_module_refuses_a_malformed_release_change_without_queueing_it(
    hugging_face_module,
):
    controls = hugging_face_module
    request, _ = publish_request(controls.tmp_path, FIRST_BUILD)
    message = request.message()
    message["files"] = [
        entry for entry in message["files"] if entry["role"] != "release_manifest"
    ]
    receipt = controls.module.query(message)["receipt"]
    assert (receipt["status"], receipt["error_code"]) == ("refused", "INVALID_REQUEST")
    controls.module.delivery.run_cycle()
    assert controls.hub.commits(PRIVATE_REPOSITORY) == []


def test_the_module_writer_uses_the_injected_policy(tmp_path, hub):
    module = HuggingFaceModule(
        {"handover_database_path": str(tmp_path / "events.handovers.sqlite3")},
        ModuleContext(os.getpid()),
        api_factory=lambda: hub,
        release_change_policy=lambda change: "refused by policy",
        background_workers=False,
    )
    module.initialize()
    try:
        request, _ = publish_request(tmp_path, FIRST_BUILD)
        module.query(request.message())
        module.delivery.run_cycle()
        receipt = module.query(
            {
                "protocol_version": 1,
                "command": "handover_receipt",
                "handover_id": request.handover_id(),
            }
        )["receipt"]
    finally:
        module.close(0)
    assert (receipt["status"], receipt["error_code"]) == ("refused", "NOT_AUTHORIZED")
