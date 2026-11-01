"""HTTP endpoints for employee import preview, validation errors and commit."""

import uuid

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext, get_request_context
from app.db.session import get_db
from app.domains.imports import service

router = APIRouter(prefix="/imports", tags=["imports"])


@router.get("")
async def imports(ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)):
    return await service.list_jobs(db, ctx)


@router.post("/employees", status_code=201)
async def upload_employees(
    file: UploadFile = File(...),
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
):
    return await service.create_job(db, ctx, file)


@router.get("/{job_id}")
async def import_detail(
    job_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
):
    return await service.get_job(db, ctx, job_id)


@router.get("/{job_id}/errors")
async def import_errors(
    job_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
):
    return await service.errors(db, ctx, job_id)


@router.get("/{job_id}/preview")
async def import_preview(
    job_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
):
    return await service.preview(db, ctx, job_id)


@router.post("/{job_id}/execute")
async def execute_import(
    job_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
):
    return await service.execute(db, ctx, job_id)
