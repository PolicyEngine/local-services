"""The release writer: build folders side by side and one releases.json."""

import hashlib
import json
import os

import pytest
from fake_hugging_face import PRIVATE_REPOSITORY, FakeHubHTTPError

from microcosm_emitter.host.contracts import ModuleContext
from microcosm_emitter.huggingface.service.hub import DatasetRepository
from microcosm_emitter.huggingface.service.module import HuggingFaceModule
from microcosm_emitter.huggingface.service.releases import apply_release_change
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


def apply(hub, request, *, published_at="2026-10-20T00:00:00+00:00"):
    return apply_release_change(
        DatasetRepository(PRIVATE_REPOSITORY, hub), request, published_at=published_at
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


def test_a_change_policy_can_refuse_a_promotion(tmp_path, hub):
    def only_listing(change, caller):
        return None if isinstance(change, PublishRelease) else "not permitted"

    module = HuggingFaceModule(
        {"handover_database_path": str(tmp_path / "events.handovers.sqlite3")},
        ModuleContext(os.getpid()),
        api_factory=lambda: hub,
        release_change_policy=only_listing,
        background_workers=False,
    )
    module.initialize()
    try:
        request, _ = publish_request(tmp_path, FIRST_BUILD)
        assert module.query(request.message())["receipt"]["status"] == "pending"
        promotion_receipt = module.query(
            PromoteBuild(
                repository=PRIVATE_REPOSITORY, release=NATIONAL, build=FIRST_BUILD
            ).message()
        )["receipt"]
    finally:
        module.close(0)
    assert (promotion_receipt["status"], promotion_receipt["error_code"]) == (
        "refused",
        "NOT_AUTHORIZED",
    )


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


def test_a_change_the_policy_refuses_is_never_written(tmp_path, hub):
    module = HuggingFaceModule(
        {"handover_database_path": str(tmp_path / "events.handovers.sqlite3")},
        ModuleContext(os.getpid()),
        api_factory=lambda: hub,
        release_change_policy=lambda change, caller: "refused by policy",
        background_workers=False,
    )
    module.initialize()
    try:
        request, _ = publish_request(tmp_path, FIRST_BUILD)
        receipt = module.query(request.message())["receipt"]
        module.delivery.run_cycle()
    finally:
        module.close(0)
    assert (receipt["status"], receipt["error_code"]) == ("refused", "NOT_AUTHORIZED")
    assert hub.commits(PRIVATE_REPOSITORY) == []


def queue_change(controls, request):
    receipt = controls.module.query(request.message())["receipt"]
    controls.module.delivery.run_cycle()
    controls.clock.advance(1)
    return controls.module.query(
        {
            "protocol_version": 1,
            "command": "handover_receipt",
            "handover_id": receipt["handover_id"],
        }
    )["receipt"]


def current_of(controls):
    return ReleasesRecord.parse(
        controls.hub.files(PRIVATE_REPOSITORY)["releases.json"]
    ).releases[NATIONAL]


def promotion(build, **options):
    return PromoteBuild(
        repository=PRIVATE_REPOSITORY, release=NATIONAL, build=build, **options
    )


def withdrawal(build, **options):
    return WithdrawBuild(
        repository=PRIVATE_REPOSITORY, release=NATIONAL, build=build, **options
    )


def publish_both(controls):
    for build in (FIRST_BUILD, SECOND_BUILD):
        request, _ = publish_request(
            controls.tmp_path, build, make_current=build == FIRST_BUILD
        )
        assert queue_change(controls, request)["status"] == "published"


def test_promoting_back_to_an_earlier_build_applies(hugging_face_module):
    controls = hugging_face_module
    publish_both(controls)
    # B, then A, then B again: the second promotion of B is a new request.
    for build in (SECOND_BUILD, FIRST_BUILD, SECOND_BUILD):
        assert queue_change(controls, promotion(build))["status"] == "published"
        assert current_of(controls).current == build


def test_a_withdrawal_retried_after_another_promotion_applies(hugging_face_module):
    controls = hugging_face_module
    publish_both(controls)
    refused = queue_change(controls, withdrawal(FIRST_BUILD))
    assert (refused["status"], refused["error_code"]) == (
        "refused",
        "WITHDRAWING_CURRENT_BUILD",
    )
    assert queue_change(controls, promotion(SECOND_BUILD))["status"] == "published"
    assert queue_change(controls, withdrawal(FIRST_BUILD))["status"] == "published"
    assert current_of(controls).builds[FIRST_BUILD].withdrawn is True


def test_a_refused_change_applies_when_requested_again_after_its_cause_is_gone(
    hugging_face_module,
):
    controls = hugging_face_module
    publish_both(controls)
    controls.hub.write_directly(PRIVATE_REPOSITORY, {"latest-national.json": b"{}"})
    refused = queue_change(controls, promotion(SECOND_BUILD))
    assert refused["error_code"] == "POINTER_FILES_PRESENT"
    controls.hub.delete_directly(PRIVATE_REPOSITORY, ["latest-national.json"])
    assert queue_change(controls, promotion(SECOND_BUILD))["status"] == "published"
    assert current_of(controls).current == SECOND_BUILD


def test_resending_the_same_request_is_a_retry_and_is_applied_once(
    hugging_face_module,
):
    controls = hugging_face_module
    publish_both(controls)
    request = promotion(SECOND_BUILD)
    first = queue_change(controls, request)
    commits = len(controls.hub.commits(PRIVATE_REPOSITORY))
    again = queue_change(controls, request)
    assert again == first
    assert len(controls.hub.commits(PRIVATE_REPOSITORY)) == commits


def test_a_release_change_without_a_request_id_is_refused(hugging_face_module):
    controls = hugging_face_module
    message = promotion(FIRST_BUILD).message()
    del message["request_id"]
    receipt = controls.module.query(message)["receipt"]
    assert (receipt["status"], receipt["error_code"]) == ("refused", "INVALID_REQUEST")


def test_a_landed_publication_retried_after_another_promotion_is_published(
    hub, tmp_path
):
    """The commit landed but raised; another build was promoted meanwhile."""
    request, _ = publish_request(tmp_path, FIRST_BUILD, make_current=True)
    hub.fail_after_next_commit(FakeHubHTTPError(503))
    first = apply(hub, request)
    assert (first.status, first.error_code) == ("pending", "UPLOAD_FAILED")
    landed = hub.commits(PRIVATE_REPOSITORY)[-1]
    other, _ = publish_request(tmp_path, SECOND_BUILD, make_current=True)
    assert apply(hub, other).status == "published"
    retried = apply(hub, request, published_at="2026-10-21T00:00:00+00:00")
    assert (retried.status, retried.commit) == ("published", landed.oid)
    assert record_of(hub).releases[NATIONAL].current == SECOND_BUILD
    assert len(hub.commits(PRIVATE_REPOSITORY)) == 2


def test_republishing_a_published_build_does_not_promote_it(hub, tmp_path):
    """Promoting is ``promote_build``'s job; publication reports what landed."""
    listed, _ = publish_request(tmp_path, FIRST_BUILD)
    assert apply(hub, listed).status == "published"
    again, _ = publish_request(tmp_path, FIRST_BUILD, make_current=True)
    outcome = apply(hub, again)
    assert outcome.status == "published"
    assert record_of(hub).releases[NATIONAL].current is None
    assert len(hub.commits(PRIVATE_REPOSITORY)) == 1


DEEPLY_NESTED = b"[" * 100_000 + b"]" * 100_000


def test_a_deeply_nested_record_in_the_repository_is_refused_not_retried(hub, tmp_path):
    hub.write_directly(PRIVATE_REPOSITORY, {"releases.json": DEEPLY_NESTED})
    request, _ = publish_request(tmp_path, FIRST_BUILD)
    outcome = apply(hub, request)
    assert (outcome.status, outcome.error_code, outcome.retry) == (
        "refused",
        "INVALID_RECORD",
        False,
    )


def test_a_deeply_nested_local_manifest_is_refused_not_retried(hub, tmp_path):
    request, _ = publish_request(tmp_path, FIRST_BUILD)
    manifest = next(item for item in request.files if item.role == "release_manifest")
    manifest.local_path.write_bytes(DEEPLY_NESTED)
    nested = PublishRelease(
        repository=request.repository,
        release=request.release,
        build=request.build,
        release_role=request.release_role,
        make_current=False,
        files=tuple(
            ReleaseFile(
                name=item.name,
                role=item.role,
                local_path=item.local_path,
                sha256=hashlib.sha256(DEEPLY_NESTED).hexdigest(),
                bytes=len(DEEPLY_NESTED),
            )
            if item is manifest
            else item
            for item in request.files
        ),
    )
    outcome = apply(hub, nested)
    assert (outcome.status, outcome.error_code, outcome.retry) == (
        "refused",
        "MANIFEST_MISMATCH",
        False,
    )
    assert hub.commits(PRIVATE_REPOSITORY) == []


def hand_over_private_run_file(controls):
    from microcosm_emitter_interface.staging import RunFile, StageRunFiles

    local = controls.tmp_path / "run-files" / "progress.json"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"{}")
    controls.module.query(
        StageRunFiles(
            repository=PRIVATE_REPOSITORY,
            run_id="uk-run-1",
            files=(RunFile("progress.json", local),),
            flush=True,
        ).message()
    )


