"""Best-effort telemetry delivery; shared authentication lives outside this component."""

from __future__ import annotations

import sys
import time
from collections.abc import Mapping
from http import HTTPStatus
from http.client import HTTPException
from typing import Any

from microcosm_emitter.core.auth import (
    CollectorSession,
    SessionResult,
    UnreadableLoginError,
    _http_post,
    huggingface_token,
)
from microcosm_emitter.core.collector_origin import resolve_collector_origin
from microcosm_emitter.core.constants import (
    GIVEN_TOKEN_UNUSABLE_MESSAGE,
    INITIAL_RETRY_SECONDS,
    LOCAL_ONLY_MISSING_CREDENTIAL,
    LOCAL_ONLY_REJECTED_COLLECTOR_AUTHORIZATION,
    LOCAL_ONLY_REJECTED_CREDENTIAL,
    LOCAL_ONLY_REJECTED_REGISTRATION,
    MAX_RETRY_SECONDS,
    NO_CREDENTIAL_MESSAGE,
    PENDING_GIVEN_TOKEN_EXPIRED,
    PENDING_GIVEN_TOKEN_REJECTED,
    REJECTED_CREDENTIAL_MESSAGE,
    RUN_EVENTS_PATH_TEMPLATE,
    RUN_REGISTRATION_PATH,
    UNREADABLE_LOGIN_LOCAL_ONLY_MESSAGE,
    UNSENDABLE_CREDENTIAL_MESSAGE,
)
from microcosm_emitter.telemetry.constants import (
    LOCAL_ONLY_REJECTED_EVENTS,
    REJECTED_EVENTS_MESSAGE,
)
from microcosm_emitter.telemetry.service.spool import EventSpool

_RETRYABLE_CLIENT_ERRORS = frozenset(
    {HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS}
)
#: What a request to the collector raises when it gets no usable answer, all
#: retried with backoff: no answer or a timeout (OSError), or an answer that is
#: not valid HTTP or ends early, such as a truncated chunked answer
#: (http.client's HTTPException, which is not an OSError).
_NETWORK_FAILURES = (OSError, TimeoutError, HTTPException)
#: Session answers for a given token that can no longer be used, and the
#: reason each pending run records.
_UNUSABLE_GIVEN_TOKEN_REASONS: dict[SessionResult, str] = {
    "token_expired": PENDING_GIVEN_TOKEN_EXPIRED,
    "token_rejected": PENDING_GIVEN_TOKEN_REJECTED,
}


def _permanent_client_error(status: int) -> bool:
    return 400 <= int(status) < 500 and status not in _RETRYABLE_CLIENT_ERRORS


def _registration_key(registration: Mapping[str, Any]) -> str:
    return f"{registration['run_id']}:{registration['producer_id']}"


