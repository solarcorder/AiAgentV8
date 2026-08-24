from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, uuid_pk


class ApprovalStatus(str, enum.Enum):
    PENDING = "PENDING"
    CONSUMED = "CONSUMED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class Approval(Base, TenantMixin, TimestampMixin):
    """
    §18.3's redesigned approval mechanism, with RC-6's execution-binding
    fix applied. Preserved verbatim from the legacy system because it was
    correct there: payload-hash binding, TTL, single-use, atomic
    compare-and-swap consumption (§14.5).

    Changed from the legacy design per the master spec + red-team review:
      - `token_hash` only. The raw token is CSPRNG-generated
        (secrets.token_urlsafe), returned to a human out-of-band, and
        NEVER placed in a column, a log line, or the model's context
        (fixes S4/S5 and the model-self-approval flaw). See service.py.
      - `requested_by` / `approved_by` are always recorded (fixes the
        legacy system's missing actor identity, T7).
      - Lifecycle has EXECUTING/EXECUTED/FAILED states so "approved but
        the job dead-lettered" (W4) is a visible, queryable state instead
        of a silent gap between "consumed" and "actually happened."
      - `approved_payload_hash` is echoed onto the job row at enqueue
        time (see app/jobs/models.py) so the worker can re-verify
        immediately before the side effect — RC-6's fix for
        approval-to-execution drift.

    `token_hash` is indexed globally, not org-leading (§14.3): a
    consuming human is already authenticated into a specific org before
    this lookup runs (RC-6 requires an authenticated session, never a bare
    emailed link), so the query itself is always issued inside that org's
    tenant_session() — RLS then makes a token belonging to another org
    invisible, giving the SAME "not found" response for "no such token"
    and "token belongs to another org" without any extra code. That
    uniformity is exactly what closes W6 (the approval-token existence
    oracle).
    """

    __tablename__ = "approvals"
    __table_args__ = (
        # Required for tasks.approval_id's composite FK (org_id, approval_id)
        # -> approvals(org_id, id) — §10.3 rule 5. Postgres requires a unique
        # constraint on exactly the referenced column set for a composite FK
        # target, so this is needed even though `id` alone is already unique.
        UniqueConstraint("org_id", "id", name="uq_approvals_org_id_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)  # validated against a closed registry
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256 hex of canonical payload
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)  # sha256 hex, never raw

    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus, name="approval_status", native_enum=False, validate_strings=True, length=16),
        nullable=False,
        default=ApprovalStatus.PENDING,
    )

    requested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
