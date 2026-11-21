"""Tenant profile services."""

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.errors import Conflict, Forbidden, NotFound
from app.core.redis import get_redis, perms_key
from app.core.security import generate_opaque_token, hash_token
from app.domains.notifications import service as notification_service


async def get_current(db: AsyncSession, ctx: RequestContext) -> dict:
    ctx.require("tenant.read")
    async with db.begin():
        row = (
            (
                await db.execute(
                    text("""SELECT id,name,slug,domain,status,timezone,locale,branding_json,created_at
            FROM tenants WHERE id=:tid AND deleted_at IS NULL"""),
                    {"tid": ctx.tenant_id},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise NotFound("Workspace not found", code="tenant.not_found")
    return dict(row)


async def update_current(db: AsyncSession, ctx: RequestContext, changes: dict) -> dict:
    ctx.require("tenant.update")
    allowed = {k: v for k, v in changes.items() if v is not None}
    if not allowed:
        return await get_current(db, ctx)
    columns = {"name", "timezone", "locale", "branding_json"}
    if set(allowed) - columns:
        raise ValueError("Unsupported tenant fields")
    assignments = ",".join(
        f"{key}=CAST(:{key} AS jsonb)" if key == "branding_json" else f"{key}=:{key}" for key in allowed
    )
    if "branding_json" in allowed:
        allowed["branding_json"] = json.dumps(allowed["branding_json"])
    async with db.begin():
        before = (
            await db.execute(
                text("SELECT to_jsonb(t) FROM tenants t WHERE id=:tid AND deleted_at IS NULL"),
                {"tid": ctx.tenant_id},
            )
        ).scalar_one_or_none()
        if before is None:
            raise NotFound("Workspace not found", code="tenant.not_found")
        await db.execute(
            text(f"UPDATE tenants SET {assignments} WHERE id=:tid"), {**allowed, "tid": ctx.tenant_id}
        )
        await db.execute(
            text("""INSERT INTO audit_logs(tenant_id,actor_user_id,action,entity_type,entity_id,source,before_json,after_json,request_id)
          SELECT :tid,:uid,'tenant.updated','tenant',:tid,'api',CAST(:before AS jsonb),to_jsonb(t),:rid FROM tenants t WHERE t.id=:tid"""),
            {
                "tid": ctx.tenant_id,
                "uid": ctx.user_id,
                "before": json.dumps(before, default=str),
                "rid": ctx.request_id,
            },
        )
        row = (
            (
                await db.execute(
                    text("""SELECT id,name,slug,domain,status,timezone,locale,branding_json,created_at
            FROM tenants WHERE id=:tid"""),
                    {"tid": ctx.tenant_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def memberships(db: AsyncSession, ctx: RequestContext) -> list[dict]:
    ctx.require("users.read")
    async with db.begin():
        rows = (
            (
                await db.execute(
                    text("""SELECT u.id,u.email,u.full_name,m.status,m.is_owner,m.joined_at,
          COALESCE(array_agg(DISTINCT r.name) FILTER(WHERE r.name IS NOT NULL),'{}') AS roles
          FROM tenant_memberships m JOIN users u ON u.id=m.user_id LEFT JOIN user_roles ur ON ur.tenant_id=m.tenant_id AND ur.user_id=m.user_id
          LEFT JOIN roles r ON r.id=ur.role_id WHERE m.tenant_id=:tid GROUP BY u.id,m.id ORDER BY u.email"""),
                    {"tid": ctx.tenant_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def invite_member(db: AsyncSession, ctx: RequestContext, email: str, full_name: str) -> dict:
    ctx.require("users.manage")
    token = generate_opaque_token()
    async with db.begin():
        user = (
            await db.execute(
                text("""INSERT INTO users(email,full_name) VALUES (:email,:name)
          ON CONFLICT(email) DO UPDATE SET email=EXCLUDED.email RETURNING id"""),
                {"email": email, "name": full_name},
            )
        ).scalar_one()
        existing = await db.scalar(
            text("SELECT status FROM tenant_memberships WHERE tenant_id=:tid AND user_id=:uid FOR UPDATE"),
            {"tid": ctx.tenant_id, "uid": user},
        )
        if existing == "active" or existing == "suspended":
            raise Conflict("User already has a membership", code="membership.exists")
        await db.execute(
            text("""INSERT INTO tenant_memberships(tenant_id,user_id,status,invited_by)
          VALUES (:tid,:uid,'invited',:actor) ON CONFLICT(tenant_id,user_id) DO UPDATE SET status='invited',invited_by=:actor"""),
            {"tid": ctx.tenant_id, "uid": user, "actor": ctx.user_id},
        )
        await db.execute(
            text("DELETE FROM user_roles WHERE tenant_id=:tid AND user_id=:uid"),
            {"tid": ctx.tenant_id, "uid": user},
        )
        await db.execute(
            text("""INSERT INTO user_roles(tenant_id,user_id,role_id,granted_by)
          SELECT :tid,:uid,id,:actor FROM roles WHERE tenant_id IS NULL AND name='Employee'
          ON CONFLICT DO NOTHING"""),
            {"tid": ctx.tenant_id, "uid": user, "actor": ctx.user_id},
        )
        await db.execute(
            text(
                "INSERT INTO auth_tokens(user_id,tenant_id,purpose,token_hash,expires_at) VALUES (:uid,:tid,'invitation',:hash,now()+interval '7 days')"
            ),
            {"uid": user, "tid": ctx.tenant_id, "hash": hash_token(token)},
        )
        tenant_slug = await db.scalar(text("SELECT slug FROM tenants WHERE id=:tid"), {"tid": ctx.tenant_id})
        await notification_service.queue_invitation(db, ctx.tenant_id, user, tenant_slug, full_name, token)
        await db.execute(
            text("""INSERT INTO audit_logs(tenant_id,actor_user_id,action,entity_type,entity_id,source,after_json,request_id)
          VALUES (:tid,:actor,'membership.invited','user',:uid,:source,jsonb_build_object('email',:email),:rid)"""),
            {
                "tid": ctx.tenant_id,
                "actor": ctx.user_id,
                "uid": user,
                "source": ctx.source,
                "email": email,
                "rid": ctx.request_id,
            },
        )
    response = {"user_id": user, "status": "invited"}
    if get_settings().env != "prod":
        response["invitation_token"] = token
    await _invalidate_membership_cache(ctx.tenant_id, user)
    return response


async def update_membership(db: AsyncSession, ctx: RequestContext, user_id: uuid.UUID, status: str) -> dict:
    ctx.require("users.manage")
    if user_id == ctx.user_id and status != "active":
        raise Forbidden("You cannot suspend yourself", code="membership.self_change")
    async with db.begin():
        row = (
            (
                await db.execute(
                    text(
                        "SELECT status,is_owner FROM tenant_memberships WHERE tenant_id=:tid AND user_id=:uid FOR UPDATE"
                    ),
                    {"tid": ctx.tenant_id, "uid": user_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound("Membership not found", code="membership.not_found")
        if row["is_owner"] and status != "active":
            owners = await db.scalar(
                text(
                    "SELECT count(*) FROM tenant_memberships WHERE tenant_id=:tid AND is_owner AND status='active'"
                ),
                {"tid": ctx.tenant_id},
            )
            if owners <= 1:
                raise Conflict("The last Admin cannot be suspended", code="membership.last_admin")
        await db.execute(
            text("UPDATE tenant_memberships SET status=:status WHERE tenant_id=:tid AND user_id=:uid"),
            {"status": status, "tid": ctx.tenant_id, "uid": user_id},
        )
        if status == "suspended":
            await db.execute(
                text(
                    "UPDATE refresh_tokens SET revoked_at=now() WHERE tenant_id=:tid AND user_id=:uid AND revoked_at IS NULL"
                ),
                {"tid": ctx.tenant_id, "uid": user_id},
            )
        await db.execute(
            text(
                "INSERT INTO audit_logs(tenant_id,actor_user_id,action,entity_type,entity_id,source,request_id) VALUES (:tid,:actor,:action,'user',:uid,:source,:rid)"
            ),
            {
                "tid": ctx.tenant_id,
                "actor": ctx.user_id,
                "action": "membership." + status,
                "uid": user_id,
                "source": ctx.source,
                "rid": ctx.request_id,
            },
        )
    await _invalidate_membership_cache(ctx.tenant_id, user_id)
    return {"user_id": user_id, "status": status}


async def remove_membership(db: AsyncSession, ctx: RequestContext, user_id: uuid.UUID) -> None:
    ctx.require("users.manage")
    if user_id == ctx.user_id:
        raise Forbidden("You cannot remove yourself", code="membership.self_change")
    async with db.begin():
        row = (
            (
                await db.execute(
                    text(
                        "SELECT is_owner FROM tenant_memberships WHERE tenant_id=:tid AND user_id=:uid FOR UPDATE"
                    ),
                    {"tid": ctx.tenant_id, "uid": user_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound("Membership not found", code="membership.not_found")
        if row["is_owner"]:
            count = await db.scalar(
                text(
                    "SELECT count(*) FROM tenant_memberships WHERE tenant_id=:tid AND is_owner AND status='active'"
                ),
                {"tid": ctx.tenant_id},
            )
            if count <= 1:
                raise Conflict("The last Admin cannot be removed", code="membership.last_admin")
        await db.execute(
            text("UPDATE tenant_memberships SET status='removed' WHERE tenant_id=:tid AND user_id=:uid"),
            {"tid": ctx.tenant_id, "uid": user_id},
        )
        await db.execute(
            text(
                "UPDATE refresh_tokens SET revoked_at=now() WHERE tenant_id=:tid AND user_id=:uid AND revoked_at IS NULL"
            ),
            {"tid": ctx.tenant_id, "uid": user_id},
        )
        await db.execute(
            text(
                "INSERT INTO audit_logs(tenant_id,actor_user_id,action,entity_type,entity_id,source,request_id) VALUES (:tid,:actor,'membership.removed','user',:uid,:source,:rid)"
            ),
            {
                "tid": ctx.tenant_id,
                "actor": ctx.user_id,
                "uid": user_id,
                "source": ctx.source,
                "rid": ctx.request_id,
            },
        )
    await _invalidate_membership_cache(ctx.tenant_id, user_id)


async def _invalidate_membership_cache(tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
    if get_settings().redis_enabled:
        await get_redis().delete(perms_key(tenant_id, user_id))
