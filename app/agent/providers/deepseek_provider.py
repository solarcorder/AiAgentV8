"""
DeepSeek, via its documented OpenAI-SDK-compatible endpoint. BYO-only.

Pricing below is a PLACEHOLDER — no live-verification tool for DeepSeek
in this codebase. Re-check against platform.deepseek.com/api-docs/pricing
— DeepSeek in particular has historically run off-peak discount pricing
on a clock, so a single static number here is already a simplification
even once verified once.
"""
from __future__ import annotations

from decimal import Decimal

from app.agent.providers._openai_compatible import OpenAICompatibleProvider
from app.config import AIProvider, ModelClass


class DeepSeekProvider(OpenAICompatibleProvider):
    name = AIProvider.DEEPSEEK.value
    base_url = "https://api.deepseek.com"

    _PRICING_PER_TOKEN = {  # PLACEHOLDER — verify
        ModelClass.CHEAP: (Decimal("0.00000027"), Decimal("0.0000011")),
        ModelClass.STANDARD: (Decimal("0.00000027"), Decimal("0.0000011")),
        ModelClass.REASONING: (Decimal("0.00000055"), Decimal("0.00000219")),
    }
