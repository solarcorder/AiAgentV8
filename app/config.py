"""
Central configuration.

Two deliberate design choices baked in from the red-team review:

- Model IDs are configuration, not code (S9 / §17.3): a provider retiring a
  model is a config change, not a deploy.
- The database URL used by the running app must point at a role that is
  NOT the table owner and does NOT have BYPASSRLS (§10 security
  implications). Migrations run under a *separate* role/URL, out-of-band.
  boot_assertions.py verifies this at startup rather than trusting that
  whoever wrote the .env file got it right.
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelClass(str, Enum):
    """Task-class routing, never a specific model ID in code (§17.3)."""

    CHEAP = "CHEAP"  # e.g. triage, extraction — Flash-Lite / Haiku class
    STANDARD = "STANDARD"  # conversational agent with tools — Flash / Sonnet class
    REASONING = "REASONING"  # rare, explicit, logged — Pro / Opus class


class AIProvider(str, Enum):
    """
    Client-facing model choice, layered on top of ModelClass. Every
    provider except Gemini is BYO-only (app/agent/providers/registry.py):
    the org supplies and pays for their own key, mirroring RC-1's
    BYO-Twilio pattern — same reasoning (liability and cost sit with
    whoever's credential is actually being billed), applied to AI
    providers instead of messaging. Gemini alone keeps an operator-paid
    fallback so a brand-new org isn't dead on arrival before connecting
    any key of their own.
    """

    GEMINI = "gemini"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    PERPLEXITY = "perplexity"
    DEEPSEEK = "deepseek"


# Display metadata + key-format hints for the connect-a-provider UI
# (app/api/v1/ai_providers.py) and light client-side validation before an
# obviously-wrong key is even encrypted and stored. NOT a security
# control — the real check is the provider actually accepting the key on
# first call.
AI_PROVIDER_METADATA: dict[str, dict[str, object]] = {
    AIProvider.GEMINI.value: {
        "display_name": "Google Gemini",
        "key_prefix_hint": None,
        "operator_fallback_available": True,
        "supports_search_grounding": True,  # the "Google AI" answer-box behaviour — see gemini_provider.py
    },
    AIProvider.OPENAI.value: {
        "display_name": "OpenAI (GPT)",
        "key_prefix_hint": "sk-",
        "operator_fallback_available": False,
        "supports_search_grounding": False,
    },
    AIProvider.ANTHROPIC.value: {
        "display_name": "Anthropic (Claude)",
        "key_prefix_hint": "sk-ant-",
        "operator_fallback_available": False,
        "supports_search_grounding": False,
    },
    AIProvider.PERPLEXITY.value: {
        "display_name": "Perplexity",
        "key_prefix_hint": "pplx-",
        "operator_fallback_available": False,
        "supports_search_grounding": False,  # Perplexity is itself search-grounded by default; not the same feature
    },
    AIProvider.DEEPSEEK.value: {
        "display_name": "DeepSeek",
        "key_prefix_hint": "sk-",
        "operator_fallback_available": False,
        "supports_search_grounding": False,
    },
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_nested_delimiter="__", extra="ignore")

    env: str = Field(default="development")  # development | staging | production

    # --- Database -----------------------------------------------------
    # Request-serving connection: app role, NOT owner, NOT BYPASSRLS.
    database_url: str = Field(
        default="postgresql+asyncpg://app_user:app_password@localhost:5432/aiagentv8",
        description="Connection used by the running application. Must not have BYPASSRLS.",
    )
    # Out-of-band connection for Alembic only. Never used by request-serving code.
    migrations_database_url: str = Field(
        default="postgresql+psycopg://migrator:migrator_password@localhost:5432/aiagentv8",
    )
    # Distinct role for the background worker process. Per §10.3 rule 10,
    # cross-tenant queries are only ever allowed through a "separately
    # named, individually audited" path — the worker's need to poll
    # `jobs` across every org is exactly that case. This role has its own
    # narrow RLS policy (see migrations/versions/0001) granting it full
    # visibility on `jobs` ONLY, not on any other tenant table. It must
    # NOT be the same role as the web tier's (RC-9: web tier has no
    # access to org_credentials; only the worker does).
    worker_database_url: str = Field(
        default="postgresql+asyncpg://worker_role:worker_password@localhost:5432/aiagentv8",
    )
    db_pool_size: int = 10
    db_pool_max_overflow: int = 5

    # §10: with a transaction-mode pooler SET LOCAL is unsafe unless it runs
    # inside the same transaction as the query. Set this to True only after
    # verifying pooler behaviour against test 13 in tests/test_tenant_isolation.py.
    db_pooler_verified_transaction_safe: bool = False

    # --- Auth (buy, don't build — §11) ---------------------------------
    auth_jwks_url: str | None = None
    auth_issuer: str | None = None
    auth_audience: str | None = None
    session_cookie_name: str = "aiagentv8_session"

    # --- Credential vault / KMS (RC-2: per-org keys, not one master) ---
    kms_provider: str = Field(default="local_dev", description="local_dev | gcp_kms | aws_kms")
    # local_dev only: directory OUTSIDE the database backup set where
    # per-org key material lives, so "destroy the key" is actually true
    # even in development. Never used in production.
    local_dev_kms_key_dir: str = Field(default="./.local_kms_keys")

    # --- AI provider: operator-held fallback (Gemini only) ----------------
    # This is NOT where OpenAI/Anthropic/Perplexity/DeepSeek keys go — those
    # are strictly BYO, stored per-org in the credential vault
    # (org_credentials, provider="openai"|"anthropic"|...), never here.
    # This section exists only for the one operator-paid default channel.
    gemini_api_key: str | None = None
    # RC-7: production must refuse to boot against a non-billing-enabled
    # (free-tier) project — occupant data on a free key is a data-protection
    # incident, not a bug (FF-6). Applies to this operator key only; a BYO
    # key's tier is the connecting org's own responsibility and risk.
    gemini_billing_enabled: bool = Field(
        default=False,
        description="Set true only once the configured Gemini project/key is verified paid-tier.",
    )
    # Gemini's Search-grounding mode (live Google Search results feeding the
    # answer, with citations) — the closest legitimate, ToS-compliant match
    # to "the AI answer that appears in Google Search." Not a separate
    # provider; it is a request-time flag on Gemini calls. Scraping the
    # actual Search results page for its AI Overview box was considered and
    # rejected — it violates Google's ToS and is fragile/blockable by
    # design, the same category of thing §12.1 already ruled out for Gmail.
    gemini_search_grounding_enabled: bool = Field(default=True)

    # Per-provider, per-task-class model IDs — nested one level deeper than
    # before to add the provider dimension. Anthropic's IDs below are
    # live-verified (claude-api skill, cached 2026-06-24: claude-opus-5
    # $5/$25 per 1M, claude-sonnet-5 $2/$10, claude-haiku-4-5 $1/$5 — see
    # that skill for current pricing before changing these). OpenAI,
    # Perplexity, and DeepSeek entries are PLACEHOLDERS with no equivalent
    # live-verification tool in this codebase yet — re-verify against each
    # provider's own current model list before routing real traffic to
    # them, the same "VERIFY" discipline the master spec applied to every
    # unconfirmed price or model ID rather than presenting a guess as fact.
    model_routing: dict[str, dict[str, str]] = Field(
        default_factory=lambda: {
            AIProvider.GEMINI.value: {
                ModelClass.CHEAP.value: "gemini-3.5-flash-lite",
                ModelClass.STANDARD.value: "gemini-3.5-flash",
                ModelClass.REASONING.value: "gemini-3.1-pro",
            },
            AIProvider.ANTHROPIC.value: {
                ModelClass.CHEAP.value: "claude-haiku-4-5",
                ModelClass.STANDARD.value: "claude-sonnet-5",
                ModelClass.REASONING.value: "claude-opus-5",
            },
            AIProvider.OPENAI.value: {  # PLACEHOLDER — verify at platform.openai.com/docs/models
                ModelClass.CHEAP.value: "gpt-5-mini",
                ModelClass.STANDARD.value: "gpt-5",
                ModelClass.REASONING.value: "gpt-5-pro",
            },
            AIProvider.PERPLEXITY.value: {  # PLACEHOLDER — verify at docs.perplexity.ai
                ModelClass.CHEAP.value: "sonar",
                ModelClass.STANDARD.value: "sonar-pro",
                ModelClass.REASONING.value: "sonar-reasoning-pro",
            },
            AIProvider.DEEPSEEK.value: {  # PLACEHOLDER — verify at platform.deepseek.com/docs
                ModelClass.CHEAP.value: "deepseek-chat",
                ModelClass.STANDARD.value: "deepseek-chat",
                ModelClass.REASONING.value: "deepseek-reasoner",
            },
        }
    )

    # --- AI cost controls, structural not policy (RC-4 / FF-5) ---------
    max_input_tokens_per_request: int = 60_000
    max_tool_iterations_per_turn: int = 8
    max_turns_per_conversation: int = 40
    default_daily_budget_usd: float = 10.00
    trial_daily_budget_usd: float = 2.00
    default_org_requests_per_minute: int = 30

    # --- Jobs ------------------------------------------------------------
    job_visibility_timeout_seconds: int = 300  # W3: reclaim locks older than this
    job_poll_interval_seconds: float = 1.0
    job_max_attempts_default: int = 5

    # --- Approvals (§18 / RC-6) ------------------------------------------
    approval_default_ttl_seconds: int = 30 * 60

    # --- Messaging (RC-1: BYO-Twilio is the default, not subaccounts) ---
    twilio_mode: str = Field(default="byo", description="byo | subaccount (subaccount requires FF-1 mitigations)")

    # --- Inbound email (RC-3) --------------------------------------------
    inbound_email_domain: str = Field(default="inbound.example.com")
    inbound_address_rotation_overlap_days: int = 30

    # --- Observability -----------------------------------------------------
    log_level: str = "INFO"
    sentry_dsn: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
