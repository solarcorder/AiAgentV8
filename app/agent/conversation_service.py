"""
The chat turn, as a service function — factored out of app/api/v1/
conversations.py so it can be called directly (tests, and any future
non-HTTP entrypoint) the same way app/approvals/service.py sits behind
app/api/v1/approvals.py. See that route file's docstring for the turn
structure this implements; this module owns the logic, the route owns
translating exceptions to HTTP status codes.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.budget import BudgetExceededError, reconcile_actual_cost, reserve_budget
from app.agent.models import Conversation, Message
from app.agent.providers.base import ModelMessage
from app.agent.providers.registry import (
    AmbiguousProviderError,
    NoProviderAvailableError,
    resolve_provider_for_org,
)
from app.config import ModelClass, get_settings
from app.domain.models.org import Org, OrgStatus

# Placeholder until §32's "versioned prompt registry" exists — a single
# fixed system prompt for every org, rather than per-tenant tone/policy/
# jurisdiction. Flagged, not silently pretended-complete.
DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant for this organization's real estate portfolio. "
    "Answer only from information you are given; say so plainly if you don't know."
)

OUTPUT_TOKEN_ESTIMATE = 16000  # matches the max_tokens every provider adapter currently requests


class ConversationNotFoundError(Exception):
    """Also raised for a conversation belonging to another user — §20 not-found-not-forbidden."""


class TurnLimitExceededError(Exception):
    pass


class ProviderCallFailedError(Exception):
    def __init__(self, message: str, *, original: Exception) -> None:
        super().__init__(message)
        self.original = original


@dataclass(frozen=True, slots=True)
class MessageResult:
    message_id: uuid.UUID
    content: str | None
    provider: str
    model_id: str
    tokens_in: int
    tokens_out: int
    cost_estimate_usd: Decimal
    turn_count: int


async def create_conversation(session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID:
    conversation = Conversation(org_id=org_id, user_id=user_id)
    session.add(conversation)
    await session.flush()
    return conversation.id


async def send_message(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    content: str,
    provider: str | None,
    model_class: ModelClass,
    enable_search_grounding: bool = False,
) -> MessageResult:
    settings = get_settings()

    # org_id filtered explicitly here (Layer 1, §14.4) even though RLS
    # (Layer 2) already scopes this session to org_id — the two layers
    # are meant to fail independently; a query that only ever relied on
    # RLS would quietly stop being belt-and-braces the moment anyone
    # copies this pattern into a context that isn't already
    # tenant_session()-scoped.
    conversation = (
        await session.execute(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.org_id == org_id, Conversation.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise ConversationNotFoundError(f"conversation {conversation_id} not found")

    if conversation.turn_count >= settings.max_turns_per_conversation:
        raise TurnLimitExceededError("conversation turn limit reached — start a new conversation")

    # NoProviderAvailableError / AmbiguousProviderError propagate as-is —
    # the route layer maps them to HTTP status codes.
    resolved_provider = await resolve_provider_for_org(session, org_id=org_id, requested_provider=provider)

    org = (await session.execute(select(Org).where(Org.id == org_id))).scalar_one()
    daily_cap = settings.trial_daily_budget_usd if org.status == OrgStatus.TRIALING else settings.default_daily_budget_usd

    estimated_cost = resolved_provider.estimate_cost_usd(
        model_class=model_class,
        input_tokens=settings.max_input_tokens_per_request,
        output_tokens=OUTPUT_TOKEN_ESTIMATE,
    )
    # BudgetExceededError propagates as-is — see NoProviderAvailableError note above.
    await reserve_budget(session, org_id=org_id, estimated_cost_usd=estimated_cost, daily_cap_usd=daily_cap)

    history_rows = (
        (
            await session.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id, Message.org_id == org_id)
                .order_by(Message.created_at)
            )
        )
        .scalars()
        .all()
    )

    history = [ModelMessage(role="system", content=DEFAULT_SYSTEM_PROMPT)]
    history += [ModelMessage(role=m.role, content=m.content, provenance=m.provenance) for m in history_rows]
    # The authenticated user's own input carries only their own tenant's
    # authority — §9's "direct prompt injection" reasoning: it cannot
    # cross tenants (the executor would re-derive org_id regardless) and
    # cannot self-approve anything, so it is classified "internal" here,
    # unlike text arriving from inbound email or a document (app/
    # integrations/email_inbound.py), which is tagged "untrusted_external"
    # before it ever reaches a prompt.
    history.append(ModelMessage(role="user", content=content, provenance="internal"))

    session.add(Message(org_id=org_id, conversation_id=conversation_id, role="user", content=content))

    try:
        response = await resolved_provider.complete(
            messages=history,
            tools=[],
            model_class=model_class,
            org_id=org_id,
            budget_remaining_usd=daily_cap,
            tools_enabled=False,  # no tool implemented against the registry yet — see conversations.py docstring
            enable_search_grounding=enable_search_grounding,
        )
    except Exception as exc:
        # No real usage happened — refund the reservation in full rather
        # than leave a phantom charge against the org's daily cap for a
        # call that never completed.
        await reconcile_actual_cost(session, org_id=org_id, reserved_usd=estimated_cost, actual_usd=Decimal(0))
        raise ProviderCallFailedError(f"provider call failed: {exc}", original=exc) from exc

    await reconcile_actual_cost(session, org_id=org_id, reserved_usd=estimated_cost, actual_usd=response.cost_estimate_usd)

    assistant_message = Message(
        org_id=org_id,
        conversation_id=conversation_id,
        role="assistant",
        content=response.content or "",  # Message.content is NOT NULL; tools_enabled=False makes an empty reply unlikely but not impossible
        tokens_in=response.tokens_in,
        tokens_out=response.tokens_out,
        model=response.model_id,
        cost_estimate_usd=response.cost_estimate_usd,
    )
    session.add(assistant_message)

    conversation.turn_count += 1
    await session.flush()

    return MessageResult(
        message_id=assistant_message.id,
        content=response.content,
        provider=response.provider,
        model_id=response.model_id,
        tokens_in=response.tokens_in,
        tokens_out=response.tokens_out,
        cost_estimate_usd=response.cost_estimate_usd,
        turn_count=conversation.turn_count,
    )
