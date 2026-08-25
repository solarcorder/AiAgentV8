"""
Gemini. The one provider with an operator-paid fallback path
(app/agent/providers/registry.py) alongside the BYO option every other
provider is limited to — see app/config.py's AI_PROVIDER_METADATA for
why. RC-7 from the red-team review still applies to the OPERATOR key
specifically: production refuses real customer data on a non-billing-
enabled Gemini project (Google's non-paid-tier terms permit using
submitted content to improve Google's products and allow human review —
occupant records are personal information, FF-6). A BYO Gemini key's
tier is the connecting org's own responsibility; this check does not
apply to it.

Search grounding — the "Google AI answer" behaviour: `enable_search_grounding=True`
adds Gemini's own Search-grounding tool to the request, so the model's
answer is backed by live Google Search results with citations. This was
chosen over scraping Google's Search-results AI Overview box, which was
rejected outright: scraping violates Google's ToS and is fragile/
blockable by design, the same category of thing §12.1 already ruled out
for reading Gmail directly. Grounding is an official, documented Gemini
API feature — legitimate, stable, and it is actually the same underlying
product surface a user is describing when they say "the AI answer in
Google search."
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from app.agent.providers.base import ModelMessage, ModelProvider, ModelResponse
from app.config import AIProvider, ModelClass, get_settings


class GeminiNotPaidTierError(RuntimeError):
    """Raised at boot if the OPERATOR's gemini_billing_enabled is False — see RC-7 / FF-6."""


class GeminiProvider(ModelProvider):
    name = AIProvider.GEMINI.value

    def __init__(self, *, api_key: str | None, is_operator_key: bool = False) -> None:
        settings = get_settings()
        resolved_key = api_key or settings.gemini_api_key
        if not resolved_key:
            raise ValueError("GeminiProvider requires an API key (org BYO key or operator fallback)")

        # RC-7 gate applies only to the operator's own key — a BYO key is
        # the connecting org's own account and risk, not this codebase's
        # compliance surface to police.
        is_operator_key = is_operator_key or (api_key is None)
        if is_operator_key and settings.env == "production" and not settings.gemini_billing_enabled:
            raise GeminiNotPaidTierError(
                "refusing to serve the operator Gemini fallback: GEMINI_BILLING_ENABLED is not set. "
                "Non-paid-tier Gemini content may be used to improve Google's products and reviewed by "
                "humans (FF-6) — verify the configured project/key is on the paid tier first."
            )

        self._api_key = resolved_key
        self._settings = settings

    def _resolve_model_id(self, model_class: ModelClass) -> str:
        return self._settings.model_routing[AIProvider.GEMINI.value][model_class.value]

    async def complete(
        self,
        *,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]],
        model_class: ModelClass,
        org_id: uuid.UUID,
        budget_remaining_usd: Decimal,
        tools_enabled: bool = True,
        enable_search_grounding: bool = False,
    ) -> ModelResponse:
        model_id = self._resolve_model_id(model_class)

        if enable_search_grounding and not self._settings.gemini_search_grounding_enabled:
            enable_search_grounding = False

        # NOT IMPLEMENTED: wire up the real google-genai SDK call here —
        # client.models.generate_content(..., tools=[Tool(google_search=...)]
        # if enable_search_grounding else tools, ...). Left as an explicit
        # stub deliberately: a scaffold that silently returns canned model
        # output is worse than one that fails loudly when actually invoked.
        # (This mirrors the pre-multi-provider version of this file, which
        # had the same stub for the same reason — nothing about adding
        # other providers changes the honesty bar here.)
        raise NotImplementedError(
            f"GeminiProvider.complete() not wired up yet (would call model_id={model_id!r}, "
            f"tools_enabled={tools_enabled}, enable_search_grounding={enable_search_grounding}). "
            "See app/agent/providers/gemini_provider.py."
        )
