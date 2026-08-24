"""
§22: a PostgreSQL-backed job queue using `SELECT ... FOR UPDATE SKIP
LOCKED`. Chosen over an external broker specifically because it gives
transactional enqueue — the job row and the business write that caused it
commit together, which is the outbox pattern §14.5 requires ("external
side effects never occur inside a database transaction; write an outbox
row inside the transaction; a worker performs the side effect and marks
it done").

Per the red-team review's simplification list: a separate `outbox` table
was redundant with `jobs` and has been collapsed into it — a job IS an
outbox entry when its type represents an external side effect.
`webhook_events` stays separate because its role is different: a pure
inbound dedupe ledger, not a queue of work to perform.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, uuid_pk


class JobStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"


class Job(Base, TenantMixin, TimestampMixin):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_jobs_org_id_id"),
        UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),
        ForeignKeyConstraint(
            ["org_id", "approval_id"], ["approvals.org_id", "approvals.id"], name="fk_jobs_approval_org"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # Payloads may contain PII (an email body, an occupant name); they
    # must NEVER contain credentials — a job that needs to call a
    # provider stores only a *reference* the worker resolves through
    # app/credentials/vault.py at execution time.
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", native_enum=False, validate_strings=True, length=16),
        nullable=False,
        default=JobStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # RC-6: the approval this job executes, and the payload hash that was
    # approved. The worker re-verifies payload_hash immediately before
    # performing the side effect — this is what closes the
    # approval-to-execution drift window (W4/"can an approved action be
    # altered between approval and execution?").
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    approved_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class WebhookEvent(Base, TimestampMixin):
    """
    Pure inbound dedupe ledger (§34/A10). NOT tenant-scoped — tenant
    resolution for a webhook happens downstream (receiving Twilio number
    -> org; receiving inbound-email address -> org), and this table's job
    is only to answer "have we already enqueued work for this exact
    provider event?" before that resolution has necessarily happened.
    A webhook handler's only writes are here and to `jobs` (§12: "the
    handler does no business logic; it validates and enqueues").
    """

    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("provider", "external_event_id", name="uq_webhook_events_provider_event"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(255), nullable=False)  # Message-ID / MessageSid / event id
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
