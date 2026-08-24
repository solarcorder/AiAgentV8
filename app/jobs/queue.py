"""
Queue operations. `enqueue` is meant to be called inside the SAME
transaction as the business write that causes it (the outbox pattern,
§14.5) — pass it a `tenant_session()` that is already inside a
transaction and do not commit until both the domain write and the
enqueue are ready to go together.

`dequeue_batch` / `reap_stale_locks` run against `worker_session()`
instead — see that function's docstring for why cross-org polling is a
distinct, narrowly-scoped exception rather than a hole in RLS.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.models import Job, JobStatus


class DuplicateJobError(Exception):
    """The idempotency key already exists — the caller should treat this as success, not retry."""


async def enqueue(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    job_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
    run_after: datetime | None = None,
    max_attempts: int = 5,
    approval_id: uuid.UUID | None = None,
    approved_payload_hash: str | None = None,
) -> Job:
    """
    Idempotent by `idempotency_key`, which callers derive from the SOURCE
    EVENT (e.g. f"send-sms:{org_id}:{approval_id}"), never from a
    timestamp or a value generated fresh at send time — a fresh key on
    retry would defeat the whole point of the guard (§16, normative
    idempotency requirements).
    """
    stmt = (
        pg_insert(Job)
        .values(
            org_id=org_id,
            job_type=job_type,
            payload=payload,
            idempotency_key=idempotency_key,
            run_after=run_after or datetime.now(timezone.utc),
            max_attempts=max_attempts,
            approval_id=approval_id,
            approved_payload_hash=approved_payload_hash,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(Job.id)
    )
    result = await session.execute(stmt)
    job_id = result.scalar_one_or_none()

    if job_id is None:
        # Idempotency key collision: this exact side effect was already
        # enqueued. Fetch and return the existing row rather than erroring
        # — the caller almost always wants "ensure this happens once,"
        # which this already satisfies.
        existing = await session.execute(select(Job).where(Job.idempotency_key == idempotency_key))
        return existing.scalar_one()

    result = await session.execute(select(Job).where(Job.id == job_id))
    return result.scalar_one()


async def dequeue_batch(session: AsyncSession, *, worker_id: str, limit: int = 10) -> list[Job]:
    """
    Uses `FOR UPDATE SKIP LOCKED` so multiple worker processes can poll
    concurrently without blocking each other on the same rows. Call
    against `worker_session()`.
    """
    locked_rows = await session.execute(
        select(Job.id)
        .where(Job.status == JobStatus.PENDING, Job.run_after <= datetime.now(timezone.utc))
        .order_by(Job.run_after)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    ids = [row[0] for row in locked_rows.all()]
    if not ids:
        return []

    await session.execute(
        update(Job)
        .where(Job.id.in_(ids))
        .values(status=JobStatus.RUNNING, locked_by=worker_id, locked_at=datetime.now(timezone.utc))
    )
    result = await session.execute(select(Job).where(Job.id.in_(ids)))
    return list(result.scalars().all())


async def mark_completed(session: AsyncSession, job_id: uuid.UUID) -> None:
    await session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(status=JobStatus.COMPLETED, completed_at=datetime.now(timezone.utc), locked_by=None, locked_at=None)
    )


async def mark_failed(session: AsyncSession, job: Job, error: str) -> None:
    """Exponential-backoff retry until max_attempts, then dead-letter rather than retrying forever."""
    attempts = job.attempts + 1
    if attempts >= job.max_attempts:
        await session.execute(
            update(Job)
            .where(Job.id == job.id)
            .values(status=JobStatus.DEAD_LETTER, attempts=attempts, last_error=error, locked_by=None, locked_at=None)
        )
        return

    backoff_seconds = min(2**attempts, 300)
    await session.execute(
        update(Job)
        .where(Job.id == job.id)
        .values(
            status=JobStatus.PENDING,
            attempts=attempts,
            last_error=error,
            locked_by=None,
            locked_at=None,
            run_after=datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds),
        )
    )


async def reap_stale_locks(session: AsyncSession, *, visibility_timeout_seconds: int) -> int:
    """
    W3 fix: nothing previously reclaimed a job whose worker crashed
    mid-execution (locked_by/locked_at existed, but no reaper ever read
    them). A worker OOM or a killed container left the job RUNNING
    forever — a silent stall, not a visible failure. Run this on a timer
    from the worker's own supervisory loop.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=visibility_timeout_seconds)
    result = await session.execute(
        update(Job)
        .where(Job.status == JobStatus.RUNNING, Job.locked_at < cutoff)
        .values(status=JobStatus.PENDING, locked_by=None, locked_at=None)
        .returning(Job.id)
    )
    return len(result.all())
