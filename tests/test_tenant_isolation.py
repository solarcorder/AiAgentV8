"""
The gating suite (§28/§32 Phase 2 acceptance: "all 12 isolation tests
pass, including test 12 (raw SQL under RLS). This is the gate for
everything downstream — do not proceed without it.") Numbers in test
names correspond to the master spec's §28 list; RC-5 test 13 (from the
red-team review) is included as well.

Every test here runs through `test_app_role` — NOSUPERUSER, NOBYPASSRLS,
not the table owner — never through the schema-owning connection, which
would make every assertion here vacuous.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from tests.conftest import no_tenant_session, requires_db, tenant_session_as

pytestmark = requires_db


@pytest.mark.asyncio
async def test_list_endpoint_returns_zero_cross_org_rows(app_session_factory, two_orgs):
    """§28 test 1."""
    from app.domain.models.property import Property

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        result = await session.execute(sa.select(Property))
        rows = result.scalars().all()

    assert len(rows) == 1
    assert rows[0].id == two_orgs["property_a_id"]
    assert all(r.org_id == two_orgs["org_a_id"] for r in rows)


@pytest.mark.asyncio
async def test_direct_id_fetch_of_other_org_resource_returns_nothing(app_session_factory, two_orgs):
    """§28 test 2 — the 404-not-403 property, at the data layer: a wrong-org fetch returns no row, full stop."""
    from app.domain.models.property import Property
    from app.domain.repository import TenantRepository

    class PropertyRepository(TenantRepository[Property]):
        model = Property

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        repo = PropertyRepository(session)
        row = await repo.get(two_orgs["org_a_id"], two_orgs["property_b_id"])

    assert row is None


@pytest.mark.asyncio
async def test_composite_fk_rejects_cross_org_reference(app_session_factory, two_orgs):
    """
    §28 test 4. A Lease in org A referencing a property that belongs to
    org B must be rejected by the DATABASE, regardless of what the
    application asks for — this is the strongest control in the design
    per the red-team review (§11: "the database physically refuses
    cross-tenant references").
    """
    from app.domain.models.lease import Lease

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        bad_lease = Lease(
            org_id=two_orgs["org_a_id"],
            property_id=two_orgs["property_b_id"],  # belongs to org B, not org A
            occupant_id=uuid.uuid4(),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )
        session.add(bad_lease)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_12_raw_sql_under_rls_with_org_a_guc_returns_only_org_a_rows(app_session_factory, two_orgs):
    """
    §28 test 12, verbatim intent: "Connect to Postgres as the app role
    with Org A's GUC and assert SELECT * FROM properties returns only
    A's rows — proves RLS independently of the application." No ORM,
    no repository layer, no application filter at all — raw SQL only.
    """
    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        result = await session.execute(sa.text("SELECT id, org_id FROM properties"))
        rows = result.all()

    assert len(rows) == 1
    assert str(rows[0].org_id) == str(two_orgs["org_a_id"])


@pytest.mark.asyncio
async def test_13_mismatched_guc_and_application_org_id_yields_nothing(app_session_factory, two_orgs):
    """
    RC-5 test 13 (red-team review §5 FF-3 fix #4): a scenario where the
    GUC and the application-derived org_id disagree must fail closed, not
    silently serve whichever one "wins." Here the session's GUC is set to
    org B (as if a bug set the wrong tenant context) while the repository
    call still asks for an org-A-scoped row — the two layers should
    combine to a NON-match rather than either one alone deciding the
    outcome.
    """
    from app.domain.models.property import Property
    from app.domain.repository import TenantRepository

    class PropertyRepository(TenantRepository[Property]):
        model = Property

    async with tenant_session_as(app_session_factory, two_orgs["org_b_id"]) as session:  # GUC = org B
        repo = PropertyRepository(session)
        row = await repo.get(two_orgs["org_a_id"], two_orgs["property_a_id"])  # app layer asks for org A's property

    assert row is None  # neither org A's repo filter nor org B's RLS scope alone produces a match


@pytest.mark.asyncio
async def test_unset_guc_fails_closed_to_zero_rows(app_session_factory, two_orgs):
    """FF-3 fix #3: an unset `app.current_org` must yield zero rows, not an exception and not all rows."""
    async with no_tenant_session(app_session_factory) as session:
        result = await session.execute(sa.text("SELECT id FROM properties"))
        rows = result.all()

    assert rows == []


@pytest.mark.asyncio
async def test_scoped_unique_constraint_allows_same_email_across_orgs(app_session_factory, two_orgs):
    """
    §10.3 rule 4: uniqueness on tenant data is always `UNIQUE(org_id, ...)`,
    never bare `UNIQUE(email)` — the same occupant email must be usable
    independently in two different orgs (this is also the fix for T16 /
    S15, the legacy system's unscoped `WHERE email = $1 LIMIT 1`).
    """
    from app.domain.models.occupant import Occupant

    shared_email = f"shared-{uuid.uuid4()}@example.com"

    async with tenant_session_as(app_session_factory, two_orgs["org_a_id"]) as session:
        session.add(Occupant(org_id=two_orgs["org_a_id"], first_name="A", email=shared_email))
        await session.flush()

    async with tenant_session_as(app_session_factory, two_orgs["org_b_id"]) as session:
        session.add(Occupant(org_id=two_orgs["org_b_id"], first_name="B", email=shared_email))
        await session.flush()  # must NOT raise — this is the whole point of the scoped constraint
