"""
The tool executor — §9.2 rule 2 ("the agent proposes; the executor
disposes") and the AI boundary marked explicitly in the target
architecture diagram: "Re-derives org_id from session, NOT from model
output. Validates args against strict schema. Re-checks authz. Enforces
budget. Routes side effects to approval."

Every one of those five sentences is a numbered step below, in order,
and none of them can be skipped by anything the model says — the model
never gets to choose the tier, the capability, or whether approval is
required; it only gets to choose which tool to call and with what
(validated) arguments.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools.registry import RiskTier, ToolDeclaration, get_tool
from app.approvals.service import ApprovalHandle, request_approval
from app.logging import log_event
from app.tenancy.context import TenantContext

logger = logging.getLogger("tool_executor")


class UnknownToolError(Exception):
    pass


class ToolArgumentValidationError(Exception):
    pass


class ToolCapabilityDeniedError(Exception):
    pass


class ToolIterationLimitExceededError(Exception):
    """FF-5 control #2: hard max tool iterations per turn."""


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    tool_name: str
    status: str  # "completed" | "pending_approval"
    result: Any | None = None
    approval: ApprovalHandle | None = None


class ToolExecutor:
    def __init__(self, *, max_iterations_per_turn: int) -> None:
        self._max_iterations = max_iterations_per_turn
        self._memo: dict[str, Any] = {}  # (tool_name, canonical_args) -> result, within one turn — §17.4 point 3

    def _idempotency_key(
        self, *, org_id: uuid.UUID, conversation_id: uuid.UUID, turn_id: str, tool_name: str, args: dict[str, Any]
    ) -> str:
        canonical_args = json.dumps(args, sort_keys=True, separators=(",", ":"))
        args_hash = hashlib.sha256(canonical_args.encode()).hexdigest()
        return f"{org_id}:{conversation_id}:{turn_id}:{tool_name}:{args_hash}"

    async def execute(
        self,
        *,
        session: AsyncSession,
        context: TenantContext,
        conversation_id: uuid.UUID,
        turn_id: str,
        iteration_index: int,
        tool_name: str,
        raw_args: dict[str, Any],
        requested_by: uuid.UUID,
        deliver_approval_token: Any,  # Callable[[str], Awaitable[None]] — see approvals/service.py
    ) -> ToolExecutionResult:
        # --- FF-5 control #2: hard iteration cap, checked before anything else ---
        if iteration_index >= self._max_iterations:
            raise ToolIterationLimitExceededError(
                f"turn exceeded max_tool_iterations_per_turn ({self._max_iterations})"
            )

        decl = get_tool(tool_name)
        if decl is None:
            raise UnknownToolError(f"unknown tool: {tool_name!r}")

        # --- §9.2 rule 2 / §18.2: a model-supplied org_id is ignored and
        # its presence is logged as an anomaly, never trusted. ---
        if "org_id" in raw_args:
            log_event(
                logger,
                logging.WARNING,
                "tool_call_contained_org_id_anomaly",
                tool_name=tool_name,
                supplied_org_id=str(raw_args.get("org_id")),
                actual_org_id=str(context.org_id),
            )
            raw_args = {k: v for k, v in raw_args.items() if k != "org_id"}

        # --- validate args against the strict schema; unknown fields rejected ---
        try:
            validated_args: BaseModel = decl.args_schema.model_validate(raw_args)
        except ValidationError as exc:
            raise ToolArgumentValidationError(str(exc)) from exc

        # --- capability check against the SESSION's role, never anything model-supplied ---
        if not context.has_capability(decl.capability):
            raise ToolCapabilityDeniedError(
                f"role does not have capability '{decl.capability}' required by tool '{tool_name}'"
            )

        # --- memoize within this turn: a retried turn reuses prior tool
        # results instead of re-invoking them (§17.4 point 3 — fixes S8,
        # the legacy retry cascade re-running side-effecting tools). ---
        idem_key = self._idempotency_key(
            org_id=context.org_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            tool_name=tool_name,
            args=validated_args.model_dump(mode="json"),
        )
        if idem_key in self._memo:
            return self._memo[idem_key]

        # --- risk tier gate: the model never decides this. READ/LOW_WRITE
        # execute directly; everything else routes to approval (§18.4). ---
        if decl.risk_tier in (RiskTier.HIGH, RiskTier.CRITICAL, RiskTier.MEDIUM):
            handle = await request_approval(
                session,
                org_id=context.org_id,
                requested_by=requested_by,
                action_type=tool_name,
                payload=validated_args.model_dump(mode="json"),
                deliver_token=deliver_approval_token,
            )
            outcome = ToolExecutionResult(tool_name=tool_name, status="pending_approval", approval=handle)
            self._memo[idem_key] = outcome
            return outcome

        result = await decl.fn(validated_args, context, session)
        outcome = ToolExecutionResult(tool_name=tool_name, status="completed", result=result)
        self._memo[idem_key] = outcome
        return outcome
