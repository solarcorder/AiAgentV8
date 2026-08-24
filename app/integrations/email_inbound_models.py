"""
Models backing RC-3. Kept alongside email_inbound.py rather than under
app/domain/ because these are integration/security-plumbing tables
(inbound routing, quarantine), not portfolio domain data — but they are
still tenant-owned once an org is known, so they still take TenantMixin
and are still subject to RLS + the boot assertion like everything else.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, uuid_pk


class InboundEmailAddress(Base, TenantMixin, TimestampMixin):
    """
    RC-3: `{32-char-random}@inbound.…` — no org identifier in the address
    itself (unlike the master plan's original `{org-slug}-{random}@...`,
    which the red team showed appears in forwarded headers, mail rules,
    support threads and bounce messages and is therefore guessable/
    harvestable). The address is treated as a bearer secret: rotatable,
    with the old address kept valid for an overlap window
    (settings.inbound_address_rotation_overlap_days) so a mid-flight
    forwarding-rule update doesn't drop mail.
    """

    __tablename__ = "inbound_email_addresses"
    __table_args__ = (UniqueConstraint("address", name="uq_inbound_email_addresses_address"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    address: Mapped[str] = mapped_column(String(128), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)


class QuarantinedMessage(Base, TenantMixin, TimestampMixin):
    """
    RC-3's fail-closed path: mail that resolves to a real org's inbound
    address but fails sender authentication lands here — visible in that
    org's UI, but explicitly NOT a maintenance ticket, NOT passed to any
    tool-enabled model call, NOT a trigger for SMS, and NOT auto-replied
    to (closes S6 and S12 in one structural move: nothing downstream of
    this table can cause cost or backscatter).
    """

    __tablename__ = "quarantined_messages"

    id: Mapped[uuid.UUID] = uuid_pk()
    address_used: Mapped[str] = mapped_column(String(128), nullable=False)
    from_header: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(998), nullable=True)  # RFC 5322 header length cap
    body_preview: Mapped[str] = mapped_column(String(2000), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)  # e.g. "dkim_not_aligned", "no_arc_chain"
    reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
