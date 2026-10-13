"""Bounded, private Unix-socket transport without domain-specific behavior."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

from microcosm_emitter.host.constants import (
    ACK_OK,
    DELIMITER,
    MAX_MESSAGE_BYTES,
    SEND_TIMEOUT,
)
from microcosm_emitter.host.contracts import JsonObject
from microcosm_emitter.host.errors import LocalServiceError


class SocketClient:
    def __init__(self, socket_path: Path, *, timeout: float = SEND_TIMEOUT):
        self.socket_path = socket_path
        self.timeout = timeout
        self.closed = False

    def _request(
        self,
        payload: JsonObject,
        *,
        timeout: float | None = None,
        deadline: float | None = None,
    ) -> JsonObject | None:
        try:
            data = (
                json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
                + DELIMITER
            )
            if len(data) > MAX_MESSAGE_BYTES:
                raise ValueError("local message exceeds size limit")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:

                def bound_operation():
                    remaining = self.timeout if timeout is None else timeout
                    if deadline is not None:
                        remaining = min(remaining, deadline - time.monotonic())
                        if remaining <= 0:
                            raise TimeoutError()
                    peer.settimeout(remaining)

                bound_operation()
                peer.connect(str(self.socket_path))
                bound_operation()
                peer.sendall(data)
                response = bytearray()
                while DELIMITER not in response:
                    bound_operation()
                    part = peer.recv(min(4096, MAX_MESSAGE_BYTES + 1 - len(response)))
                    if not part:
                        break
                    response.extend(part)
                    if len(response) > MAX_MESSAGE_BYTES:
                        raise ValueError("local response exceeds size limit")
                if bytes(response) == ACK_OK:
                    return None
                if not response.endswith(DELIMITER):
                    raise ValueError("incomplete response")
                result = json.loads(bytes(response).removesuffix(DELIMITER))
                if not isinstance(result, dict):
                    raise ValueError("service rejected the request")
                return result
        except (OSError, ValueError, TypeError) as error:
            raise LocalServiceError(type(error).__name__) from error

    def ping(self, *, timeout: float | None = None) -> None:
        """Bound a readiness probe across connection, send and all reads."""
        self._request(
            {"action": "ping"},
            timeout=timeout,
            deadline=None if timeout is None else time.monotonic() + timeout,
        )

    def for_module(self, name: str) -> ModuleClient:
        """Route messages without giving a component ownership of host shutdown."""
        return ModuleClient(self, name)

    def request(
        self, message: JsonObject, *, module: str, timeout: float | None = None
    ) -> JsonObject:
        """Return one bounded service result after its request has completed."""
        if self.closed:
            raise LocalServiceError("client is closed")
        response = self._request(
            {"action": "query", "module": module, "message": message}, timeout=timeout
        )
        if response is None:
            raise LocalServiceError("service did not return a result")
        return response

    def send(self, message: JsonObject) -> None:
        if self.closed:
            raise LocalServiceError("client is closed")
        self._request({"action": "message", "message": message})

    def close(self) -> None:
        if self.closed:
            return
        try:
            self._request({"action": "close"})
        finally:
            self.closed = True


class ModuleClient:
    """One selected module's transport; the enclosing ServiceHandle owns shutdown."""

    def __init__(self, client: SocketClient, name: str):
        self.client, self.name = client, name

    def send(self, message: JsonObject) -> None:
        if self.client.closed:
            raise LocalServiceError("client is closed")
        self.client._request(
            {"action": "message", "module": self.name, "message": message}
        )

    def request(
        self, message: JsonObject, *, timeout: float | None = None
    ) -> JsonObject:
        return self.client.request(message, module=self.name, timeout=timeout)

    def close(self) -> None:
        """A domain component cannot close the shared host."""
