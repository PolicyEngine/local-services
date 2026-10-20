"""The Hugging Face hand-over module of the local emitter host.

Requests are queries: the module checks and durably records a hand-over, then
answers with its receipt. The module's worker makes the Hugging Face commits.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from microcosm_emitter.host.contracts import JsonObject, ModuleContext
from microcosm_emitter.huggingface.service.delivery import (
    CREDENTIAL_MISSING,
    DATASET_BUNDLE,
    HuggingFaceDelivery,
)
from microcosm_emitter.huggingface.service.hub import (
    DatasetRepository,
    hugging_face_api,
)
from microcosm_emitter.huggingface.service.queue import HandoverQueue, sha256_bytes
from microcosm_emitter.huggingface.service.requests import (
    RequestRefusedError,
    parse_stage_dataset_bundle,
    parse_stage_run_files,
    require_supported_version,
    run_reference,
)
from microcosm_emitter_interface.identifiers import repository_identifier

WORKER_ERROR_MESSAGE = (
    "Microcosm Hugging Face delivery hit a local queue error; handed-over files "
    "stay queued for a later attempt."
)


def _refused_run_files_receipt(message: Mapping[str, Any], code: str) -> dict[str, Any]:
    repository = message.get("repository")
    run_id = message.get("run_id")
    return {
        "status": "refused",
        "repository": repository if isinstance(repository, str) else "",
        "run_id": run_id if isinstance(run_id, str) else "",
        "pending_files": 0,
        "files_attempted": 0,
        "files_committed": 0,
        "commit_attempts": 0,
        "commits": 0,
        "last_error_code": None,
        "last_commit": None,
        "refusal_code": code,
    }


class HuggingFaceModule:
    """Queue hand-overs on request; commit them on the worker."""

    def __init__(
        self,
        configuration: JsonObject,
        context: ModuleContext,
        *,
        api_factory: Callable[[], Any | None] = hugging_face_api,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.configuration, self.context = configuration, context
        self.api_factory, self.clock = api_factory, clock
        self.queue: HandoverQueue | None = None
        self.delivery: HuggingFaceDelivery | None = None
        self._closed = False

    def initialize(self) -> None:
        self.queue = HandoverQueue(Path(self.configuration["spool_path"]))
        self.delivery = HuggingFaceDelivery(
            self.queue, api_factory=self.api_factory, clock=self.clock
        )

    def handle_message(self, message: JsonObject) -> None:
        raise ValueError("Hugging Face hand-overs are queries with receipts.")

    def query(self, message: JsonObject) -> dict[str, Any]:
        assert self.queue is not None, "The host initializes modules first."
        command = message.get("command")
        if command == "stage_run_files":
            return {"receipt": self._stage_run_files(message)}
        if command == "run_files_receipt":
            try:
                repository, run_id = run_reference(message)
            except RequestRefusedError as refusal:
                return {"receipt": _refused_run_files_receipt(message, refusal.code)}
            return {
                "receipt": {
                    "status": "recorded",
                    **self.queue.run_receipt(repository, run_id),
                }
            }
        if command == "read_back_run_files":
            return {"read_back": self._read_back_run_files(message)}
        if command == "stage_dataset_bundle":
            return {"receipt": self._stage_dataset_bundle(message)}
        if command == "handover_receipt":
            return {"receipt": self._handover_receipt(message)}
        if command == "check_write_access":
            return {"access": self._check_write_access(message)}
        raise ValueError("unsupported Hugging Face hand-over command")

    def _stage_dataset_bundle(self, message: JsonObject) -> dict[str, Any]:
        assert self.queue is not None
        try:
            hand_over = parse_stage_dataset_bundle(message)
        except RequestRefusedError as refusal:
            repository = message.get("repository")
            return {
                "handover_id": "",
                "status": "refused",
                "error_code": refusal.code,
                "revision": None,
                "attempts": 0,
                "repository": repository if isinstance(repository, str) else "",
                "prefix": "",
                "copied_files": [],
            }
        return self.queue.enqueue_job(
            handover_id=hand_over.handover_id,
            kind=DATASET_BUNDLE,
            target=hand_over.repository,
            request=hand_over.request,
            receipt={
                "handover_id": hand_over.handover_id,
                "status": "pending",
                "error_code": None,
                "revision": None,
                "attempts": 0,
                "repository": hand_over.repository,
                "prefix": hand_over.remote_prefix,
                "copied_files": sorted(hand_over.copied),
            },
            now=self.clock(),
            files=hand_over.copied,
        )

    def _handover_receipt(self, message: JsonObject) -> dict[str, Any] | None:
        assert self.queue is not None
        require_supported_version(message)
        handover_id = message.get("handover_id")
        if not isinstance(handover_id, str) or not handover_id:
            raise ValueError("handover_id must be a nonempty string")
        return self.queue.job_receipt(handover_id)

    def _check_write_access(self, message: JsonObject) -> dict[str, Any]:
        """Microcosm's staged-dataset preflight: credential, reach and scope."""
        require_supported_version(message)
        repository = repository_identifier(message.get("repository"))
        access: dict[str, Any] = {
            "status": "checked",
            "credential": "missing",
            "reachable": None,
            "can_write": None,
            "error_type": None,
        }
        api = self.api_factory()
        if api is None:
            return access
        hub = DatasetRepository(repository, api)
        access["credential"] = "present"
        try:
            hub.head_revision()
        except Exception as error:
            # Only the type name: the Hub's message can carry request details.
            return {**access, "reachable": False, "error_type": type(error).__name__}
        try:
            can_write = hub.credential_can_write()
        except Exception:
            can_write = None
        return {**access, "reachable": True, "can_write": can_write}

    def _stage_run_files(self, message: JsonObject) -> dict[str, Any]:
        assert self.queue is not None
        try:
            hand_over = parse_stage_run_files(message)
        except RequestRefusedError as refusal:
            return _refused_run_files_receipt(message, refusal.code)
        self.queue.stage_run_files(
            repository=hand_over.repository,
            run_id=hand_over.run_id,
            files=hand_over.files,
            run_index_entry=hand_over.run_index_entry,
            flush=hand_over.flush,
            interval=hand_over.interval,
            now=self.clock(),
        )
        return {
            "status": "recorded",
            **self.queue.run_receipt(hand_over.repository, hand_over.run_id),
        }

    def _read_back_run_files(self, message: JsonObject) -> dict[str, Any]:
        assert self.queue is not None
        result: dict[str, Any] = {
            "status": "refused",
            "files": 0,
            "mismatched_paths": [],
            "error_code": None,
        }
        try:
            repository, run_id = run_reference(message)
        except RequestRefusedError as refusal:
            return {**result, "error_code": refusal.code}
        if self.queue.run_receipt(repository, run_id)["pending_files"]:
            return {**result, "status": "pending"}
        api = self.api_factory()
        if api is None:
            return {**result, "status": "failed", "error_code": CREDENTIAL_MISSING}
        committed = self.queue.committed_run_files(repository, run_id)
        hub = DatasetRepository(repository, api)
        mismatched = []
        try:
            for path_in_repo, digest in sorted(committed.items()):
                if sha256_bytes(hub.download(path_in_repo)) != digest:
                    mismatched.append(path_in_repo)
        except Exception:
            return {
                **result,
                "status": "failed",
                "files": len(committed),
                "error_code": "REPOSITORY_UNAVAILABLE",
            }
        return {
            "status": "failed" if mismatched else "passed",
            "files": len(committed),
            "mismatched_paths": mismatched,
            "error_code": None,
        }

    def tick(self, now: float) -> None:
        assert self.delivery is not None, "The host initializes modules first."
        try:
            self.delivery.run_cycle()
        except Exception:
            # The host stops a module whose tick raises; a local queue error
            # leaves the work queued and must not end every later delivery.
            self.delivery.warn_once("worker", WORKER_ERROR_MESSAGE)

    def parent_exited(self) -> None:
        """Queued hand-overs stay eligible for the next emitter host."""

    def close(self, deadline: float) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.delivery is not None and time.monotonic() < deadline:
                self.delivery.drain(deadline)
        except Exception:
            if self.delivery is not None:
                self.delivery.warn_once("worker", WORKER_ERROR_MESSAGE)
        finally:
            if self.queue is not None:
                self.queue.close()
