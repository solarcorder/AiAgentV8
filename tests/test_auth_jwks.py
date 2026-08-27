"""
app/auth/jwks.py — pure logic, no database required. Uses a locally
generated RSA keypair and a monkeypatched httpx.AsyncClient.get instead of
a real IdP, so these tests exercise the fetch/cache/kid-matching/signature
verification behaviour without any network dependency.
"""
from __future__ import annotations

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt
from jose.exceptions import JWTError

from app.auth import jwks as jwks_module
from app.auth.jwks import JWKSFetchError, verify_jwt

JWKS_URL = "https://example-idp.test/.well-known/jwks.json"
KID = "test-key-1"


def _make_keypair() -> tuple[bytes, bytes]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


@pytest.fixture(autouse=True)
def _clear_jwks_cache():
    jwks_module._cache.clear()
    yield
    jwks_module._cache.clear()


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


def _patch_jwks_endpoint(monkeypatch, jwk_dict: dict, *, fail: bool = False) -> None:
    async def _fake_get(self, url, *args, **kwargs):
        if fail:
            raise httpx.ConnectError("simulated network failure", request=httpx.Request("GET", url))
        assert url == JWKS_URL
        return _FakeResponse({"keys": [jwk_dict]})

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)


@pytest.mark.asyncio
async def test_verify_jwt_accepts_a_correctly_signed_token(monkeypatch):
    private_pem, public_pem = _make_keypair()
    public_jwk = jwk.construct(public_pem, algorithm="RS256").to_dict()
    public_jwk["kid"] = KID
    _patch_jwks_endpoint(monkeypatch, public_jwk)

    token = jwt.encode(
        {"sub": "user_123", "email": "a@example.com", "iss": "https://issuer.test", "aud": "aud_123"},
        private_pem,
        algorithm="RS256",
        headers={"kid": KID},
    )

    claims = await verify_jwt(token, jwks_url=JWKS_URL, issuer="https://issuer.test", audience="aud_123")
    assert claims["sub"] == "user_123"


@pytest.mark.asyncio
async def test_verify_jwt_rejects_a_token_signed_by_a_different_key(monkeypatch):
    _, public_pem = _make_keypair()
    other_private_pem, _ = _make_keypair()

    public_jwk = jwk.construct(public_pem, algorithm="RS256").to_dict()
    public_jwk["kid"] = KID
    _patch_jwks_endpoint(monkeypatch, public_jwk)

    # Signed by a DIFFERENT private key than the one whose public half is
    # published under this kid — the signature must not verify.
    token = jwt.encode({"sub": "user_123"}, other_private_pem, algorithm="RS256", headers={"kid": KID})

    with pytest.raises(JWTError):
        await verify_jwt(token, jwks_url=JWKS_URL, issuer=None, audience=None)


@pytest.mark.asyncio
async def test_verify_jwt_rejects_unknown_kid(monkeypatch):
    private_pem, public_pem = _make_keypair()
    public_jwk = jwk.construct(public_pem, algorithm="RS256").to_dict()
    public_jwk["kid"] = KID
    _patch_jwks_endpoint(monkeypatch, public_jwk)

    token = jwt.encode({"sub": "user_123"}, private_pem, algorithm="RS256", headers={"kid": "no-such-kid"})

    with pytest.raises(JWTError):
        await verify_jwt(token, jwks_url=JWKS_URL, issuer=None, audience=None)


@pytest.mark.asyncio
async def test_verify_jwt_raises_fetch_error_on_network_failure(monkeypatch):
    _patch_jwks_endpoint(monkeypatch, {}, fail=True)

    token = jwt.encode({"sub": "user_123"}, "irrelevant-hs256-secret", algorithm="HS256", headers={"kid": KID})

    with pytest.raises(JWKSFetchError):
        await verify_jwt(token, jwks_url=JWKS_URL, issuer=None, audience=None)
