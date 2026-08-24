"""
Repository base — §14.4 Layer 1 of the four defence-in-depth layers.

The red team's FF-3 correction is honoured here explicitly: this layer
and RLS (Layer 2) both depend on the SAME upstream value — the org_id the
caller passes in — so they are not independent of each other. They ARE
independent of the composite-FK layer (Layer 3), which is why that layer
is not weakened by a bug here. This class exists so "forgot a WHERE
org_id = ..." becomes structurally impossible to write, which is what
Layer 1 actually buys: not independence from Layer 2, but elimination of
the most common way Layer 2 would otherwise need to catch a mistake.

Every repository method takes `org_id` as its first argument and every
query it builds includes an explicit `.where(Model.org_id == org_id)`
filter — belt-and-braces with RLS, never a substitute for it, and the
session passed in must already be a tenant_session() so RLS is active
regardless.
"""
from __future__ import annotations

import uuid
from typing import Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class TenantRepository(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, org_id: uuid.UUID, entity_id: uuid.UUID) -> ModelT | None:
        result = await self.session.execute(
            select(self.model).where(self.model.org_id == org_id, self.model.id == entity_id)  # type: ignore[attr-defined]
        )
        return result.scalar_one_or_none()

    async def list_all(self, org_id: uuid.UUID, *, limit: int = 50, offset: int = 0) -> list[ModelT]:
        result = await self.session.execute(
            select(self.model).where(self.model.org_id == org_id).limit(limit).offset(offset)  # type: ignore[attr-defined]
        )
        return list(result.scalars().all())

    async def add(self, org_id: uuid.UUID, entity: ModelT) -> ModelT:
        if entity.org_id != org_id:  # type: ignore[attr-defined]
            raise ValueError("entity.org_id does not match the repository's org_id — refusing to write")
        self.session.add(entity)
        await self.session.flush()
        return entity
