"""
§19: "approvals inbox — the highest-value screen — it is where humans
exercise the authority §18 gives them." This is the API behind it.

Note what is deliberately absent: there is no endpoint that accepts a
bare token in a URL for one-click consumption. §18.3 step 5 requires an
AUTHENTICATED session to consume an approval; RC-6 additionally requires
that an emailed link carry only an opaque `approval_id`, never the raw
token (W5: a raw token in a URL lands in mail-provider logs, browser
history, and any third-party asset's Referer header). The raw token is
something a human reads off a dashboard card or a delivered code and
types/pastes into an authenticated session — see `ConsumeApprovalRequest`
below, where `token` is a request BODY field on an authenticated POST,
never a path or query parameter.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.approvals import service as approval_service
from app.auth.dependencies import require_capability
from app.db.session import tenant_session
from app.tenancy.context import TenantContext

router = APIRouter(prefix="/v1/approvals", tags=["approvals"])


class ConsumeApprovalRequest(BaseModel):
    model_config = {"extra": "forbid"}

    token: str
    expected_payload_hash: str


class ConsumeApprovalResponse(BaseModel):
    approval_id: str
    status: str


@router.post("/{approval_id}/consume", response_model=ConsumeApprovalResponse)
async def consume_approval(
    approval_id: str,
    body: ConsumeApprovalRequest,
    context: TenantContext = Depends(require_capability("approval.consume")),
):
    from fastapi import HTTPException

    async with tenant_session(context.org_id) as session:
        try:
            approval = await approval_service.consume_approval(
                session,
                context=context,
                raw_token=body.token,
                expected_payload_hash=body.expected_payload_hash,
            )
        except (approval_service.ApprovalNotFoundError, approval_service.ApprovalExpiredError) as exc:
            # Same response for "no such token," "token belongs to
            # another org," and "expired" (W6's anti-oracle fix) — never
            # let the client distinguish these cases.
            raise HTTPException(status_code=404, detail="invalid or expired approval") from exc
        except approval_service.ApprovalPayloadMismatchError as exc:
            raise HTTPException(status_code=422, detail="payload changed since approval was requested") from exc
        except approval_service.ApprovalAlreadyResolvedError as exc:
            raise HTTPException(status_code=409, detail="approval already resolved") from exc

    return ConsumeApprovalResponse(approval_id=str(approval.id), status=approval.status.value)
