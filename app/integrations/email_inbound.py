"""
RC-3 — inbound email intake with sender authentication, replacing the
master plan's §12.1 design after the red team's FF-4 finding.

FF-4, restated because it is the sharpest single point in the whole
review: the master plan diagnosed the LEGACY system's spoofable-sender
auto-reply (S12) and AI-triggered SMS from attacker-controlled email text
(S6) as defects — then specified a TARGET intake architecture that
matches occupants by an unauthenticated `From:` header, with no sender
authentication, and cannot fix this with SPF because **mail forwarding
breaks SPF by construction** (the forwarding server is not authorized to
send for the original domain, so forwarded mail fails SPF regardless of
whether the original sender was legitimate). The defect would have
survived the rewrite unchanged.

The fix does NOT evaluate SPF. It consumes the inbound provider's own
authentication results and requires ONE of:
  - DKIM aligned to the sender's domain (DKIM signatures generally
    survive forwarding, unlike SPF), or
  - a valid ARC chain from the forwarding hop.

Anything that fails both authenticates as nothing and is quarantined —
never processed as a ticket, never shown to a tool-enabled model, never
allowed to trigger SMS, never auto-replied to.
"""
from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.email_inbound_models import InboundEmailAddress, QuarantinedMessage
from app.jobs.queue import enqueue


@dataclass(frozen=True, slots=True)
class ProviderAuthResults:
    """What the inbound email provider (Postmark/Mailgun/Cloudflare) reports, NOT self-evaluated SPF."""

    dkim_aligned: bool
    arc_chain_valid: bool


@dataclass(frozen=True, slots=True)
class InboundEmailPayload:
    to_address: str
    from_header: str
    subject: str | None
    body: str
    message_id: str
    auth: ProviderAuthResults
    size_bytes: int


class MessageTooLargeError(Exception):
    pass


MAX_INBOUND_MESSAGE_BYTES = 10 * 1024 * 1024  # body+attachment cap — A "cheap DoS" gap the red team flagged


def generate_inbound_address(domain: str) -> str:
    """No org identifier embedded — see InboundEmailAddress docstring."""
    token = secrets.token_urlsafe(24).replace("-", "").replace("_", "")[:32]
    return f"{token}@{domain}"


def sender_is_authenticated(auth: ProviderAuthResults) -> bool:
    return auth.dkim_aligned or auth.arc_chain_valid


async def _resolve_org_id(session: AsyncSession, address: str) -> uuid.UUID | None:
    """
    Looked up WITHOUT tenant scoping (we don't know the org yet — that's
    what this resolves) via the address's own uniqueness. Callers must
    run this inside a session capable of seeing inbound_email_addresses
    across orgs for exactly this lookup — analogous to the jobs worker's
    cross-org polling exception (§10.3 rule 10), and equally narrow: it
    answers exactly one question, "which org does this address belong
    to," and nothing else.
    """
    result = await session.execute(
        select(InboundEmailAddress.org_id).where(
            InboundEmailAddress.address == address, InboundEmailAddress.active.is_(True)
        )
    )
    row = result.first()
    return row[0] if row else None


async def handle_inbound_email(session: AsyncSession, payload: InboundEmailPayload) -> str:
    """
    Returns "queued" (routed to maintenance intake as a job) or
    "quarantined". Raises MessageTooLargeError before touching the
    database at all for an oversized payload — cheap rejection before any
    model or storage cost is incurred.

    `session` here must already be `tenant_session(org_id)` scoped to the
    org resolved by the caller via `_resolve_org_id` against a
    cross-org-capable connection — see the note on that function. This
    function itself only performs org-scoped writes.
    """
    if payload.size_bytes > MAX_INBOUND_MESSAGE_BYTES:
        raise MessageTooLargeError(f"{payload.size_bytes} bytes exceeds cap of {MAX_INBOUND_MESSAGE_BYTES}")

    org_id = await _resolve_org_id(session, payload.to_address)
    if org_id is None:
        # No org owns this address at all. Nothing to quarantine into —
        # drop it. The webhook_events dedupe ledger (app/jobs/models.py)
        # still records the provider event id upstream of this call.
        return "dropped_unresolved_address"

    if not sender_is_authenticated(payload.auth):
        quarantined = QuarantinedMessage(
            org_id=org_id,
            address_used=payload.to_address,
            from_header=payload.from_header,
            subject=payload.subject,
            body_preview=payload.body[:2000],
            reason="dkim_not_aligned_and_no_arc_chain",
        )
        session.add(quarantined)
        await session.flush()
        # Deliberately NO auto-reply (closes S12/backscatter) and NO
        # ticket, NO model call, NO SMS trigger for anything landing here.
        return "quarantined"

    await enqueue(
        session,
        org_id=org_id,
        job_type="maintenance_intake_triage",
        payload={
            "from_header": payload.from_header,
            "subject": payload.subject,
            "body": payload.body,
            "message_id": payload.message_id,
        },
        idempotency_key=f"inbound-email:{org_id}:{payload.message_id}",
    )
    return "queued"
