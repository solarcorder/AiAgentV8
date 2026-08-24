"""
These tests require a real, disposable PostgreSQL database — RLS,
`FOR UPDATE SKIP LOCKED`, and role-based BYPASSRLS behaviour are exactly
the things a mock or SQLite cannot exercise meaningfully, and the whole
point of §28's cross-tenant suite is proving isolation against the real
enforcement mechanism, not a stand-in for it.

Set `TEST_DATABASE_URL` (a sync psycopg URL, connecting as a role that
CAN create roles/tables — e.g. the Postgres superuser in a throwaway CI
database) to run them. Without it, every test in this package is skipped
rather than failed — a missing test database is an environment gap, not
a code defect, and the suite should say so plainly rather than turning
red for the wrong reason.

Fixtures build a fresh schema per test session using the exact same
`app.db.rls` helpers the real Alembic migration uses (see
migrations/versions/0001), then create a dedicated low-privilege
`test_app_role` (NOSUPERUSER, NOBYPASSRLS, not the table owner) and run
all tenant-scoped assertions through THAT role — never through the
owning/admin connection, which would bypass RLS by definition and prove
nothing.
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")  # sync, e.g. postgresql+psycopg://postgres:postgres@localhost/aiagentv8_test

requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set — skipping tests that require a live Postgres"
)

APP_ROLE = "test_app_role"
APP_ROLE_PASSWORD = "test_app_role_password"


def _async_url(sync_url: str) -> str:
    if sync_url.startswith("postgresql+psycopg://"):
        return sync_url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)
    if sync_url.startswith("postgresql://"):
        return sync_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return sync_url


def _app_role_url(sync_url: str) -> str:
    """
    Swap the connecting role/password in the async URL for the low-privilege
    test role. NOTE: SQLAlchemy's URL.__str__ / str(url) masks the password
    as '***' by default (render_as_string(hide_password=True)) — using
    str(url) here would silently produce a URL asyncpg cannot authenticate
    with. render_as_string(hide_password=False) is required.
    """
    async_url = _async_url(sync_url)
    url = sa.engine.make_url(async_url)
    url = url.set(username=APP_ROLE, password=APP_ROLE_PASSWORD)
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def schema_ready():
    """Builds the full schema + RLS once per test session, using an owner-privileged sync connection."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")

    from app.db.base import Base
    from app.db.rls import enable_rls_on_tenant_tables, grant_worker_jobs_exception

    import app.domain.models  # noqa: F401

    engine = sa.create_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        Base.metadata.drop_all(bind=conn)  # idempotent across repeated local test runs
        Base.metadata.create_all(bind=conn)
        enable_rls_on_tenant_tables(conn)
        grant_worker_jobs_exception(conn)

        if not _role_exists(conn, APP_ROLE):
            conn.execute(sa.text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_ROLE_PASSWORD}' NOSUPERUSER NOBYPASSRLS"))
        conn.execute(sa.text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
        conn.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"))

    engine.dispose()
    yield


def _role_exists(conn: sa.engine.Connection, role_name: str) -> bool:
    return conn.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role_name}).first() is not None


@pytest_asyncio.fixture
async def app_engine(schema_ready):
    """
    Function-scoped (NOT session-scoped) deliberately: pytest-asyncio
    gives each test function its own event loop by default, and an
    asyncpg connection pool created under one loop cannot be reused from
    another — a session-scoped engine here produces exactly the
    confusing "another operation is in progress" InterfaceError this
    comment is warning you away from reintroducing.
    """
    engine = create_async_engine(_app_role_url(TEST_DATABASE_URL))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app_session_factory(app_engine):
    return async_sessionmaker(bind=app_engine, expire_on_commit=False)


@asynccontextmanager
async def tenant_session_as(factory: async_sessionmaker, org_id: uuid.UUID):
    """Test-local equivalent of app/db/session.py's tenant_session(), against the low-privilege test role."""
    async with factory() as session:
        async with session.begin():
            await session.execute(sa.text("SELECT set_config('app.current_org', :org_id, true)"), {"org_id": str(org_id)})
            yield session


@asynccontextmanager
async def no_tenant_session(factory: async_sessionmaker):
    """No GUC set at all — used to assert the fail-closed behaviour on an unset app.current_org."""
    async with factory() as session:
        async with session.begin():
            yield session


@pytest_asyncio.fixture
async def two_orgs(app_session_factory, schema_ready):
    """
    Seeds Org A and Org B, each with a property, an occupant, a lease,
    and a user+membership, via the SAME low-privilege role and the SAME
    tenant_session mechanism the application itself uses — fixture setup
    exercises the real path, not an admin shortcut.
    """
    from app.auth.models import Membership, MembershipStatus, Role, User
    from app.domain.models.occupant import Occupant
    from app.domain.models.property import Property, PropertyStatus

    org_a_id, org_b_id = uuid.uuid4(), uuid.uuid4()

    # orgs/users/memberships are not RLS-scoped — write them via a plain (no-GUC) session.
    async with no_tenant_session(app_session_factory) as session:
        from app.domain.models.org import Org, OrgStatus

        session.add_all(
            [
                Org(id=org_a_id, name="Org A", slug=f"org-a-{org_a_id.hex[:8]}", country="US", status=OrgStatus.ACTIVE),
                Org(id=org_b_id, name="Org B", slug=f"org-b-{org_b_id.hex[:8]}", country="US", status=OrgStatus.ACTIVE),
            ]
        )
        user_a = User(auth_provider_subject=f"sub-a-{uuid.uuid4()}", email=f"a-{uuid.uuid4()}@example.com")
        user_b = User(auth_provider_subject=f"sub-b-{uuid.uuid4()}", email=f"b-{uuid.uuid4()}@example.com")
        session.add_all([user_a, user_b])
        await session.flush()

        session.add_all(
            [
                Membership(org_id=org_a_id, user_id=user_a.id, role=Role.ADMIN, status=MembershipStatus.ACTIVE),
                Membership(org_id=org_b_id, user_id=user_b.id, role=Role.ADMIN, status=MembershipStatus.ACTIVE),
            ]
        )
        await session.flush()
        user_a_id, user_b_id = user_a.id, user_b.id

    property_a_id = uuid.uuid4()
    property_b_id = uuid.uuid4()

    async with tenant_session_as(app_session_factory, org_a_id) as session:
        session.add(
            Property(
                id=property_a_id, org_id=org_a_id, address_line1="1 Org A St", city="Dubai", country="AE"
            )
        )
        await session.flush()

    async with tenant_session_as(app_session_factory, org_b_id) as session:
        session.add(
            Property(
                id=property_b_id, org_id=org_b_id, address_line1="1 Org B St", city="Jaipur", country="IN"
            )
        )
        await session.flush()

    return {
        "org_a_id": org_a_id,
        "org_b_id": org_b_id,
        "user_a_id": user_a_id,
        "user_b_id": user_b_id,
        "property_a_id": property_a_id,
        "property_b_id": property_b_id,
    }
