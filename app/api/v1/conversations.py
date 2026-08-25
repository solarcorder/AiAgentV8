"""
The chat endpoint. Thin route over app/agent/conversation_service.py —
see that module's docstring for the turn structure (resolve conversation
-> turn cap -> resolve provider -> reserve budget -> call -> persist).
This file's only job is translating the service's typed exceptions to
HTTP status codes.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.agent import conversation_service as svc
from app.agent.providers.registry import AmbiguousProviderError, NoProviderAvailableError
from app.agent.budget import BudgetExceededError
from app.auth.dependencies import require_capability
from app.config import ModelClass
from app.db.session import tenant_session
from app.tenancy.context import TenantContext

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])


class CreateConversationResponse(BaseModel):
    conversation_id: uuid.UUID


class SendMessageRequest(BaseModel):
    model_config = {"extra": "forbid"}

    content: str
    provider: str | None = None
    model_class: str = ModelClass.STANDARD.value
    enable_search_grounding: bool = False


class SendMessageResponse(BaseModel):
    message_id: uuid.UUID
    content: str | None
    provider: str
    model_id: str
    tokens_in: int
    tokens_out: int
    cost_estimate_usd: Decimal
    turn_count: int


@router.post("", response_model=CreateConversationResponse)
async def create_conversation(context: TenantContext = Depends(require_capability("domain.read"))):
    async with tenant_session(context.org_id) as session:
        conversation_id = await svc.create_conversation(session, org_id=context.org_id, user_id=context.user_id)
    return CreateConversationResponse(conversation_id=conversation_id)


@router.post("/{conversation_id}/messages", response_model=SendMessageResponse)
async def send_message(
    conversation_id: uuid.UUID,
    body: SendMessageRequest,
    context: TenantContext = Depends(require_capability("domain.read")),
):
    try:
        model_class = ModelClass(body.model_class)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"unknown model_class '{body.model_class}'") from exc

    async with tenant_session(context.org_id) as session:
        try:
            result = await svc.send_message(
                session,
                org_id=context.org_id,
                user_id=context.user_id,
                conversation_id=conversation_id,
                content=body.content,
                provider=body.provider,
                model_class=model_class,
                enable_search_grounding=body.enable_search_grounding,
            )
        except svc.ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail="conversation not found") from exc
        except svc.TurnLimitExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except NoProviderAvailableError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except AmbiguousProviderError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"multiple providers connected ({exc.connected}) — specify one or set orgs.default_ai_provider",
            ) from exc
        except BudgetExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except svc.ProviderCallFailedError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return SendMessageResponse(
        message_id=result.message_id,
        content=result.content,
        provider=result.provider,
        model_id=result.model_id,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost_estimate_usd=result.cost_estimate_usd,
        turn_count=result.turn_count,
    )
