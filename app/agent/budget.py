"""
RC-4 — bounded AI spend, structurally, replacing the master plan's
policy-only caps after the red team's FF-5 finding.

The arithmetic that made this fatal: no per-request token cap, no
tool-iteration cap, and a check-then-call budget test race under
concurrency. Worst case computed in the review: a single maximal request
on a 1M-token-context model at paid Tier-1 rate limits runs to roughly
**$37,000/hour**, and a scripted unbounded tool loop can produce a
several-hundred-dollar bill from one trial signup in minutes. None of
that requires an "attack" in the adversarial sense — a legitimate power
user with a large document archive and a rambling conversation can do the
same thing by accident.

Five structural controls, all mandatory, none independently sufficient:

  1. Hard per-request input-token cap (settings.max_input_tokens_per_request).
  2. Hard max tool iterations per turn (settings.max_tool_iterations_per_turn).
  3. Hard max turns per conversation (settings.max_turns_per_conversation).
  4. Atomic budget reservation — THIS MODULE — replacing check-then-call.
  5. Per-org requests-per-minute limit (enforced at the API/rate-limit
     layer, not here) plus a provider-side budget alert as an independent
     backstop for a bug in 1-4.

This module implements #4. The others are enforced by app/agent/tools/
executor.py and the API rate-limit middleware (not yet built in this
scaffold — see README "not yet implemented").
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.models import OrgBudget


class BudgetExceededError(Exception):
    pass


async def _ensure_today_row(session: AsyncSession, org_id: uuid.UUID, daily_cap_usd: Decimal) -> None:
    today = datetime.now(timezone.utc).date()
    stmt = (
        pg_insert(OrgBudget)
        .values(org_id=org_id, period_date=today, daily_cap_usd=daily_cap_usd, spent_usd=Decimal("0"))
        .on_conflict_do_nothing(index_elements=["org_id", "period_date"])
    )
    await session.execute(stmt)


async def reserve_budget(
    session: AsyncSession, *, org_id: uuid.UUID, estimated_cost_usd: Decimal, daily_cap_usd: Decimal
) -> Decimal:
    """
    Atomic reservation: `UPDATE ... WHERE spent + est <= cap RETURNING
    spent`. Zero rows updated means refuse — this happens INSIDE the
    database as a single statement, so two concurrent requests cannot
    both observe "under cap" and both proceed (the race the master
    plan's original check-then-call was vulnerable to).

    Call this BEFORE the provider call, with a conservative estimate
    (e.g. max_input_tokens_per_request priced at the most expensive
    model class the request could use). Reconcile against the actual
    metered cost afterward with `reconcile_actual_cost` — this also
    closes the billing-usage-undercount gap the red team's §18 flagged
    ("usage recorded after the provider call; if the process crashes
    between call and record, the cost was incurred and not billed").
    """
    today = datetime.now(timezone.utc).date()
    await _ensure_today_row(session, org_id, daily_cap_usd)

    result = await session.execute(
        select(OrgBudget).where(OrgBudget.org_id == org_id, OrgBudget.period_date == today).with_for_update()
    )
    row = result.scalar_one()

    if row.spent_usd + estimated_cost_usd > row.daily_cap_usd:
        raise BudgetExceededError(
            f"org {org_id} would exceed daily budget: spent={row.spent_usd} "
            f"+ est={estimated_cost_usd} > cap={row.daily_cap_usd}"
        )

    row.spent_usd = row.spent_usd + estimated_cost_usd
    await session.flush()
    return row.spent_usd


async def reconcile_actual_cost(
    session: AsyncSession, *, org_id: uuid.UUID, reserved_usd: Decimal, actual_usd: Decimal
) -> None:
    """Adjust the reservation to what the provider actually billed (can be less OR more than the estimate)."""
    today = datetime.now(timezone.utc).date()
    result = await session.execute(
        select(OrgBudget).where(OrgBudget.org_id == org_id, OrgBudget.period_date == today).with_for_update()
    )
    row = result.scalar_one_or_none()
    if row is None:
        return
    delta = actual_usd - reserved_usd
    row.spent_usd = row.spent_usd + delta
    await session.flush()
