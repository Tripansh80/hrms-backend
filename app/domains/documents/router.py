"""HTTP layer for the documents domain: parse, call the service, shape the response."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext, get_request_context
from app.db.session import get_db
from app.domains.documents import service
from app.domains.documents.schemas import DocumentCreate

router = APIRouter(prefix="/documents", tags=["documents"])


@router.get("")
async def get_documents(
    ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
):
    return {"items": await service.list_documents(db, ctx)}


@router.post("", status_code=201)
async def post_document(
    body: DocumentCreate,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
):
    return await service.create_document(db, ctx, body.model_dump())


@router.post("/{document_id}/status")
async def post_document_status(
    document_id: uuid.UUID,
    status: str,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
):
    if status not in {"verified", "rejected", "expired"}:
        from app.core.errors import ValidationFailed

        raise ValidationFailed("Invalid document status", code="document.invalid_status")
    return await service.update_status(db, ctx, document_id, status)
