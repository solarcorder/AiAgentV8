"""tenancy, domain schema, RLS, and composite FKs

Revision ID: 0001
Revises:
Create Date: 2026-08-24

Phase 2 of the migration plan (§32): "full domain schema with org_id,
composite FKs, scoped uniqueness, tenants->occupants rename; RLS on
every tenant table; repository layer; the cross-tenant test suite."
This is the single migration the red team's FF-3/RC-5 required to gate
everything downstream — nothing in Phase 3 onward should be built before
this passes the cross-tenant suite (tests/test_tenant_isolation.py) and
the boot assertion (app/db/boot_assertions.py) against a real database.

Table creation delegates to `Base.metadata.create_all()` rather than
hand-written `op.create_table()` calls for every one of the ~20 tables:
the SQLAlchemy models (app/domain/models/*, app/auth/models.py, etc.)
are the single source of truth for columns, composite foreign keys
(§10.3 rule 5), and scoped unique constraints (§10.3 rule 4) — writing
the same shape twice, once in the ORM and once by hand in this file,
is exactly the kind of drift that produced the master spec's own
"documented tool contract contradicts the action registry" defect
(S13). What CANNOT come from the ORM layer — RLS enablement, FORCE ROW
LEVEL SECURITY, the policies themselves, and the worker role's
jobs-only exception — is written explicitly in app/db/rls.py (shared
with tests/conftest.py, which builds the same schema directly against a
disposable test database without going through Alembic).
"""
from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from app.db.base import Base
    from app.db.rls import enable_rls_on_tenant_tables, grant_app_role_privileges, grant_worker_jobs_exception

    import app.domain.models  # noqa: F401  — populates Base.metadata

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)

    enable_rls_on_tenant_tables(bind)
    grant_app_role_privileges(bind)
    grant_worker_jobs_exception(bind)


def downgrade() -> None:
    from app.db.base import Base

    import app.domain.models  # noqa: F401

    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
