from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext, get_request_context
from app.db.session import get_db
from app.domains.dashboards import service

router = APIRouter(prefix="/dashboards", tags=["dashboards"])


@router.get("/me")
async def dashboards_me(
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.available(db, ctx)


@router.get("/{view}")
async def dashboard(
    view: str,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if view not in {"admin", "hr", "manager", "employee"}:
        from app.core.errors import NotFound

        raise NotFound("Dashboard not found", code="dashboard.not_found")
    return await service.get_dashboard(db, ctx, view)
