"""
Model provider abstraction — §17.3, generalized from a single Gemini
implementation to a multi-provider system (GPT, Claude, Perplexity,
DeepSeek, Gemini) behind one interface.

What stays true from the original single-provider design, and why it
generalizes cleanly rather than needing a rewrite: calling code was
already written against `ModelClass` (CHEAP/STANDARD/REASONING), never a
concrete model ID — §17.3's whole point was that a model retiring (S9,
Gemini 2.0 Flash) should be a config change, not a code change. Adding
providers is the same shape of change one level up: calling code now
asks the registry (registry.py) for "this org's provider," gets back
something implementing `ModelProvider`, and calls `.complete()` exactly
as before. Nothing about the tool executor, the budget system, or the
approval flow needed to change for this.

Credential model: every concrete provider except Gemini's operator
fallback decrypts an org-owned key from the vault (app/credentials/
vault.py) — see registry.py for the BYO resolution logic. A provider
class itself never touches the vault directly; it receives an
already-decrypted key string and holds it only for the duration of one
call, same discipline as any other credential use in this codebase (§13:
"decrypt in memory only, never to agent/browser").
"""
from __future__ import annotations

import abc
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.config import ModelClass


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
    provider: str
    # Populated only for a Gemini call made with search grounding on —
    # the "Google AI answer" behaviour (app/config.py
    # gemini_search_grounding_enabled). Empty for every other provider
    # and for ungrounded Gemini calls.
    grounding_citations: list[str] | None = None


class ModelProvider(abc.ABC):
    """One instance per (org, provider) resolved call — see registry.py. Never cached across orgs."""

    name: str  # matches an app.config.AIProvider value; set by each concrete subclass

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
        enable_search_grounding: bool = False,
    ) -> ModelResponse:
        """
        `tools_enabled=False` MUST be honoured structurally, not by
        convention — §18.2/§9's fix for indirect prompt injection is that
        classification calls (triage, extraction) run WITHOUT a tool
        registry available at all, so injected text has nothing to
        invoke even if it fully succeeds at manipulating the model's
        output. This applies identically regardless of which provider is
        serving the call.

        `enable_search_grounding` is meaningful only to GeminiProvider —
        the "Google AI answer" mode (live Google Search results feeding
        the response, with citations; see gemini_provider.py). Every
        other provider accepts and silently ignores it, so calling code
        doesn't need an `isinstance` check to request it — it either does
        something or it's a no-op, never an error.
        """
