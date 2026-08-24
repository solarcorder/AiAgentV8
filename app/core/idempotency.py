"""
API-level idempotency (§20), with W2's fix applied.

The master plan's original design keyed the cache by
`(org_id, key, request_hash)` but described the intended behaviour as "a
replayed key with a different body is a 409, not a silent overwrite." The
red team pointed out those two statements contradict each other: with the
request hash INSIDE the uniqueness key, a differing body produces a
different key entirely — a cache MISS, which re-executes the request
rather than rejecting it.

Fixed here: the uniqueness key is `(org_id, idempotency_key)` ONLY. The
request hash is stored as a plain column on the same row and compared
after lookup — same key + same hash -> return the cached response;
same key + different hash -> 409; no existing row -> execute and cache.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import DateTime, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import select

from app.db.base import Base, TenantMixin, TimestampMixin, uuid_pk

IDEMPOTENCY_TTL = timedelta(hours=24)


class IdempotentResponse(Base, TenantMixin, TimestampMixin):
    __tablename__ = "idempotent_responses"
    __table_args__ = (UniqueConstraint("org_id", "idempotency_key", name="uq_idempotent_responses_org_key"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IdempotencyConflictError(Exception):
    """Same idempotency key, different request body — 409 per §20."""


@dataclass(frozen=True, slots=True)
class IdempotencyLookup:
    cached_response: tuple[int, dict[str, Any]] | None  # (status, body) if a cached match exists


def hash_request_body(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


async def check_idempotency(
    session: AsyncSession, *, org_id: uuid.UUID, idempotency_key: str, request_hash: str
) -> IdempotencyLookup:
    result = await session.execute(
        select(IdempotentResponse).where(
            IdempotentResponse.org_id == org_id, IdempotentResponse.idempotency_key == idempotency_key
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return IdempotencyLookup(cached_response=None)

    if row.request_hash != request_hash:
        raise IdempotencyConflictError(
            f"idempotency key {idempotency_key!r} was already used with a different request body"
        )

    return IdempotencyLookup(cached_response=(row.response_status, row.response_body))


async def store_idempotent_response(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    idempotency_key: str,
    request_hash: str,
    status_code: int,
    body: dict[str, Any],
) -> None:
    session.add(
        IdempotentResponse(
            org_id=org_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            response_status=status_code,
            response_body=body,
            expires_at=datetime.now(timezone.utc) + IDEMPOTENCY_TTL,
        )
    )
    await session.flush()
