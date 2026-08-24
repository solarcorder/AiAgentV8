"""
RC-1 — BYO-Twilio by default, replacing the master plan's subaccounts-
under-one-parent design (AD-10) after the red team's FF-1 finding.

Why the original design was disqualified rather than merely risky:
Twilio's own documentation states plainly that it bills ALL subaccount
usage to the parent account on ONE SHARED BALANCE, and that suspending
the parent suspends every subaccount with it. A design whose entire
thesis is tenant isolation had, in its messaging layer, a single point of
both financial liability and availability shared across every customer —
one customer's credential leak or carrier complaint could drain the
operator's balance or take SMS away from all five customers at once.
That is a cross-tenant blast radius recommended by the tenant-isolation
document itself.

The fix: the customer creates and owns their own Twilio account, does
their own Brand/Campaign registration (the regulatory relationship that
should sit with whoever is actually sending the messages), and supplies a
**scoped API key** — never Account SID + Auth Token, which are root
credentials — stored in the credential vault under their own org_id.
Liability, credit risk, and carrier standing all sit with the customer.

Subaccounts remain available as an explicit, gated exception (settings.
twilio_mode == "subaccount") for a customer who wants it managed —
requires Twilio Usage Triggers, a capped auto-recharge ceiling on the
parent card, an application-layer hard per-org cap enforced BEFORE the
Twilio call, and a documented acceptance that the operator carries the
credit risk. None of that is implemented here; the default path (BYO) is.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.config import get_settings
from app.credentials.vault import CredentialVault


class SubaccountModeNotHardenedError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class OutboundSmsRequest:
    org_id: uuid.UUID
    to_number: str
    body: str
    idempotency_key: str  # derived from the source event — see app/jobs/queue.py's enqueue() docstring


class TwilioAdapter:
    def __init__(self, vault: CredentialVault | None = None) -> None:
        self._vault = vault or CredentialVault()
        self._settings = get_settings()

    async def send_sms(self, request: OutboundSmsRequest) -> None:
        if self._settings.twilio_mode == "subaccount":
            # Not implemented deliberately: FF-1's required mitigations
            # (Usage Triggers, capped auto-recharge, pre-call per-org hard
            # cap, written credit-risk acceptance) are an operational
            # decision, not a default a scaffold should silently enable.
            raise SubaccountModeNotHardenedError(
                "twilio_mode='subaccount' requires FF-1's mitigations to be implemented and explicitly "
                "accepted before use — see the docstring above. Use 'byo' (the default) instead."
            )

        # BYO path: look up this org's own Twilio API key from the vault,
        # decrypt in memory for exactly this call, never persist or log it.
        # NOT IMPLEMENTED: wire up the real Twilio SDK call here once an
        # org_credentials row of provider='twilio' exists for this org.
        raise NotImplementedError(
            "TwilioAdapter.send_sms: BYO-Twilio SDK call not wired up yet. "
            f"Would send to {request.to_number!r} for org {request.org_id}."
        )
