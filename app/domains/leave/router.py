"""HTTP layer for the leave domain: parse, call the service, shape the response."""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext, get_request_context
from app.db.session import get_db
from app.domains.leave import service
from app.domains.leave.schemas import LeaveDecision, LeaveRequestCreate, LeaveTypeUpdate

router = APIRouter(prefix="/leave", tags=["leave"])


@router.get("/types")
async def get_leave_types(
    ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> dict:
    return {"items": await service.list_leave_types(db, ctx)}


@router.patch("/types/{leave_type_id}")
async def patch_leave_type(
    leave_type_id: uuid.UUID,
    body: LeaveTypeUpdate,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.update_leave_type(db, ctx, leave_type_id, body.model_dump(exclude_unset=True))


@router.get("/balances")
async def get_leave_balances(
    employee_id: uuid.UUID | None = None,
    year: int | None = Query(default=None, ge=2000, le=2100),
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return {"items": await service.list_balances(db, ctx, employee_id, year)}


@router.get("/requests")
async def get_leave_requests(
    employee_id: uuid.UUID | None = None,
    status: str | None = Query(default=None, pattern="^(pending|approved|rejected|cancelled)$"),
    limit: int = Query(default=50, ge=1, le=100),
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return {"items": await service.list_requests(db, ctx, employee_id, status, limit)}


@router.get("/approvers")
async def get_leave_approvers(
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return {"items": await service.list_approvers(db, ctx)}


@router.post("/requests", status_code=201)
async def post_leave_request(
    body: LeaveRequestCreate,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.create_request(db, ctx, body.model_dump())


@router.post("/requests/{request_id}/approve")
async def approve_leave_request(
    request_id: uuid.UUID,
    body: LeaveDecision | None = None,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.decide_request(db, ctx, request_id, "approved", body.note if body else None)


@router.post("/requests/{request_id}/reject")
async def reject_leave_request(
    request_id: uuid.UUID,
    body: LeaveDecision | None = None,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.decide_request(db, ctx, request_id, "rejected", body.note if body else None)


@router.post("/requests/{request_id}/cancel")
async def cancel_leave_request(
    request_id: uuid.UUID,
    body: LeaveDecision | None = None,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.cancel_request(db, ctx, request_id, body.note if body else None)
