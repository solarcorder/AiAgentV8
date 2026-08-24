from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, uuid_pk


class OrgIntegration(Base, TenantMixin, TimestampMixin):
    """Connection *state* per provider — metadata only, never secret material (§14.2)."""

    __tablename__ = "org_integrations"
    __table_args__ = (UniqueConstraint("org_id", "provider", name="uq_org_integrations_org_provider"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    provider: Mapped[str] = mapped_column(String(64), nullable=False)  # "google" | "twilio" | ...
    external_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="connected")  # connected|revoked|error


class OrgCredential(Base, TenantMixin, TimestampMixin):
    """
    Envelope-encrypted secret material. See app/credentials/vault.py for
    the encrypt/decrypt path and app/credentials/kms.py for RC-2's
    per-org-key deletion guarantee.

    Every column here is either non-sensitive metadata or ciphertext.
    Plaintext secret material is never a column, never logged, and never
    held longer than the single outbound call that needs it (§13).
    """

    __tablename__ = "org_credentials"
    __table_args__ = ()

    id: Mapped[uuid.UUID] = uuid_pk()
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_type: Mapped[str] = mapped_column(String(64), nullable=False)  # "oauth_refresh_token" | "api_key"

    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    auth_tag: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    wrapped_data_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
