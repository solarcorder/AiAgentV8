"""
Worker process entrypoint. Run as a separate process/container from the
web tier (RC-9: the internet-facing process should not hold the
credentials needed for bulk KMS unwrap operations; only the worker does).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import socket
import uuid
from typing import Any, Awaitable, Callable

from app.approvals import service as approval_service
from app.config import get_settings
from app.db.session import tenant_session, worker_session
from app.jobs import queue
from app.jobs.models import Job
from app.logging import log_event

logger = logging.getLogger("worker")

JobHandler = Callable[[Job], Awaitable[None]]

_HANDLERS: dict[str, JobHandler] = {}


def register_handler(job_type: str):
    def _decorator(fn: JobHandler) -> JobHandler:
        _HANDLERS[job_type] = fn
        return fn

    return _decorator


async def _verify_approval_binding(job: Job) -> None:
    """
    RC-6's core fix: if this job executes an approved action, re-verify
    the payload hash immediately before doing anything externally
    visible. This closes the window between "a human approved X" and
    "the system did X" — without it, a bug (or a compromised worker)
    could enqueue a job that claims approval but carries a different
    payload than what was actually approved.
    """
    if job.approval_id is None:
        return
    canonical = json.dumps(job.payload, sort_keys=True, separators=(",", ":"))
    actual_hash = hashlib.sha256(canonical.encode()).hexdigest()
    if actual_hash != job.approved_payload_hash:
        raise ValueError(
            f"job {job.id} payload does not match its approval's approved_payload_hash — refusing to execute"
        )


async def _execute_one(job: Job) -> None:
    handler = _HANDLERS.get(job.job_type)
    if handler is None:
        raise ValueError(f"no handler registered for job_type={job.job_type!r}")

    await _verify_approval_binding(job)

    if job.approval_id is not None:
        async with tenant_session(job.org_id) as session:
            await approval_service.mark_executing(session, job.approval_id)

    await handler(job)

    if job.approval_id is not None:
        async with tenant_session(job.org_id) as session:
            await approval_service.mark_executed(session, job.approval_id)


async def run_forever(*, poll_interval: float | None = None, batch_size: int = 10) -> None:
    settings = get_settings()
    worker_id = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
    interval = poll_interval or settings.job_poll_interval_seconds

    logger.info("worker starting: %s", worker_id)

    while True:
        async with worker_session() as session:
            reaped = await queue.reap_stale_locks(
                session, visibility_timeout_seconds=settings.job_visibility_timeout_seconds
            )
            if reaped:
                log_event(logger, logging.WARNING, "jobs_reaped", count=reaped)

            jobs = await queue.dequeue_batch(session, worker_id=worker_id, limit=batch_size)

        for job in jobs:
            try:
                await _execute_one(job)
                async with worker_session() as session:
                    await queue.mark_completed(session, job.id)
            except Exception as exc:  # noqa: BLE001 — job execution failures are data, not process crashes
                log_event(logger, logging.ERROR, "job_failed", job_id=str(job.id), job_type=job.job_type, error=str(exc))
                async with worker_session() as session:
                    await queue.mark_failed(session, job, str(exc))
                if job.approval_id is not None:
                    async with tenant_session(job.org_id) as tsession:
                        await approval_service.mark_failed(tsession, job.approval_id, str(exc))

        if not jobs:
            await asyncio.sleep(interval)


if __name__ == "__main__":
    from app.logging import configure_logging

    configure_logging(get_settings().log_level)
    asyncio.run(run_forever())
