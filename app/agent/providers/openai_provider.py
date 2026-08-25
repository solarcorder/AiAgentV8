"""
GPT, via the official `openai` SDK. BYO-only — org's own key from the
vault, decrypted by app/agent/providers/registry.py before this class is
instantiated.

Pricing below is a PLACEHOLDER — no live-verification tool for OpenAI in
this codebase. Re-check against platform.openai.com/docs/pricing before
this feeds a real budget-reservation calculation (app/agent/budget.py).
"""
from __future__ import annotations

from decimal import Decimal

from app.agent.providers._openai_compatible import OpenAICompatibleProvider
from app.config import AIProvider, ModelClass


class OpenAIProvider(OpenAICompatibleProvider):
    name = AIProvider.OPENAI.value
    base_url = None  # SDK default: api.openai.com

    _PRICING_PER_TOKEN = {  # PLACEHOLDER — verify
        ModelClass.CHEAP: (Decimal("0.00000025"), Decimal("0.000002")),
        ModelClass.STANDARD: (Decimal("0.0000025"), Decimal("0.00001")),
        ModelClass.REASONING: (Decimal("0.000015"), Decimal("0.00006")),
    }
