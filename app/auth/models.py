"""
Identity tables.

§11: authentication is bought, not built (Supabase Auth / Clerk / WorkOS).
`users` stores the local identity keyed by the provider's opaque subject
ID so the provider is replaceable without re-keying the domain model —
nothing else in the schema references the provider directly.

`memberships` is THE tenant-resolution table (§14.2). Every authenticated
request's org_id comes from a row here, never from client input. It is
intentionally NOT a TenantMixin table itself — it is what tenant
resolution is built on top of, and it is queried via system_session()
by the resolver in app/tenancy/context.py, before a tenant_session()
can even be opened.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, uuid_pk


class Role(str, enum.Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class MembershipStatus(str, enum.Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REMOVED = "removed"


class User(Base, TimestampMixin):
    """Global identity. A pilot-scope user belongs to exactly one org (W11)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    auth_provider_subject: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Membership(Base, TimestampMixin):
    """
    (org_id, user_id, role). The one row every request's authorization
    ultimately depends on — see app/tenancy/context.py.

    Pilot scope deliberately omits multi-org membership per W11 in the
    red-team review: the UNIQUE(user_id) constraint below means a user can
    belong to at most one org. This removes both the org-switcher attack
    surface (A3) and the OAuth-callback org-binding race, at the cost of a
    feature no pilot customer needs. Lift the constraint only alongside
    its own isolation tests (see red-team §"FUTURE" item 33).
    """

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("org_id", "user_id", name="uq_memberships_org_user"),
        UniqueConstraint("user_id", name="uq_memberships_single_org_per_user_pilot"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    role: Mapped[Role] = mapped_column(
        Enum(Role, name="membership_role", native_enum=False, validate_strings=True, length=16), nullable=False
    )
    status: Mapped[MembershipStatus] = mapped_column(
        Enum(MembershipStatus, name="membership_status", native_enum=False, validate_strings=True, length=16),
        nullable=False,
        default=MembershipStatus.ACTIVE,
    )


class Invitation(Base, TimestampMixin):
    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[Role] = mapped_column(
        Enum(Role, name="invitation_role", native_enum=False, validate_strings=True, length=16), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)  # sha256 hex, never raw
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
