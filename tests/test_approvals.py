from __future__ import annotations

import hashlib
import json

import pytest

from app.approvals import service as approval_service
from app.auth.models import Role
from app.tenancy.context import TenantContext
from tests.conftest import requires_db, tenant_session_as

pytestmark = requires_db


def _payload_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _noop_deliver(_token: str) -> None:
    """Stand-in for the real out-of-band delivery (dashboard/email/SMS)."""


@pytest.mark.asyncio
async def test_full_request_and_consume_flow(app_session_factory, two_orgs):
    """§18.3's redesigned flow end to end: request -> out-of-band token -> authenticated consume -> CAS."""
    captured_token: dict[str, str] = {}

    async def capture(token: str) -> None:
        captured_token["value"] = token

    payload = {"recipient": "occupant@example.com", "template": "rent_reminder"}

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        handle = await approval_service.request_approval(
            session,
            org_id=two_orgs["org_a_id"],
            requested_by=two_orgs["user_a_id"],
            action_type="send_communication",
            payload=payload,
            deliver_token=capture,
        )

    # The handle returned to the (would-be model-facing) caller has NO token field at all.
    assert not hasattr(handle, "token")
    assert not hasattr(handle, "raw_token")
    assert "value" in captured_token  # but the out-of-band channel received it

    context = TenantContext(org_id=two_orgs["org_a_id"], user_id=two_orgs["user_a_id"], role=Role.ADMIN)

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        approval = await approval_service.consume_approval(
            session,
            context=context,
            raw_token=captured_token["value"],
            expected_payload_hash=_payload_hash(payload),
        )

    assert approval.status.value == "CONSUMED"
    assert approval.approved_by == two_orgs["user_a_id"]


@pytest.mark.asyncio
async def test_cross_org_token_consumption_fails_uniformly(app_session_factory, two_orgs):
    """
    W6 fix: consuming org A's token while authenticated into org B must
    raise the SAME ApprovalNotFoundError as a token that never existed —
    never a different error that would let a caller distinguish
    "exists elsewhere" from "doesn't exist."
    """
    captured_token: dict[str, str] = {}

    async def capture(token: str) -> None:
        captured_token["value"] = token

    payload = {"amount": "100.00"}

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        await approval_service.request_approval(
            session,
            org_id=two_orgs["org_a_id"],
            requested_by=two_orgs["user_a_id"],
            action_type="approve_expense",
            payload=payload,
            deliver_token=capture,
        )

    context_b = TenantContext(org_id=two_orgs["org_b_id"], user_id=two_orgs["user_b_id"], role=Role.ADMIN)

    async with tenant_session_as(app_session_factory, two_orgs["org_b_id"]) as session:  # GUC = org B
        with pytest.raises(approval_service.ApprovalNotFoundError):
            await approval_service.consume_approval(
                session,
                context=context_b,
                raw_token=captured_token["value"],
                expected_payload_hash=_payload_hash(payload),
            )


@pytest.mark.asyncio
async def test_double_consume_loses_the_race(app_session_factory, two_orgs):
    """§14.5's compare-and-swap, preserved: the second consume of an already-CONSUMED approval must fail."""
    captured_token: dict[str, str] = {}

    async def capture(token: str) -> None:
        captured_token["value"] = token

    payload = {"x": 1}

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        await approval_service.request_approval(
            session,
            org_id=two_orgs["org_a_id"],
            requested_by=two_orgs["user_a_id"],
            action_type="send_communication",
            payload=payload,
            deliver_token=capture,
        )

    context = TenantContext(org_id=two_orgs["org_a_id"], user_id=two_orgs["user_a_id"], role=Role.ADMIN)

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        await approval_service.consume_approval(
            session, context=context, raw_token=captured_token["value"], expected_payload_hash=_payload_hash(payload)
        )

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        with pytest.raises(approval_service.ApprovalAlreadyResolvedError):
            await approval_service.consume_approval(
                session,
                context=context,
                raw_token=captured_token["value"],
                expected_payload_hash=_payload_hash(payload),
            )


@pytest.mark.asyncio
async def test_tampered_payload_is_rejected(app_session_factory, two_orgs):
    """Payload-hash binding: consuming with a DIFFERENT payload than what was approved must fail."""
    captured_token: dict[str, str] = {}

    async def capture(token: str) -> None:
        captured_token["value"] = token

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        await approval_service.request_approval(
            session,
            org_id=two_orgs["org_a_id"],
            requested_by=two_orgs["user_a_id"],
            action_type="approve_expense",
            payload={"amount": "100.00"},
            deliver_token=capture,
        )

    context = TenantContext(org_id=two_orgs["org_a_id"], user_id=two_orgs["user_a_id"], role=Role.ADMIN)

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        with pytest.raises(approval_service.ApprovalPayloadMismatchError):
            await approval_service.consume_approval(
                session,
                context=context,
                raw_token=captured_token["value"],
                expected_payload_hash=_payload_hash({"amount": "999999.00"}),  # tampered
            )


@pytest.mark.asyncio
async def test_unknown_action_type_is_rejected(app_session_factory, two_orgs):
    """§18.1's closed action registry: an unregistered action_type must never be accepted."""
    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        with pytest.raises(approval_service.UnknownActionTypeError):
            await approval_service.request_approval(
                session,
                org_id=two_orgs["org_a_id"],
                requested_by=two_orgs["user_a_id"],
                action_type="wire_all_funds_to_attacker",
                payload={},
                deliver_token=_noop_deliver,
            )
