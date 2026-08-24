"""
§12.2: Google Drive is deliberately not used for customer documents — own
object storage, keyed `org/{org_id}/...` (§10.3 rule 8). `storage_key` here
is an opaque, generated identifier (e.g. a UUID-based path), never a
human-supplied filename or one derived from occupant/property names — the
red team's A8 finding is that titles and filenames leak PII via error
messages and search autocomplete if they carry real names. Real names
live only in `display_name`, which is itself org-scoped data subject to
RLS like everything else here; the storage key is not.
"""
from __future__ import annotations

import uuid

from sqlalchemy import ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TenantMixin, TimestampMixin, uuid_pk


class Document(Base, TenantMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_documents_org_id_id"),
        UniqueConstraint("storage_key", name="uq_documents_storage_key"),
        ForeignKeyConstraint(
            ["org_id", "property_id"], ["properties.org_id", "properties.id"], name="fk_documents_property_org"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    property_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)  # org/{org_id}/documents/{uuid}
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(127), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
