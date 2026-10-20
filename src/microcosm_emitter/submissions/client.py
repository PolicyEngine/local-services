"""Submit one planned submission to the collector's artifact intake.

The steps follow the collector's submission contract: exchange the build's
Hugging Face token for an ``artifacts:submit`` credential, create the
submission, upload each file the collector still lacks to its signed URL, and
complete it. Every step can be repeated: the collector names a submission by
its content, so a later attempt continues where an earlier one stopped.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import urlsplit

import httpx

from microcosm_emitter.core.constants import LOOPBACK_HOSTS
from microcosm_emitter.huggingface.service.hub import (
    PRESERVED_BYTES_CHANGED,
    PRESERVED_FILES_MISSING,
    UPLOAD_FAILED,
)
from microcosm_emitter.huggingface.service.queue import sha256_file
from microcosm_emitter.submissions.planning import PlannedSubmission

SUBMIT_SCOPE: Final = "artifacts:submit"
SUBMISSIONS_PATH: Final = "/v1/submissions"
#: Signed upload URLs point at Cloud Storage's XML API host.
STORAGE_HOST: Final = "storage.googleapis.com"
REQUEST_TIMEOUT_SECONDS: Final = 30.0
#: Large files take as long as the connection needs; only stalls time out.
UPLOAD_TIMEOUT: Final = httpx.Timeout(60.0, write=None)
RETRYABLE_STATUSES: Final = frozenset({408, 429})

CREDENTIAL_MISSING: Final = "CREDENTIAL_MISSING"
CREDENTIAL_REJECTED: Final = "CREDENTIAL_REJECTED"
COLLECTOR_UNAVAILABLE: Final = "COLLECTOR_UNAVAILABLE"
INVALID_RESPONSE: Final = "INVALID_RESPONSE"
UPLOAD_URL_REFUSED: Final = "UPLOAD_URL_REFUSED"
FILES_INCOMPLETE: Final = "FILES_INCOMPLETE"


@dataclass(frozen=True)
class SubmissionOutcome:
    """One attempt's result. ``pending`` is retried; the others are final."""

    status: Literal["accepted", "pending", "failed"]
    error_code: str | None = None
    submission_id: str | None = None
    collector_status: str | None = None


def upload_url_allowed(url: str, *, allow_loopback: bool) -> bool:
    """Whether a signed URL may receive a build's files."""

    parsed = urlsplit(url)
    if parsed.username or parsed.password:
        return False
    if parsed.scheme == "https" and parsed.hostname == STORAGE_HOST:
        return True
    return (
        allow_loopback
        and parsed.scheme in {"http", "https"}
        and parsed.hostname in LOOPBACK_HOSTS
    )


def _preserved_files_outcome(planned: PlannedSubmission) -> SubmissionOutcome | None:
    """Re-hash the files submitted from their paths; copied files are not read."""
    for file in planned.body["files"]:
        if file["name"] in planned.copied:
            continue
        path = Path(planned.local_paths[file["name"]])
        try:
            if not path.is_file():
                return SubmissionOutcome("pending", PRESERVED_FILES_MISSING)
            if path.stat().st_size != file["size"]:
                return SubmissionOutcome("failed", PRESERVED_BYTES_CHANGED)
            if sha256_file(path) != file["sha256"]:
                return SubmissionOutcome("failed", PRESERVED_BYTES_CHANGED)
        except OSError:
            return SubmissionOutcome("pending", PRESERVED_FILES_MISSING)
    return None


class _StepFailedError(Exception):
    def __init__(self, outcome: SubmissionOutcome) -> None:
        super().__init__(outcome.error_code)
        self.outcome = outcome


