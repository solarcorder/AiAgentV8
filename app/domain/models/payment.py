from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKeyConstraint, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, uuid_pk


class PaymentStatus(str, enum.Enum):
    DUE = "DUE"
    OVERDUE = "OVERDUE"
    PAID = "PAID"
    WAIVED = "WAIVED"


class Payment(Base, TenantMixin, TimestampMixin):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_payments_org_id_id"),
        ForeignKeyConstraint(
            ["org_id", "lease_id"], ["leases.org_id", "leases.id"], name="fk_payments_lease_org"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    lease_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status", native_enum=False, validate_strings=True, length=16),
        nullable=False,
        default=PaymentStatus.DUE,
    )
    # Money: NUMERIC + explicit currency, never float (§14.1) — non-negotiable
    # for a multi-currency portfolio (the source system already spans AED/INR).
    expected_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    due_date: Mapped[date] = mapped_column(nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
