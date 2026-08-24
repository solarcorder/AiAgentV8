from __future__ import annotations

import uuid

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TenantMixin, TimestampMixin, uuid_pk


class Vendor(Base, TenantMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "vendors"
    __table_args__ = (UniqueConstraint("org_id", "id", name="uq_vendors_org_id_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Untrusted-content warning (§9 AI-agent red-team results): this field
    # is free text a vendor controls. If it is ever rendered into a model
    # prompt, it MUST go through the same untrusted-content delimiting and
    # tool-free treatment as inbound email — the current system's own
    # prompt already flags vendor `notes` as a known live injection vector.
    notes: Mapped[str | None] = mapped_column(String(4000), nullable=True)
