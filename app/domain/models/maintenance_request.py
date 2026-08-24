"""
Ports 30_MAINTENANCE_INTAKE's domain shape, with two of the master spec's
own defects (S6, S7) closed structurally rather than by convention:

  - `priority`/`category` are enum-constrained columns (native_enum=False
    -> VARCHAR + CHECK), not a free-text field a model's JSON *example*
    output gets inserted into unvalidated (S7). The service layer that
    writes this row must validate against MaintenancePriority/Category
    before insert; the CHECK constraint is the backstop when it doesn't.
  - There is deliberately NO column here that lets a model-assigned
    priority directly cause an SMS. See app/agent/ (RC-4/§18.2's "the
    model informs; the policy decides" fix for S6) — this table only
    records what was reported and how it was triaged.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKeyConstraint, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, uuid_pk


class MaintenanceCategory(str, enum.Enum):
    HVAC = "HVAC"
    PLUMBING = "PLUMBING"
    ELECTRICAL = "ELECTRICAL"
    APPLIANCE = "APPLIANCE"
    STRUCTURAL = "STRUCTURAL"
    OTHER = "OTHER"


class MaintenancePriority(str, enum.Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    EMERGENCY = "EMERGENCY"


class MaintenanceStatus(str, enum.Enum):
    OPEN = "OPEN"
    TRIAGED = "TRIAGED"
    DISPATCHED = "DISPATCHED"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class MaintenanceSource(str, enum.Enum):
    INBOUND_EMAIL = "INBOUND_EMAIL"
    MANUAL = "MANUAL"
    OCCUPANT_PORTAL = "OCCUPANT_PORTAL"


class MaintenanceRequest(Base, TenantMixin, TimestampMixin):
    __tablename__ = "maintenance_requests"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_maintenance_requests_org_id_id"),
        ForeignKeyConstraint(
            ["org_id", "property_id"],
            ["properties.org_id", "properties.id"],
            name="fk_maintenance_requests_property_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "occupant_id"],
            ["occupants.org_id", "occupants.id"],
            name="fk_maintenance_requests_occupant_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "vendor_id"], ["vendors.org_id", "vendors.id"], name="fk_maintenance_requests_vendor_org"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    property_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    occupant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)

    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    category: Mapped[MaintenanceCategory] = mapped_column(
        Enum(MaintenanceCategory, name="maintenance_category", native_enum=False, validate_strings=True, length=16),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # Untrusted content: sourced from inbound email in the common case.
    # Never render this into a prompt outside a delimited, tool-free
    # classification call (§18.2).
    description: Mapped[str] = mapped_column(String(8000), nullable=False)
    priority: Mapped[MaintenancePriority] = mapped_column(
        Enum(MaintenancePriority, name="maintenance_priority", native_enum=False, validate_strings=True, length=16),
        nullable=False,
        default=MaintenancePriority.NORMAL,
    )
    status: Mapped[MaintenanceStatus] = mapped_column(
        Enum(MaintenanceStatus, name="maintenance_status", native_enum=False, validate_strings=True, length=16),
        nullable=False,
        default=MaintenanceStatus.OPEN,
    )
    # Range-checked at the service layer per §17.7; stored as NUMERIC(3,2)
    # (0.00-1.00) rather than float for the same "never trust float
    # comparisons at a threshold" reasoning as money.
    ai_confidence: Mapped[Numeric | None] = mapped_column(Numeric(3, 2), nullable=True)
    source: Mapped[MaintenanceSource] = mapped_column(
        Enum(MaintenanceSource, name="maintenance_source", native_enum=False, validate_strings=True, length=24),
        nullable=False,
    )
