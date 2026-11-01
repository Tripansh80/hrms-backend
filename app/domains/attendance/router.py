"""HTTP endpoints for employee attendance and correction decisions."""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext, get_request_context
from app.db.session import get_db
from app.domains.attendance import service
from app.domains.attendance.schemas import CheckIn, CorrectionCreate, Decision

router = APIRouter(prefix="/attendance", tags=["attendance"])


@router.post("/check-in", status_code=201)
async def post_check_in(
    body: CheckIn | None = None,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.check_in(db, ctx, body.notes if body else None)


@router.post("/check-out")
async def post_check_out(
    ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> dict:
    return await service.check_out(db, ctx)


@router.get("/me")
async def get_my_attendance(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.my_attendance(db, ctx, month)


@router.get("")
async def get_attendance(
    month: str | None = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(50, ge=1, le=100),
    cursor: uuid.UUID | None = None,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.list_attendance(db, ctx, month, limit, cursor)


@router.get("/summary")
async def get_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.summary(db, ctx, month)


@router.post("/corrections", status_code=201)
async def post_correction(
    body: CorrectionCreate,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.create_correction(db, ctx, body.model_dump())


@router.post("/corrections/{correction_id}/approve")
async def approve_correction(
    correction_id: uuid.UUID,
    body: Decision,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.decide_correction(db, ctx, correction_id, True, body.comment)


@router.post("/corrections/{correction_id}/reject")
async def reject_correction(
    correction_id: uuid.UUID,
    body: Decision,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.decide_correction(db, ctx, correction_id, False, body.comment)
