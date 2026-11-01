"""Notification outbox operations."""

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.errors import NotFound


async def list_notifications(
    db: AsyncSession, ctx: RequestContext, unread_only: bool, limit: int
) -> list[dict]:
    query = """SELECT id, type, template, payload_json, status, created_at, read_at
                 FROM notifications
                 WHERE tenant_id=:tid AND user_id=:uid AND channel='in_app'
                 {unread_clause}
                 ORDER BY created_at DESC LIMIT :limit"""
    unread_clause = "AND status != 'read'" if unread_only else ""
    result = await db.execute(
        text(query.format(unread_clause=unread_clause)),
        {"tid": ctx.tenant_id, "uid": ctx.user_id, "limit": limit},
    )
    return [dict(row) for row in result.mappings().all()]


async def mark_read(db: AsyncSession, ctx: RequestContext, notification_id: uuid.UUID) -> dict:
    result = await db.execute(
        text("""UPDATE notifications SET status='read', read_at=COALESCE(read_at, now())
                WHERE tenant_id=:tid AND user_id=:uid AND id=:nid
                RETURNING id, type, template, payload_json, status, created_at, read_at"""),
        {"tid": ctx.tenant_id, "uid": ctx.user_id, "nid": notification_id},
    )
    row = result.mappings().first()
    if row is None:
        raise NotFound("Notification not found", code="notification.not_found")
    return dict(row)


async def queue_invitation(
    db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID, tenant_slug: str, full_name: str, token: str
) -> None:
    """Write an invitation email to the transactional notification outbox."""
    settings = get_settings()
    scheme = "http" if settings.env == "dev" else "https"
    port = ":8000" if settings.env == "dev" else ""
    invite_url = f"{scheme}://{tenant_slug}.{settings.base_domain}{port}/accept-invite?token={token}"
    payload = {"name": full_name, "invite_url": invite_url}
    await db.execute(
        text("""INSERT INTO notifications(tenant_id,user_id,channel,type,template,payload_json,dedupe_key)
      VALUES (:tid,:uid,'email','membership.invited','user.invited',CAST(:payload AS jsonb),:dedupe)"""),
        {
            "tid": tenant_id,
            "uid": user_id,
            "payload": json.dumps(payload),
            "dedupe": f"invite:{user_id}:{uuid.uuid4()}",
        },
    )
