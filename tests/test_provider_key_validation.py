"""
app/agent/providers/validation.py — pure logic, no live provider network
calls. Monkeypatches each SDK's async client class so these tests exercise
the dispatch + "only a confirmed auth failure raises" behaviour without
ever hitting a real API.
"""
from __future__ import annotations

import anthropic
import httpx
import openai
import pytest

from app.agent.providers.validation import ProviderKeyValidationError, validate_provider_key


def _auth_error(exc_cls):
    request = httpx.Request("GET", "https://example.test/v1/models")
    response = httpx.Response(status_code=401, request=request)
    return exc_cls("invalid api key", response=response, body=None)


def _other_error(exc_cls):
    request = httpx.Request("GET", "https://example.test/v1/models")
    response = httpx.Response(status_code=404, request=request)
    return exc_cls("not found", response=response, body=None)


class _FakeModelsResource:
    def __init__(self, exc: Exception | None) -> None:
        self._exc = exc

    async def list(self, *args, **kwargs):
        if self._exc is not None:
            raise self._exc
        return []


class _FakeAnthropicClient:
    def __init__(self, *, api_key: str, exc: Exception | None = None) -> None:
        self.models = _FakeModelsResource(exc)


class _FakeOpenAIClient:
    def __init__(self, *, api_key: str, base_url: str | None = None, exc: Exception | None = None) -> None:
        self.models = _FakeModelsResource(exc)


@pytest.mark.asyncio
async def test_anthropic_authentication_error_raises(monkeypatch):
    monkeypatch.setattr(
        anthropic, "AsyncAnthropic", lambda api_key: _FakeAnthropicClient(api_key=api_key, exc=_auth_error(anthropic.AuthenticationError))
    )
    with pytest.raises(ProviderKeyValidationError):
        await validate_provider_key("anthropic", "sk-ant-bad-key")


@pytest.mark.asyncio
async def test_anthropic_valid_key_does_not_raise(monkeypatch):
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda api_key: _FakeAnthropicClient(api_key=api_key))
    await validate_provider_key("anthropic", "sk-ant-good-key")  # must not raise


@pytest.mark.asyncio
async def test_anthropic_ambiguous_error_does_not_raise(monkeypatch):
    """A non-auth APIError (e.g. a transient 5xx) must not block connecting a possibly-valid key."""
    monkeypatch.setattr(
        anthropic, "AsyncAnthropic", lambda api_key: _FakeAnthropicClient(api_key=api_key, exc=_other_error(anthropic.NotFoundError))
    )
    await validate_provider_key("anthropic", "sk-ant-good-key")  # must not raise


@pytest.mark.asyncio
async def test_openai_authentication_error_raises(monkeypatch):
    monkeypatch.setattr(
        openai,
        "AsyncOpenAI",
        lambda api_key, base_url=None: _FakeOpenAIClient(api_key=api_key, base_url=base_url, exc=_auth_error(openai.AuthenticationError)),
    )
    with pytest.raises(ProviderKeyValidationError):
        await validate_provider_key("openai", "sk-bad-key")


@pytest.mark.asyncio
async def test_perplexity_unsupported_models_endpoint_does_not_raise(monkeypatch):
    """Perplexity doesn't document a GET /models endpoint — a 404 there must not be treated as an invalid key."""
    monkeypatch.setattr(
        openai,
        "AsyncOpenAI",
        lambda api_key, base_url=None: _FakeOpenAIClient(api_key=api_key, base_url=base_url, exc=_other_error(openai.NotFoundError)),
    )
    await validate_provider_key("perplexity", "pplx-good-key")  # must not raise


@pytest.mark.asyncio
async def test_gemini_is_a_no_op():
    """No Gemini SDK dependency is installed — validating a BYO Gemini key is out of scope here (see module docstring)."""
    await validate_provider_key("gemini", "any-key-at-all")  # must not raise, must not attempt a call
