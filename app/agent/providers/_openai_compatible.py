"""
Shared implementation for the three providers that speak OpenAI's Chat
Completions wire format: OpenAI itself, Perplexity, and DeepSeek both
document explicit compatibility with the `openai` Python SDK against
their own `base_url` — this is a real, low-risk, well-documented pattern,
not a guess. What IS unverified per-provider is the exact current model
ID strings and pricing (app/config.py flags each as PLACEHOLDER) — there
is no live-verification tool for these three in this codebase the way
the claude-api skill provides for Anthropic, so re-check before routing
real traffic.

Subclasses set `name`, `base_url`, and `_PRICING_PER_TOKEN`; everything
else is identical across the three.
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

from app.agent.providers.base import ModelMessage, ModelProvider, ModelResponse, ToolCallRequest
from app.config import ModelClass, get_settings


class OpenAICompatibleProvider(ModelProvider):
    name: str
    base_url: str | None = None  # None -> SDK default (api.openai.com)
    _PRICING_PER_TOKEN: dict[ModelClass, tuple[Decimal, Decimal]] = {}

    def __init__(self, *, api_key: str | None) -> None:
        if not api_key:
            raise ValueError(f"{type(self).__name__} requires a decrypted org API key")
        self._api_key = api_key
        self._settings = get_settings()

    def _resolve_model_id(self, model_class: ModelClass) -> str:
        return self._settings.model_routing[self.name][model_class.value]

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
        from openai import AsyncOpenAI

        model_id = self._resolve_model_id(model_class)
        client = AsyncOpenAI(api_key=self._api_key, base_url=self.base_url)

        oi_messages = [{"role": m.role, "content": m.content} for m in messages if m.role in ("system", "user", "assistant")]

        kwargs: dict[str, Any] = {"model": model_id, "messages": oi_messages, "max_tokens": 16000}
        # §18.2: structurally omit tools, not merely instructed not to use
        # them — see the identical note in anthropic_provider.py.
        if tools_enabled and tools:
            kwargs["tools"] = tools

        response = await client.chat.completions.create(**kwargs)
        choice = response.choices[0]

        tool_calls: list[ToolCallRequest] = []
        for tc in choice.message.tool_calls or []:
            # Never string-match raw tool-call JSON — parse it. Malformed
            # arguments from the model are a tool-validation-layer concern
            # (app/agent/tools/executor.py), not something to paper over here.
            tool_calls.append(ToolCallRequest(tool_name=tc.function.name, arguments=json.loads(tc.function.arguments)))

        usage = response.usage
        input_price, output_price = self._PRICING_PER_TOKEN.get(model_class, (Decimal(0), Decimal(0)))
        cost = (Decimal(usage.prompt_tokens) * input_price) + (Decimal(usage.completion_tokens) * output_price)

        return ModelResponse(
            content=choice.message.content,
            tool_calls=tool_calls,
            tokens_in=usage.prompt_tokens,
            tokens_out=usage.completion_tokens,
            cost_estimate_usd=cost,
            model_id=model_id,
            provider=self.name,
        )
