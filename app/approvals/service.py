"""
Approval service — §18.3, with RC-6's execution-binding fix.

The flaw this replaces, precisely (S1 in the master spec, confirmed live
in the inspected n8n workflows): `request_approval` returned the raw
token INTO THE AGENT'S CONTEXT, and `verify_approval` was bound to the
same agent as a callable tool. The agent held both the credential and the
means to spend it; the only thing stopping self-approval was a sentence
in the system prompt. That is prompt-enforced authorization, which a
prompt injection, a hallucination, or an ordinary reasoning error defeats.

The fix, enforced by this module's function signatures rather than by
convention:

  - `request_approval()` returns an `ApprovalHandle` that structurally
    CANNOT carry the raw token — there is no field for it. The raw token
    exists only as a local variable inside this function and is handed to
    a `deliver_token` callback for out-of-band human delivery (dashboard
    card, email, SMS code). Whatever calls this from the agent's tool
    layer never sees the token, because it is not in the return value it
    receives.
  - `consume_approval()` requires a `TenantContext` — i.e. an
    authenticated human session that has already been through
    tenant-context resolution (app/tenancy/context.py) — plus the raw
    token. There is no code path by which the model can call this: it
    would need a live authenticated user session bound to a human, which
    the agent's tool-execution context never has (see app/agent/tools/
    executor.py — the executor's context is the calling human's session,
    but the model itself is never handed a callable that reaches this
    function directly with attacker-controlled arguments strong enough to
    substitute for that session).
"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.approvals.models import Approval, ApprovalStatus
from app.config import get_settings
from app.tenancy.context import TenantContext

# Closed action registry (§18.1: "Closed action registry... refuses
# unregistered action_type values rather than accepting arbitrary
# strings" — one of the things the legacy system already got right).
# risk_tier drives the executor's auto-vs-approval decision (§18.4).
ACTION_REGISTRY: dict[str, str] = {
    "send_communication": "MEDIUM",
    "approve_expense": "HIGH",
    "renewal_notice_dispatch": "HIGH",
    "lease_termination_notice": "CRITICAL",
    "vendor_dispatch_with_cost": "HIGH",
    "record_deletion": "CRITICAL",
}


class UnknownActionTypeError(Exception):
    pass


class ApprovalNotFoundError(Exception):
    """
    Raised for BOTH 'no such token' and 'token belongs to another org'.
    Deliberately the same exception with the same message in both cases —
    see the Approval model docstring on W6 (the existence-oracle fix).
    """


class ApprovalPayloadMismatchError(Exception):
    """The payload changed between request and consume — hash mismatch."""


class ApprovalAlreadyResolvedError(Exception):
    """Lost the compare-and-swap race, or the approval already left PENDING."""


class ApprovalExpiredError(Exception):
    pass


def _canonical_payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ApprovalHandle:
    """What the tool-facing caller (ultimately, the model) receives. No token field — see module docstring."""

    approval_id: uuid.UUID
    status: str
    human_message: str


async def request_approval(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    requested_by: uuid.UUID,
    action_type: str,
    payload: dict[str, Any],
    deliver_token: Callable[[str], Awaitable[None]],
    ttl_seconds: int | None = None,
) -> ApprovalHandle:
    if action_type not in ACTION_REGISTRY:
        raise UnknownActionTypeError(f"'{action_type}' is not in the closed action registry")

    settings = get_settings()
    ttl = ttl_seconds or settings.approval_default_ttl_seconds

    raw_token = secrets.token_urlsafe(32)  # CSPRNG — fixes S4 (legacy used Math.random())
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()  # compared, never stored raw — fixes S5
    payload_hash = _canonical_payload_hash(payload)

    approval = Approval(
        org_id=org_id,
        action_type=action_type,
        payload=payload,
        payload_hash=payload_hash,
        token_hash=token_hash,
        status=ApprovalStatus.PENDING,
        requested_by=requested_by,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl),
    )
    session.add(approval)
    await session.flush()

    # Out-of-band delivery. This is the ONLY place the raw token exists
    # after this function returns — deliver_token is wired to a dashboard
    # notification / email / SMS in the API layer, never to anything the
    # agent's tool executor can observe.
    await deliver_token(raw_token)

    return ApprovalHandle(
        approval_id=approval.id,
        status=approval.status.value,
        human_message=f"Approval required for {action_type}. A request has been sent for review.",
    )


async def consume_approval(
    session: AsyncSession,
    *,
    context: TenantContext,
    raw_token: str,
    expected_payload_hash: str,
) -> Approval:
    """
    Requires an authenticated TenantContext (a real human session that
    has already passed tenancy resolution) plus the raw token. Emailed
    approval links must carry only an opaque approval_id and route the
    human into an authenticated session before this is called (RC-6),
    never the raw token itself in a URL — a raw token in a URL lands in
    mail-provider logs, browser history, and any third-party asset's
    Referer header (W5).
    """
    if not context.has_capability("approval.consume"):
        raise PermissionError("role lacks approval.consume capability")

    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    # `session` MUST already be a tenant_session(context.org_id) — RLS
    # scopes this lookup to the caller's own org, so a token belonging to
    # another org is invisible here and produces the identical
    # ApprovalNotFoundError as a token that does not exist at all (W6).
    result = await session.execute(select(Approval).where(Approval.token_hash == token_hash))
    approval = result.scalar_one_or_none()
    if approval is None:
        raise ApprovalNotFoundError("invalid or expired approval")

    if approval.expires_at < datetime.now(timezone.utc):
        await session.execute(
            update(Approval)
            .where(Approval.id == approval.id, Approval.status == ApprovalStatus.PENDING)
            .values(status=ApprovalStatus.EXPIRED)
        )
        raise ApprovalExpiredError("invalid or expired approval")

    if approval.payload_hash != expected_payload_hash:
        raise ApprovalPayloadMismatchError("payload changed since the approval was requested")

    # Atomic compare-and-swap — preserved verbatim from the legacy design,
    # which got this right (§14.5). Zero rows updated means someone else
    # (another admin, a revoke, an expiry sweep) won the race.
    cas_result = await session.execute(
        update(Approval)
        .where(Approval.id == approval.id, Approval.status == ApprovalStatus.PENDING)
        .values(status=ApprovalStatus.CONSUMED, consumed_at=datetime.now(timezone.utc), approved_by=context.user_id)
        .returning(Approval.id)
    )
    if cas_result.first() is None:
        raise ApprovalAlreadyResolvedError("approval already resolved by another request")

    await session.refresh(approval)
    return approval


async def mark_executing(session: AsyncSession, approval_id: uuid.UUID) -> None:
    await session.execute(
        update(Approval)
        .where(Approval.id == approval_id, Approval.status == ApprovalStatus.CONSUMED)
        .values(status=ApprovalStatus.EXECUTING)
    )


async def mark_executed(session: AsyncSession, approval_id: uuid.UUID) -> None:
    await session.execute(
        update(Approval)
        .where(Approval.id == approval_id)
        .values(status=ApprovalStatus.EXECUTED, executed_at=datetime.now(timezone.utc))
    )


async def mark_failed(session: AsyncSession, approval_id: uuid.UUID, reason: str) -> None:
    """
    W4 fix: a dead-lettered job after consumption no longer leaves the
    approval silently CONSUMED-forever with the user told it succeeded.
    FAILED is a visible terminal state; a fresh request_approval() call is
    required to try again — the original token is never reissued or
    reopened.
    """
    await session.execute(
        update(Approval).where(Approval.id == approval_id).values(status=ApprovalStatus.FAILED, failure_reason=reason)
    )
