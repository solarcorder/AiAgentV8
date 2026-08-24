"""
Shared RLS-application logic, factored out of migrations/versions/0001 so
it has one normal, importable home — both the migration and
tests/conftest.py (which builds a schema directly against a disposable
test database, without going through Alembic) call the same code. See
migrations/versions/0001_tenancy_and_domain.py for the full rationale.
"""
from __future__ import annotations

import sqlalchemy as sa

# See migrations/versions/0001 for why these three are excluded.
NON_TENANT_TABLES = {"orgs", "users", "memberships"}


def enable_rls_on_tenant_tables(bind: sa.engine.Connection) -> list[str]:
    """
    The policy predicate is `org_id = NULLIF(current_setting('app.current_org',
    true), '')::uuid`, not the simpler `current_setting(..., true)::uuid`
    it might look like it should be. The difference matters and was found
    by this codebase's own test suite, not reasoned out in advance —
    exactly the kind of pooler-adjacent subtlety §10 warns to verify
    rather than assume:

    `app.current_org` is a "custom" GUC (not declared via
    postgresql.conf/an extension), and Postgres only starts answering
    `current_setting(..., true)` with NULL for one that has NEVER been
    set on that physical connection. Once *any* transaction on that
    connection has called `set_config('app.current_org', ..., true)`
    (i.e. SET LOCAL) even once, Postgres materializes the GUC for the
    rest of the connection's life — and when a later transaction's
    LOCAL scope ends without ever setting it again, the value reverts
    to `''` (empty string), NOT back to NULL. `''::uuid` is a hard
    error, not a clean "no match." A connection pool that reuses
    physical connections across requests — exactly the transaction-mode
    pooler scenario app/db/session.py's tenant_session() docstring
    already flags — will hit this on any request that doesn't open a
    tenant_session() after one that did. `NULLIF(..., '')` collapses
    that reverted-empty-string case back to NULL before the cast, so
    "never set" and "set earlier on this connection, not set now" both
    fail closed to zero rows, identically, instead of one of them
    raising a 500.
    """
    rows = bind.execute(
        sa.text(
            """
            SELECT table_name FROM information_schema.columns
            WHERE table_schema = 'public' AND column_name = 'org_id'
            """
        )
    ).fetchall()
    tenant_tables = sorted({r[0] for r in rows} - NON_TENANT_TABLES)

    for table in tenant_tables:
        bind.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        bind.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
        bind.execute(
            sa.text(
                f'CREATE POLICY {table}_tenant_isolation ON "{table}" '
                f"USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid) "
                f"WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)"
            )
        )
    return tenant_tables


def grant_app_role_privileges(bind: sa.engine.Connection, *, role_name: str = "app_user") -> None:
    """
    §10: the request-serving role must be able to read/write every table
    (RLS is what scopes it, not table-level GRANTs) but must NOT own any
    table and must NOT have BYPASSRLS — ownership and BYPASSRLS are
    checked separately by app/db/boot_assertions.py. This grants
    row-level-security-subject CRUD only. Role creation is idempotent
    (mirroring grant_worker_jobs_exception below) so this migration is
    self-sufficient in a fresh CI database even before scripts/
    create_roles.sql has been run by hand — but production should
    provision `app_user` via IAM/role management with its own rotated
    password, not the placeholder here.
    """
    bind.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role_name}') THEN
                    CREATE ROLE {role_name} LOGIN PASSWORD 'CHANGE_ME_IN_PRODUCTION' NOSUPERUSER NOBYPASSRLS;
                END IF;
            END
            $$;
            """
        )
    )
    bind.execute(sa.text(f"GRANT USAGE ON SCHEMA public TO {role_name}"))
    bind.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role_name}"))
    bind.execute(sa.text(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {role_name}"))


def grant_worker_jobs_exception(bind: sa.engine.Connection, *, role_name: str = "worker_role") -> None:
    """§10.3 rule 10 exception for background-worker polling of `jobs` across every org. See module docstring."""
    bind.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role_name}') THEN
                    CREATE ROLE {role_name} LOGIN PASSWORD 'CHANGE_ME_IN_PRODUCTION';
                END IF;
            END
            $$;
            """
        )
    )
    bind.execute(sa.text(f"GRANT SELECT, UPDATE ON jobs TO {role_name}"))
    bind.execute(
        sa.text(
            f"CREATE POLICY jobs_worker_full_access ON jobs FOR ALL TO {role_name} USING (true) WITH CHECK (true)"
        )
    )
