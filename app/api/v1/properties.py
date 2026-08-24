"""
Minimal example route demonstrating the required shape (§20): no `org_id`
anywhere in the path, tenant context comes only from
`Depends(require_capability(...))`, and a lookup miss — whether the
property genuinely doesn't exist or exists in another org — returns the
identical 404 either way (§20 / test 2 in the cross-tenant suite).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import require_capability
from app.db.session import tenant_session
from app.domain.models.property import Property
from app.domain.repository import TenantRepository
from app.tenancy.context import TenantContext

router = APIRouter(prefix="/v1/properties", tags=["properties"])


class PropertyOut(BaseModel):
    id: uuid.UUID
    address_line1: str
    city: str
    country: str
    status: str

    model_config = {"from_attributes": True}


class PropertyRepository(TenantRepository[Property]):
    model = Property


@router.get("", response_model=list[PropertyOut])
async def list_properties(context: TenantContext = Depends(require_capability("domain.read"))):
    async with tenant_session(context.org_id) as session:
        repo = PropertyRepository(session)
        rows = await repo.list_all(context.org_id)
        return [PropertyOut.model_validate(r) for r in rows]


@router.get("/{property_id}", response_model=PropertyOut)
async def get_property(
    property_id: uuid.UUID, context: TenantContext = Depends(require_capability("domain.read"))
):
    async with tenant_session(context.org_id) as session:
        repo = PropertyRepository(session)
        row = await repo.get(context.org_id, property_id)
        if row is None:
            # Not-found-not-forbidden, uniformly (§20). RLS would already
            # make a cross-org row invisible to this query even if the
            # repository filter above were somehow skipped — belt and
            # braces, per §14.4 Layer 1 + Layer 2.
            raise HTTPException(status_code=404, detail="not found")
        return PropertyOut.model_validate(row)
