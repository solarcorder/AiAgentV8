from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import ForeignKeyConstraint, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, uuid_pk


class Expense(Base, TenantMixin, TimestampMixin):
    __tablename__ = "expenses"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_expenses_org_id_id"),
        ForeignKeyConstraint(
            ["org_id", "property_id"], ["properties.org_id", "properties.id"], name="fk_expenses_property_org"
        ),
        ForeignKeyConstraint(
            ["org_id", "vendor_id"], ["vendors.org_id", "vendors.id"], name="fk_expenses_vendor_org"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    property_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    incurred_at: Mapped[date] = mapped_column(nullable=False)
