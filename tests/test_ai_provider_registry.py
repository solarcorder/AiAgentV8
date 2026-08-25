"""
The 0/1/2+ connected-provider resolution logic in
app/agent/providers/registry.py — the rule the client-facing "connect
your own AI provider" system is built on:

  0 connected -> operator Gemini fallback (if configured), else error.
  1 connected -> used automatically, no prompt.
  2+ connected, no default set -> AmbiguousProviderError (must pick or
      set a default, never silently guessed).
  2+ connected, default set -> the default, automatically.
  explicit `requested_provider` -> always wins, provided it's connected.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.agent.providers.anthropic_provider import AnthropicProvider
from app.agent.providers.gemini_provider import GeminiProvider
from app.agent.providers.registry import (
    AmbiguousProviderError,
    NoProviderAvailableError,
    list_provider_status_for_org,
    resolve_provider_for_org,
)
from app.config import get_settings
from app.credentials.kms import LocalFileKMSProvider
from app.credentials.models import OrgCredential, OrgIntegration
from app.credentials.vault import CredentialVault
from app.domain.models.org import Org
from sqlalchemy import select

from tests.conftest import requires_db, tenant_session_as

pytestmark = requires_db


@pytest.fixture
def vault(tmp_path) -> CredentialVault:
    return CredentialVault(kms=LocalFileKMSProvider(str(tmp_path / "kms_keys")))


async def _connect(app_session_factory, vault, org_id, provider, api_key="test-key-value"):
    encrypted = await vault.encrypt(org_id=org_id, plaintext=api_key.encode())
    async with tenant_session_as(app_session_factory, org_id) as session:
        session.add(
            OrgCredential(
                org_id=org_id,
                provider=provider,
                credential_type="api_key",
                ciphertext=encrypted.ciphertext,
                nonce=encrypted.nonce,
                auth_tag=encrypted.auth_tag,
                wrapped_data_key=encrypted.wrapped_data_key,
                key_version=encrypted.key_version,
            )
        )
        await session.execute(
            pg_insert(OrgIntegration)
            .values(org_id=org_id, provider=provider, status="connected")
            .on_conflict_do_update(index_elements=["org_id", "provider"], set_={"status": "connected"})
        )


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_zero_connected_falls_back_to_operator_gemini(app_session_factory, two_orgs, vault, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "operator-fallback-key")
    get_settings.cache_clear()

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        provider = await resolve_provider_for_org(session, org_id=two_orgs["org_a_id"], vault=vault)

    assert isinstance(provider, GeminiProvider)


@pytest.mark.asyncio
async def test_zero_connected_no_operator_key_raises(app_session_factory, two_orgs, vault, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    get_settings.cache_clear()

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        with pytest.raises(NoProviderAvailableError):
            await resolve_provider_for_org(session, org_id=two_orgs["org_a_id"], vault=vault)


@pytest.mark.asyncio
async def test_single_connected_provider_used_automatically(app_session_factory, two_orgs, vault):
    await _connect(app_session_factory, vault, two_orgs["org_a_id"], "anthropic")

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        provider = await resolve_provider_for_org(session, org_id=two_orgs["org_a_id"], vault=vault)

    assert isinstance(provider, AnthropicProvider)


@pytest.mark.asyncio
async def test_two_connected_no_default_is_ambiguous(app_session_factory, two_orgs, vault):
    await _connect(app_session_factory, vault, two_orgs["org_a_id"], "anthropic")
    await _connect(app_session_factory, vault, two_orgs["org_a_id"], "openai")

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        with pytest.raises(AmbiguousProviderError) as exc_info:
            await resolve_provider_for_org(session, org_id=two_orgs["org_a_id"], vault=vault)

    assert set(exc_info.value.connected) == {"anthropic", "openai"}


@pytest.mark.asyncio
async def test_two_connected_with_default_resolves_automatically(app_session_factory, two_orgs, vault):
    await _connect(app_session_factory, vault, two_orgs["org_a_id"], "anthropic")
    await _connect(app_session_factory, vault, two_orgs["org_a_id"], "openai")

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        org = (await session.execute(select(Org).where(Org.id == two_orgs["org_a_id"]))).scalar_one()
        org.default_ai_provider = "anthropic"

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        provider = await resolve_provider_for_org(session, org_id=two_orgs["org_a_id"], vault=vault)

    assert isinstance(provider, AnthropicProvider)


@pytest.mark.asyncio
async def test_explicit_requested_provider_overrides_default(app_session_factory, two_orgs, vault):
    await _connect(app_session_factory, vault, two_orgs["org_a_id"], "anthropic")
    await _connect(app_session_factory, vault, two_orgs["org_a_id"], "openai")

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        org = (await session.execute(select(Org).where(Org.id == two_orgs["org_a_id"]))).scalar_one()
        org.default_ai_provider = "openai"

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        provider = await resolve_provider_for_org(
            session, org_id=two_orgs["org_a_id"], requested_provider="anthropic", vault=vault
        )

    assert isinstance(provider, AnthropicProvider)


@pytest.mark.asyncio
async def test_org_b_sees_none_of_org_a_connected_providers(app_session_factory, two_orgs, vault, monkeypatch):
    """The cross-tenant property that actually matters here: connecting a provider in org A must not leak into org B."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    get_settings.cache_clear()

    await _connect(app_session_factory, vault, two_orgs["org_a_id"], "anthropic")

    async with tenant_session_as(app_session_factory, two_orgs["org_b_id"]) as session:
        with pytest.raises(NoProviderAvailableError):
            await resolve_provider_for_org(session, org_id=two_orgs["org_b_id"], vault=vault)


@pytest.mark.asyncio
async def test_list_status_reflects_connected_and_default(app_session_factory, two_orgs, vault, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "operator-fallback-key")
    get_settings.cache_clear()

    await _connect(app_session_factory, vault, two_orgs["org_a_id"], "anthropic")

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        org = (await session.execute(select(Org).where(Org.id == two_orgs["org_a_id"]))).scalar_one()
        org.default_ai_provider = "anthropic"

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        statuses = await list_provider_status_for_org(session, two_orgs["org_a_id"])

    by_provider = {s.provider: s for s in statuses}

    assert by_provider["anthropic"].byo_connected is True
    assert by_provider["anthropic"].usable is True
    assert by_provider["anthropic"].is_default is True

    assert by_provider["openai"].byo_connected is False
    assert by_provider["openai"].usable is False  # no operator fallback for OpenAI

    assert by_provider["gemini"].byo_connected is False
    assert by_provider["gemini"].usable is True  # operator fallback covers it even without a BYO key
