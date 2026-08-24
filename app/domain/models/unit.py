from __future__ import annotations

import enum
import uuid

from sqlalchemy import Enum, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TenantMixin, TimestampMixin, uuid_pk


class UnitStatus(str, enum.Enum):
    OCCUPIED = "OCCUPIED"
    VACANT = "VACANT"
    MAINTENANCE = "MAINTENANCE"


class Unit(Base, TenantMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "units"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_units_org_id_id"),
        ForeignKeyConstraint(
            ["org_id", "property_id"], ["properties.org_id", "properties.id"], name="fk_units_property_org"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    property_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    unit_number: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[UnitStatus] = mapped_column(
        Enum(UnitStatus, name="unit_status", native_enum=False, validate_strings=True, length=16),
        nullable=False,
        default=UnitStatus.VACANT,
    )
