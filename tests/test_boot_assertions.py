from __future__ import annotations

import pytest

from tests.conftest import requires_db

pytestmark = requires_db


@pytest.mark.asyncio
async def test_boot_assertions_pass_against_the_low_privilege_role(app_engine, schema_ready):
    """
    RC-5: the app must refuse to start if RLS is not enabled+forced with
    at least one policy on every tenant table, or if the connecting role
    has BYPASSRLS/is a superuser. `test_app_role` in conftest.py is
    deliberately constructed to satisfy all of these — this test proves
    the fixture setup and the migration's RLS application (app/db/rls.py)
    actually produce a schema the boot assertion accepts.
    """
    from app.db.boot_assertions import assert_tenant_isolation_invariants

    await assert_tenant_isolation_invariants(app_engine)  # raises TenantIsolationBootError on failure


@pytest.mark.asyncio
async def test_boot_assertions_reject_a_bypassrls_role(schema_ready):
    """The negative case: connecting as a superuser/table-owner role must FAIL the assertion, not silently pass."""
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db.boot_assertions import TenantIsolationBootError, assert_tenant_isolation_invariants
    from tests.conftest import TEST_DATABASE_URL, _async_url

    owner_engine = create_async_engine(_async_url(TEST_DATABASE_URL))
    try:
        with pytest.raises(TenantIsolationBootError):
            await assert_tenant_isolation_invariants(owner_engine)
    finally:
        await owner_engine.dispose()
