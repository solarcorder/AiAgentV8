"""
The tenant root. Deliberately NOT a TenantMixin table and NOT subject to
RLS — there is no "org_id" to scope orgs by; orgs IS the scope. Every
other tenant table's org_id FKs into this table.
"""
from __future__ import annotations

import enum
import uuid

from sqlalchemy import Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, uuid_pk


class OrgStatus(str, enum.Enum):
    TRIALING = "trialing"
    ACTIVE = "active"
    GRACE = "grace"
    SUSPENDED = "suspended"
    OFFBOARDING = "offboarding"


class Org(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "orgs"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    country: Mapped[str] = mapped_column(String(2), nullable=False)  # ISO 3166-1 alpha-2
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    default_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    status: Mapped[OrgStatus] = mapped_column(
        Enum(OrgStatus, name="org_status", native_enum=False, validate_strings=True, length=16),
        nullable=False,
        default=OrgStatus.TRIALING,
    )
    # Which connected AI provider auto-routing uses when this org has 2+
    # providers connected (app/agent/providers/registry.py). NULL is valid
    # and means "no default chosen yet" — the registry then requires an
    # explicit provider on the request rather than guessing. Not an FK:
    # values come from the fixed app.config.AI_PROVIDERS list, not a table.
    default_ai_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
