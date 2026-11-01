"""Employee HTTP endpoints."""

import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext, get_request_context
from app.db.session import get_db
from app.domains.employees import service
from app.domains.employees.schemas import EmployeeCreate, EmployeeTransfer, EmployeeUpdate

router = APIRouter(prefix="/employees", tags=["employees"])


@router.get("")
async def get_employees(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: uuid.UUID | None = None,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.list_employees(db, ctx, limit, cursor)


@router.post("", status_code=201)
async def post_employee(
    body: EmployeeCreate,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.create_employee(db, ctx, body.model_dump(exclude_unset=True))


@router.get("/{employee_id}")
async def get_employee(
    employee_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.get_employee(db, ctx, employee_id)


@router.get("/{employee_id}/profile")
async def get_profile(
    employee_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.get_profile(db, ctx, employee_id)


@router.patch("/{employee_id}")
async def patch_employee(
    employee_id: uuid.UUID,
    body: EmployeeUpdate,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.update_employee(db, ctx, employee_id, body.model_dump(exclude_unset=True))


@router.post("/{employee_id}/transfer")
async def transfer_employee(
    employee_id: uuid.UUID,
    body: EmployeeTransfer,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.transfer_employee(db, ctx, employee_id, body.model_dump(exclude_unset=True))


@router.delete("/{employee_id}", status_code=204)
async def remove_employee(
    employee_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await service.delete_employee(db, ctx, employee_id)
    return Response(status_code=204)


@router.get("/{employee_id}/history")
async def get_history(
    employee_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return {"items": await service.history(db, ctx, employee_id)}


@router.get("/{employee_id}/reports")
async def get_reports(
    employee_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return {"items": await service.reports(db, ctx, employee_id)}


@router.post("/{employee_id}/invite", status_code=202)
async def invite_employee(
    employee_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.invite_employee(db, ctx, employee_id)
