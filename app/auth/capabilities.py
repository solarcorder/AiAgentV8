"""
Capability-based authorization.

§11: "Permissions checked as capabilities, not by role-name comparison
scattered through handlers." A handler or tool asks "can this role do
approval.consume?", never "is this role == 'admin'?" — so tightening a
permission is a one-line change here, not a grep across the codebase.
"""
from __future__ import annotations

from app.auth.models import Role

CAPABILITIES: dict[Role, frozenset[str]] = {
    Role.OWNER: frozenset(
        {
            "billing.manage",
            "members.manage",
            "org.delete",
            "settings.manage",
            "integrations.connect",
            "approval.request",
            "approval.consume",
            "approval.revoke",
            "domain.read",
            "domain.write",
        }
    ),
    Role.ADMIN: frozenset(
        {
            "settings.manage",
            "integrations.connect",
            "approval.request",
            "approval.consume",
            "approval.revoke",
            "domain.read",
            "domain.write",
        }
    ),
    Role.MEMBER: frozenset(
        {
            "approval.request",
            "domain.read",
            "domain.write",
        }
    ),
    Role.VIEWER: frozenset(
        {
            "domain.read",
        }
    ),
}


def role_has_capability(role: Role, capability: str) -> bool:
    return capability in CAPABILITIES.get(role, frozenset())
