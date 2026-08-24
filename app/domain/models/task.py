"""
tasks — general work items, distinct from the approvals mechanism (§18.3,
app/approvals/). The legacy schema stored approval_token/payload_hash/
token_expires_at directly on `tasks`; that design let two producers
disagree about whether a row was actually resolvable (S11: some rows
had required_approval=true with no token at all, permanently stuck).
Here, a task that needs approval points at an `approvals` row via
`approval_id` — approvals is the single producer of approval state, and
a task with required_approval=true and approval_id=NULL is a visible,
queryable bug rather than a silently stuck row.
"""
from __future__ import annotations

import enum
import uuid

from sqlalchemy import Boolean, Enum, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, uuid_pk


class TaskPriority(str, enum.Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"


class TaskStatus(str, enum.Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


class Task(Base, TenantMixin, TimestampMixin):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_tasks_org_id_id"),
        ForeignKeyConstraint(
            ["org_id", "property_id"], ["properties.org_id", "properties.id"], name="fk_tasks_property_org"
        ),
        ForeignKeyConstraint(
            ["org_id", "approval_id"], ["approvals.org_id", "approvals.id"], name="fk_tasks_approval_org"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    property_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    priority: Mapped[TaskPriority] = mapped_column(
        Enum(TaskPriority, name="task_priority", native_enum=False, validate_strings=True, length=16),
        nullable=False,
        default=TaskPriority.NORMAL,
    )
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status", native_enum=False, validate_strings=True, length=16),
        nullable=False,
        default=TaskStatus.OPEN,
    )
    required_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
