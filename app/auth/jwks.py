"""
JWKS fetch + cache for verifying auth-provider-issued session tokens
(§11 — buy, don't build). Any JWKS-based OIDC provider works against this
module; it has no provider-specific wire format baked in. WorkOS AuthKit
is the chosen provider (see .env.example's AUTH_JWKS_URL comment) mainly
because its Organizations/Users model maps directly onto this schema's
org_id + memberships design and it's free at pilot/early-growth MAU
volumes — but Supabase Auth or Clerk would plug into this exact same
function, since all three expose a standard JWKS endpoint.

python-jose's `jwt.decode()` takes an actual key (a JWK dict or PEM), not
a URL — fetching the provider's JWKS document, caching it, and picking
the right key by the token's `kid` header is application code's job. The
previous version of app/auth/dependencies.py passed `settings.auth_jwks_url`
straight through as `key=`, which jose does not resolve as a URL; every
token verification would have failed the moment a real provider was
wired up. This module is that missing piece, factored out so it can be
unit-tested against a fake JWKS document without a real IdP.
"""
from __future__ import annotations

import time

import httpx
from jose import jwt
from jose.exceptions import JWTError

# Re-fetch at most this often. A signing key that rotated since the last
# fetch and isn't in the cache yet is handled by the forced one-shot
# refresh in verify_jwt() below, not by shortening this interval.
_CACHE_TTL_SECONDS = 600


class JWKSFetchError(Exception):
    """The JWKS document itself could not be retrieved or parsed."""


class _JWKSCache:
    def __init__(self) -> None:
        self._keys_by_url: dict[str, tuple[float, list[dict]]] = {}

    async def get_keys(self, jwks_url: str, *, force_refresh: bool = False) -> list[dict]:
        cached = self._keys_by_url.get(jwks_url)
        now = time.monotonic()
        if not force_refresh and cached is not None and (now - cached[0]) < _CACHE_TTL_SECONDS:
            return cached[1]

        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                response = await client.get(jwks_url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise JWKSFetchError(f"failed to fetch JWKS from {jwks_url}: {exc}") from exc

        try:
            keys = response.json()["keys"]
        except (ValueError, KeyError) as exc:
            raise JWKSFetchError(f"malformed JWKS document from {jwks_url}") from exc

        self._keys_by_url[jwks_url] = (now, keys)
        return keys

    def clear(self) -> None:
        """Test-only: drop all cached JWKS documents."""
        self._keys_by_url.clear()


_cache = _JWKSCache()


async def verify_jwt(token: str, *, jwks_url: str, issuer: str | None, audience: str | None) -> dict:
    """
    Verifies `token`'s signature against the JWKS at `jwks_url` (keyed by
    the token's `kid` header) plus issuer/audience when configured.
    Raises `jose.exceptions.JWTError` or `JWKSFetchError` on any failure —
    callers map both to 401; there is no partial/soft-accept path.
    """
    try:
        unverified_header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise JWTError(f"malformed token header: {exc}") from exc

    kid = unverified_header.get("kid")
    if not kid:
        raise JWTError("token header missing 'kid'")

    keys = await _cache.get_keys(jwks_url)
    matching_key = next((k for k in keys if k.get("kid") == kid), None)
    if matching_key is None:
        # The signing key may have rotated since our last fetch — refresh
        # once, forced, before giving up. Never silently accept a token
        # whose kid we can't actually match to a key.
        keys = await _cache.get_keys(jwks_url, force_refresh=True)
        matching_key = next((k for k in keys if k.get("kid") == kid), None)
    if matching_key is None:
        raise JWTError(f"no matching JWKS key for kid={kid!r}")

    return jwt.decode(
        token,
        key=matching_key,
        algorithms=[matching_key.get("alg", "RS256")],
        audience=audience,
        issuer=issuer,
        options={"verify_aud": audience is not None},
    )
