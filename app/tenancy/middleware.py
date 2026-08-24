"""
Request-scoped observability middleware.

Tenant *resolution* itself lives in app/auth/dependencies.py as a FastAPI
dependency (testable in isolation, composable per-route). This middleware
only stamps a request_id and emits the mandatory §24 log line
(request_id, org_id, user_id, route, latency_ms, status) — org_id/user_id
are populated into contextvars by get_tenant_context() during request
handling, so they show up here even though this middleware runs first.
"""
from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.logging import log_event, new_request_id, org_id_var, request_id_var, user_id_var

logger = logging.getLogger("request")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = new_request_id()
        request_id_var.set(request_id)
        org_id_var.set("")
        user_id_var.set("")

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            latency_ms = round((time.perf_counter() - start) * 1000, 2)
            log_event(
                logger,
                logging.ERROR,
                "request_failed",
                route=request.url.path,
                latency_ms=latency_ms,
                status=500,
            )
            raise

        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        response.headers["x-request-id"] = request_id
        log_event(
            logger,
            logging.INFO,
            "request_completed",
            route=request.url.path,
            latency_ms=latency_ms,
            status=response.status_code,
        )
        return response
