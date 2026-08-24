"""
properties — a parent in several composite foreign keys (units, leases,
maintenance_requests, expenses, documents, tasks all reference a property
within the same org). Postgres requires a unique constraint on exactly the
referenced column set for a composite FK target, so `UniqueConstraint
("org_id", "id")` is added even though `id` alone is already unique — it
is what lets a child declare `FOREIGN KEY (org_id, property_id)
REFERENCES properties (org_id, id)`, which the database then enforces:
a lease cannot reference a property in a different org, full stop,
regardless of what application code asks for (§10.3 rule 5).
"""
from __future__ import annotations

import enum
import uuid

from sqlalchemy import Enum, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TenantMixin, TimestampMixin, uuid_pk


class PropertyStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    SOLD = "SOLD"


class Property(Base, TenantMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "properties"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_properties_org_id_id"),
        UniqueConstraint("org_id", "external_ref", name="uq_properties_org_external_ref"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    external_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    address_line1: Mapped[str] = mapped_column(String(255), nullable=False)
    address_line2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str] = mapped_column(String(120), nullable=False)
    region: Mapped[str | None] = mapped_column(String(120), nullable=True)
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[PropertyStatus] = mapped_column(
        Enum(PropertyStatus, name="property_status", native_enum=False, validate_strings=True, length=16),
        nullable=False,
        default=PropertyStatus.ACTIVE,
    )
