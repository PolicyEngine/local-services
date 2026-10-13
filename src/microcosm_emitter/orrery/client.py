"""Thin graph requests; hashing, persistence, auth and uploads stay in the service."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast

from microcosm_emitter.host.constants import STARTUP_TIMEOUT
from microcosm_emitter.host.contracts import JsonObject
from microcosm_emitter.host.errors import (
    LocalServiceError,
    ServiceStartupError,
    safe_startup_error,
)
from microcosm_emitter.host.launcher import ServiceHandle, start_service
from microcosm_emitter.orrery.constants import GRAPH_REQUEST_TIMEOUT_SECONDS
from microcosm_emitter.orrery.contracts import (
    PublicationFileRole,
    PublicationInventory,
    PublicationReceipt,
)


class GraphTransport(Protocol):
    """Only a request adapter, never a database or HTTP implementation."""

    def request(
        self, message: JsonObject, *, timeout: float | None = None
    ) -> JsonObject: ...

    def close(self) -> None: ...


def default_spool_path() -> Path:
    """Keep the existing queue location so versioned jobs survive extraction."""
    configured = os.environ.get("XDG_CACHE_HOME", "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".cache"
    return root / "microcosm" / "telemetry" / "events.sqlite3"


def _unavailable(publication_id: str | None) -> PublicationReceipt:
    return {
        "version": 1,
        "publication_id": publication_id,
        "status": "failed",
        "error_code": "local_service_unavailable",
    }


class GraphPublicationClient:
    """Send metadata to a separately running graph publisher over its private socket."""

    def __init__(self, transport: GraphTransport | None = None) -> None:
        self.transport = transport
        self._handle: ServiceHandle | None = None
        self.startup_error: ServiceStartupError | None = None

    @property
    def available(self) -> bool:
        return self.transport is not None

    @classmethod
    def start(
        cls,
        *,
        spool_path: Path | None = None,
        development_collector_url: str | None = None,
        publication_origin: str | None = None,
        startup_timeout: float = STARTUP_TIMEOUT,
    ) -> GraphPublicationClient:
        client = cls()
        try:
            configuration = {"spool_path": str(spool_path or default_spool_path())}
            if development_collector_url is not None:
                configuration["development_collector_url"] = development_collector_url
            if publication_origin is not None:
                configuration["publication_origin"] = publication_origin
            client._handle = start_service(
                "orrery", configuration, startup_timeout=startup_timeout
            )
            client.transport = client._handle.client.for_module("orrery")
        except (LocalServiceError, OSError, ValueError, TypeError) as error:
            # No acknowledgement means no claim that a durable job exists.
            client.startup_error = safe_startup_error(error)
        return client

    def _request(self, message: JsonObject) -> JsonObject | None:
        if self.transport is None:
            return None
        try:
            return self.transport.request(
                message, timeout=GRAPH_REQUEST_TIMEOUT_SECONDS
            )
        except (LocalServiceError, OSError, ValueError, TypeError):
            return None

    def inventory(
        self,
        directory: Path,
        roles: Mapping[str, PublicationFileRole],
        *,
        publication_id: str | None = None,
    ) -> PublicationInventory | None:
        response = self._request(
            {
                "command": "inventory",
                "directory": str(directory.resolve()),
                "roles": dict(roles),
                "publication_id": publication_id,
            }
        )
        value = response.get("inventory") if response is not None else None
        return cast(PublicationInventory, value) if isinstance(value, dict) else None

    def enqueue(
        self, directory: Path, inventory: PublicationInventory
    ) -> PublicationReceipt:
        response = self._request(
            {
                "command": "enqueue",
                "directory": str(directory.resolve()),
                "inventory": inventory,
            }
        )
        value = response.get("receipt") if response is not None else None
        return (
            cast(PublicationReceipt, value)
            if isinstance(value, dict)
            else _unavailable(inventory["publication_id"])
        )

    def receipt(self, publication_id: str) -> PublicationReceipt:
        response = self._request(
            {"command": "receipt", "publication_id": publication_id}
        )
        value = response.get("receipt") if response is not None else None
        return (
            cast(PublicationReceipt, value)
            if isinstance(value, dict)
            else _unavailable(publication_id)
        )

    def retry(
        self, publication_id: str, *, wait_seconds: float = 0
    ) -> PublicationReceipt:
        response = self._request({"command": "retry", "publication_id": publication_id})
        value = response.get("receipt") if response is not None else None
        receipt = (
            cast(PublicationReceipt, value)
            if isinstance(value, dict)
            else _unavailable(publication_id)
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
            receipt = self.receipt(publication_id)
        return receipt

    def close(self) -> None:
        try:
            if self._handle is not None:
                self._handle.close()
            elif self.transport is not None:
                self.transport.close()
        except (LocalServiceError, OSError):
            pass
        finally:
            self.transport = None


def publication_inventory(
    directory: Path,
    roles: Mapping[str, PublicationFileRole],
    *,
    publication_id: str | None = None,
    client: GraphPublicationClient | None = None,
    spool_path: Path | None = None,
) -> PublicationInventory | None:
    """Ask the service to hash allowed files without importing its implementation."""
    publisher = (
        client
        if client is not None
        else GraphPublicationClient.start(spool_path=spool_path)
    )
    try:
        return publisher.inventory(directory, roles, publication_id=publication_id)
    finally:
        if client is None:
            publisher.close()


def publish_graph(
    directory: Path,
    inventory: PublicationInventory,
    *,
    spool_path: Path | None = None,
    client: GraphPublicationClient | None = None,
    wait_seconds: float = 30.0,
) -> PublicationReceipt:
    """Wait only after service-side durable enqueue; never write a fallback queue."""
    publisher = (
        client
        if client is not None
        else GraphPublicationClient.start(spool_path=spool_path)
    )
    try:
        receipt = publisher.enqueue(directory, inventory)
        deadline = time.monotonic() + max(0, wait_seconds)
        while (
            publisher.available
            and receipt["status"] == "pending"
            and not receipt.get("attempts", 0)
            and time.monotonic() < deadline
        ):
            time.sleep(0.1)
            receipt = publisher.receipt(inventory["publication_id"])
        return receipt
    finally:
        if client is None:
            publisher.close()
