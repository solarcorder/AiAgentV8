"""
Connect / list / disconnect a BYO AI-provider key, and set the org's
default when 2+ are connected. This is the API behind the "highlighted
vs. placeholder" provider picker: `GET` returns every provider the
product knows about with this org's real connection state, so the
frontend never has to hardcode which providers exist.

Capability: `integrations.connect` (owner/admin only, per
app/auth/capabilities.py) gates connect/disconnect/set-default — the
same capability §12 already uses for Google/Twilio. Listing status uses
the broader `domain.read` since it's informational, not a mutation.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.agent.providers.registry import list_provider_status_for_org
from app.agent.providers.validation import ProviderKeyValidationError, validate_provider_key
from app.auth.dependencies import require_capability
from app.config import AI_PROVIDER_METADATA
from app.credentials.models import OrgCredential, OrgIntegration
from app.credentials.vault import CredentialVault
from app.db.session import tenant_session
from app.domain.models.org import Org
from app.tenancy.context import TenantContext

router = APIRouter(prefix="/v1/ai-providers", tags=["ai-providers"])


class ProviderStatusOut(BaseModel):
    provider: str
    display_name: str
    byo_connected: bool
    usable: bool
    is_default: bool
    supports_search_grounding: bool


class ConnectProviderRequest(BaseModel):
    model_config = {"extra": "forbid"}

    api_key: str


class SetDefaultProviderRequest(BaseModel):
    model_config = {"extra": "forbid"}

    provider: str


def _require_known_provider(provider: str) -> None:
    if provider not in AI_PROVIDER_METADATA:
        raise HTTPException(status_code=404, detail=f"unknown provider '{provider}'")


@router.get("", response_model=list[ProviderStatusOut])
async def list_ai_providers(context: TenantContext = Depends(require_capability("domain.read"))):
    async with tenant_session(context.org_id) as session:
        statuses = await list_provider_status_for_org(session, context.org_id)
    return [ProviderStatusOut(**s.__dict__) for s in statuses]


@router.post("/{provider}/connect", response_model=ProviderStatusOut)
async def connect_ai_provider(
    provider: str,
    body: ConnectProviderRequest,
    context: TenantContext = Depends(require_capability("integrations.connect")),
):
    _require_known_provider(provider)

    key_prefix_hint = AI_PROVIDER_METADATA[provider]["key_prefix_hint"]
    if key_prefix_hint and not body.api_key.startswith(key_prefix_hint):
        # A hint, not a security control (see app/config.py) — catches an
        # obvious paste error early, same "test before it's live" spirit
        # as the onboarding flow's test-send/test-email checks elsewhere.
        raise HTTPException(
            status_code=422, detail=f"'{provider}' keys are expected to start with '{key_prefix_hint}'"
        )

    # The real check: one minimal, real, read-only call against the
    # provider's own API (app/agent/providers/validation.py). Only a
    # confirmed authentication failure blocks the connect — an ambiguous
    # error (network blip, unsupported probe endpoint) does not, so a
    # legitimate key is never refused because of that.
    try:
        await validate_provider_key(provider, body.api_key)
    except ProviderKeyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    vault = CredentialVault()
    encrypted = await vault.encrypt(org_id=context.org_id, plaintext=body.api_key.encode())

    async with tenant_session(context.org_id) as session:
        # Reconnecting with a new key supersedes the old one — soft-revoke
        # rather than delete, so an audit trail survives (consistent with
        # how OAuth token revocation is handled elsewhere in this design;
        # see §12.5). The old ciphertext becomes unreachable in practice
        # because registry.py's lookup filters revoked_at IS NULL, without
        # destroying the org's KMS key — that stays reserved for full org
        # offboarding (§29), not a single credential swap.
        await session.execute(
            update(OrgCredential)
            .where(
                OrgCredential.provider == provider,
                OrgCredential.credential_type == "api_key",
                OrgCredential.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(timezone.utc))
        )

        session.add(
            OrgCredential(
                org_id=context.org_id,
                provider=provider,
                credential_type="api_key",
                ciphertext=encrypted.ciphertext,
                nonce=encrypted.nonce,
                auth_tag=encrypted.auth_tag,
                wrapped_data_key=encrypted.wrapped_data_key,
                key_version=encrypted.key_version,
            )
        )

        upsert = (
            pg_insert(OrgIntegration)
            .values(org_id=context.org_id, provider=provider, status="connected")
            .on_conflict_do_update(
                index_elements=["org_id", "provider"],
                set_={"status": "connected"},
            )
        )
        await session.execute(upsert)

        statuses = await list_provider_status_for_org(session, context.org_id)

    return next(ProviderStatusOut(**s.__dict__) for s in statuses if s.provider == provider)


@router.delete("/{provider}", response_model=ProviderStatusOut)
async def disconnect_ai_provider(
    provider: str, context: TenantContext = Depends(require_capability("integrations.connect"))
):
    _require_known_provider(provider)

    async with tenant_session(context.org_id) as session:
        await session.execute(
            update(OrgCredential)
            .where(
                OrgCredential.provider == provider,
                OrgCredential.credential_type == "api_key",
                OrgCredential.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(timezone.utc))
        )
        await session.execute(
            update(OrgIntegration).where(OrgIntegration.provider == provider).values(status="revoked")
        )

        org = (await session.execute(select(Org).where(Org.id == context.org_id))).scalar_one()
        if org.default_ai_provider == provider:
            # The default just went away — clear it rather than silently
            # keep pointing auto-routing at a now-disconnected provider
            # (app/agent/providers/registry.py would otherwise raise
            # AmbiguousProviderError or NoProviderAvailableError with a
            # confusing cause the next time a request comes in).
            org.default_ai_provider = None

        statuses = await list_provider_status_for_org(session, context.org_id)

    return next(ProviderStatusOut(**s.__dict__) for s in statuses if s.provider == provider)


@router.patch("/default", response_model=ProviderStatusOut)
async def set_default_ai_provider(
    body: SetDefaultProviderRequest, context: TenantContext = Depends(require_capability("integrations.connect"))
):
    _require_known_provider(body.provider)

    async with tenant_session(context.org_id) as session:
        statuses = await list_provider_status_for_org(session, context.org_id)
        target = next((s for s in statuses if s.provider == body.provider), None)
        if target is None or not target.usable:
            raise HTTPException(
                status_code=422, detail=f"'{body.provider}' is not connected — connect it before setting it as default"
            )

        org = (await session.execute(select(Org).where(Org.id == context.org_id))).scalar_one()
        org.default_ai_provider = body.provider

        statuses = await list_provider_status_for_org(session, context.org_id)

    return next(ProviderStatusOut(**s.__dict__) for s in statuses if s.provider == body.provider)
