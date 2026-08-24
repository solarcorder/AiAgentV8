"""
Tenant context resolution.

§11's four-step chain, and the reason it is a chain in this specific
order: "authenticate -> resolve org -> filter to org -> authorise action ->
execute." Authorization is meaningless before we know which org's data is
even in scope; a system that authorizes before filtering can leak the
existence of a resource in another tenant via a 403.

`org_id` is NEVER accepted as a parameter to this module. It is looked up,
every time, from (authenticated user_id) -> memberships -> org_id. This is
rule 6 of §10.3 and it is the one invariant most worth re-reading before
touching this file: "No endpoint accepts org_id as a parameter."
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select

from app.auth.models import Membership, MembershipStatus, Role
from app.db.session import system_session


class NoActiveMembershipError(Exception):
    """Raised when an authenticated user has no active org membership."""


@dataclass(frozen=True, slots=True)
class TenantContext:
    org_id: uuid.UUID
    user_id: uuid.UUID
    role: Role

    def has_capability(self, capability: str) -> bool:
        from app.auth.capabilities import role_has_capability

        return role_has_capability(self.role, capability)


async def resolve_tenant_context(user_id: uuid.UUID) -> TenantContext:
    """
    The ONLY place org_id is derived. Reads the memberships table via a
    system_session (no tenant GUC — memberships is deliberately outside
    RLS's scope, see app/auth/models.py) and returns the single active org
    for this user (pilot scope: at most one, per W11).

    An anomaly worth logging as a potential attack signal: if a caller
    anywhere in the codebase ever passes something claiming to be an
    org_id into this function or bypasses it entirely, that is the bug
    class this function exists to make impossible.
    """
    async with system_session() as session:
        result = await session.execute(
            select(Membership).where(
                Membership.user_id == user_id,
                Membership.status == MembershipStatus.ACTIVE,
            )
        )
        membership = result.scalar_one_or_none()

    if membership is None:
        raise NoActiveMembershipError(f"user {user_id} has no active org membership")

    return TenantContext(org_id=membership.org_id, user_id=user_id, role=membership.role)
