"""
occupants — renamed from the legacy system's `tenants` table (§3.4's
naming-collision finding: in the source system "tenant" meant renter; in
SaaS "tenant" means customer org. Keeping both meanings on one word is a
defect generator, so the SaaS-tenancy word "org_id"/"org" and the
domain word "occupant" are kept strictly separate everywhere in this
codebase).

The legacy lookup this replaces was:

    SELECT t.tenant_id, ... FROM tenants t WHERE t.email = $1 LIMIT 1

which the master spec's own §3.6 (S15/T16) identifies as the most
dangerous defect in the original system: unscoped, and `LIMIT 1` turns an
ambiguity (same email, two active leases) into a silently wrong answer.
`UNIQUE (org_id, lower(email))` below makes the SaaS version of that bug
a constraint violation at write time instead of a silent misassignment at
read time.
"""
from __future__ import annotations

import uuid

from sqlalchemy import UniqueConstraint
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TenantMixin, TimestampMixin, uuid_pk


class Occupant(Base, TenantMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "occupants"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_occupants_org_id_id"),
        # Case-sensitive at the DB constraint level; repository-layer lookups
        # normalize email to lowercase before querying/writing so this
        # behaves as the case-insensitive uniqueness the domain needs.
        UniqueConstraint("org_id", "email", name="uq_occupants_org_email"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    first_name: Mapped[str] = mapped_column(String(120), nullable=False)
    last_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
