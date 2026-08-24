# AiAgentV8 — Real Estate AI SaaS backend

This is a scaffold, not a finished product. It implements Phase 1
(Foundation) and Phase 2 (Tenancy and Schema) of the migration plan in
the master architecture spec, with the red-team review's nine
pre-implementation blockers designed in from the start rather than
retrofitted — retrofitting tenant isolation and credential security onto
working code is exactly the mistake the review found in the plan it was
reviewing.

If you have not read them, the two documents this scaffold implements
are:

- **Master Architecture & Implementation Specification** — the target
  architecture: FastAPI modular monolith, `org_id` + RLS multi-tenancy,
  envelope-encrypted per-tenant credentials, n8n removed from the
  customer path, no restricted Google OAuth scopes, approval tokens
  withheld from the model.
- **Red-Team Review** — hostile review of that spec. Verdict was **NO-GO
  pending nine blockers**; six of them rose to "fatal" (unbounded Twilio
  liability, broken crypto-shredding deletion, overclaimed RLS
  independence, unauthenticated inbound email, unbounded AI cost
  exposure, unverified Gemini free-tier data handling). This scaffold is
  built to the review's *corrected* architecture (the RC-1..RC-9 changes
  in its §25), not the original plan.

Every non-trivial file in this codebase has a docstring explaining which
decision or finding it implements and why — read those before changing
the security-critical modules (`app/db/session.py`,
`app/db/boot_assertions.py`, `app/credentials/`, `app/approvals/`,
`app/jobs/`). They are not incidental comments; they are the reasoning
you need before touching that code safely.

## What's actually here

```
app/
  config.py            Settings — model IDs and cost caps are config, never code (§17.3)
  logging.py            Structured JSON logs with structural field redaction (§24)
  db/
    base.py              Declarative base, TenantMixin/TimestampMixin/SoftDeleteMixin
    session.py            tenant_session()/system_session()/worker_session() — READ THIS FIRST
    boot_assertions.py     RC-5: refuses to start if RLS isn't provably enforced
    rls.py                  RLS + role-grant SQL shared by the migration and the test suite
  auth/                  Identity + capability-based authorization (§11) — buy, don't build
  tenancy/               org_id resolution — session -> membership -> org_id, NEVER from a request
  domain/                Portfolio schema: properties, leases, occupants, payments, etc. (§14.2)
  credentials/            Envelope-encrypted vault with PER-ORG KMS keys (RC-2, fixes FF-2)
  approvals/               Human-in-the-loop approval service, token withheld from the model (§18.3, RC-6)
  jobs/                    Postgres-backed queue, FOR UPDATE SKIP LOCKED, reaper (§22, fixes W3)
  agent/                  Model provider abstraction, atomic budget caps (RC-4, fixes FF-5), tool executor
  integrations/            BYO-Twilio (RC-1, fixes FF-1); authenticated inbound email + quarantine (RC-3, fixes FF-4)
  api/v1/                 FastAPI routes — no endpoint ever accepts org_id (§20)
migrations/               Alembic — 0001 creates the full schema + RLS + composite FKs in one gated migration
tests/                    The cross-tenant isolation suite (§28) — the gate for everything downstream
```

## What is deliberately NOT here yet

This is a scaffold of the parts that gate everything else, not a
finished SaaS. Explicitly out of scope for this pass:

- Real provider SDK calls (Gemini, Twilio, a real inbound-email provider,
  Google OAuth). `app/agent/provider.py` and `app/integrations/*` define
  the correct interfaces and raise `NotImplementedError` where a real API
  call belongs — wiring those up needs real vendor credentials this
  scaffold doesn't have.
- Billing (Stripe), the frontend, the operator/admin console, rate
  limiting middleware, and most of Phase 5–9 in the migration plan.
- A real KMS integration (`app/credentials/kms.py`'s `GCPKMSProvider` is
  a stub). Development/tests use `LocalFileKMSProvider`, which is
  explicitly documented as NOT a security control — it only exists so
  the per-org-key-destruction code path behaves correctly without cloud
  credentials.
- Cache-key tenancy rules (no Redis yet — nothing to scope).

## Before you build on this

The red-team review's blockers list still applies. In particular, do
these before processing any real customer data, in this order (per the
review's §30):

1. **Verify the tier of any live Gemini key.** `GeminiProvider` refuses
   to start in production unless `GEMINI_BILLING_ENABLED=true` (RC-7),
   but that flag is only as honest as whoever set it.
2. **Send the n8n licensing email** if n8n is retained for any purpose —
   see the master spec §0 and §15. Nothing in this repo touches n8n.
3. **Verify Postgres pooler behavior with `SET LOCAL`** against whatever
   managed Postgres you actually deploy to — `app/db/session.py`'s
   `tenant_session()` depends on it, and
   `settings.db_pooler_verified_transaction_safe` defaults to `false`
   for a reason.
4. Decide BYO-Twilio vs. subaccounts consciously — `app/integrations/
   twilio_adapter.py` refuses subaccount mode until FF-1's mitigations
   are actually implemented, not just configured.

## Running it locally

```bash
cp .env.example .env
docker compose up --build
```

This starts Postgres (seeded with the `migrator`/`app_user` roles via
`scripts/create_roles.sql`), runs the Alembic migration (which builds the
schema, enables + forces RLS on every tenant table, and grants the
worker role its narrow `jobs`-only cross-tenant exception), then starts
the API on `:8000` and the background worker.

`GET /health` checks DB connectivity. Nothing else is meaningfully
usable yet without `AUTH_JWKS_URL` pointed at a real auth provider (§11)
and at least one seeded org/membership — see `tests/conftest.py`'s
`two_orgs` fixture for the shape of a minimal seed.

## Running the tests

```bash
pip install -e ".[dev]"
createdb aiagentv8_test   # or point TEST_DATABASE_URL at any throwaway Postgres you can CREATE ROLE in
export TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/aiagentv8_test
pytest
```

`tests/test_credential_vault.py` needs no database. Everything else is
skipped (not failed) without `TEST_DATABASE_URL` — a missing test
database is an environment gap, not a code defect.

`tests/test_tenant_isolation.py` is the suite the migration plan calls
"the gate for everything downstream" (§32 Phase 2 acceptance: "all 12
isolation tests pass, including test 12 — raw SQL under RLS"). Do not
build Phase 3+ (real auth wiring, the agent loop, integrations) on a
branch where this suite is red.

## The one file worth understanding before changing anything

`app/db/session.py`. The red team's FF-3 finding, in one sentence: RLS
and composite foreign keys are provably correct, but tenant *resolution*
— "which org is this request for" — rests on one small piece of
middleware being right, and nothing else in the four-layer design is
independent of it. That middleware is `tenant_session()` plus
`app/tenancy/context.py`'s `resolve_tenant_context()`. Treat changes
there as security-sensitive changes requiring the full isolation suite,
not just the touched code path.