def bundle_request(controls):
    from microcosm_emitter_interface.bundles import BundleFile, StageDatasetBundle

    directory = controls.tmp_path / "bundle"
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
    return StageDatasetBundle(
        repository=PRIVATE_REPOSITORY, run_id="uk-run-1", files=tuple(files)
    )


def receipt_status(controls, handover_id):
    return controls.module.query(
        {
            "protocol_version": 1,
            "command": "handover_receipt",
            "handover_id": handover_id,
        }
    )["receipt"]["status"]


def test_release_changes_skip_the_commit_spacing_but_bundles_keep_it(
    hugging_face_module,
):
    from microcosm_emitter.huggingface.service.queue import (
        MIN_COMMIT_SPACING_SECONDS,
    )

    controls = hugging_face_module
    hand_over_private_run_file(controls)
    controls.module.delivery.run_cycle()
    assert len(controls.hub.commits(PRIVATE_REPOSITORY)) == 1
    bundle = bundle_request(controls)
    controls.module.query(bundle.message())
    controls.clock.advance(1)
    controls.module.delivery.run_cycle()
    # Within the spacing, nothing but a release change may commit.
    assert len(controls.hub.commits(PRIVATE_REPOSITORY)) == 1
    assert receipt_status(controls, bundle.handover_id()) == "pending"
    publication, _ = publish_request(controls.tmp_path, FIRST_BUILD)
    controls.module.query(publication.message())
    controls.module.delivery.run_cycle()
    assert receipt_status(controls, publication.handover_id()) == "published"
    assert receipt_status(controls, bundle.handover_id()) == "pending"
    controls.clock.advance(MIN_COMMIT_SPACING_SECONDS - 1)
    controls.module.delivery.run_cycle()
    assert receipt_status(controls, bundle.handover_id()) == "pending"
    controls.clock.advance(1)
    controls.module.delivery.run_cycle()
    assert receipt_status(controls, bundle.handover_id()) == "uploaded"