class CollectorDelivery:
    """Authenticate queued runs and deliver idempotent event batches."""

    def __init__(
        self,
        spool: EventSpool,
        *,
        development_collector_url: str | None = None,
        collector_origin: str | None = None,
        session: CollectorSession | None = None,
    ) -> None:
        self.collector_url = resolve_collector_origin(
            development_collector_url=development_collector_url,
            collector_origin=collector_origin,
        )
        self.spool = spool
        self.session = session or CollectorSession(
            self.collector_url,
            post=lambda *args: _http_post(*args),
            credential=lambda: huggingface_token(),
        )
        self._registered: set[str] = set()
        self._warned_no_token = False
        #: Set once the given collector token can no longer be used: nothing
        #: more can be delivered until an emitter with a credential runs.
        self.waiting_for_credential = False
        self._recorded_waiting: set[str] = set()
        self._warned_denied: set[str] = set()
        self._next_attempt_at = 0.0
        self._retry_seconds = INITIAL_RETRY_SECONDS

    def flush_once(self) -> bool:
        """Attempt one delivery pass without waiting for retry deadlines."""

        if time.monotonic() < self._next_attempt_at:
            return False
        made_progress = False
        for registration in self.spool.pending_runs():
            run_id = str(registration["run_id"])
            registration_key = _registration_key(registration)
            token = self._collector_token(registration)
            if token is None:
                continue
            if registration_key not in self._registered:
                if not self._register(registration, token):
                    continue
            events = self.spool.batch(run_id, str(registration["producer_id"]))
            if not events:
                continue
            try:
                status, _ = _http_post(
                    self.collector_url + RUN_EVENTS_PATH_TEMPLATE.format(run_id=run_id),
                    {"events": events},
                    token,
                )
            except _NETWORK_FAILURES:
                self._defer_retry()
                continue
            if status == HTTPStatus.ACCEPTED:
                self.spool.acknowledge([event["event_id"] for event in events])
                made_progress = True
                self._retry_seconds = INITIAL_RETRY_SECONDS
            elif status == HTTPStatus.UNAUTHORIZED:
                self.session.invalidate()
            elif status == HTTPStatus.FORBIDDEN:
                self._make_local_only(
                    registration,
                    LOCAL_ONLY_REJECTED_COLLECTOR_AUTHORIZATION,
                )
            elif _permanent_client_error(status):
                self._make_local_only(
                    registration,
                    LOCAL_ONLY_REJECTED_EVENTS,
                    message=REJECTED_EVENTS_MESSAGE.format(status=int(status)),
                )
            else:
                self._defer_retry()
        return made_progress

    def _collector_token(self, registration: Mapping[str, Any]) -> str | None:
        try:
            result, token = self.session.credential()
        except UnreadableLoginError as error:
            # Like a missing login: this emitter has no credential to send.
            self._local_only_without_credential(
                registration,
                UNREADABLE_LOGIN_LOCAL_ONLY_MESSAGE.format(error_type=error.error_type),
            )
            return None
        if result == "missing":
            self._local_only_without_credential(registration, NO_CREDENTIAL_MESSAGE)
        elif result == "rejected":
            self._make_local_only(registration, LOCAL_ONLY_REJECTED_CREDENTIAL)
        elif result == "unsendable":
            self._make_local_only(
                registration,
                LOCAL_ONLY_REJECTED_CREDENTIAL,
                message=UNSENDABLE_CREDENTIAL_MESSAGE,
            )
        elif result in _UNUSABLE_GIVEN_TOKEN_REASONS:
            self._keep_pending(registration, _UNUSABLE_GIVEN_TOKEN_REASONS[result])
        elif result != "ok":
            self._defer_retry()
        return token

    def _local_only_without_credential(
        self, registration: Mapping[str, Any], warning: str
    ) -> None:
        """Keep the run's events local, warning once for this module."""

        if not self._warned_no_token:
            print(warning, file=sys.stderr, flush=True)
            self._warned_no_token = True
        self._make_local_only(registration, LOCAL_ONLY_MISSING_CREDENTIAL)

    def _keep_pending(self, registration: Mapping[str, Any], reason: str) -> None:
        """Leave the run for an emitter with a credential, saying why once."""

        if not self.waiting_for_credential:
            print(GIVEN_TOKEN_UNUSABLE_MESSAGE, file=sys.stderr, flush=True)
            self.waiting_for_credential = True
        registration_key = _registration_key(registration)
        if registration_key not in self._recorded_waiting:
            self.spool.record_pending_reason(
                str(registration["run_id"]), str(registration["producer_id"]), reason
            )
            self._recorded_waiting.add(registration_key)

    def _register(self, registration: Mapping[str, Any], token: str) -> bool:
        registration_key = _registration_key(registration)
        try:
            status, _ = _http_post(
                self.collector_url + RUN_REGISTRATION_PATH,
                registration,
                token,
            )
        except _NETWORK_FAILURES:
            self._defer_retry()
            return False
        if status == HTTPStatus.CREATED:
            self._registered.add(registration_key)
            self._retry_seconds = INITIAL_RETRY_SECONDS
            # A run left by an emitter whose given token became unusable.
            self.spool.record_pending_reason(
                str(registration["run_id"]), str(registration["producer_id"]), None
            )
            return True
        if status == HTTPStatus.UNAUTHORIZED:
            self.session.invalidate()
        elif status in {
            HTTPStatus.FORBIDDEN,
            HTTPStatus.CONFLICT,
        } or _permanent_client_error(status):
            self._make_local_only(registration, LOCAL_ONLY_REJECTED_REGISTRATION)
        else:
            self._defer_retry()
        return False

    def _make_local_only(
        self,
        registration: Mapping[str, Any],
        reason: str,
        *,
        message: str = REJECTED_CREDENTIAL_MESSAGE,
    ) -> None:
        run_id = str(registration["run_id"])
        producer_id = str(registration["producer_id"])
        registration_key = _registration_key(registration)
        self.spool.make_local_only(run_id, producer_id, reason)
        if reason != LOCAL_ONLY_MISSING_CREDENTIAL:
            self._warn_denied(registration_key, message)

    def _defer_retry(self) -> None:
        self._next_attempt_at = time.monotonic() + self._retry_seconds
        self._retry_seconds = min(MAX_RETRY_SECONDS, self._retry_seconds * 2)

    def _warn_denied(self, registration_key: str, message: str) -> None:
        if registration_key in self._warned_denied:
            return
        print(message, file=sys.stderr, flush=True)
        self._warned_denied.add(registration_key)
