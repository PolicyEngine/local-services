"""Durable hand-overs, kept in the emitter's spool database.

Two record shapes share one SQLite database (and its Alembic history) with
telemetry and graph jobs:

- **Staging run files** coalesce: each repository path keeps only the newest
  handed-over bytes, so a commit carries the latest state of every pending
  path at once. One commit per repository per cycle replaces Microcosm's one
  ``upload_file`` commit per file.
- **Jobs** (dataset bundles, and later releases) follow the graph queue's
  pattern: a row per hand-over and destination, a lease while a worker holds
  it, retries with backoff, and a receipt.

A per-repository schedule spaces commits and leases the repository to one
writer on this machine at a time.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from microcosm_emitter.core.database import (
    create_spool_engine,
    create_spool_session_factory,
)
from microcosm_emitter.core.migrations import upgrade_spool_database
from microcosm_emitter.core.models import (
    HandoverJob,
    RepositoryCommitSchedule,
    StagedRunDelivery,
    StagedRunFile,
)

#: The shortest time between two commits to one repository from this machine,
#: even for hand-overs that ask to be committed at the next cycle.
MIN_COMMIT_SPACING_SECONDS = 10.0
MAX_RETRY_SECONDS = 300.0
DESTINATION_HUGGING_FACE = "huggingface"
JOB_PENDING = "pending"


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _entry_text(entry: Mapping[str, Any]) -> str:
    """Order-preserving JSON, so the merged index keeps the build's key order."""
    return json.dumps(dict(entry), separators=(",", ":"), allow_nan=False)


def _entry(text: str | None) -> dict[str, Any]:
    return {} if text is None else json.loads(text)


def retry_delay(failures: int) -> float:
    return min(MAX_RETRY_SECONDS, float(2 ** min(failures, 8)))


@dataclass(frozen=True)
class PendingRunFile:
    """The bytes a commit will write at one repository path."""

    path_in_repo: str
    run_id: str
    content: bytes
    sha256: str


@dataclass(frozen=True)
class ClaimedJob:
    """A job leased until ``lease_until``; the value also identifies the lease."""

    handover_id: str
    destination: str
    kind: str
    target: str
    request: dict[str, Any]
    attempts: int
    lease_until: float


