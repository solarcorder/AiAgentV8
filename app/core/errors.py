"""
RFC 9457 Problem Details, uniformly, for one specific reason beyond
general API hygiene (§20): "Errors must not distinguish 'does not exist'
from 'exists in another tenant' — both are 404." A 403 on a resource that
belongs to another org confirms the resource exists somewhere, which is
itself a leak (§28 cross-tenant suite, test 2). Every handler in this
codebase that looks up a tenant resource by ID should raise
`not_found()` for both cases — never branch on which one it actually was.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

PROBLEM_CONTENT_TYPE = "application/problem+json"


def problem_response(
    *, status_code: int, problem_type: str, title: str, request_id: str, detail: str | None = None
) -> JSONResponse:
    body = {
        "type": problem_type,
        "title": title,
        "status": status_code,
        "request_id": request_id,
    }
    if detail:
        body["detail"] = detail
    return JSONResponse(status_code=status_code, content=body, media_type=PROBLEM_CONTENT_TYPE)


def not_found(request: Request) -> JSONResponse:
    from app.logging import request_id_var

    return problem_response(
        status_code=404,
        problem_type="https://errors.aiagentv8.example/not-found",
        title="Not Found",
        request_id=request_id_var.get(""),
    )
