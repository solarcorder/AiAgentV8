"""
Provider resolution — turns "which AI answers this org's request" into a
concrete `ModelProvider` instance.

Resolution rule, per the product decision this implements:

  - Org has 0 connected BYO providers -> fall back to the operator's own
    Gemini key (config), if one is configured. This is the only
    operator-paid path in the system; every other provider is BYO-only,
    so a brand-new org has something to talk to before connecting a key
    of their own, without the operator silently underwriting five
    providers' worth of usage.
  - Org has exactly 1 connected provider -> use it automatically. No
    prompt, no ambiguity.
  - Org has 2+ connected providers -> use `org.default_ai_provider` if
    set; otherwise this is genuinely ambiguous and the caller must pass
    an explicit `requested_provider`. The API layer surfaces this as "you
    have N providers connected, pick one or set a default" rather than
    guessing — silently picking one for the customer here would be the
    same class of mistake as picking money-affecting policy for them.

`org_id` and the caller's own request are the only inputs that decide
which provider is used. Nothing here trusts a provider name embedded in
model output — this module is called by the tool executor / agent loop
BEFORE any model call happens, and its result is fixed for the turn.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.providers.base import ModelProvider
from app.config import AIProvider, get_settings
from app.credentials.models import OrgCredential, OrgIntegration
from app.credentials.vault import CredentialVault
from app.domain.models.org import Org


class NoProviderAvailableError(Exception):
    """No BYO provider connected and no operator fallback configured."""


class AmbiguousProviderError(Exception):
    """2+ providers connected, no default set, and no explicit provider requested."""

    def __init__(self, connected: list[str]) -> None:
        self.connected = connected
        super().__init__(f"multiple providers connected ({connected}) — pick one or set a default")


@dataclass(frozen=True, slots=True)
class ConnectedProviderStatus:
    provider: str
    display_name: str
    byo_connected: bool  # org has its own key stored for this provider
    usable: bool  # byo_connected, OR operator fallback covers it (Gemini only)
    is_default: bool
    supports_search_grounding: bool


async def list_provider_status_for_org(session: AsyncSession, org_id: uuid.UUID) -> list[ConnectedProviderStatus]:
    """
    Every provider the product knows about, with this org's connection
    state — what the "connect a provider" UI renders: connected ones
    highlighted, the rest placeholders. `session` must be a
    tenant_session(org_id) — org_integrations is RLS-scoped like any
    other tenant table.
    """
    from app.config import AI_PROVIDER_METADATA

    connected_rows = await session.execute(
        select(OrgIntegration.provider).where(OrgIntegration.status == "connected")
    )
    connected_names = {row[0] for row in connected_rows.all()}

    org = (await session.execute(select(Org).where(Org.id == org_id))).scalar_one()

    return [
        ConnectedProviderStatus(
            provider=name,
            display_name=meta["display_name"],
            byo_connected=(name in connected_names),
            usable=(name in connected_names) or bool(meta["operator_fallback_available"]),
            is_default=(name == org.default_ai_provider),
            supports_search_grounding=bool(meta["supports_search_grounding"]),
        )
        for name, meta in AI_PROVIDER_METADATA.items()
    ]


async def _connected_byo_providers(session: AsyncSession, org_id: uuid.UUID) -> list[str]:
    result = await session.execute(select(OrgIntegration.provider).where(OrgIntegration.status == "connected"))
    ai_provider_values = {p.value for p in AIProvider}
    return sorted({row[0] for row in result.all() if row[0] in ai_provider_values})


async def _decrypt_org_key(session: AsyncSession, org_id: uuid.UUID, provider: str, vault: CredentialVault) -> str:
    result = await session.execute(
        select(OrgCredential)
        .where(
            OrgCredential.provider == provider,
            OrgCredential.credential_type == "api_key",
            OrgCredential.revoked_at.is_(None),
        )
        .order_by(OrgCredential.created_at.desc())
        .limit(1)
    )
    credential = result.scalar_one_or_none()
    if credential is None:
        raise NoProviderAvailableError(f"org {org_id} has no active '{provider}' credential")
    plaintext = await vault.decrypt(org_id=org_id, credential=credential)
    return plaintext.decode()


def _instantiate(provider: str, *, api_key: str | None) -> ModelProvider:
    # Imported lazily so instantiating one provider doesn't require every
    # provider's SDK to be importable — a deployment missing the OpenAI
    # package, say, can still serve Gemini/Anthropic traffic.
    if provider == AIProvider.GEMINI.value:
        from app.agent.providers.gemini_provider import GeminiProvider

        return GeminiProvider(api_key=api_key)
    if provider == AIProvider.ANTHROPIC.value:
        from app.agent.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key=api_key)
    if provider == AIProvider.OPENAI.value:
        from app.agent.providers.openai_provider import OpenAIProvider

        return OpenAIProvider(api_key=api_key)
    if provider == AIProvider.PERPLEXITY.value:
        from app.agent.providers.perplexity_provider import PerplexityProvider

        return PerplexityProvider(api_key=api_key)
    if provider == AIProvider.DEEPSEEK.value:
        from app.agent.providers.deepseek_provider import DeepSeekProvider

        return DeepSeekProvider(api_key=api_key)
    raise ValueError(f"unknown provider: {provider!r}")


async def resolve_provider_for_org(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    requested_provider: str | None = None,
    vault: CredentialVault | None = None,
) -> ModelProvider:
    vault = vault or CredentialVault()
    settings = get_settings()

    if requested_provider is not None:
        if requested_provider == AIProvider.GEMINI.value:
            connected = await _connected_byo_providers(session, org_id)
            if AIProvider.GEMINI.value in connected:
                api_key = await _decrypt_org_key(session, org_id, AIProvider.GEMINI.value, vault)
                return _instantiate(AIProvider.GEMINI.value, api_key=api_key)
            if settings.gemini_api_key:
                return _instantiate(AIProvider.GEMINI.value, api_key=settings.gemini_api_key)
            raise NoProviderAvailableError("gemini requested but no BYO key connected and no operator fallback configured")
        api_key = await _decrypt_org_key(session, org_id, requested_provider, vault)
        return _instantiate(requested_provider, api_key=api_key)

    connected = await _connected_byo_providers(session, org_id)

    if len(connected) == 0:
        if settings.gemini_api_key:
            return _instantiate(AIProvider.GEMINI.value, api_key=settings.gemini_api_key)
        raise NoProviderAvailableError(f"org {org_id} has no connected AI provider and no operator fallback is configured")

    if len(connected) == 1:
        provider = connected[0]
        api_key = await _decrypt_org_key(session, org_id, provider, vault)
        return _instantiate(provider, api_key=api_key)

    org = (await session.execute(select(Org).where(Org.id == org_id))).scalar_one()
    if org.default_ai_provider and org.default_ai_provider in connected:
        api_key = await _decrypt_org_key(session, org_id, org.default_ai_provider, vault)
        return _instantiate(org.default_ai_provider, api_key=api_key)

    raise AmbiguousProviderError(connected)
