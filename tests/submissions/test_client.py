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


class ManualClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def submission_client(
    collector, invalidations=None, clock=None
) -> CollectorSubmissionClient:
    session = CollectorSession(
        collector.origin, credential=lambda: "hf_synthetic", scope=SUBMIT_SCOPE
    )
    return CollectorSubmissionClient(
        collector.origin,
        credential=session.credential,
        invalidate_credential=invalidations or session.invalidate,
        allow_loopback_uploads=True,
        clock=clock or ManualClock(),
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


@pytest.mark.parametrize("intake", ["off", "absent"])
def test_a_collector_without_intake_leaves_the_submission_pending(
    collector, tmp_path, intake
) -> None:
    """Off: the exchange answers 404 ``intake_not_enabled``. Absent: a collector
    released before intake answers 200 with a ``telemetry:write`` token."""
    collector.intake = intake

    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    assert outcome.status == "pending"
    assert outcome.error_code == "COLLECTOR_INTAKE_NOT_ENABLED"
    assert outcome.retry_after == pytest.approx(3600)
    assert collector.created == []


def test_without_intake_no_request_is_sent_until_the_hour_has_passed(
    collector, tmp_path
) -> None:
    collector.intake = "absent"
    clock = ManualClock()
    client = submission_client(collector, clock=clock)
    planned = planned_submission(tmp_path)
    client.submit(planned)
    exchanges = len(collector.exchange_requests)

    clock.now += 3599
    waiting = client.submit(planned)
    assert (waiting.status, waiting.error_code) == (
        "pending",
        "COLLECTOR_INTAKE_NOT_ENABLED",
    )
    assert waiting.retry_after == pytest.approx(1)
    assert len(collector.exchange_requests) == exchanges

    collector.intake = "on"
    clock.now += 1
    assert client.submit(planned).status == "accepted"
    assert collector.exchange_requests[-1] == {"scope": "artifacts:submit"}


def test_a_token_without_the_submit_scope_is_never_used_for_submissions():
    session = CollectorSession(
        "https://collector.test",
        post=lambda url, payload, token: (
            200,
            {
                "access_token": "telemetry",
                "expires_in": 900,
                "scope": "telemetry:write",
            },
        ),
        credential=lambda: "hf",
        scope=SUBMIT_SCOPE,
    )
    assert session.credential() == ("intake_unavailable", None)


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
        return 200, {
            "access_token": "token",
            "expires_in": 900,
            **({"scope": payload["scope"]} if "scope" in payload else {}),
        }

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


@pytest.mark.parametrize(
    "answer",
    [404, (404, "intake_not_enabled"), (404, "submission_not_found")],
    ids=["no-code", "intake-not-enabled", "submission-not-found"],
)
def test_a_404_to_create_means_intake_is_not_available(
    collector, tmp_path, answer
) -> None:
    """Only completion, status and receipt may say a submission is unknown."""
    collector.create_statuses = [answer]
    invalidations = Invalidations()

    outcome = submission_client(collector, invalidations).submit(
        planned_submission(tmp_path)
    )

    assert (outcome.status, outcome.error_code) == (
        "pending",
        "COLLECTOR_INTAKE_NOT_ENABLED",
    )
    assert outcome.retry_after == pytest.approx(3600)
    assert invalidations.count == 1


def test_a_404_to_completion_without_its_code_means_intake_is_not_available(
    collector, tmp_path
) -> None:
    collector.complete_statuses = [404]

    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    assert (outcome.status, outcome.error_code) == (
        "pending",
        "COLLECTOR_INTAKE_NOT_ENABLED",
    )


def test_an_unknown_submission_at_completion_is_created_again(
    collector, tmp_path
) -> None:
    """For example after the collector's database was reset."""
    collector.complete_statuses = [(404, "submission_not_found")]
    client = submission_client(collector)
    planned = planned_submission(tmp_path)

    first = client.submit(planned)
    second = client.submit(planned)

    assert (first.status, first.error_code) == ("pending", "SUBMISSION_NOT_FOUND")
    assert first.retry_after is None
    assert second.status == "accepted"
    assert len(collector.created) == 2


def test_a_stored_object_with_another_digest_fails_the_submission(
    collector, tmp_path
) -> None:
    """Completion answers 200 ``rejected`` with ``stored_object_mismatch``."""
    collector.corrupt_uploads = True

    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    assert (outcome.status, outcome.error_code) == (
        "failed",
        "stored_object_mismatch",
    )
    assert outcome.collector_status == "rejected"


@pytest.mark.parametrize(
    ("final_status", "code"),
    [("rejected", "content_mismatch"), ("failed", "worker_error")],
)
def test_a_final_completion_answer_that_did_not_succeed_fails(
    collector, tmp_path, final_status, code
) -> None:
    collector.complete_answers = [
        (
            200,
            {
                "contract_version": 1,
                "submission_id": "sub-chosen",
                "status": final_status,
                "rejection": {"code": code, "message": "chosen"},
            },
        )
    ]

    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    assert (outcome.status, outcome.error_code, outcome.collector_status) == (
        "failed",
        code,
        final_status,
    )


def test_a_queued_completion_is_accepted_with_its_status(collector, tmp_path) -> None:
    outcome = submission_client(collector).submit(planned_submission(tmp_path))

    assert (outcome.status, outcome.collector_status) == ("accepted", "queued")
    document = collector._status_document(outcome.submission_id)
    assert document["job"]["processing_round"] == 1
    assert len(document["files_sha256"]) == 64
