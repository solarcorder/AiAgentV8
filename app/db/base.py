"""
Declarative base and shared column mixins.

Conventions, normative per §14.1:
  - UUIDv7-style primary keys (we use uuid4 here; swap for a real uuid7
    generator before production — time-sortable IDs matter for index
    locality at scale, not for correctness of this scaffold).
  - org_id UUID NOT NULL on every tenant-owned table (TenantMixin).
  - All timestamps UTC, TIMESTAMPTZ.
  - Money as NUMERIC(14,2) + explicit currency CHAR(3). Never float.
  - Soft delete via deleted_at, not a boolean — audit needs "when".
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, MetaData, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Named constraint convention so Alembic autogenerate produces stable,
# diffable migration names instead of Postgres's auto-generated ones.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TenantMixin:
    """
    Every tenant-owned table gets this. org_id is NEVER nullable and is
    always the leading column of any composite index or composite FK
    (§10.3 rules 1, 3, 5) — the database itself refuses a cross-tenant
    reference when child tables declare their FKs as (org_id, parent_id)
    -> parent(org_id, id), rather than merely parent_id -> parent(id).
    """

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False, index=True
    )