class CollectorSubmissionClient:
    """Deliver planned submissions to one collector origin."""

    def __init__(
        self,
        origin: str,
        *,
        credential: Callable[[], tuple[str, str | None]],
        invalidate_credential: Callable[[], None],
        http: httpx.Client | None = None,
        allow_loopback_uploads: bool = False,
    ) -> None:
        self.origin = origin.rstrip("/")
        self._credential = credential
        self._invalidate_credential = invalidate_credential
        self._http = http or httpx.Client(follow_redirects=False)
        self._allow_loopback_uploads = allow_loopback_uploads

    def close(self) -> None:
        self._http.close()

    def _token(self) -> str:
        result, token = self._credential()
        if result == "ok" and token:
            return token
        if result == "missing":
            raise _StepFailedError(SubmissionOutcome("pending", CREDENTIAL_MISSING))
        if result == "rejected":
            raise _StepFailedError(SubmissionOutcome("failed", CREDENTIAL_REJECTED))
        # Includes the 404 a collector answers while its intake is off.
        raise _StepFailedError(SubmissionOutcome("pending", COLLECTOR_UNAVAILABLE))

    def _settled_answer(self, response: httpx.Response) -> SubmissionOutcome:
        status = response.status_code
        if status == 401:
            self._invalidate_credential()
            return SubmissionOutcome("pending", COLLECTOR_UNAVAILABLE)
        if 400 <= status < 500 and status not in RETRYABLE_STATUSES:
            return SubmissionOutcome("failed", f"COLLECTOR_REFUSED_{status}")
        return SubmissionOutcome("pending", COLLECTOR_UNAVAILABLE)

    def _post(self, path: str, token: str, body: Any = None) -> httpx.Response:
        try:
            return self._http.post(
                self.origin + path,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            raise _StepFailedError(
                SubmissionOutcome("pending", COLLECTOR_UNAVAILABLE)
            ) from None

    def _uploads(self, response: httpx.Response, planned: PlannedSubmission) -> tuple:
        try:
            document = response.json()
        except ValueError:
            raise _StepFailedError(
                SubmissionOutcome("pending", INVALID_RESPONSE)
            ) from None
        submission_id = document.get("submission_id")
        uploads = document.get("uploads")
        if not isinstance(submission_id, str) or not isinstance(uploads, list):
            raise _StepFailedError(SubmissionOutcome("pending", INVALID_RESPONSE))
        for upload in uploads:
            if (
                not isinstance(upload, Mapping)
                or upload.get("name") not in planned.local_paths
                or upload.get("method") != "PUT"
                or not isinstance(upload.get("url"), str)
                or not isinstance(upload.get("headers"), Mapping)
                or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in upload["headers"].items()
                )
            ):
                raise _StepFailedError(SubmissionOutcome("pending", INVALID_RESPONSE))
            if not upload_url_allowed(
                upload["url"], allow_loopback=self._allow_loopback_uploads
            ):
                raise _StepFailedError(SubmissionOutcome("failed", UPLOAD_URL_REFUSED))
        return submission_id, uploads

    def _upload(self, upload: Mapping[str, Any], planned: PlannedSubmission) -> None:
        copied = planned.copied.get(upload["name"])
        path = Path(planned.local_paths[upload["name"]])
        try:
            size = len(copied) if copied is not None else path.stat().st_size
            headers = {**upload["headers"], "Content-Length": str(size)}
            if copied is not None:
                response = self._http.put(
                    upload["url"],
                    content=copied,
                    headers=headers,
                    timeout=UPLOAD_TIMEOUT,
                )
            else:
                with path.open("rb") as stream:
                    response = self._http.put(
                        upload["url"],
                        content=stream,
                        headers=headers,
                        timeout=UPLOAD_TIMEOUT,
                    )
        except (httpx.HTTPError, OSError):
            raise _StepFailedError(
                SubmissionOutcome("pending", UPLOAD_FAILED)
            ) from None
        # 412: the object exists, and the bucket never replaces one.
        if response.status_code not in {200, 201, 412}:
            raise _StepFailedError(SubmissionOutcome("pending", UPLOAD_FAILED))

    def submit(self, planned: PlannedSubmission) -> SubmissionOutcome:
        """Make one attempt; safe to repeat until the outcome is final."""

        preserved = _preserved_files_outcome(planned)
        if preserved is not None:
            return preserved
        try:
            token = self._token()
            created = self._post(SUBMISSIONS_PATH, token, planned.body)
            if created.status_code not in {200, 201}:
                return self._settled_answer(created)
            submission_id, uploads = self._uploads(created, planned)
            for upload in uploads:
                self._upload(upload, planned)
            completed = self._post(
                f"{SUBMISSIONS_PATH}/{submission_id}/complete", token
            )
        except _StepFailedError as failure:
            return failure.outcome
        if completed.status_code == 409:
            # Something is not stored as declared: create again, upload again.
            return SubmissionOutcome("pending", FILES_INCOMPLETE, submission_id)
        if completed.status_code not in {200, 202}:
            answer = self._settled_answer(completed)
            return SubmissionOutcome(answer.status, answer.error_code, submission_id)
        try:
            collector_status = completed.json().get("status")
        except (ValueError, AttributeError):
            collector_status = None
        return SubmissionOutcome(
            "accepted",
            submission_id=submission_id,
            collector_status=collector_status
            if isinstance(collector_status, str)
            else None,
        )
