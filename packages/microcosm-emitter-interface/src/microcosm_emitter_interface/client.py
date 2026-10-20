"""One typed call per kind of hand-over from a build to its local emitter.

Starting the client starts the installed ``microcosm-emitter`` command as this
process's child. No call raises into the build: not when the emitter is missing,
slow or refusing, and not when an argument cannot be converted to JSON (for
example a multi-element NumPy array or a self-referencing mapping). Each call
converts its arguments inside its error handling, reports that it was not
accepted, records why in ``EmitterClient.last_failure``, and the build continues.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from microcosm_emitter_interface.launcher import (
    EmitterProcess,
    EmitterStartupError,
    start_emitter,
)
from microcosm_emitter_interface.orrery import (
    OrrerySettings,
    PublicationFileRole,
    PublicationInventory,
    PublicationReceipt,
    answered_inventory,
    answered_receipt,
    enqueue_message,
    inventory_message,
    publication_id_of,
    receipt_message,
    retry_message,
    unavailable_receipt,
)
from microcosm_emitter_interface.protocol import (
    ACKNOWLEDGEMENT_TIMEOUT_SECONDS,
    FILE_REQUEST_TIMEOUT_SECONDS,
    ORRERY_MODULE,
    STARTUP_TIMEOUT_SECONDS,
    TELEMETRY_MODULE,
)
from microcosm_emitter_interface.spool import default_spool_path
from microcosm_emitter_interface.telemetry import (
    TelemetryCommand,
    TelemetryRecorder,
    TelemetrySettings,
    telemetry_message,
)

STARTUP_WARNING = (
    "Local Microcosm emitter failed to start ({reason}); the build will continue."
)
REQUEST_WARNING = (
    "The local Microcosm emitter did not accept a {kind} request "
    "({reason}, {error_type}); the build will continue."
)

type RequestFailureReason = Literal[
    # No emitter is running, or it did not load the call's module.
    "emitter_unavailable",
    # The call's arguments could not be converted to a protocol message.
    "invalid_arguments",
    # The emitter refused the request, or did not answer in time.
    "not_accepted",
]

type MessageBuilder = Callable[[], Mapping[str, Any]]


@dataclass(frozen=True)
class RequestFailure:
    """Why a call reported that the emitter did not accept it.

    ``error_type`` is only the exception's type name, never its message.
    """

    kind: str
    reason: RequestFailureReason
    error_type: str | None = None


class EmitterClient:
    """Typed hand-overs to one emitter process owned by this build."""

    def __init__(
        self,
        process: EmitterProcess | None = None,
        *,
        startup_error: EmitterStartupError | None = None,
    ) -> None:
        self._process = process
        self.startup_error = startup_error
        self._closed = False
        self._warned: set[str] = set()
        self._warning_lock = threading.Lock()
        #: The most recent call that reported failure, and why.
        self.last_failure: RequestFailure | None = None
        self.telemetry = TelemetryRecorder(self)

    @classmethod
    def start(
        cls,
        *,
        telemetry: TelemetrySettings | None = None,
        orrery: OrrerySettings | None = None,
        spool_path: Path | str | None = None,
        command: str | os.PathLike[str] | None = None,
        startup_timeout: float = STARTUP_TIMEOUT_SECONDS,
    ) -> EmitterClient:
        """Start the selected modules; return an unavailable client on failure."""
        try:
            path = Path(spool_path) if spool_path is not None else default_spool_path()
            modules: dict[str, dict[str, Any]] = {}
            if telemetry is not None:
                modules[TELEMETRY_MODULE] = telemetry.configuration(path)
            if orrery is not None:
                modules[ORRERY_MODULE] = orrery.configuration(path)
        except Exception:
            failure = EmitterStartupError("invalid_configuration")
            print(STARTUP_WARNING.format(reason=failure.reason), file=sys.stderr)
            return cls(startup_error=failure)
        try:
            process = start_emitter(
                modules, command=command, startup_timeout=startup_timeout
            )
        except EmitterStartupError as error:
            print(STARTUP_WARNING.format(reason=error.reason), file=sys.stderr)
            return cls(startup_error=error)
        except Exception:
            failure = EmitterStartupError("startup_failure")
            print(STARTUP_WARNING.format(reason=failure.reason), file=sys.stderr)
            return cls(startup_error=failure)
        return cls(process)

    @property
    def available(self) -> bool:
        return self._process is not None and not self._closed

    @property
    def modules(self) -> frozenset[str]:
        """The modules the running emitter loaded."""
        if not self.available:
            return frozenset()
        assert self._process is not None
        return frozenset(self._process.description.get("modules", ()))

    @property
    def emitter_version(self) -> str | None:
        if self._process is None:
            return None
        version = self._process.description.get("emitter_version")
        return version if isinstance(version, str) else None

    def _fail(
        self,
        kind: str,
        reason: RequestFailureReason,
        error: BaseException | None = None,
    ) -> None:
        """Record why a call failed; warn once per kind of call.

        A missing emitter was already reported when it failed to start, so
        only refused requests and unconvertible arguments print a warning.
        """
        error_type = None if error is None else type(error).__name__
        self.last_failure = RequestFailure(kind, reason, error_type)
        if reason == "emitter_unavailable":
            return
        with self._warning_lock:
            if kind in self._warned:
                return
            self._warned.add(kind)
        print(
            REQUEST_WARNING.format(kind=kind, reason=reason, error_type=error_type),
            file=sys.stderr,
        )

    def _message(
        self, build_message: MessageBuilder, *, kind: str
    ) -> dict[str, Any] | None:
        """The converted message, or None after recording why it failed."""
        try:
            return dict(build_message())
        except Exception as error:
            self._fail(kind, "invalid_arguments", error)
            return None

    def _send(self, module: str, build_message: MessageBuilder, *, kind: str) -> bool:
        if module not in self.modules:
            self._fail(kind, "emitter_unavailable")
            return False
        message = self._message(build_message, kind=kind)
        if message is None:
            return False
        assert self._process is not None
        try:
            self._process.transport.send(
                module, message, timeout=ACKNOWLEDGEMENT_TIMEOUT_SECONDS
            )
        except Exception as error:
            self._fail(kind, "not_accepted", error)
            return False
        return True

    def _request(
        self,
        module: str,
        build_message: MessageBuilder,
        *,
        kind: str,
        timeout: float = FILE_REQUEST_TIMEOUT_SECONDS,
    ) -> tuple[dict[str, Any] | None, RequestFailureReason | None]:
        """The module's answer, or None and why this call failed.

        The reason belongs to this call only. ``last_failure`` is shared by
        every call and thread, so a receipt's error code never reads it.
        """
        if module not in self.modules:
            self._fail(kind, "emitter_unavailable")
            return None, "emitter_unavailable"
        message = self._message(build_message, kind=kind)
        if message is None:
            return None, "invalid_arguments"
        assert self._process is not None
        try:
            return self._process.transport.query(module, message, timeout=timeout), None
        except Exception as error:
            self._fail(kind, "not_accepted", error)
            return None, "not_accepted"

    def _query(
        self,
        module: str,
        build_message: MessageBuilder,
        *,
        kind: str,
        timeout: float = FILE_REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, Any] | None:
        answer, _ = self._request(module, build_message, kind=kind, timeout=timeout)
        return answer

    def emit_telemetry(self, command: TelemetryCommand) -> bool:
        """Hand over one telemetry event; True once the emitter committed it."""
        return self._send(
            TELEMETRY_MODULE, lambda: telemetry_message(command), kind="telemetry"
        )

    def orrery_inventory(
        self,
        directory: Path,
        roles: Mapping[str, PublicationFileRole],
        *,
        publication_id: str | None = None,
    ) -> PublicationInventory | None:
        """Ask the emitter to measure the selected preserved Orrery input files."""
        response = self._query(
            ORRERY_MODULE,
            lambda: inventory_message(directory, roles, publication_id),
            kind="Orrery",
        )
        return answered_inventory(response)

    def hand_over_orrery_inputs(
        self,
        directory: Path,
        inventory: PublicationInventory,
        *,
        wait_seconds: float = 30.0,
    ) -> PublicationReceipt:
        """Queue the inventoried files; wait only after the durable enqueue."""
        publication_id = publication_id_of(inventory)
        if publication_id is None:
            self._fail("Orrery", "invalid_arguments")
            return unavailable_receipt(None, error_code="invalid_arguments")
        receipt = self._orrery_receipt_request(
            lambda: enqueue_message(directory, inventory), publication_id
        )
        deadline = time.monotonic() + _wait_seconds(wait_seconds)
        while (
            self.available
            and receipt["status"] == "pending"
            and not receipt.get("attempts", 0)
            and time.monotonic() < deadline
        ):
            time.sleep(0.1)
            receipt = self.orrery_receipt(publication_id)
        return receipt

    def orrery_receipt(self, publication_id: str) -> PublicationReceipt:
        return self._orrery_receipt_request(
            lambda: receipt_message(publication_id), publication_id
        )

    def retry_orrery_inputs(
        self, publication_id: str, *, wait_seconds: float = 0
    ) -> PublicationReceipt:
        receipt = self._orrery_receipt_request(
            lambda: retry_message(publication_id), publication_id
        )
        previous_attempts = receipt.get("attempts", 0)
        deadline = time.monotonic() + _wait_seconds(wait_seconds)
        while (
            self.available
            and receipt["status"] == "pending"
            and receipt.get("attempts", 0) == previous_attempts
            and time.monotonic() < deadline
        ):
            time.sleep(0.1)
            receipt = self.orrery_receipt(publication_id)
        return receipt

    def _orrery_receipt_request(
        self, build_message: MessageBuilder, publication_id: str | None
    ) -> PublicationReceipt:
        answer, failure = self._request(ORRERY_MODULE, build_message, kind="Orrery")
        return answered_receipt(
            answer, publication_id, error_code=_receipt_error_code(failure)
        )

    def close(self) -> None:
        """Ask the emitter to drain and stop; repeated calls do nothing."""
        if self._closed:
            return
        self._closed = True
        if self._process is not None:
            try:
                self._process.close()
            except Exception as error:
                self._fail("close", "not_accepted", error)


def _receipt_error_code(failure: RequestFailureReason | None) -> str:
    """The error code of an unavailable receipt, from its own call's failure.

    An answer without a receipt, or no answer, is ``local_service_unavailable``,
    as in the emitter's in-process client.
    """
    if failure == "invalid_arguments":
        return "invalid_arguments"
    return "local_service_unavailable"


def _wait_seconds(value: Any) -> float:
    """A usable waiting time; an unusable one means not waiting."""
    try:
        seconds = float(value)
    except Exception:
        return 0.0
    return seconds if seconds > 0 else 0.0
