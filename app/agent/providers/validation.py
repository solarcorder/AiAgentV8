"""
Live key validation for POST /v1/ai-providers/{provider}/connect —
the follow-up flagged in README's "What is deliberately NOT here yet":
the prefix-hint check in app/config.py's AI_PROVIDER_METADATA catches an
obvious paste error, never a wrong-but-valid-looking key. This module
makes one minimal, real, read-only call against each provider's own API
(listing models — never a completion call, so it never spends real
tokens) to confirm the key actually authenticates before it's encrypted
and stored.

Deliberately conservative about false positives: this only raises
ProviderKeyValidationError on an explicit authentication failure
(the SDK's own "invalid api key" exception, HTTP 401/403). Any other
failure — network blip, provider outage, an endpoint the provider
doesn't implement — is logged and treated as "couldn't confirm," not
"confirmed invalid," so a flaky check or an unsupported endpoint never
blocks a legitimate key from being connected. The prefix-hint check
ahead of this one in app/api/v1/ai_providers.py is unaffected.

Gemini has no validation here: GeminiProvider.complete() itself is not
wired to a real SDK yet (see gemini_provider.py) and no Gemini SDK
dependency is installed in this codebase — validating a BYO Gemini key is
a follow-up tied to actually wiring up google-genai, not a gap unique to
this module.
"""
from __future__ import annotations

import logging

from app.config import AIProvider
from app.logging import log_event

logger = logging.getLogger("provider_key_validation")


class ProviderKeyValidationError(Exception):
    """Raised only on a confirmed authentication failure — never on an ambiguous/network error."""


async def _validate_anthropic_key(api_key: str) -> None:
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=api_key)
    try:
        await client.models.list(limit=1)
    except anthropic.AuthenticationError as exc:
        raise ProviderKeyValidationError("Anthropic rejected this key (authentication failed)") from exc
    except anthropic.APIError as exc:
        log_event(logger, logging.WARNING, "anthropic_key_validation_inconclusive", error=str(exc))


async def _validate_openai_compatible_key(api_key: str, *, provider: str, base_url: str | None) -> None:
    import openai

    client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
    try:
        await client.models.list()
    except openai.AuthenticationError as exc:
        raise ProviderKeyValidationError(f"'{provider}' rejected this key (authentication failed)") from exc
    except openai.APIError as exc:
        # Covers providers (Perplexity in particular) that don't implement
        # GET /models at all — a 404/501 there is not evidence the KEY is
        # bad, just that this specific probe endpoint doesn't exist.
        log_event(logger, logging.WARNING, "openai_compatible_key_validation_inconclusive", provider=provider, error=str(exc))


async def validate_provider_key(provider: str, api_key: str) -> None:
    """
    Best-effort live check. Callers should treat a clean return as "not
    proven invalid" rather than "proven valid" — see the module docstring
    on why ambiguous failures don't raise.
    """
    if provider == AIProvider.ANTHROPIC.value:
        await _validate_anthropic_key(api_key)
    elif provider == AIProvider.OPENAI.value:
        await _validate_openai_compatible_key(api_key, provider=provider, base_url=None)
    elif provider == AIProvider.PERPLEXITY.value:
        await _validate_openai_compatible_key(api_key, provider=provider, base_url="https://api.perplexity.ai")
    elif provider == AIProvider.DEEPSEEK.value:
        await _validate_openai_compatible_key(api_key, provider=provider, base_url="https://api.deepseek.com")
    # AIProvider.GEMINI: no-op — see module docstring.
