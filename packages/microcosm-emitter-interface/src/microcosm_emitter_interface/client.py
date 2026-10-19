"""One typed call per kind of hand-over from a build to its local emitter.

Starting the client starts the installed ``microcosm-emitter`` command as this
process's child. Nothing here raises because the emitter is missing, slow or
refusing: each call reports that it was not accepted, and the build continues.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

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
    unavailable_receipt,
)
from microcosm_emitter_interface.protocol import (
    ACKNOWLEDGEMENT_TIMEOUT_SECONDS,
    FILE_REQUEST_TIMEOUT_SECONDS,
    ORRERY_MODULE,
    STARTUP_TIMEOUT_SECONDS,
    TELEMETRY_MODULE,
)
from microcosm_emitter_interface.telemetry import (
    TelemetryCommand,
    TelemetryRecorder,
    TelemetrySettings,
    telemetry_message,
)
from microcosm_emitter_interface.transport import LocalEmitterError

STARTUP_WARNING = (
    "Local Microcosm emitter failed to start ({reason}); the build will continue."
)
REQUEST_WARNING = (
    "The local Microcosm emitter did not accept a {kind} request ({error_type}); "
    "the build will continue."
)


def default_spool_path() -> Path:
    """The emitter's existing queue location, so queued work survives upgrades."""
    configured = os.environ.get("XDG_CACHE_HOME", "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".cache"
    return root / "microcosm" / "telemetry" / "events.sqlite3"


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
        path = Path(spool_path) if spool_path is not None else default_spool_path()
        modules: dict[str, dict[str, Any]] = {}
        if telemetry is not None:
            modules[TELEMETRY_MODULE] = telemetry.configuration(path)
        if orrery is not None:
            modules[ORRERY_MODULE] = orrery.configuration(path)
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

    def _warn(self, kind: str, error: Exception) -> None:
        with self._warning_lock:
            if kind in self._warned:
                return
            self._warned.add(kind)
        print(
            REQUEST_WARNING.format(kind=kind, error_type=type(error).__name__),
            file=sys.stderr,
        )

    def _send(self, module: str, message: Mapping[str, Any], *, kind: str) -> bool:
        if module not in self.modules:
            return False
        assert self._process is not None
        try:
            self._process.transport.send(
                module, message, timeout=ACKNOWLEDGEMENT_TIMEOUT_SECONDS
            )
        except LocalEmitterError as error:
            self._warn(kind, error)
            return False
        return True

    def _query(
        self,
        module: str,
        message: Mapping[str, Any],
        *,
        kind: str,
        timeout: float = FILE_REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, Any] | None:
        if module not in self.modules:
            return None
        assert self._process is not None
        try:
            return self._process.transport.query(module, message, timeout=timeout)
        except LocalEmitterError as error:
            self._warn(kind, error)
            return None

    def emit_telemetry(self, command: TelemetryCommand) -> bool:
        """Hand over one telemetry event; True once the emitter committed it."""
        return self._send(
            TELEMETRY_MODULE, telemetry_message(command), kind="telemetry"
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
            {
                "command": "inventory",
                "directory": str(Path(directory).resolve()),
                "roles": dict(roles),
                "publication_id": publication_id,
            },
            kind="Orrery",
        )
        value = None if response is None else response.get("inventory")
        return cast(PublicationInventory, value) if isinstance(value, dict) else None

    def hand_over_orrery_inputs(
        self,
        directory: Path,
        inventory: PublicationInventory,
        *,
        wait_seconds: float = 30.0,
    ) -> PublicationReceipt:
        """Queue the inventoried files; wait only after the durable enqueue."""
        receipt = self._orrery_receipt_request(
            {
                "command": "enqueue",
                "directory": str(Path(directory).resolve()),
                "inventory": inventory,
            },
            inventory["publication_id"],
        )
        deadline = time.monotonic() + max(0, wait_seconds)
        while (
            self.available
            and receipt["status"] == "pending"
            and not receipt.get("attempts", 0)
            and time.monotonic() < deadline
        ):
            time.sleep(0.1)
            receipt = self.orrery_receipt(inventory["publication_id"])
        return receipt

    def orrery_receipt(self, publication_id: str) -> PublicationReceipt:
        return self._orrery_receipt_request(
            {"command": "receipt", "publication_id": publication_id}, publication_id
        )

    def retry_orrery_inputs(
        self, publication_id: str, *, wait_seconds: float = 0
    ) -> PublicationReceipt:
        receipt = self._orrery_receipt_request(
            {"command": "retry", "publication_id": publication_id}, publication_id
        )
        previous_attempts = receipt.get("attempts", 0)
        deadline = time.monotonic() + max(0, wait_seconds)
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
        self, message: Mapping[str, Any], publication_id: str | None
    ) -> PublicationReceipt:
        response = self._query(ORRERY_MODULE, message, kind="Orrery")
        value = None if response is None else response.get("receipt")
        if isinstance(value, dict):
            return cast(PublicationReceipt, value)
        return unavailable_receipt(publication_id)

    def close(self) -> None:
        """Ask the emitter to drain and stop; repeated calls do nothing."""
        if self._closed:
            return
        self._closed = True
        if self._process is not None:
            self._process.close()
