"""HF-to-collector session shared by telemetry and graph publication.

An emitter without a Hugging Face credential, such as one in a Modal
container, can be given a telemetry-only collector token instead, through
the variables named in ``core.constants``; ``CollectorSession`` then uses it.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

from huggingface_hub import get_token

from microcosm_emitter.core.constants import (
    COLLECTOR_RESPONSE_TOO_LARGE_ERROR,
    COLLECTOR_TOKEN_EXPIRES_AT_VARIABLE,
    COLLECTOR_TOKEN_ORIGIN_VARIABLE,
    COLLECTOR_TOKEN_VARIABLE,
    COLLECTOR_URL_HTTPS_ERROR,
    COLLECTOR_URL_ORIGIN_ERROR,
    DEFAULT_TOKEN_LIFETIME_SECONDS,
    DEVELOPMENT_COLLECTOR_LOOPBACK_ERROR,
    GIVEN_TOKEN_EXPIRY_ERROR,
    GIVEN_TOKEN_ORIGIN_ERROR,
    GIVEN_TOKEN_VARIABLES_ERROR,
    HTTP_TIMEOUT_SECONDS,
    HTTP_USER_AGENT,
    LOOPBACK_HOSTS,
    MAX_HTTP_RESPONSE_BYTES,
    MINIMUM_TOKEN_LIFETIME_SECONDS,
    TELEMETRY_SCOPE,
    TOKEN_EXCHANGE_PATH,
    TOKEN_REFRESH_MARGIN_SECONDS,
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep bearer credentials on the explicitly configured origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _collector_origin(value: str, *, allow_loopback_http: bool = False) -> str:
    parsed = urlsplit(value)
    loopback = parsed.hostname in LOOPBACK_HOSTS
    valid_scheme = parsed.scheme == "https" or (
        allow_loopback_http and loopback and parsed.scheme == "http"
    )
    if not parsed.hostname or not valid_scheme:
        raise ValueError(COLLECTOR_URL_HTTPS_ERROR)
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(COLLECTOR_URL_ORIGIN_ERROR)
    return value.rstrip("/")


def _development_collector_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError(DEVELOPMENT_COLLECTOR_LOOPBACK_ERROR)
    return _collector_origin(value, allow_loopback_http=True)


def _decode_response(body: bytes) -> dict[str, Any]:
    try:
        response = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return response if isinstance(response, dict) else {}


def _http_post(
    url: str,
    payload: Mapping[str, Any],
    bearer_token: str,
    *,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
            "User-Agent": HTTP_USER_AGENT,
        },
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler)
        with opener.open(request, timeout=timeout) as response:
            body = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
            if len(body) > MAX_HTTP_RESPONSE_BYTES:
                raise OSError(COLLECTOR_RESPONSE_TOO_LARGE_ERROR)
            return response.status, _decode_response(body)
    except urllib.error.HTTPError as error:
        body = error.read(MAX_HTTP_RESPONSE_BYTES + 1)
        if len(body) > MAX_HTTP_RESPONSE_BYTES:
            return error.code, {}
        return error.code, _decode_response(body)


def huggingface_token() -> str | None:
    """The build's Hugging Face token: `HF_TOKEN`, then `HUGGINGFACE_TOKEN`, then
    the token saved by `hf auth login`; `None` without one."""
    return (
        os.environ.get("HF_TOKEN", "").strip()
        or os.environ.get("HUGGINGFACE_TOKEN", "").strip()
        or get_token()
    )


@dataclass(frozen=True)
class GivenCollectorToken:
    """A telemetry-only collector token obtained on another machine.

    ``expires_at`` is in seconds since the epoch. ``origin`` is the collector
    the token was obtained from; it is only ever compared.
    """

    access_token: str = field(repr=False)
    expires_at: float
    origin: str


class GivenCollectorTokenError(ValueError):
    """The given-token variables are incomplete, unreadable, or name another
    collector. The message names the variables, never their values."""


def given_collector_token(
    environment: Mapping[str, str],
) -> GivenCollectorToken | None:
    """The collector token in ``environment``, or ``None`` without one."""

    access_token, expires_at, origin = (
        environment.get(name, "").strip()
        for name in (
            COLLECTOR_TOKEN_VARIABLE,
            COLLECTOR_TOKEN_EXPIRES_AT_VARIABLE,
            COLLECTOR_TOKEN_ORIGIN_VARIABLE,
        )
    )
    if not (access_token or expires_at or origin):
        return None
    if not (access_token and expires_at and origin):
        raise GivenCollectorTokenError(GIVEN_TOKEN_VARIABLES_ERROR)
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        raise GivenCollectorTokenError(GIVEN_TOKEN_EXPIRY_ERROR) from None
    if expiry.tzinfo is None:
        raise GivenCollectorTokenError(GIVEN_TOKEN_EXPIRY_ERROR)
    return GivenCollectorToken(access_token, expiry.timestamp(), origin.rstrip("/"))


@dataclass(frozen=True)
class TelemetryTokenExchange:
    """What the collector answered a request for a telemetry token.

    ``result`` is ``ok``, ``missing`` (no Hugging Face credential, nothing
    sent), ``rejected`` (401 or 403), ``lifetime_unsupported`` (422: the
    collector's strict exchange body has no ``lifetime_seconds``) or
    ``unavailable`` (no answer, or no usable token). ``expires_at`` is an
    ISO 8601 UTC time, counted from before the request was sent.
    """

    result: str
    status: int | None = None
    access_token: str | None = field(default=None, repr=False)
    expires_at: str | None = None


def exchange_telemetry_token(
    origin: str,
    lifetime_seconds: int,
    *,
    post: Callable[..., tuple[int, dict[str, Any]]] | None = None,
    credential: Callable[[], str | None] | None = None,
    clock: Callable[[], float] = time.time,
) -> TelemetryTokenExchange:
    """Exchange the Hugging Face credential for a token that only writes
    telemetry and lasts ``lifetime_seconds``, or as long as the collector
    allows, for a build on a machine without that credential."""

    hugging_face_token = (credential or huggingface_token)()
    if not hugging_face_token:
        return TelemetryTokenExchange("missing")
    requested_at = clock()
    try:
        status, response = (post or _http_post)(
            origin + TOKEN_EXCHANGE_PATH,
            {"scope": TELEMETRY_SCOPE, "lifetime_seconds": lifetime_seconds},
            hugging_face_token,
        )
    except (OSError, TimeoutError):
        return TelemetryTokenExchange("unavailable")
    if status in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
        return TelemetryTokenExchange("rejected", status)
    if status == HTTPStatus.UNPROCESSABLE_ENTITY:
        return TelemetryTokenExchange("lifetime_unsupported", status)
    access_token = response.get("access_token")
    expires_in = response.get("expires_in")
    if (
        status != HTTPStatus.OK
        or not isinstance(access_token, str)
        or not access_token
        or type(expires_in) is not int
        or expires_in <= 0
    ):
        return TelemetryTokenExchange("unavailable", status)
    expires_at = datetime.fromtimestamp(requested_at + expires_in, UTC)
    return TelemetryTokenExchange(
        "ok",
        status,
        access_token=access_token,
        expires_at=expires_at.isoformat(timespec="seconds"),
    )


class CollectorSession:
    """One thread-safe short-lived credential shared by independent publishers.

    ``scope`` asks the exchange for a credential with that scope, such as
    ``artifacts:submit``. Without it the exchange body stays ``{}``, which the
    collector answers with a ``telemetry:write`` credential. A scoped session
    accepts a token only when the answer names that scope; any other 200, or
    a 404 from the exchange, means the collector has no intake for it
    (``intake_unavailable``): a collector released before intake answers 200
    with a ``telemetry:write`` token, and one with intake switched off 404.

    With a token given in ``environment`` (by default the process
    environment; see ``given_collector_token``), the session uses it and never
    reads or exchanges a Hugging Face credential. The given token only writes
    telemetry, so a session for any other scope has no credential
    (``missing``). A given token obtained from another origin than ``origin``
    raises ``GivenCollectorTokenError``: its origin is only compared, and
    never chooses the destination.
    """

    def __init__(
        self,
        origin: str,
        *,
        post=None,
        credential=None,
        scope=None,
        environment: Mapping[str, str] | None = None,
    ):
        import threading

        self.origin = origin
        self.scope = scope
        self._post = post or _http_post
        self._credential = credential or huggingface_token
        self._session_token: tuple[str, float] | None = None
        self._lock = threading.Lock()
        self._given_token = given_collector_token(
            os.environ if environment is None else environment
        )
        if self._given_token is not None and self._given_token.origin != origin:
            raise GivenCollectorTokenError(GIVEN_TOKEN_ORIGIN_ERROR)

    def invalidate(self) -> None:
        with self._lock:
            self._session_token = None

    def credential(self) -> tuple[str, str | None]:
        with self._lock:
            if self._given_token is not None:
                return self._given_credential(self._given_token)
            if (
                self._session_token is not None
                and self._session_token[1]
                > time.monotonic() + TOKEN_REFRESH_MARGIN_SECONDS
            ):
                return "ok", self._session_token[0]
            hf_token = self._credential()
            if not hf_token:
                return "missing", None
            try:
                status, response = self._post(
                    self.origin + TOKEN_EXCHANGE_PATH,
                    {} if self.scope is None else {"scope": self.scope},
                    hf_token,
                )
            except (OSError, TimeoutError):
                return "unavailable", None
            if self.scope is not None and (
                status == HTTPStatus.NOT_FOUND
                or (status == HTTPStatus.OK and response.get("scope") != self.scope)
            ):
                return "intake_unavailable", None
            if status == HTTPStatus.OK and isinstance(
                response.get("access_token"), str
            ):
                try:
                    expires_in = max(
                        MINIMUM_TOKEN_LIFETIME_SECONDS,
                        int(response.get("expires_in", DEFAULT_TOKEN_LIFETIME_SECONDS)),
                    )
                except (ValueError, TypeError):
                    return "unavailable", None
                token = response["access_token"]
                self._session_token = (token, time.monotonic() + expires_in)
                return "ok", token
            if status in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
                return "rejected", None
            return "unavailable", None

    def _given_credential(self, given: GivenCollectorToken) -> tuple[str, str | None]:
        if self.scope not in (None, TELEMETRY_SCOPE):
            return "missing", None
        return "ok", given.access_token


def shared_session(services: dict, origin: str) -> CollectorSession:
    """Share a session among explicit modules without adding auth to the host."""
    session = services.setdefault("collector_session", CollectorSession(origin))
    if not isinstance(session, CollectorSession) or session.origin != origin:
        raise ValueError("Provider modules must use the same collector origin.")
    return session
