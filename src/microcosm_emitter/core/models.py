"""SQLAlchemy ORM models for the local telemetry spool."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from microcosm_emitter.core.constants import (
    UPLOAD_STATE_PENDING,
)


class JsonObjectText(TypeDecorator[dict[str, Any]]):
    """Store JSON objects in the spool's existing text-column format."""

    impl = Text
    cache_ok = True

    def process_bind_param(
        self,
        value: dict[str, Any] | None,
        dialect,
    ) -> str | None:
        if value is None:
            return None
        return _serialize_json_object(value)

    def process_result_value(
        self,
        value: str | None,
        dialect,
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        decoded = json.loads(value)
        if not isinstance(decoded, dict):
            raise ValueError("telemetry spool JSON must contain an object")
        return decoded


def serialized_json_length(value: dict[str, Any]) -> int:
    """Return the stored character count for one mapped JSON object."""

    return len(_serialize_json_object(value))


def _serialize_json_object(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


class SpoolModel(DeclarativeBase):
    """Declarative base for the local telemetry spool."""


class TelemetryRunRecord(SpoolModel):
    """One telemetry producer registered for one build run."""

    __tablename__ = "telemetry_runs"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    producer_id: Mapped[str] = mapped_column(Text, primary_key=True)
    registration: Mapped[dict[str, Any]] = mapped_column(
        "registration_json",
        JsonObjectText,
        nullable=False,
    )
    next_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    upload_state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=UPLOAD_STATE_PENDING,
    )
    local_only_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    events: Mapped[list[TelemetryEventRecord]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )


class TelemetryEventRecord(SpoolModel):
    """One ordered telemetry event awaiting collector acknowledgement."""

    __tablename__ = "telemetry_events"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "producer_id",
            "sequence",
            name="uq_telemetry_events_run_producer_sequence",
        ),
        ForeignKeyConstraint(
            ["run_id", "producer_id"],
            ["telemetry_runs.run_id", "telemetry_runs.producer_id"],
            name="fk_telemetry_events_run",
        ),
        Index(
            "telemetry_events_run_sequence",
            "run_id",
            "producer_id",
            "sequence",
        ),
    )

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    producer_id: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        "payload_json",
        JsonObjectText,
        nullable=False,
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    run: Mapped[TelemetryRunRecord] = relationship(back_populates="events")


class GraphPublicationJob(SpoolModel):
    """Durable artifact delivery, independent of event retention and run IDs."""

    __tablename__ = "graph_publication_jobs"
    publication_id: Mapped[str] = mapped_column(Text, primary_key=True)
    inventory: Mapped[dict[str, Any]] = mapped_column(JsonObjectText, nullable=False)
    directory: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[float] = mapped_column(Float, nullable=False)
    lease_until: Mapped[float] = mapped_column(Float, nullable=False)
    receipt: Mapped[dict[str, Any]] = mapped_column(JsonObjectText, nullable=False)


class StagedRunFile(SpoolModel):
    """The latest handed-over bytes for one repository path of a staging run.

    A later hand-over of the same path replaces pending bytes, so each commit
    carries only the newest state. Committed rows keep the committed digest.
    """

    __tablename__ = "staged_run_files"
    repository: Mapped[str] = mapped_column(Text, primary_key=True)
    path_in_repo: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    committed_sha256: Mapped[str | None] = mapped_column(Text)
    pending: Mapped[bool] = mapped_column(Boolean, nullable=False)
    due_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class StagedRunDelivery(SpoolModel):
    """Delivery counts for one staging run, and its pending runs-index entry."""

    __tablename__ = "staged_run_deliveries"
    repository: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    files_attempted: Mapped[int] = mapped_column(Integer, nullable=False)
    files_committed: Mapped[int] = mapped_column(Integer, nullable=False)
    commit_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    commits: Mapped[int] = mapped_column(Integer, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(Text)
    last_commit: Mapped[str | None] = mapped_column(Text)
    # JSON text in the build's key order: the merged runs.json keeps it.
    run_index_entry: Mapped[str | None] = mapped_column(Text)
    run_index_pending: Mapped[bool] = mapped_column(Boolean, nullable=False)
    run_index_due_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class RepositoryCommitSchedule(SpoolModel):
    """When a repository may next receive a commit from this machine."""

    __tablename__ = "repository_commit_schedules"
    repository: Mapped[str] = mapped_column(Text, primary_key=True)
    last_commit_at: Mapped[float] = mapped_column(Float, nullable=False)
    retry_after: Mapped[float] = mapped_column(Float, nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_until: Mapped[float] = mapped_column(Float, nullable=False)


class HandoverJob(SpoolModel):
    """One durable hand-over for one destination, retried until it settles.

    The same hand-over can have a row per destination (today only
    ``huggingface``), so a later destination needs no new table.
    """

    __tablename__ = "handover_jobs"
    handover_id: Mapped[str] = mapped_column(Text, primary_key=True)
    destination: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JsonObjectText, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[float] = mapped_column(Float, nullable=False)
    lease_until: Mapped[float] = mapped_column(Float, nullable=False)
    receipt: Mapped[dict[str, Any]] = mapped_column(JsonObjectText, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
