"""
Application entrypoint — the FastAPI modular monolith (§19). Internally
partitioned into auth / tenancy / domain / agent / integrations / billing
/ jobs; one deployable, not microservices (§47's exclusion list).

The lifespan hook is deliberately strict: `assert_tenant_isolation_invariants`
(RC-5) runs before the app starts accepting traffic and the process
refuses to boot if it fails. A silently-open tenant table is worse than
downtime — see app/db/boot_assertions.py.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1 import ai_providers, approvals, conversations, health, properties
from app.config import get_settings
from app.db.boot_assertions import assert_tenant_isolation_invariants
from app.db.session import get_engine
from app.logging import configure_logging
from app.tenancy.middleware import RequestContextMiddleware

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

    # Import every domain model so its table is registered on Base's
    # metadata before boot_assertions queries pg_catalog for it.
    import app.domain.models  # noqa: F401

    await assert_tenant_isolation_invariants(get_engine())
    logger.info("startup complete: tenant isolation invariants verified")

    yield


def create_app() -> FastAPI:
    app = FastAPI(title="AiAgentV8 — Real Estate AI SaaS API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router)
    app.include_router(properties.router)
    app.include_router(approvals.router)
    app.include_router(ai_providers.router)
    app.include_router(conversations.router)

    return app


app = create_app()
