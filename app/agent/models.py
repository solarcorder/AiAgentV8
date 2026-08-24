from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKeyConstraint, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, uuid_pk


class OrgBudget(Base, TenantMixin, TimestampMixin):
    """
    RC-4: atomic budget reservation, not the master plan's original
    check-then-call (which the red team showed races under concurrency —
    50 simultaneous requests each read `usage < cap`, all pass, all
    execute; the cap becomes advisory under load). See
    app/agent/budget.py for the `UPDATE ... WHERE spent + est <= cap`
    reservation query this table backs.

    One row per (org_id, period_date): the daily cap resets by inserting
    a fresh row for the new date rather than zeroing a counter, so
    historical spend per day is retained for the billing reconciliation
    described in §23/§18 rather than overwritten.
    """

    __tablename__ = "org_budgets"
    __table_args__ = (UniqueConstraint("org_id", "period_date", name="uq_org_budgets_org_period"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    period_date: Mapped[date] = mapped_column(Date, nullable=False)
    daily_cap_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    spent_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, default=0)


class Conversation(Base, TenantMixin, TimestampMixin):
    """
    §17.5: session keys are `org_id:user_id:conversation_id`, ALL
    server-derived (fixes S3 — the legacy system's
    `'realestate-' + body.session_id`, a client-supplied conversation key
    that let anyone read anyone's history by guessing an ID). `user_id`
    here is the human who owns this conversation; nothing about routing
    to this row is ever taken from client-supplied identifiers other than
    the conversation's own primary key, which is always looked up scoped
    to (org_id, user_id) from the authenticated session first.
    """

    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("org_id", "id", name="uq_conversations_org_id_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    turn_count: Mapped[int] = mapped_column(nullable=False, default=0)


class Message(Base, TenantMixin, TimestampMixin):
    __tablename__ = "messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "conversation_id"],
            ["conversations.org_id", "conversations.id"],
            name="fk_messages_conversation_org",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # system|user|assistant|tool
    # Provenance tag per §18.2 ("tool results tagged with provenance") —
    # lets the agent loop and any audit distinguish first-party domain
    # data from untrusted external content (inbound email, document text,
    # vendor notes) even after both are in conversation history.
    provenance: Mapped[str] = mapped_column(String(32), nullable=False, default="internal")
    content: Mapped[str] = mapped_column(nullable=False)
    tokens_in: Mapped[int | None] = mapped_column(nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cost_estimate_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