def test_release_outcome_statuses_are_literal():
    import typing

    from microcosm_emitter.huggingface.service.releases import ReleaseOutcome

    status = typing.get_type_hints(ReleaseOutcome)["status"]
    assert set(typing.get_args(status.__value__)) == {"pending", "published", "refused"}


def test_the_queued_request_is_the_validated_typed_request(hugging_face_module):
    controls = hugging_face_module
    request = promotion(FIRST_BUILD)
    message = {**request.message(), "unvalidated": "extra"}
    receipt = controls.module.query(message)["receipt"]
    assert receipt["handover_id"] == request.handover_id()
    job = controls.module.queue.claim_job(
        PRIVATE_REPOSITORY, now=controls.clock(), lease_seconds=1
    )
    assert job.request == request.message()


def test_the_change_policy_receives_the_typed_change_and_its_caller(tmp_path, hub):
    import getpass

    seen = []

    def record_and_refuse(change, caller):
        seen.append((change, caller))
        return "refused by policy"

    module = HuggingFaceModule(
        {"handover_database_path": str(tmp_path / "events.handovers.sqlite3")},
        ModuleContext(os.getpid()),
        api_factory=lambda: hub,
        release_change_policy=record_and_refuse,
        background_workers=False,
    )
    module.initialize()
    try:
        request = promotion(FIRST_BUILD)
        receipt = module.query(request.message())["receipt"]
        assert (receipt["status"], receipt["error_code"], receipt["message"]) == (
            "refused",
            "NOT_AUTHORIZED",
            "refused by policy",
        )
        # Refused at hand-over: nothing is queued.
        assert module.queue.job_receipt(request.handover_id()) is None
    finally:
        module.close(0)
    ((change, caller),) = seen
    assert change == request
    assert (caller.parent_pid, caller.user) == (os.getpid(), getpass.getuser())


def test_the_default_policy_allows_every_change(hugging_face_module):
    from microcosm_emitter.huggingface.service.releases import (
        ReleaseCaller,
        allow_every_change,
    )

    caller = ReleaseCaller(user="fixture-user", parent_pid=1)
    assert allow_every_change(promotion(FIRST_BUILD), caller) is None
