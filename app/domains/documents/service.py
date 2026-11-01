"""Business logic for the documents domain.

Services own authorization (ctx.require), data scope, business rules, transactions and audit.
Routers, the AI layer and workers all call these functions.
"""

import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound


async def list_documents(db: AsyncSession, ctx: RequestContext) -> list[dict]:
    ctx.require("documents.read")
    rows = await db.execute(
        text("""SELECT d.*, e.first_name, e.last_name
                FROM documents d LEFT JOIN employees e
                  ON e.tenant_id=d.tenant_id AND e.id=d.employee_id
                WHERE d.tenant_id=:tid AND d.deleted_at IS NULL
                ORDER BY d.created_at DESC"""),
        {"tid": ctx.tenant_id},
    )
    return [dict(row) for row in rows.mappings().all()]


async def create_document(db: AsyncSession, ctx: RequestContext, values: dict) -> dict:
    ctx.require("documents.manage")
    values["uploaded_by"] = ctx.user_id
    values["expires_at"] = datetime.fromisoformat(values["expires_at"]) if values.get("expires_at") else None
    row = (
        (
            await db.execute(
                text("""INSERT INTO documents
                    (tenant_id, employee_id, document_type, title, storage_key, file_name,
                     mime_type, size_bytes, expires_at, uploaded_by, status)
                    VALUES (:tid,:employee_id,:document_type,:title,:storage_key,:file_name,
                            :mime_type,:size_bytes,:expires_at,:uploaded_by,'pending')
                    RETURNING *"""),
                {**values, "tid": ctx.tenant_id},
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


async def update_status(db: AsyncSession, ctx: RequestContext, document_id: uuid.UUID, status: str) -> dict:
    ctx.require("documents.manage")
    row = (
        (
            await db.execute(
                text("""UPDATE documents SET status=:status, verified_by=:uid,
                    verified_at=CASE WHEN :status='verified' THEN now() ELSE NULL END,
                    updated_at=now()
                    WHERE tenant_id=:tid AND id=:did AND deleted_at IS NULL
                    RETURNING *"""),
                {"status": status, "uid": ctx.user_id, "tid": ctx.tenant_id, "did": document_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Document not found", code="document.not_found")
    return dict(row)
