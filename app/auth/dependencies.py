"""
FastAPI dependencies implementing the authenticate -> resolve org chain.

Session verification is a thin JWKS-based JWT check against whatever
provider is bought (§11: WorkOS AuthKit is the chosen provider — see
.env.example — though Supabase Auth / Clerk would work identically here,
since all three issue a standard JWKS-verifiable JWT). This is
intentionally minimal — the point of buying authentication is not
re-implementing password hashing, MFA, or social login here.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from jose.exceptions import JWTError
from sqlalchemy import select

from app.auth.jwks import JWKSFetchError, verify_jwt
from app.auth.models import User
from app.config import get_settings
from app.db.session import system_session
from app.tenancy.context import NoActiveMembershipError, TenantContext, resolve_tenant_context


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    user_id: uuid.UUID
    subject: str
    email: str


def _extract_token(request: Request) -> str:
    settings = get_settings()
    cookie_token = request.cookies.get(settings.session_cookie_name)
    if cookie_token:
        return cookie_token
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:]
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")


async def get_current_auth_user(request: Request) -> AuthenticatedUser:
    """
    Verifies the session token's signature against the auth provider's
    JWKS and issuer/audience, then resolves it to our local `users` row
    keyed by the provider's subject claim (never trusting any org/role
    claim that might be embedded in the token itself — those come from
    `memberships`, resolved separately, below).
    """
    settings = get_settings()
    token = _extract_token(request)

    if not settings.auth_jwks_url:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="auth provider not configured (set AUTH_JWKS_URL) — see app/config.py",
        )

    try:
        claims = await verify_jwt(
            token,
            jwks_url=settings.auth_jwks_url,
            issuer=settings.auth_issuer,
            audience=settings.auth_audience,
        )
    except (JWTError, JWKSFetchError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid session") from exc

    subject = claims.get("sub")
    email = claims.get("email")
    if not subject or not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid session claims")

    async with system_session() as session:
        result = await session.execute(select(User).where(User.auth_provider_subject == subject))
        user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unknown user")

    return AuthenticatedUser(user_id=user.id, subject=subject, email=email)


async def get_tenant_context(
    auth_user: AuthenticatedUser = Depends(get_current_auth_user),
) -> TenantContext:
    """
    The dependency every tenant-data route uses. Resolves org_id from the
    authenticated user's membership — never from the request. Routes
    should depend on this, not on get_current_auth_user directly, unless
    they are genuinely org-agnostic (e.g. "list my orgs", which does not
    exist in pilot scope per W11).
    """
    try:
        context = await resolve_tenant_context(auth_user.user_id)
    except NoActiveMembershipError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="no active organization membership"
        ) from exc

    # populate logging context vars for this request
    from app.logging import org_id_var, user_id_var

    org_id_var.set(str(context.org_id))
    user_id_var.set(str(context.user_id))

    return context


def require_capability(capability: str):
    """Route dependency factory: `Depends(require_capability("approval.consume"))`."""

    async def _check(context: TenantContext = Depends(get_tenant_context)) -> TenantContext:
        if not context.has_capability(capability):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient capability")
        return context

    return _check
