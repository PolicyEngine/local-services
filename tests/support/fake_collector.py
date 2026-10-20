"""A loopback stand-in for the collector's artifact intake and its bucket.

It follows the submission contract (PolicyEngine/microcosm-collector,
``docs/artifact-submissions.md`` at ``673df3d``) closely enough to drive the
emitter: the scoped token exchange, creation with signed upload URLs,
single-creation uploads, completion with problems, and status codes the tests
choose.

``intake`` chooses the collector the tests talk to: ``"on"``, ``"off"`` (the
exchange and every submission path answer 404 ``intake_not_enabled``) or
``"absent"`` (a collector released before intake: the exchange answers 200
with a ``telemetry:write`` token and no ``scope``, and submission paths 404
without a ``code``).
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeCollector:
    def __init__(self) -> None:
        self.exchange_requests: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.completed: list[str] = []
        self.uploads: list[str] = []
        self.objects: dict[str, bytes] = {}
        self.submissions: dict[str, dict[str, Any]] = {}
        self.exchange_status = 200
        self.intake = "on"
        self.create_statuses: list[int] = []
        self.complete_statuses: list[int] = []
        #: Whole completion answers the tests choose: (status, document).
        self.complete_answers: list[tuple[int, dict[str, Any]]] = []
        #: Store each upload with its bytes reversed, so its SHA-256 differs.
        self.corrupt_uploads = False
        self.sign_stored_files = False
        self.upload_origin: str | None = None
        #: Each issued session token and its scope.
        self._tokens: dict[str, str] = {}
        self._lock = threading.Lock()
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *arguments: Any) -> None:
                pass

            def _body(self) -> bytes:
                return self.rfile.read(int(self.headers.get("Content-Length", "0")))

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                encoded = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def _token_scope(self) -> str | None:
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                return collector._tokens.get(token)

            def do_POST(self) -> None:
                body = self._body()
                with collector._lock:
                    if self.path == "/v1/auth/huggingface/exchange":
                        return collector._exchange(self, body)
                    if collector.intake != "on":
                        return collector._intake_not_available(self)
                    scope = self._token_scope()
                    if scope is None:
                        return self._json(401, {"detail": "invalid"})
                    if scope != "artifacts:submit":
                        # The token's scope does not allow the request.
                        return self._json(403, {"detail": "scope"})
                    if self.path == "/v1/submissions":
                        return collector._create(self, json.loads(body))
                    if self.path.endswith("/complete"):
                        return collector._complete(self, self.path.split("/")[3])
                self._json(404, {"detail": "Not Found"})

            def do_PUT(self) -> None:
                body = self._body()
                with collector._lock:
                    collector._store(self, self.path.removeprefix("/storage/"), body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )

    def start(self) -> FakeCollector:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _intake_not_available(self, handler: Any) -> None:
        if self.intake == "off":
            return handler._json(
                404, {"detail": "Intake is off.", "code": "intake_not_enabled"}
            )
        handler._json(404, {"detail": "Not Found"})

    def _exchange(self, handler: Any, body: bytes) -> None:
        request = json.loads(body) if body else {}
        self.exchange_requests.append(request)
        if self.exchange_status != 200:
            return handler._json(self.exchange_status, {"detail": "refused"})
        scope = request.get("scope", "telemetry:write")
        if scope == "artifacts:submit" and self.intake == "off":
            return self._intake_not_available(handler)
        if self.intake == "absent":
            # Released before intake: every exchange gives a telemetry token.
            token = f"telemetry-token-{len(self._tokens) + 1}"
            self._tokens[token] = "telemetry:write"
            return handler._json(200, {"access_token": token, "expires_in": 900})
        token = f"session-token-{len(self._tokens) + 1}"
        self._tokens[token] = scope
        handler._json(
            200,
            {
                "access_token": token,
                "token_type": "bearer",
                "expires_in": 900,
                "scope": scope,
            },
        )

    @staticmethod
    def _chosen(handler: Any, answer: int | tuple[int, str]) -> None:
        """A status the test chose, optionally with an error ``code``."""
        status, code = answer if isinstance(answer, tuple) else (answer, None)
        handler._json(status, {"detail": "chosen", **({"code": code} if code else {})})

    def _create(self, handler: Any, body: dict[str, Any]) -> None:
        self.created.append(body)
        if self.create_statuses:
            return self._chosen(handler, self.create_statuses.pop(0))
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        submission_id = "sub-" + hashlib.sha256(canonical.encode()).hexdigest()[:32]
        created = submission_id not in self.submissions
        self.submissions.setdefault(
            submission_id,
            {"body": body, "status": "awaiting_upload", "job": None, "rejection": None},
        )
        submission = self.submissions[submission_id]
        uploads = []
        if submission["status"] == "awaiting_upload":
            origin = self.upload_origin or self.origin
            for file in body["files"]:
                object_name = f"submissions/{submission_id}/{file['name']}"
                if object_name in self.objects and not self.sign_stored_files:
                    continue
                uploads.append(
                    {
                        "name": file["name"],
                        "method": "PUT",
                        "url": f"{origin}/storage/{object_name}",
                        "headers": {
                            "Content-Type": "application/octet-stream",
                            "x-goog-meta-sha256": file["sha256"],
                            "x-goog-content-length-range": (
                                f"{file['size']},{file['size']}"
                            ),
                            "x-goog-if-generation-match": "0",
                        },
                        "expires_at": "2026-10-20T10:00:00+00:00",
                    }
                )
        handler._json(
            201 if created else 200,
            {**self._status_document(submission_id), "uploads": uploads},
        )

    def _status_document(self, submission_id: str) -> dict[str, Any]:
        """The contract's status document for one submission."""
        submission = self.submissions[submission_id]
        files = sorted(
            (
                {key: file[key] for key in ("name", "role", "size", "sha256")}
                for file in submission["body"]["files"]
            ),
            key=lambda file: file["name"],
        )
        canonical = json.dumps(files, sort_keys=True, separators=(",", ":"))
        return {
            "contract_version": 1,
            "submission_id": submission_id,
            "kind": submission["body"]["kind"],
            "status": submission["status"],
            "files_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
            "created_at": "2026-10-20T09:00:00.000000+00:00",
            "updated_at": "2026-10-20T09:00:00.000000+00:00",
            "accepted_at": None,
            "finished_at": None,
            "job": submission["job"],
            "rejection": submission["rejection"],
        }

    def _store(self, handler: Any, object_name: str, body: bytes) -> None:
        self.uploads.append(object_name)
        expected = handler.headers.get("x-goog-content-length-range", "")
        if expected != f"{len(body)},{len(body)}":
            return handler._json(400, {"detail": "size"})
        if object_name in self.objects:
            return handler._json(412, {"detail": "exists"})
        self.objects[object_name] = body[::-1] if self.corrupt_uploads else body
        handler._json(200, {})

    def _complete(self, handler: Any, submission_id: str) -> None:
        self.completed.append(submission_id)
        if self.complete_answers:
            return handler._json(*self.complete_answers.pop(0))
        if self.complete_statuses:
            return self._chosen(handler, self.complete_statuses.pop(0))
        submission = self.submissions.get(submission_id)
        if submission is None:
            return handler._json(
                404,
                {"detail": "Submission was not found.", "code": "submission_not_found"},
            )
        if submission["status"] in {"rejected", "failed", "succeeded"}:
            return handler._json(200, self._status_document(submission_id))
        problems = []
        mismatched = []
        for file in submission["body"]["files"]:
            stored = self.objects.get(f"submissions/{submission_id}/{file['name']}")
            if stored is None:
                problems.append({"name": file["name"], "problem": "missing"})
            elif hashlib.sha256(stored).hexdigest() != file["sha256"]:
                mismatched.append(file["name"])
        if problems:
            return handler._json(
                409,
                {
                    "detail": "Some files are not stored.",
                    "code": "files_missing",
                    "problems": problems,
                },
            )
        if mismatched:
            # Stored objects are never replaced: the submission is rejected.
            submission["status"] = "rejected"
            submission["rejection"] = {
                "code": "stored_object_mismatch",
                "message": f"{mismatched[0]} has another recorded SHA-256.",
            }
            return handler._json(200, self._status_document(submission_id))
        submission["status"] = "queued"
        submission["job"] = {
            "job_id": "job-" + submission_id.removeprefix("sub-"),
            "processing_round": 1,
            "attempts": 0,
        }
        handler._json(202, self._status_document(submission_id))
