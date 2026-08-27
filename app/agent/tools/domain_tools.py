"""
The first tools registered against app/agent/tools/registry.py — until
now the registry and executor (executor.py) were correct and tested but
had nothing to call (see README's "What is deliberately NOT here yet").

`list_properties` is READ-tier: no approval gate, tenant-scoped by
construction (both the explicit org_id filter below AND the RLS policy on
`properties`, same belt-and-braces reasoning as
app/agent/conversation_service.py's own query). It is deliberately the
very first domain tool because it needs no write path, no approval flow,
and no idempotency key — the simplest possible slice that proves the
executor's five steps (re-derive org_id, validate args, check capability,
memoize, dispatch) end to end against a real domain table.

Importing this module registers its tools as a side effect
(`register_tool` calls at module scope) — see app/main.py's lifespan,
which imports it once at startup the same way it imports
app.domain.models before boot_assertions runs.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools.registry import RiskTier, ToolDeclaration, register_tool
from app.domain.models.property import Property, PropertyStatus
from app.tenancy.context import TenantContext

_MAX_LIMIT = 100


class ListPropertiesArgs(BaseModel):
    model_config = {"extra": "forbid"}

    status: Literal["ACTIVE", "INACTIVE", "SOLD"] | None = Field(
        default=None, description="Filter to properties in this status. Omit to return all statuses."
    )
    limit: int = Field(default=20, ge=1, le=_MAX_LIMIT, description="Maximum number of properties to return.")


async def _list_properties(args: ListPropertiesArgs, context: TenantContext, session: AsyncSession) -> list[dict]:
    # Belt-and-braces (§14.4 Layer 1): explicit org_id filter even though
    # this session is already RLS-scoped (Layer 2) to context.org_id — see
    # conversation_service.py's identical reasoning. context.org_id itself
    # is re-derived by the executor from the authenticated session before
    # this function is ever called, never taken from model output.
    query = select(Property).where(Property.org_id == context.org_id, Property.deleted_at.is_(None))
    if args.status is not None:
        query = query.where(Property.status == PropertyStatus(args.status))
    query = query.order_by(Property.created_at).limit(args.limit)

    rows = (await session.execute(query)).scalars().all()

    return [
        {
            "id": str(row.id),
            "external_ref": row.external_ref,
            "address_line1": row.address_line1,
            "address_line2": row.address_line2,
            "city": row.city,
            "region": row.region,
            "country": row.country,
            "postal_code": row.postal_code,
            "status": row.status.value,
        }
        for row in rows
    ]


register_tool(
    ToolDeclaration(
        name="list_properties",
        description="List the organization's properties, optionally filtered by status.",
        args_schema=ListPropertiesArgs,
        read_write="read",
        capability="domain.read",
        risk_tier=RiskTier.READ,
        fn=_list_properties,
    )
)
