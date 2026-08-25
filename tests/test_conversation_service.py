"""
app/agent/conversation_service.py, tested against a fake provider so
these tests exercise the orchestration (turn cap, budget reserve/refund/
reconcile, persistence, cross-tenant scoping) rather than any real
provider SDK or network call. app/agent/providers/registry.py's own
resolution rules are already covered in test_ai_provider_registry.py —
here, resolve_provider_for_org is monkeypatched to hand back a canned
provider directly.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.agent import conversation_service as svc
from app.agent.budget import BudgetExceededError
from app.agent.models import Conversation, Message, OrgBudget
from app.agent.providers.base import ModelMessage, ModelProvider, ModelResponse
from app.config import ModelClass, get_settings
from tests.conftest import requires_db, tenant_session_as

pytestmark = requires_db


class FakeProvider(ModelProvider):
    name = "fake"

    def __init__(self, *, cost: Decimal = Decimal("0.001"), reply: str = "fake reply", fail: bool = False) -> None:
        self._cost = cost
        self._reply = reply
        self._fail = fail
        self.last_messages: list[ModelMessage] | None = None

    def estimate_cost_usd(self, *, model_class: ModelClass, input_tokens: int, output_tokens: int) -> Decimal:
        return self._cost

    async def complete(self, *, messages: list[ModelMessage], **kwargs: Any) -> ModelResponse:
        self.last_messages = messages
        if self._fail:
            raise RuntimeError("simulated provider failure")
        return ModelResponse(
            content=self._reply,
            tool_calls=[],
            tokens_in=10,
            tokens_out=5,
            cost_estimate_usd=self._cost,
            model_id="fake-model-v1",
            provider=self.name,
        )


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _patch_resolve(monkeypatch, provider: ModelProvider) -> None:
    async def _fake_resolve(session, *, org_id, requested_provider=None, vault=None):
        return provider

    monkeypatch.setattr(svc, "resolve_provider_for_org", _fake_resolve)


@pytest.mark.asyncio
async def test_happy_path_persists_both_turns_and_increments_count(app_session_factory, two_orgs, monkeypatch):
    fake = FakeProvider(reply="hello there")
    _patch_resolve(monkeypatch, fake)

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        conversation_id = await svc.create_conversation(session, org_id=two_orgs["org_a_id"], user_id=two_orgs["user_a_id"])

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        result = await svc.send_message(
            session,
            org_id=two_orgs["org_a_id"],
            user_id=two_orgs["user_a_id"],
            conversation_id=conversation_id,
            content="hi",
            provider=None,
            model_class=ModelClass.STANDARD,
        )

    assert result.content == "hello there"
    assert result.turn_count == 1
    assert result.provider == "fake"

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        messages = (
            (await session.execute(select(Message).where(Message.conversation_id == conversation_id))).scalars().all()
        )
        conversation = (await session.execute(select(Conversation).where(Conversation.id == conversation_id))).scalar_one()

    assert {m.role for m in messages} == {"user", "assistant"}
    assert conversation.turn_count == 1

    # The system prompt plus the persisted user message reached the provider.
    assert fake.last_messages is not None
    assert fake.last_messages[0].role == "system"
    assert fake.last_messages[-1] == ModelMessage(role="user", content="hi", provenance="internal")


@pytest.mark.asyncio
async def test_conversation_not_found_for_a_different_user(app_session_factory, two_orgs, monkeypatch):
    fake = FakeProvider()
    _patch_resolve(monkeypatch, fake)

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        conversation_id = await svc.create_conversation(session, org_id=two_orgs["org_a_id"], user_id=two_orgs["user_a_id"])

    other_user_id = uuid.uuid4()
    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        with pytest.raises(svc.ConversationNotFoundError):
            await svc.send_message(
                session,
                org_id=two_orgs["org_a_id"],
                user_id=other_user_id,
                conversation_id=conversation_id,
                content="hi",
                provider=None,
                model_class=ModelClass.STANDARD,
            )


@pytest.mark.asyncio
async def test_conversation_from_another_org_is_not_found(app_session_factory, two_orgs, monkeypatch):
    """Belt-and-braces (§14.4 Layer 1): explicit org_id filter catches this even though RLS already would."""
    fake = FakeProvider()
    _patch_resolve(monkeypatch, fake)

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        conversation_id = await svc.create_conversation(session, org_id=two_orgs["org_a_id"], user_id=two_orgs["user_a_id"])

    async with tenant_session_as(app_session_factory, two_orgs["org_b_id"]) as session:
        with pytest.raises(svc.ConversationNotFoundError):
            await svc.send_message(
                session,
                org_id=two_orgs["org_b_id"],
                user_id=two_orgs["user_a_id"],
                conversation_id=conversation_id,
                content="hi",
                provider=None,
                model_class=ModelClass.STANDARD,
            )


@pytest.mark.asyncio
async def test_turn_limit_is_enforced(app_session_factory, two_orgs, monkeypatch):
    monkeypatch.setenv("MAX_TURNS_PER_CONVERSATION", "1")
    get_settings.cache_clear()
    fake = FakeProvider()
    _patch_resolve(monkeypatch, fake)

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        conversation_id = await svc.create_conversation(session, org_id=two_orgs["org_a_id"], user_id=two_orgs["user_a_id"])

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        await svc.send_message(
            session,
            org_id=two_orgs["org_a_id"],
            user_id=two_orgs["user_a_id"],
            conversation_id=conversation_id,
            content="one",
            provider=None,
            model_class=ModelClass.STANDARD,
        )

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        with pytest.raises(svc.TurnLimitExceededError):
            await svc.send_message(
                session,
                org_id=two_orgs["org_a_id"],
                user_id=two_orgs["user_a_id"],
                conversation_id=conversation_id,
                content="two",
                provider=None,
                model_class=ModelClass.STANDARD,
            )


@pytest.mark.asyncio
async def test_budget_exceeded_is_enforced(app_session_factory, two_orgs, monkeypatch):
    monkeypatch.setenv("DEFAULT_DAILY_BUDGET_USD", "0.0001")
    get_settings.cache_clear()
    fake = FakeProvider(cost=Decimal("50.00"))  # estimate alone blows the cap
    _patch_resolve(monkeypatch, fake)

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        conversation_id = await svc.create_conversation(session, org_id=two_orgs["org_a_id"], user_id=two_orgs["user_a_id"])

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        with pytest.raises(BudgetExceededError):
            await svc.send_message(
                session,
                org_id=two_orgs["org_a_id"],
                user_id=two_orgs["user_a_id"],
                conversation_id=conversation_id,
                content="hi",
                provider=None,
                model_class=ModelClass.STANDARD,
            )


@pytest.mark.asyncio
async def test_provider_failure_refunds_the_reservation(app_session_factory, two_orgs, monkeypatch):
    monkeypatch.setenv("DEFAULT_DAILY_BUDGET_USD", "10.00")
    get_settings.cache_clear()
    fake = FakeProvider(cost=Decimal("1.00"), fail=True)
    _patch_resolve(monkeypatch, fake)

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        conversation_id = await svc.create_conversation(session, org_id=two_orgs["org_a_id"], user_id=two_orgs["user_a_id"])

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        with pytest.raises(svc.ProviderCallFailedError):
            await svc.send_message(
                session,
                org_id=two_orgs["org_a_id"],
                user_id=two_orgs["user_a_id"],
                conversation_id=conversation_id,
                content="hi",
                provider=None,
                model_class=ModelClass.STANDARD,
            )

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        budget = (
            await session.execute(select(OrgBudget).where(OrgBudget.org_id == two_orgs["org_a_id"]))
        ).scalar_one()

    assert budget.spent_usd == Decimal("0.0000")  # reservation refunded in full, not left as a phantom charge
