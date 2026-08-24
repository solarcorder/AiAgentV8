from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, uuid_pk


class EventSeverity(str, enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class Event(Base, TenantMixin, TimestampMixin):
    """
    Domain event log. Distinct from `audit_log` (app/core — approvals and
    security-relevant actions) and from application logs (app/logging.py,
    which are not org-scoped rows in this database at all). At 25+
    customers, §14 flags this table for monthly partitioning by
    `timestamp`; not needed at pilot scale.
    """

    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("org_id", "id", name="uq_events_org_id_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    event_category: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[EventSeverity] = mapped_column(
        Enum(EventSeverity, name="event_severity", native_enum=False, validate_strings=True, length=16),
        nullable=False,
        default=EventSeverity.INFO,
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    processed: Mapped[bool] = mapped_column(nullable=False, default=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
