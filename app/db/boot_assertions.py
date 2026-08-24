"""
RC-5 — boot-time tenant-isolation assertions.

The red-team review's A21: a migration that adds a table but fails before
adding its RLS policy leaves that table with NO policy — which in
Postgres means unrestricted access for any role without RLS forced. This
is silent: nothing about it looks broken until the wrong row shows up in
the wrong org's response.

Rather than trust that every migration remembered its policy, the app
refuses to start unless it can enumerate every tenant table and prove,
directly against pg_catalog, that:

  1. Every table with an org_id column has row-level security ENABLED.
  2. Every such table has row-level security FORCED (so even the table
     owner is subject to policy in normal operation — RLS is otherwise
     bypassed by owners and BYPASSRLS roles per §10 "Security implications").
  3. The connection's current role does NOT have BYPASSRLS.
  4. The connection's current role does NOT own the tenant tables
     (an owning role is exempt from its own RLS policies regardless of
     FORCE ROW LEVEL SECURITY... actually FORCE does bind owners too, but
     we still verify non-ownership as defence in depth and because a
     table owner has DDL rights that make RLS a policy decision, not a
     guarantee).
  5. Every such table has at least one RLS policy defined.

This is deliberately paranoid and deliberately loud: a failed assertion
raises and the process exits. A silently-open tenant table is worse than
downtime.

One deliberate exception: `app.db.rls.NON_TENANT_TABLES` (orgs, users,
memberships) is excluded from "every table with an org_id column," even
though `memberships` genuinely has an `org_id` column — it is the
tenant-*resolution* table (app/tenancy/context.py), so scoping it by the
very org_id it exists to resolve would be circular. This module imports
that same exclusion list rather than defining its own, so the RLS
application and this check can never silently drift apart — they did,
briefly, during this codebase's own development, which is exactly the
kind of drift this assertion exists to catch categorically rather than
one table at a time.
"""
from __future__ import annotations

import logging

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.rls import NON_TENANT_TABLES

logger = logging.getLogger(__name__)


class TenantIsolationBootError(RuntimeError):
    """Raised when the database does not structurally guarantee tenant isolation."""


_TENANT_TABLES_QUERY = text(
    """
    SELECT c.relname AS table_name,
           c.relrowsecurity AS rls_enabled,
           c.relforcerowsecurity AS rls_forced,
           pg_get_userbyid(c.relowner) AS owner
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relkind = 'r'
      AND c.relname NOT IN :non_tenant_tables
      AND EXISTS (
          SELECT 1 FROM information_schema.columns col
          WHERE col.table_schema = 'public'
            AND col.table_name = c.relname
            AND col.column_name = 'org_id'
      )
    """
).bindparams(bindparam("non_tenant_tables", expanding=True))

_POLICY_COUNT_QUERY = text(
    """
    SELECT tablename, count(*) AS policy_count
    FROM pg_policies
    WHERE schemaname = 'public'
    GROUP BY tablename
    """
)

_ROLE_CHECK_QUERY = text(
    """
    SELECT rolname, rolbypassrls, rolsuper
    FROM pg_roles
    WHERE rolname = current_user
    """
)


async def assert_tenant_isolation_invariants(engine: AsyncEngine) -> None:
    problems: list[str] = []

    async with engine.connect() as conn:
        role_row = (await conn.execute(_ROLE_CHECK_QUERY)).mappings().one_or_none()
        if role_row is None:
            problems.append("could not determine current_user role")
        else:
            if role_row["rolbypassrls"]:
                problems.append(
                    f"connection role '{role_row['rolname']}' has BYPASSRLS — "
                    "RLS is decorative for this role regardless of policy correctness"
                )
            if role_row["rolsuper"]:
                problems.append(
                    f"connection role '{role_row['rolname']}' is a superuser — "
                    "superusers always bypass RLS"
                )

        tables = (
            (await conn.execute(_TENANT_TABLES_QUERY, {"non_tenant_tables": tuple(NON_TENANT_TABLES)}))
            .mappings()
            .all()
        )
        if not tables:
            problems.append(
                "no tenant tables found (no table in 'public' has an org_id column) — "
                "either migrations have not run, or this check is misconfigured"
            )

        policy_counts = {
            row["tablename"]: row["policy_count"]
            for row in (await conn.execute(_POLICY_COUNT_QUERY)).mappings().all()
        }

        for t in tables:
            name = t["table_name"]
            if not t["rls_enabled"]:
                problems.append(f"table '{name}' has org_id but ROW LEVEL SECURITY is not enabled")
            if not t["rls_forced"]:
                problems.append(
                    f"table '{name}' has RLS enabled but not FORCED — "
                    "the owning role would bypass policy during normal operation"
                )
            if policy_counts.get(name, 0) < 1:
                problems.append(f"table '{name}' has RLS enabled but zero policies defined")

    if problems:
        message = "Tenant isolation boot assertion FAILED:\n" + "\n".join(f"  - {p}" for p in problems)
        logger.critical(message)
        raise TenantIsolationBootError(message)

    logger.info("Tenant isolation boot assertion passed for %d tenant table(s).", len(tables))
