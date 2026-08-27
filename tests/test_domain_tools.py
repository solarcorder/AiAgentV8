"""
app/agent/tools/domain_tools.py — the first tool registered against the
executor (app/agent/tools/executor.py). These tests exercise the
executor's full five-step flow (org_id re-derivation, arg validation,
capability check, memoization, dispatch) against a real Postgres-backed
list_properties call, not just the query function in isolation — the
same reasoning as test_conversation_service.py's choice to test
orchestration rather than mock the database out from under it.
"""
from __future__ import annotations

import uuid

import pytest

import app.agent.tools.domain_tools  # noqa: F401 — registers list_properties as an import side effect
from app.agent.tools.executor import ToolArgumentValidationError, ToolExecutor
from app.agent.tools.registry import get_tool
from app.auth.models import Role
from app.tenancy.context import TenantContext
from tests.conftest import requires_db, tenant_session_as

pytestmark = requires_db


async def _noop_deliver(_token: str) -> None:
    """READ-tier tools never route to approval, so this should never actually be called."""


def test_list_properties_is_registered():
    decl = get_tool("list_properties")
    assert decl is not None
    assert decl.capability == "domain.read"
    assert decl.read_write == "read"


@pytest.mark.asyncio
async def test_executor_returns_only_this_orgs_property(app_session_factory, two_orgs):
    executor = ToolExecutor(max_iterations_per_turn=8)
    context = TenantContext(org_id=two_orgs["org_a_id"], user_id=two_orgs["user_a_id"], role=Role.ADMIN)

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        result = await executor.execute(
            session=session,
            context=context,
            conversation_id=uuid.uuid4(),
            turn_id="turn-1",
            iteration_index=0,
            tool_name="list_properties",
            raw_args={},
            requested_by=two_orgs["user_a_id"],
            deliver_approval_token=_noop_deliver,
        )

    assert result.status == "completed"
    assert len(result.result) == 1
    assert result.result[0]["id"] == str(two_orgs["property_a_id"])


@pytest.mark.asyncio
async def test_a_model_supplied_org_id_is_ignored_not_trusted(app_session_factory, two_orgs):
    """§9.2 rule 2: a tool-call arg named org_id must be stripped, not honoured, even for a READ tool."""
    executor = ToolExecutor(max_iterations_per_turn=8)
    context = TenantContext(org_id=two_orgs["org_a_id"], user_id=two_orgs["user_a_id"], role=Role.ADMIN)

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        result = await executor.execute(
            session=session,
            context=context,
            conversation_id=uuid.uuid4(),
            turn_id="turn-1",
            iteration_index=0,
            tool_name="list_properties",
            raw_args={"org_id": str(two_orgs["org_b_id"])},
            requested_by=two_orgs["user_a_id"],
            deliver_approval_token=_noop_deliver,
        )

    assert result.status == "completed"
    assert len(result.result) == 1
    assert result.result[0]["id"] == str(two_orgs["property_a_id"])  # still org A's, never org B's


@pytest.mark.asyncio
async def test_unknown_args_are_rejected(app_session_factory, two_orgs):
    executor = ToolExecutor(max_iterations_per_turn=8)
    context = TenantContext(org_id=two_orgs["org_a_id"], user_id=two_orgs["user_a_id"], role=Role.ADMIN)

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        with pytest.raises(ToolArgumentValidationError):
            await executor.execute(
                session=session,
                context=context,
                conversation_id=uuid.uuid4(),
                turn_id="turn-1",
                iteration_index=0,
                tool_name="list_properties",
                raw_args={"unexpected_field": "x"},
                requested_by=two_orgs["user_a_id"],
                deliver_approval_token=_noop_deliver,
            )


@pytest.mark.asyncio
async def test_status_filter_excludes_non_matching_properties(app_session_factory, two_orgs):
    """two_orgs seeds an ACTIVE property (the model default) — filtering to INACTIVE must return nothing."""
    executor = ToolExecutor(max_iterations_per_turn=8)
    context = TenantContext(org_id=two_orgs["org_a_id"], user_id=two_orgs["user_a_id"], role=Role.ADMIN)

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        result = await executor.execute(
            session=session,
            context=context,
            conversation_id=uuid.uuid4(),
            turn_id="turn-1",
            iteration_index=0,
            tool_name="list_properties",
            raw_args={"status": "INACTIVE"},
            requested_by=two_orgs["user_a_id"],
            deliver_approval_token=_noop_deliver,
        )

    assert result.result == []


@pytest.mark.asyncio
async def test_repeated_call_within_a_turn_is_memoized(app_session_factory, two_orgs):
    """§17.4 point 3: the same (tool, args) within one turn returns the memoized result, not a re-query."""
    executor = ToolExecutor(max_iterations_per_turn=8)
    context = TenantContext(org_id=two_orgs["org_a_id"], user_id=two_orgs["user_a_id"], role=Role.ADMIN)

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        kwargs = {
            "session": session,
            "context": context,
            "conversation_id": uuid.uuid4(),
            "turn_id": "turn-1",
            "iteration_index": 0,
            "tool_name": "list_properties",
            "raw_args": {},
            "requested_by": two_orgs["user_a_id"],
            "deliver_approval_token": _noop_deliver,
        }
        first = await executor.execute(**kwargs)
        second = await executor.execute(**{**kwargs, "iteration_index": 1})

    assert first.result is second.result
