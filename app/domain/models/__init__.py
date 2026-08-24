"""
Importing this package registers every domain model on app.db.base.Base's
metadata. migrations/env.py and app/db/boot_assertions.py both depend on
that happening exactly once, before Alembic autogenerate or the boot
assertion queries pg_catalog.
"""
from app.auth import models as _auth_models  # noqa: F401  (users, memberships, invitations)
from app.domain.models import (  # noqa: F401
    document,
    event,
    expense,
    lease,
    maintenance_request,
    occupant,
    org,
    payment,
    property,
    task,
    unit,
    vendor,
)
from app.credentials import models as _credential_models  # noqa: F401
from app.approvals import models as _approval_models  # noqa: F401
from app.jobs import models as _job_models  # noqa: F401
from app.agent import models as _agent_models  # noqa: F401
from app.integrations import email_inbound_models as _email_inbound_models  # noqa: F401
from app.core import idempotency as _idempotency_models  # noqa: F401
