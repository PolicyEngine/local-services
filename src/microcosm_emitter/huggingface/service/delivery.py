"""The delivery cycle: at most one Hugging Face commit per repository per cycle.

Each cycle (one module tick) finds the repositories with work due, leases each
one, and makes one commit carrying every pending staging run file of that
repository. Failures keep the work queued and back off; nothing here raises
into the build.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from microcosm_emitter.huggingface.service.bundles import stage_bundle
from microcosm_emitter.huggingface.service.hub import DatasetRepository
from microcosm_emitter.huggingface.service.queue import ClaimedJob, HandoverQueue
from microcosm_emitter.huggingface.service.run_files import commit_run_files

#: How long a repository or job lease lasts without renewal. While a delivery
#: runs, its leases are renewed every ``LEASE_RENEWAL_SECONDS``, so a long
#: upload keeps them; an emitter that was killed stops renewing, and another
#: emitter on this machine takes the repository over within one lease.
REPOSITORY_LEASE_SECONDS = 120.0
LEASE_RENEWAL_SECONDS = 30.0
#: The job kinds this emitter delivers.
DATASET_BUNDLE = "dataset_bundle"
SUPPORTED_JOB_KINDS = frozenset({DATASET_BUNDLE})
CREDENTIAL_MISSING = "CREDENTIAL_MISSING"
UPLOAD_FAILED = "UPLOAD_FAILED"
CREDENTIAL_MISSING_MESSAGE = (
    "Microcosm Hugging Face uploads are paused: no Hugging Face credential was "
    "found (HF_TOKEN, HUGGINGFACE_TOKEN or `hf auth login`). Handed-over files "
    "stay queued; the build will continue."
)
UPLOAD_FAILED_MESSAGE = (
    "A Microcosm Hugging Face upload failed ({error_type}); handed-over files "
    "stay queued for a later attempt and the build will continue."
)


def iso_timestamp(seconds: float) -> str:
    """Microcosm ``staging._now()`` format, at ``seconds``."""
    return datetime.fromtimestamp(seconds, UTC).replace(microsecond=0).isoformat()


class HeldLeases:
    """A repository lease, and the job lease once one is claimed, kept renewed.

    A thread renews both every ``renewal_seconds`` until ``stop``. Read
    ``repository_lease`` and ``job`` only after ``stop``: they then name the
    current leases, which releasing the repository and finishing the job
    must match.
    """

    def __init__(
        self,
        queue: HandoverQueue,
        repository: str,
        lease: float,
        *,
        clock: Callable[[], float],
        lease_seconds: float,
        renewal_seconds: float,
    ) -> None:
        self.queue, self.repository = queue, repository
        self.repository_lease = lease
        self.job: ClaimedJob | None = None
        self.clock = clock
        self.lease_seconds, self.renewal_seconds = lease_seconds, renewal_seconds
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._renew_until_stopped, daemon=True)

    def start(self) -> HeldLeases:
        self._thread.start()
        return self

    def hold_job(self, job: ClaimedJob) -> None:
        with self._lock:
            self.job = job

    def renew(self) -> None:
        with self._lock:
            renewed = self.clock() + self.lease_seconds
            if renewed > self.repository_lease and self.queue.renew_repository(
                self.repository, self.repository_lease, renewed
            ):
                self.repository_lease = renewed
            job = self.job
            if (
                job is not None
                and renewed > job.lease_until
                and self.queue.renew_job(job, renewed)
            ):
                self.job = replace(job, lease_until=renewed)

    def _renew_until_stopped(self) -> None:
        while not self._stopped.wait(self.renewal_seconds):
            try:
                self.renew()
            except Exception:
                # A failed renewal leaves the lease to expire; the delivery's
                # own result is recorded only if it still holds the lease.
                pass

    def stop(self) -> None:
        self._stopped.set()
        if self._thread.is_alive():
            self._thread.join()


class HuggingFaceDelivery:
    """Commit queued hand-overs with the build's credential."""

    def __init__(
        self,
        queue: HandoverQueue,
        *,
        api_factory: Callable[[], Any | None],
        clock: Callable[[], float] = time.time,
        lease_renewal_seconds: float = LEASE_RENEWAL_SECONDS,
    ) -> None:
        self.queue = queue
        self.api_factory = api_factory
        self.clock = clock
        self.lease_renewal_seconds = lease_renewal_seconds
        self._warned: set[str] = set()
        self._warning_lock = threading.Lock()

    def warn_once(self, key: str, message: str) -> None:
        with self._warning_lock:
            if key in self._warned:
                return
            self._warned.add(key)
        print(message, file=sys.stderr, flush=True)

    def run_cycle(self) -> int:
        """Make at most one commit per due repository; return how many."""
        repositories = self.queue.due_repositories(
            self.clock(), kinds=SUPPORTED_JOB_KINDS
        )
        if not repositories:
            return 0
        api = self.api_factory()
        return sum(
            self._deliver_repository(repository, api) for repository in repositories
        )

    def drain(self, deadline: float) -> None:
        """At shutdown, commit every repository's pending run files once.

        Ignores the commit interval and spacing, but still leases each
        repository. ``deadline`` is a ``time.monotonic`` value.
        """
        api = self.api_factory()
        for repository in self.queue.repositories_with_pending_run_files():
            if time.monotonic() >= deadline:
                return
            self._deliver_repository(repository, api, run_files_only=True)

    def _deliver_repository(
        self, repository: str, api: Any | None, *, run_files_only: bool = False
    ) -> int:
        lease = self.queue.claim_repository(
            repository, now=self.clock(), lease_seconds=REPOSITORY_LEASE_SECONDS
        )
        if lease is None:
            return 0
        held = HeldLeases(
            self.queue,
            repository,
            lease,
            clock=self.clock,
            lease_seconds=REPOSITORY_LEASE_SECONDS,
            renewal_seconds=self.lease_renewal_seconds,
        ).start()
        try:
            if api is None:
                self._credential_missing(held, run_files_only=run_files_only)
                return 0
            hub = DatasetRepository(repository, api)
            if not run_files_only:
                job = self._claim_job(repository)
                if job is not None:
                    held.hold_job(job)
                    return int(self._deliver_job(hub, held))
                if not self.queue.run_files_due(repository, self.clock()):
                    return 0
            return int(self._deliver_run_files(hub))
        finally:
            held.stop()
            self.queue.release_repository(repository, held.repository_lease)

    def _claim_job(self, repository: str) -> ClaimedJob | None:
        return self.queue.claim_job(
            repository,
            now=self.clock(),
            lease_seconds=REPOSITORY_LEASE_SECONDS,
            kinds=SUPPORTED_JOB_KINDS,
        )

    def _finish_job(
        self,
        job: ClaimedJob,
        *,
        status: str,
        error_code: str | None,
        revision: str | None,
        retry: bool,
        committed: bool = False,
    ) -> None:
        self.queue.finish_job(
            job,
            receipt={
                "handover_id": job.handover_id,
                "status": status,
                "error_code": error_code,
                "revision": revision,
                "attempts": job.attempts + 1,
                "repository": job.target,
                "prefix": f"{job.request['prefix']}/{job.request['run_id']}",
                "copied_files": sorted(job.files),
            },
            retry=retry,
            now=self.clock(),
            committed=committed,
        )

    def _deliver_job(self, hub: DatasetRepository, held: HeldLeases) -> bool:
        assert held.job is not None, "The job is claimed before delivery."
        outcome = stage_bundle(hub, held.job.request, held.job.files)
        if outcome.retry and outcome.error_code is not None:
            self.warn_once(
                outcome.error_code,
                UPLOAD_FAILED_MESSAGE.format(error_type=outcome.error_code),
            )
        held.stop()
        assert held.job is not None
        self._finish_job(
            held.job,
            status=outcome.status,
            error_code=outcome.error_code,
            revision=outcome.revision,
            retry=outcome.retry,
            committed=outcome.committed,
        )
        return outcome.committed

    def _credential_missing(self, held: HeldLeases, *, run_files_only: bool) -> None:
        repository = held.repository
        self.warn_once(CREDENTIAL_MISSING, CREDENTIAL_MISSING_MESSAGE)
        job = None if run_files_only else self._claim_job(repository)
        if job is not None:
            held.hold_job(job)
            held.stop()
            assert held.job is not None
            self._finish_job(
                held.job,
                status="pending",
                error_code=CREDENTIAL_MISSING,
                revision=None,
                retry=True,
            )
            return
        if not run_files_only and not self.queue.run_files_due(
            repository, self.clock()
        ):
            return
        entries = self.queue.pending_run_files(repository)
        index_entries = self.queue.pending_run_index_entries(repository)
        if entries or index_entries:
            self.queue.record_run_files_failed(
                repository,
                entries,
                index_entries,
                error_code=CREDENTIAL_MISSING,
                now=self.clock(),
            )

    def _deliver_run_files(self, hub: DatasetRepository) -> bool:
        entries = self.queue.pending_run_files(hub.repo_id)
        index_entries = self.queue.pending_run_index_entries(hub.repo_id)
        if not entries and not index_entries:
            return False
        try:
            revision = commit_run_files(
                hub,
                entries,
                index_entries,
                updated_at=iso_timestamp(self.clock()),
            )
        except Exception as error:
            # Only the type name: Hub errors can carry URLs and identifiers.
            self.warn_once(
                UPLOAD_FAILED,
                UPLOAD_FAILED_MESSAGE.format(error_type=type(error).__name__),
            )
            self.queue.record_run_files_failed(
                hub.repo_id,
                entries,
                index_entries,
                error_code=UPLOAD_FAILED,
                now=self.clock(),
            )
            return False
        self.queue.record_run_files_committed(
            hub.repo_id, entries, index_entries, revision=revision, now=self.clock()
        )
        return True
