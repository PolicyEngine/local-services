"""Host explicitly selected modules with independent periodic workers."""

import json
import os
import socket
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from microcosm_emitter.host.constants import (
    ACK_ERROR,
    ACK_OK,
    DELIMITER,
    DRAIN_SECONDS,
    MAX_MESSAGE_BYTES,
    MAX_PENDING_REQUESTS,
    READ_BYTES,
    SOCKET_TIMEOUT,
    TICK_SECONDS,
)
from microcosm_emitter.host.contracts import JsonObject, ServiceModule
from microcosm_emitter.host.errors import LocalServiceError
from microcosm_emitter.host.request_dispatch import (
    ModuleRequest,
    ModuleRequestDispatcher,
)


class ServiceRuntime:
    def __init__(
        self,
        socket_path: Path,
        module: ServiceModule | None = None,
        *,
        modules: Mapping[str, ServiceModule] | None = None,
        is_parent_alive: Callable[[], bool],
        tick_seconds: float = TICK_SECONDS,
        drain_seconds: float = DRAIN_SECONDS,
    ):
        self.socket_path = socket_path
        if (module is None) == (modules is None):
            raise ValueError("Select one module or an explicit module mapping.")
        self.modules = dict(modules) if modules is not None else {"default": module}
        if not self.modules:
            raise ValueError("At least one module is required.")
        self.module = module
        self.is_parent_alive = is_parent_alive
        self.tick_seconds = tick_seconds
        self.drain_seconds = drain_seconds
        self._stop = threading.Event()
        self._worker_error: Exception | None = None
        self._failed: set[str] = set()
        self._requests: ModuleRequestDispatcher | None = None
        self._parent_exited = False
        self._shutdown_deadline: float | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        workers = {}
        initialized = []
        bound = False
        try:
            for module in self.modules.values():
                initialized.append(module)
                module.initialize()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                # The launcher owns a fresh private directory. Never unlink an
                # existing socket which might belong to another running host.
                server.bind(str(self.socket_path))
                bound = True
                os.chmod(self.socket_path, 0o600)
                server.listen(16)
                server.settimeout(SOCKET_TIMEOUT)
                self._requests = ModuleRequestDispatcher(
                    self.modules, max_pending_requests=MAX_PENDING_REQUESTS
                )
                self._requests.start()
                for name, module in self.modules.items():
                    worker = threading.Thread(
                        target=self._work, args=(name, module), daemon=True
                    )
                    worker.start()
                    workers[name] = worker
                while not self._stop.is_set():
                    if not self.is_parent_alive():
                        self._parent_exited = True
                        break
                    try:
                        connection, _ = server.accept()
                    except TimeoutError:
                        continue
                    connection.settimeout(SOCKET_TIMEOUT)
                    response = self._respond(connection)
                    if response is not None:
                        try:
                            connection.sendall(response)
                        except OSError:
                            pass
                        finally:
                            connection.close()
        finally:
            deadline = time.monotonic() + self.drain_seconds
            join_deadline = deadline + SOCKET_TIMEOUT
            self._shutdown_deadline = deadline
            self.stop()
            if self._requests is not None:
                self._requests.stop()
            for worker in workers.values():
                worker.join(timeout=max(0, join_deadline - time.monotonic()))
                if worker.is_alive():
                    self._worker_error = TimeoutError("Service shutdown timed out.")
            for name, module in self.modules.items():
                if module in initialized and name not in workers:
                    if self._requests is None or self._requests.wait(
                        name, timeout=max(0, join_deadline - time.monotonic())
                    ):
                        module.close(deadline)
                    else:
                        self._worker_error = TimeoutError("Service shutdown timed out.")
            if bound:
                self.socket_path.unlink(missing_ok=True)
        if self._worker_error is not None:
            raise LocalServiceError(
                f"Service worker failed ({type(self._worker_error).__name__})."
            ) from self._worker_error

    def _respond(self, connection: socket.socket) -> bytes | None:
        try:
            data = bytearray()
            while DELIMITER not in data:
                chunk = connection.recv(
                    min(READ_BYTES, MAX_MESSAGE_BYTES + 1 - len(data))
                )
                if not chunk:
                    raise ValueError("incomplete request")
                data.extend(chunk)
                if len(data) > MAX_MESSAGE_BYTES:
                    raise ValueError("request too large")
            request = json.loads(bytes(data).split(DELIMITER, 1)[0])
            if not isinstance(request, Mapping):
                raise ValueError("request must be an object")
            action = request.get("action")
            if action in {"message", "query"}:
                message = request.get("message")
                if not isinstance(message, Mapping):
                    raise ValueError("message must be an object")
                name = request.get("module")
                if name is None and len(self.modules) == 1:
                    name = next(iter(self.modules))
                if name not in self.modules or name in self._failed:
                    raise ValueError("module is unavailable")
                assert self._requests is not None, (
                    "Requests start before socket intake."
                )
                if not self._requests.submit(
                    name,
                    ModuleRequest(
                        connection,
                        lambda: self._handle_request(name, action, message),
                    ),
                ):
                    raise ValueError("module request queue is unavailable")
                return None  # The module worker now owns this connection.
            elif action == "close":
                self.stop()
            elif action != "ping":
                raise ValueError("unsupported action")
        except Exception:
            # Module errors reject this request, never acknowledge lost data.
            return ACK_ERROR
        return ACK_OK

    def _handle_request(self, name: str, action: str, message: JsonObject) -> bytes:
        try:
            if name in self._failed:
                raise ValueError("module is unavailable")
            module = self.modules[name]
            if action == "query":
                handler = getattr(module, "query", None)
                if not callable(handler):
                    raise ValueError("module does not support queries")
                response = (
                    json.dumps(handler(message), allow_nan=False).encode() + DELIMITER
                )
                if len(response) > MAX_MESSAGE_BYTES:
                    raise ValueError("response too large")
                return response
            module.handle_message(message)
        except Exception:
            return ACK_ERROR
        return ACK_OK

    def _work(self, name: str, module: ServiceModule) -> None:
        try:
            while not self._stop.wait(self.tick_seconds):
                module.tick(time.monotonic())
        except Exception as error:
            self._worker_error = error
            self._failed.add(name)
            if len(self._failed) == len(self.modules):
                self.stop()
        finally:
            try:
                # Periodic uploads stay independent of request work, but lifecycle
                # callbacks must never close resources still in use by a request.
                assert self._requests is not None, "Requests start before workers."
                if self._stop.is_set():
                    self._requests.stop()
                    self._requests.wait_all()
                else:
                    self._requests.stop_module(name)
                    self._requests.wait(name)
                if self._parent_exited and name not in self._failed:
                    module.parent_exited()
                module.close(
                    self._shutdown_deadline
                    if self._shutdown_deadline is not None
                    else time.monotonic() + self.drain_seconds
                )
            except Exception as error:
                self._worker_error = error
                self._failed.add(name)
                if len(self._failed) == len(self.modules):
                    self.stop()
