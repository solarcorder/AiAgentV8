"""
Model provider abstraction — §17.3.

Two things this buys, both motivated by a real incident (S9 in the master
spec): the live system's model cascade was `gemini-2.5-flash ->
gemini-2.5-pro -> gemini-2.0-flash`, and Gemini 2.0 Flash was shut down on
1 June 2026 — the fallback's last safety net was a guaranteed error before
this review was even written. The fix is structural, not "remember to
update the model string":

  1. `ModelClass` (CHEAP/STANDARD/REASONING) is what calling code asks
     for — never a concrete model ID. The mapping from class to ID lives
     in configuration (app/config.py Settings.model_routing), so a model
     retirement is a config change and a redeploy, not a code change.
  2. A second provider can be added later by implementing this same
     `ModelProvider` interface, without touching any calling code —
     mitigating the single-provider dependency risk that just
     materialized once already.

RC-7: production must refuse to run real customer data through a
non-billing-enabled Gemini key. Google's terms permit non-paid-service
content to be used to improve Google products and to be reviewed by
humans, and explicitly ask users not to submit personal information to
non-paid services — occupant records ARE personal information. This is
enforced at boot (see app/main.py) rather than trusted.
"""
from __future__ import annotations

import abc
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.config import ModelClass, get_settings


class GeminiNotPaidTierError(RuntimeError):
    """Raised at boot if gemini_billing_enabled is False — see RC-7 / FF-6."""


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    provenance: str = "internal"  # "internal" | "untrusted_external" — §18.2


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str | None
    tool_calls: list[ToolCallRequest]
    tokens_in: int
    tokens_out: int
    cost_estimate_usd: Decimal
    model_id: str


class ModelProvider(abc.ABC):
    @abc.abstractmethod
    async def complete(
        self,
        *,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]],
        model_class: ModelClass,
        org_id: uuid.UUID,
        budget_remaining_usd: Decimal,
        tools_enabled: bool = True,
    ) -> ModelResponse:
        """
        `tools_enabled=False` MUST be honoured structurally, not by
        convention — §18.2/§9's fix for indirect prompt injection is that
        classification calls (triage, extraction) run WITHOUT a tool
        registry available at all, so injected text has nothing to
        invoke even if it fully succeeds at manipulating the model's
        output.
        """


class GeminiProvider(ModelProvider):
    def __init__(self) -> None:
        settings = get_settings()
        if settings.env == "production" and not settings.gemini_billing_enabled:
            raise GeminiNotPaidTierError(
                "refusing to start: GEMINI_BILLING_ENABLED is not set. Non-paid-tier Gemini content may be "
                "used to improve Google's products and reviewed by humans (FF-6) — verify the configured "
                "project/key is on the paid tier before processing any real occupant data."
            )
        self._settings = settings

    def _resolve_model_id(self, model_class: ModelClass) -> str:
        return self._settings.model_routing[model_class.value]

    async def complete(
        self,
        *,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]],
        model_class: ModelClass,
        org_id: uuid.UUID,
        budget_remaining_usd: Decimal,
        tools_enabled: bool = True,
    ) -> ModelResponse:
        model_id = self._resolve_model_id(model_class)
        # NOT IMPLEMENTED: wire up the real google-genai SDK call here.
        # Deliberately left as an explicit stub rather than a fake
        # response — a scaffold that silently returns canned model output
        # is worse than one that fails loudly when actually invoked.
        raise NotImplementedError(
            f"GeminiProvider.complete() not wired up yet (would call model_id={model_id!r}, "
            f"tools_enabled={tools_enabled}). See app/agent/provider.py."
        )
