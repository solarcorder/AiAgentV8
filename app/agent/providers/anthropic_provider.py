"""
Claude, via the official `anthropic` SDK. Model IDs and pricing here are
live-verified (the claude-api skill, cached 2026-06-24) — this is the one
provider file in this package that isn't a documented placeholder.

Credential handling: `api_key` arrives already decrypted by the caller
(app/agent/providers/registry.py, from this org's own vault-stored key —
Claude is BYO-only, no operator fallback, per app/config.py's
AI_PROVIDER_METADATA). It is held only for the lifetime of this call and
is never logged — see app/logging.py's redaction deny-list, which
includes "api_key".
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from app.agent.providers.base import ModelMessage, ModelProvider, ModelResponse, ToolCallRequest
from app.config import AIProvider, ModelClass, get_settings

# $ per token, live-verified via the claude-api skill (cached 2026-06-24).
# Re-check against that skill (or https://anthropic.com/pricing) before
# trusting this for a real bill — it is a snapshot, not a live feed.
_PRICING_PER_TOKEN: dict[ModelClass, tuple[Decimal, Decimal]] = {
    ModelClass.CHEAP: (Decimal("0.000001"), Decimal("0.000005")),  # claude-haiku-4-5: $1 / $5 per 1M
    ModelClass.STANDARD: (Decimal("0.000002"), Decimal("0.00001")),  # claude-sonnet-5: $2 / $10 per 1M
    ModelClass.REASONING: (Decimal("0.000005"), Decimal("0.000025")),  # claude-opus-5: $5 / $25 per 1M
}


class AnthropicProvider(ModelProvider):
    name = AIProvider.ANTHROPIC.value

    def __init__(self, *, api_key: str | None) -> None:
        if not api_key:
            raise ValueError("AnthropicProvider requires a decrypted org API key")
        self._api_key = api_key
        self._settings = get_settings()

    def _resolve_model_id(self, model_class: ModelClass) -> str:
        return self._settings.model_routing[AIProvider.ANTHROPIC.value][model_class.value]

    async def complete(
        self,
        *,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]],
        model_class: ModelClass,
        org_id: uuid.UUID,
        budget_remaining_usd: Decimal,
        tools_enabled: bool = True,
        enable_search_grounding: bool = False,  # no-op here — see base.py docstring
    ) -> ModelResponse:
        import anthropic

        model_id = self._resolve_model_id(model_class)
        client = anthropic.AsyncAnthropic(api_key=self._api_key)

        system_text = "\n\n".join(m.content for m in messages if m.role == "system") or None
        conversation = [
            {"role": m.role, "content": m.content} for m in messages if m.role in ("user", "assistant")
        ]

        kwargs: dict[str, Any] = {
            "model": model_id,
            "max_tokens": 16000,
            "messages": conversation,
        }
        if system_text:
            kwargs["system"] = system_text
        if model_class == ModelClass.REASONING:
            # Adaptive thinking, current API — see claude-api skill. Never
            # budget_tokens (removed / 400 on claude-opus-5).
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": "high"}
        # §18.2: tools_enabled=False must structurally remove the tool
        # registry from the call, not just be asked nicely not to use it —
        # this is the actual mechanism that makes classification/triage
        # calls immune to indirect prompt injection regardless of what
        # injected text asks the model to do.
        if tools_enabled and tools:
            kwargs["tools"] = tools

        response = await client.messages.create(**kwargs)

        text_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCallRequest(tool_name=block.name, arguments=block.input))

        input_price, output_price = _PRICING_PER_TOKEN[model_class]
        cost = (Decimal(response.usage.input_tokens) * input_price) + (
            Decimal(response.usage.output_tokens) * output_price
        )

        return ModelResponse(
            content="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            cost_estimate_usd=cost,
            model_id=model_id,
            provider=self.name,
        )
