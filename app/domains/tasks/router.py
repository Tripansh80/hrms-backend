import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext, get_request_context
from app.db.session import get_db
from app.domains.tasks import service
from app.domains.tasks.schemas import TaskCreate, TaskStatusUpdate

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("")
async def get_tasks(
    assigned_only: bool = Query(default=False),
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
):
    return {"items": await service.list_tasks(db, ctx, assigned_only)}


@router.post("", status_code=201)
async def post_task(body: TaskCreate, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)):
    return await service.create_task(db, ctx, body.model_dump())


@router.patch("/{task_id}")
async def patch_task(task_id: uuid.UUID, body: TaskStatusUpdate, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)):
    return await service.update_status(db, ctx, task_id, body.status)
