"""
Perplexity, via its documented OpenAI-SDK-compatible endpoint. BYO-only.

Perplexity's `sonar` models are natively search-grounded (that's the
product), which is a different thing from Gemini's optional grounding
flag (app/agent/providers/gemini_provider.py) — Perplexity doesn't need a
flag to consult the web, it always does. `supports_search_grounding` is
False for this provider in app/config.py's metadata because that field
specifically means "the flag that produces the Google-Search-answer-box
behaviour," not "is grounded at all."

Pricing below is a PLACEHOLDER — no live-verification tool for
Perplexity in this codebase. Re-check against docs.perplexity.ai/guides/pricing.
"""
from __future__ import annotations

from decimal import Decimal

from app.agent.providers._openai_compatible import OpenAICompatibleProvider
from app.config import AIProvider, ModelClass


class PerplexityProvider(OpenAICompatibleProvider):
    name = AIProvider.PERPLEXITY.value
    base_url = "https://api.perplexity.ai"

    _PRICING_PER_TOKEN = {  # PLACEHOLDER — verify
        ModelClass.CHEAP: (Decimal("0.0000002"), Decimal("0.0000002")),
        ModelClass.STANDARD: (Decimal("0.000003"), Decimal("0.000015")),
        ModelClass.REASONING: (Decimal("0.000002"), Decimal("0.000008")),
    }
