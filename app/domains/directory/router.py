import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext, get_request_context
from app.db.session import get_db
from app.domains.directory import service

router = APIRouter(tags=["directory"])


@router.get("/directory")
async def get_directory(
    q: str | None = None,
    department_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    cursor: uuid.UUID | None = None,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.directory(db, ctx, q, department_id, team_id, location_id, cursor)


@router.get("/org-chart")
async def get_org_chart(
    focus: uuid.UUID | None = None,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.org_chart(db, ctx, focus)


@router.get("/org-chart/departments")
async def get_org_departments(
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return {"items": await service.department_tree(db, ctx)}


@router.get("/org-chart/{employee_id}/reports")
async def get_org_reports(
    employee_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return {"items": await service.reports(db, ctx, employee_id)}