class HandoverQueue:
    """Hand-over records, retained until delivered or settled."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_spool_engine(self.path)
        upgrade_spool_database(self._engine)
        self._sessions = create_spool_session_factory(self._engine)

    def close(self) -> None:
        """Release this handle's database connections without deleting records."""
        self._engine.dispose()

    # Repository schedules ------------------------------------------------

    @staticmethod
    def _schedule(session: Session, repository: str) -> RepositoryCommitSchedule:
        schedule = session.get(RepositoryCommitSchedule, repository)
        if schedule is None:
            schedule = RepositoryCommitSchedule(
                repository=repository,
                last_commit_at=0.0,
                retry_after=0.0,
                consecutive_failures=0,
                lease_until=0.0,
            )
            session.add(schedule)
            session.flush()
        return schedule

    def claim_repository(
        self, repository: str, *, now: float, lease_seconds: float
    ) -> float | None:
        """Lease the repository to this writer; ``None`` while another holds it."""
        with self._sessions.begin() as session:
            self._schedule(session, repository)
            lease = now + lease_seconds
            claimed = session.scalar(
                update(RepositoryCommitSchedule)
                .where(
                    RepositoryCommitSchedule.repository == repository,
                    RepositoryCommitSchedule.lease_until <= now,
                )
                .values(lease_until=lease)
                .returning(RepositoryCommitSchedule.repository)
            )
            return lease if claimed is not None else None

    def release_repository(self, repository: str, lease: float) -> None:
        with self._sessions.begin() as session:
            session.execute(
                update(RepositoryCommitSchedule)
                .where(
                    RepositoryCommitSchedule.repository == repository,
                    RepositoryCommitSchedule.lease_until == lease,
                )
                .values(lease_until=0.0)
            )

    def record_commit(self, repository: str, *, now: float) -> None:
        with self._sessions.begin() as session:
            self._schedule(session, repository).last_commit_at = now

    def due_repositories(
        self, now: float, *, kinds: Iterable[str] | None = None
    ) -> list[str]:
        """Repositories with work due now that may receive a commit now.

        ``kinds`` limits jobs to those this emitter can deliver, so a job
        queued by a newer emitter sharing the database waits for it.
        """
        with self._sessions() as session:
            run_file_repositories = set(
                session.scalars(
                    select(StagedRunFile.repository).where(
                        StagedRunFile.pending.is_(True), StagedRunFile.due_at <= now
                    )
                )
            ) | set(
                session.scalars(
                    select(StagedRunDelivery.repository).where(
                        StagedRunDelivery.run_index_pending.is_(True),
                        StagedRunDelivery.run_index_due_at <= now,
                    )
                )
            )
            job_query = select(HandoverJob.target).where(
                HandoverJob.destination == DESTINATION_HUGGING_FACE,
                HandoverJob.status == JOB_PENDING,
                HandoverJob.next_attempt_at <= now,
                HandoverJob.lease_until <= now,
            )
            if kinds is not None:
                job_query = job_query.where(HandoverJob.kind.in_(list(kinds)))
            job_repositories = set(session.scalars(job_query))
            due = []
            for repository in sorted(run_file_repositories | job_repositories):
                schedule = session.get(RepositoryCommitSchedule, repository)
                if schedule is not None and (
                    schedule.lease_until > now
                    or schedule.last_commit_at + MIN_COMMIT_SPACING_SECONDS > now
                ):
                    continue
                run_files_allowed = schedule is None or schedule.retry_after <= now
                if repository in job_repositories or (
                    repository in run_file_repositories and run_files_allowed
                ):
                    due.append(repository)
            return due

    # Staging run files ---------------------------------------------------

    @staticmethod
    def _delivery(
        session: Session, repository: str, run_id: str, now: float
    ) -> StagedRunDelivery:
        delivery = session.get(StagedRunDelivery, (repository, run_id))
        if delivery is None:
            delivery = StagedRunDelivery(
                repository=repository,
                run_id=run_id,
                files_attempted=0,
                files_committed=0,
                commit_attempts=0,
                commits=0,
                last_error_code=None,
                last_commit=None,
                run_index_entry=None,
                run_index_pending=False,
                run_index_due_at=0.0,
                updated_at=now,
            )
            session.add(delivery)
            session.flush()
        return delivery

    def stage_run_files(
        self,
        *,
        repository: str,
        run_id: str,
        files: Sequence[tuple[str, bytes]],
        run_index_entry: Mapping[str, Any] | None,
        flush: bool,
        interval: float,
        now: float,
    ) -> None:
        """Record the newest bytes per path; nothing reaches the Hub here.

        An unforced hand-over is due one ``interval`` after the repository's
        last commit; a forced one (``flush``) at the next cycle.
        """
        with self._sessions.begin() as session:
            schedule = self._schedule(session, repository)
            due_at = now if flush else max(now, schedule.last_commit_at + interval)
            for path_in_repo, content in files:
                digest = sha256_bytes(content)
                row = session.get(StagedRunFile, (repository, path_in_repo))
                if row is None:
                    session.add(
                        StagedRunFile(
                            repository=repository,
                            path_in_repo=path_in_repo,
                            run_id=run_id,
                            content=content,
                            content_sha256=digest,
                            committed_sha256=None,
                            pending=True,
                            due_at=due_at,
                            updated_at=now,
                        )
                    )
                    continue
                if digest == row.committed_sha256:
                    # The repository already holds exactly these bytes.
                    row.pending, row.content = False, b""
                elif not (row.pending and digest == row.content_sha256):
                    row.due_at = min(row.due_at, due_at) if row.pending else due_at
                    row.content, row.pending = content, True
                row.content_sha256 = digest
                row.run_id, row.updated_at = run_id, now
            delivery = self._delivery(session, repository, run_id, now)
            if run_index_entry is not None:
                entry = _entry_text(run_index_entry)
                if delivery.run_index_entry != entry or delivery.run_index_pending:
                    delivery.run_index_due_at = (
                        min(delivery.run_index_due_at, due_at)
                        if delivery.run_index_pending
                        else due_at
                    )
                    delivery.run_index_entry = entry
                    delivery.run_index_pending = True
            if flush:
                session.execute(
                    update(StagedRunFile)
                    .where(
                        StagedRunFile.repository == repository,
                        StagedRunFile.run_id == run_id,
                        StagedRunFile.pending.is_(True),
                        StagedRunFile.due_at > now,
                    )
                    .values(due_at=now)
                )
                if delivery.run_index_pending:
                    delivery.run_index_due_at = min(delivery.run_index_due_at, now)
            delivery.updated_at = now

    def run_files_due(self, repository: str, now: float) -> bool:
        """Whether pending run files are due and past any failure backoff."""
        with self._sessions() as session:
            schedule = session.get(RepositoryCommitSchedule, repository)
            if schedule is not None and schedule.retry_after > now:
                return False
            due_file = session.scalar(
                select(StagedRunFile.path_in_repo)
                .where(
                    StagedRunFile.repository == repository,
                    StagedRunFile.pending.is_(True),
                    StagedRunFile.due_at <= now,
                )
                .limit(1)
            )
            due_index = session.scalar(
                select(StagedRunDelivery.run_id)
                .where(
                    StagedRunDelivery.repository == repository,
                    StagedRunDelivery.run_index_pending.is_(True),
                    StagedRunDelivery.run_index_due_at <= now,
                )
                .limit(1)
            )
            return due_file is not None or due_index is not None

    def repositories_with_pending_run_files(self) -> list[str]:
        with self._sessions() as session:
            return sorted(
                set(
                    session.scalars(
                        select(StagedRunFile.repository).where(
                            StagedRunFile.pending.is_(True)
                        )
                    )
                )
                | set(
                    session.scalars(
                        select(StagedRunDelivery.repository).where(
                            StagedRunDelivery.run_index_pending.is_(True)
                        )
                    )
                )
            )

    def pending_run_files(self, repository: str) -> list[PendingRunFile]:
        with self._sessions() as session:
            rows = session.scalars(
                select(StagedRunFile)
                .where(
                    StagedRunFile.repository == repository,
                    StagedRunFile.pending.is_(True),
                )
                .order_by(StagedRunFile.path_in_repo)
            )
            return [
                PendingRunFile(
                    row.path_in_repo, row.run_id, row.content, row.content_sha256
                )
                for row in rows
            ]

    def pending_run_index_entries(
        self, repository: str
    ) -> list[tuple[str, dict[str, Any]]]:
        with self._sessions() as session:
            rows = session.scalars(
                select(StagedRunDelivery)
                .where(
                    StagedRunDelivery.repository == repository,
                    StagedRunDelivery.run_index_pending.is_(True),
                )
                .order_by(StagedRunDelivery.run_id)
            )
            return [(row.run_id, _entry(row.run_index_entry)) for row in rows]

    @staticmethod
    def _files_per_run(entries: Iterable[PendingRunFile]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in entries:
            counts[entry.run_id] = counts.get(entry.run_id, 0) + 1
        return counts

    def record_run_files_committed(
        self,
        repository: str,
        entries: Sequence[PendingRunFile],
        index_entries: Sequence[tuple[str, Mapping[str, Any]]],
        *,
        revision: str,
        now: float,
    ) -> None:
        """Settle what the commit wrote; newer hand-overs stay pending."""
        with self._sessions.begin() as session:
            for entry in entries:
                row = session.get(StagedRunFile, (repository, entry.path_in_repo))
                if row is None:
                    continue
                row.committed_sha256 = entry.sha256
                if row.content_sha256 == entry.sha256:
                    row.pending, row.content = False, b""
            counts = self._files_per_run(entries)
            index_runs = {run_id for run_id, _ in index_entries}
            for run_id in sorted(set(counts) | index_runs):
                delivery = self._delivery(session, repository, run_id, now)
                delivery.files_attempted += counts.get(run_id, 0)
                delivery.files_committed += counts.get(run_id, 0)
                delivery.commit_attempts += 1
                delivery.commits += 1
                delivery.last_commit = revision
                delivery.last_error_code = None
                delivery.updated_at = now
            for run_id, entry in index_entries:
                delivery = self._delivery(session, repository, run_id, now)
                if delivery.run_index_entry == _entry_text(entry):
                    delivery.run_index_pending = False
            schedule = self._schedule(session, repository)
            schedule.last_commit_at = now
            schedule.consecutive_failures = 0
            schedule.retry_after = 0.0

    def record_run_files_failed(
        self,
        repository: str,
        entries: Sequence[PendingRunFile],
        index_entries: Sequence[tuple[str, Mapping[str, Any]]],
        *,
        error_code: str,
        now: float,
    ) -> None:
        """Keep everything pending and back off before the next attempt."""
        with self._sessions.begin() as session:
            counts = self._files_per_run(entries)
            index_runs = {run_id for run_id, _ in index_entries}
            for run_id in sorted(set(counts) | index_runs):
                delivery = self._delivery(session, repository, run_id, now)
                delivery.files_attempted += counts.get(run_id, 0)
                delivery.commit_attempts += 1
                delivery.last_error_code = error_code
                delivery.updated_at = now
            schedule = self._schedule(session, repository)
            schedule.consecutive_failures += 1
            schedule.retry_after = now + retry_delay(schedule.consecutive_failures)

    def run_receipt(self, repository: str, run_id: str) -> dict[str, Any]:
        with self._sessions() as session:
            delivery = session.get(StagedRunDelivery, (repository, run_id))
            pending = len(
                list(
                    session.scalars(
                        select(StagedRunFile.path_in_repo).where(
                            StagedRunFile.repository == repository,
                            StagedRunFile.run_id == run_id,
                            StagedRunFile.pending.is_(True),
                        )
                    )
                )
            )
            if delivery is not None and delivery.run_index_pending:
                pending += 1
            return {
                "repository": repository,
                "run_id": run_id,
                "pending_files": pending,
                "files_attempted": delivery.files_attempted if delivery else 0,
                "files_committed": delivery.files_committed if delivery else 0,
                "commit_attempts": delivery.commit_attempts if delivery else 0,
                "commits": delivery.commits if delivery else 0,
                "last_error_code": delivery.last_error_code if delivery else None,
                "last_commit": delivery.last_commit if delivery else None,
            }

    def committed_run_files(self, repository: str, run_id: str) -> dict[str, str]:
        """Each committed path of the run and the digest of its committed bytes."""
        with self._sessions() as session:
            rows = session.scalars(
                select(StagedRunFile).where(
                    StagedRunFile.repository == repository,
                    StagedRunFile.run_id == run_id,
                    StagedRunFile.committed_sha256.is_not(None),
                )
            )
            return {row.path_in_repo: str(row.committed_sha256) for row in rows}

    # Jobs ----------------------------------------------------------------

    def enqueue_job(
        self,
        *,
        handover_id: str,
        kind: str,
        target: str,
        request: Mapping[str, Any],
        receipt: Mapping[str, Any],
        now: float,
        destination: str = DESTINATION_HUGGING_FACE,
    ) -> dict[str, Any]:
        """Durably record a hand-over once; repeating it returns its receipt."""
        with self._sessions.begin() as session:
            existing = session.get(HandoverJob, (handover_id, destination))
            if existing is not None:
                if (
                    existing.request != dict(request)
                    or existing.kind != kind
                    or existing.target != target
                ):
                    raise ValueError(
                        "Hand-over identity conflicts with a queued hand-over."
                    )
                return dict(existing.receipt)
            session.add(
                HandoverJob(
                    handover_id=handover_id,
                    destination=destination,
                    kind=kind,
                    target=target,
                    request=dict(request),
                    status=JOB_PENDING,
                    attempts=0,
                    next_attempt_at=0.0,
                    lease_until=0.0,
                    receipt=dict(receipt),
                    created_at=now,
                )
            )
            return dict(receipt)

    def claim_job(
        self,
        target: str,
        *,
        now: float,
        lease_seconds: float,
        destination: str = DESTINATION_HUGGING_FACE,
        kinds: Iterable[str] | None = None,
    ) -> ClaimedJob | None:
        """Lease the earliest-due pending job for ``target``.

        A conditional update allows one current lease; a worker that exits
        leaves its job eligible again once the lease expires.
        """
        with self._sessions.begin() as session:
            statement = select(HandoverJob).where(
                HandoverJob.destination == destination,
                HandoverJob.target == target,
                HandoverJob.status == JOB_PENDING,
                HandoverJob.next_attempt_at <= now,
                HandoverJob.lease_until <= now,
            )
            if kinds is not None:
                statement = statement.where(HandoverJob.kind.in_(list(kinds)))
            candidate = session.scalar(
                statement.order_by(
                    HandoverJob.next_attempt_at, HandoverJob.created_at
                ).limit(1)
            )
            if candidate is None:
                return None
            lease = now + lease_seconds
            claimed = session.scalar(
                update(HandoverJob)
                .where(
                    HandoverJob.handover_id == candidate.handover_id,
                    HandoverJob.destination == destination,
                    HandoverJob.status == JOB_PENDING,
                    HandoverJob.lease_until <= now,
                )
                .values(lease_until=lease)
                .returning(HandoverJob.handover_id)
            )
            if claimed is None:
                return None
            session.refresh(candidate)
            return ClaimedJob(
                handover_id=candidate.handover_id,
                destination=destination,
                kind=candidate.kind,
                target=candidate.target,
                request=dict(candidate.request),
                attempts=candidate.attempts,
                lease_until=lease,
            )

    def finish_job(
        self,
        job: ClaimedJob,
        *,
        receipt: Mapping[str, Any],
        retry: bool,
        now: float,
        committed: bool = False,
    ) -> None:
        """Record one attempt; only the current lease holder may do so."""
        with self._sessions.begin() as session:
            row = session.scalar(
                update(HandoverJob)
                .where(
                    HandoverJob.handover_id == job.handover_id,
                    HandoverJob.destination == job.destination,
                    HandoverJob.status == JOB_PENDING,
                    HandoverJob.lease_until == job.lease_until,
                )
                .values(attempts=HandoverJob.attempts + 1, lease_until=0.0)
                .returning(HandoverJob)
            )
            if row is None:
                return
            row.receipt = dict(receipt)
            if retry:
                row.next_attempt_at = now + retry_delay(row.attempts)
            else:
                row.status = str(receipt["status"])
            if committed:
                self._schedule(session, job.target).last_commit_at = now

    def job_receipt(
        self, handover_id: str, destination: str = DESTINATION_HUGGING_FACE
    ) -> dict[str, Any] | None:
        with self._sessions() as session:
            job = session.get(HandoverJob, (handover_id, destination))
            return None if job is None else dict(job.receipt)


def now_seconds() -> float:
    """Wall-clock seconds, shared by every persisted queue time."""
    return time.time()
