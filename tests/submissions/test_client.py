"""Submitting one planned submission: exchange, create, upload, complete."""

import hashlib

import pytest

from microcosm_emitter.core.auth import CollectorSession
from microcosm_emitter.submissions.client import (
    SUBMIT_SCOPE,
    CollectorSubmissionClient,
    upload_url_allowed,
)
from microcosm_emitter.submissions.planning import PlannedSubmission

DIAGNOSTICS = b'{"schema_version": 8}'
MANIFEST = b'{"build": "uk-frs-calibration-attempt-1"}'


def planned_submission(directory, *, diagnostics=DIAGNOSTICS) -> PlannedSubmission:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "calibration_diagnostics.json").write_bytes(diagnostics)
    (directory / "build_manifest.json").write_bytes(MANIFEST)
    files = [
        ("build_manifest.json", "build_manifest", MANIFEST),
        ("calibration_diagnostics.json", "calibration_diagnostics", DIAGNOSTICS),
    ]
    return PlannedSubmission(
        kind="calibration-diagnostics",
        body={
            "contract_version": 1,
            "kind": "calibration-diagnostics",
            "licensed_data": True,
            "provenance": {
                "run_id": "uk-run-1",
                "build_id": "uk-run-1",
                "country_code": "UK",
                "release": None,
                "huggingface": {
                    "repo": "policyengine/populace-uk-staging",
                    "commit": "0" * 40,
                    "folder": "staged/uk-run-1",
                },
            },
            "files": [
                {
                    "name": name,
                    "role": role,
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                for name, role, content in files
            ],
        },
        local_paths={name: str(directory / name) for name, _, _ in files},
    )


class Invalidations:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


def submission_client(collector, invalidations=None) -> CollectorSubmissionClient:
    session = CollectorSession(
        collector.origin, credential=lambda: "hf_synthetic", scope=SUBMIT_SCOPE
    )
    return CollectorSubmissionClient(
        collector.origin,
        credential=session.credential,
        invalidate_credential=invalidations or session.invalidate,
        allow_loopback_uploads=True,
    )


def test_a_submission_is_created_uploaded_and_completed(collector, tmp_path) -> None:
    planned = planned_submission(tmp_path)

    outcome = submission_client(collector).submit(planned)

    assert outcome.status == "accepted"
    assert outcome.collector_status == "queued"
    assert outcome.submission_id in collector.submissions
    assert collector.exchange_requests == [{"scope": "artifacts:submit"}]
    assert sorted(collector.objects.values()) == sorted([DIAGNOSTICS, MANIFEST])
    assert collector.completed == [outcome.submission_id]


def test_files_already_stored_are_not_uploaded_again(collector, tmp_path) -> None:
    planned = planned_submission(tmp_path)
    client = submission_client(collector)
    client.submit(planned)
    uploads_before = list(collector.uploads)

    outcome = client.submit(planned)

    assert outcome.status == "accepted"
    assert collector.uploads == uploads_before


def test_an_upload_refused_as_already_stored_counts_as_stored(
    collector, tmp_path
) -> None:
    planned = planned_submission(tmp_path)
    client = submission_client(collector)
    collector.complete_statuses = [503]
    client.submit(planned)
    collector.sign_stored_files = True

    outcome = client.submit(planned)

    assert outcome.status == "accepted"


def test_intake_switched_off_leaves_the_submission_pending(collector, tmp_path) -> None:
    collector.exchange_status = 404

    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    assert outcome.status == "pending"
    assert outcome.error_code == "COLLECTOR_UNAVAILABLE"
    assert collector.created == []


def test_a_rejected_credential_fails_the_submission(collector, tmp_path) -> None:
    collector.exchange_status = 403

    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    assert outcome.status == "failed"
    assert outcome.error_code == "CREDENTIAL_REJECTED"


@pytest.mark.parametrize("status", [403, 413, 422])
def test_a_submission_the_collector_refuses_fails(collector, tmp_path, status) -> None:
    collector.create_statuses = [status]

    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    assert outcome.status == "failed"
    assert outcome.error_code == f"COLLECTOR_REFUSED_{status}"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_collector_outage_leaves_the_submission_pending(
    collector, tmp_path, status
) -> None:
    collector.create_statuses = [status]

    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    assert outcome.status == "pending"
    assert outcome.error_code == "COLLECTOR_UNAVAILABLE"


def test_an_expired_session_is_exchanged_again(collector, tmp_path) -> None:
    collector.create_statuses = [401]
    invalidations = Invalidations()

    outcome = submission_client(collector, invalidations).submit(
        planned_submission(tmp_path)
    )

    assert outcome.status == "pending"
    assert invalidations.count == 1


def test_files_missing_at_completion_are_retried(collector, tmp_path) -> None:
    collector.complete_statuses = [409]

    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    assert outcome.status == "pending"
    assert outcome.error_code == "FILES_INCOMPLETE"


def test_changed_preserved_bytes_fail_before_contacting_the_collector(
    collector, tmp_path
) -> None:
    planned = planned_submission(tmp_path)
    (tmp_path / "calibration_diagnostics.json").write_bytes(b'{"schema_version": 9}')

    outcome = submission_client(collector).submit(planned)

    assert outcome.status == "failed"
    assert outcome.error_code == "PRESERVED_BYTES_CHANGED"
    assert collector.exchange_requests == []


def test_missing_preserved_files_stay_pending(collector, tmp_path) -> None:
    planned = planned_submission(tmp_path)
    (tmp_path / "build_manifest.json").unlink()

    outcome = submission_client(collector).submit(planned)

    assert outcome.status == "pending"
    assert outcome.error_code == "PRESERVED_FILES_MISSING"


def test_uploads_go_only_to_cloud_storage_or_an_allowed_loopback(
    collector, tmp_path
) -> None:
    collector.upload_origin = "http://192.0.2.10"

    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    assert outcome.status == "failed"
    assert outcome.error_code == "UPLOAD_URL_REFUSED"
    assert collector.uploads == []


@pytest.mark.parametrize(
    ("url", "allow_loopback", "allowed"),
    [
        (
            "https://storage.googleapis.com/bucket/object?X-Goog-Signature=1",
            False,
            True,
        ),
        ("http://storage.googleapis.com/bucket/object", False, False),
        ("https://storage.googleapis.com.example.net/object", False, False),
        ("https://user@storage.googleapis.com/object", False, False),
        ("http://127.0.0.1:8080/storage/object", False, False),
        ("http://127.0.0.1:8080/storage/object", True, True),
    ],
)
def test_upload_url_allowed(url: str, allow_loopback: bool, allowed: bool) -> None:
    assert upload_url_allowed(url, allow_loopback=allow_loopback) is allowed


def test_a_session_asks_for_its_scope_and_telemetry_sessions_still_send_nothing():
    exchanged: list[dict] = []

    def record_post(url, payload, token):
        exchanged.append(payload)
        return 200, {"access_token": "token", "expires_in": 900}

    CollectorSession(
        "https://collector.test",
        post=record_post,
        credential=lambda: "hf",
    ).credential()
    CollectorSession(
        "https://collector.test",
        post=record_post,
        credential=lambda: "hf",
        scope=SUBMIT_SCOPE,
    ).credential()

    assert exchanged == [{}, {"scope": "artifacts:submit"}]
