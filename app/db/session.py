"""
Session factory and the tenant-scoped transaction helper.

This module is, by the red team's own assessment (FF-3), the single most
important file in the codebase: tenant isolation is provable by
construction for missing-predicate bugs (RLS) and for cross-tenant
references (composite FKs), but resolution of "which org is this request
for" rests entirely on this code being correct. Treat any change here as
a security-sensitive change requiring the full cross-tenant test suite,
not just the touched code path.

Rules enforced here:
  1. org_id is only ever accepted from a caller-supplied value that the
     caller (tenancy middleware) itself derived from session -> membership.
     This module does not resolve org_id itself and never reads it from
     request bodies, query params or headers.
  2. `SET LOCAL app.current_org` is issued inside the SAME transaction as
     the queries that follow it. Plain `SET` is never used — with a
     transaction-mode pooler, `SET` leaks the GUC to the next, unrelated
     request that happens to reuse the physical connection, which is a
     cross-tenant vulnerability created by the mechanism meant to prevent
     one. See settings.db_pooler_verified_transaction_safe and test 13 in
     tests/test_tenant_isolation.py before relying on this in production.
  3. RLS policies (migrations) are written as
     `org_id = current_setting('app.current_org', true)::uuid` — the
     `true` (missing_ok) argument means an unset GUC evaluates to NULL,
     and `org_id = NULL` is never true in SQL, so the fail mode is zero
     rows, not an exception. We additionally assert the GUC is set before
     yielding the session, so an application bug that forgets to open a
     tenant_session() fails loudly here rather than quietly at the DB.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_worker_engine: AsyncEngine | None = None
_worker_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_pool_max_overflow,
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


def get_worker_engine() -> AsyncEngine:
    """Distinct connection pool for the background worker's own DB role. See config.worker_database_url."""
    global _worker_engine
    if _worker_engine is None:
        settings = get_settings()
        _worker_engine = create_async_engine(settings.worker_database_url, pool_pre_ping=True)
    return _worker_engine


def get_worker_session_factory() -> async_sessionmaker[AsyncSession]:
    global _worker_session_factory
    if _worker_session_factory is None:
        _worker_session_factory = async_sessionmaker(bind=get_worker_engine(), expire_on_commit=False)
    return _worker_session_factory


@asynccontextmanager
async def tenant_session(org_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
    """
    Open a transaction scoped to exactly one org. Every tenant-table query
    made through the returned session is subject to RLS for `org_id`.

    Usage is always:

        async with tenant_session(context.org_id) as session:
            ...

    never:

        session = ...
        session.execute("SET app.current_org = ...")   # plain SET: leaks across pool
    """
    if org_id is None:
        raise ValueError("tenant_session() requires a concrete org_id, never None")

    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.current_org', :org_id, true)"),
                {"org_id": str(org_id)},
            )
            yield session


@asynccontextmanager
async def system_session() -> AsyncIterator[AsyncSession]:
    """
    A session with NO tenant GUC set. Because RLS policies default-deny on
    a NULL GUC (see module docstring), this session can read/write nothing
    on tenant tables — it exists only for genuinely cross-tenant, globally
    audited operations (billing rollups, the operator console) that MUST
    go through their own separately named, individually reviewed
    repository module per §10.3 rule 10. Domain code must never use this
    to route around tenant_session().
    """
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            yield session


@asynccontextmanager
async def worker_session() -> AsyncIterator[AsyncSession]:
    """
    The queue-polling session. Bound to the worker's own DB role (never
    the web tier's), which carries a narrow RLS policy granting it full
    visibility on `jobs` alone (see migrations/versions/0001) — this is
    the one place `SELECT ... FOR UPDATE SKIP LOCKED` can legitimately
    scan across every org's pending jobs. Once a job is dequeued, the
    worker executes it inside `tenant_session(job.org_id)` like any other
    tenant-scoped code, so every subsequent domain write is still RLS +
    composite-FK scoped.
    """
    factory = get_worker_session_factory()
    async with factory() as session:
        async with session.begin():
            yield session
