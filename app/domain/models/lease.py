from __future__ import annotations

import enum
import uuid
from datetime import date

from sqlalchemy import Date, Enum, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TenantMixin, TimestampMixin, uuid_pk


class LeaseStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    ENDED = "ENDED"
    TERMINATED = "TERMINATED"


class Lease(Base, TenantMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "leases"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_leases_org_id_id"),
        ForeignKeyConstraint(
            ["org_id", "property_id"], ["properties.org_id", "properties.id"], name="fk_leases_property_org"
        ),
        ForeignKeyConstraint(
            ["org_id", "unit_id"], ["units.org_id", "units.id"], name="fk_leases_unit_org"
        ),
        ForeignKeyConstraint(
            ["org_id", "occupant_id"], ["occupants.org_id", "occupants.id"], name="fk_leases_occupant_org"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    property_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    unit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    occupant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    status: Mapped[LeaseStatus] = mapped_column(
        Enum(LeaseStatus, name="lease_status", native_enum=False, validate_strings=True, length=16),
        nullable=False,
        default=LeaseStatus.ACTIVE,
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
