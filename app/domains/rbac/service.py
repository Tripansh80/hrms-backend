"""RBAC operations, authorization and audit."""

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.errors import Conflict, Forbidden, NotFound, ValidationFailed
from app.core.redis import get_redis, perms_key


async def _audit(
    db: AsyncSession,
    ctx: RequestContext,
    action: str,
    entity: str,
    entity_id: uuid.UUID | None,
    before: object = None,
    after: object = None,
) -> None:
    await db.execute(
        text("""INSERT INTO audit_logs(tenant_id,actor_user_id,action,entity_type,entity_id,source,before_json,after_json,request_id)
      VALUES (:tid,:uid,:action,:entity,:eid,:source,CAST(:before AS jsonb),CAST(:after AS jsonb),:rid)"""),
        {
            "tid": ctx.tenant_id,
            "uid": ctx.user_id,
            "action": action,
            "entity": entity,
            "eid": entity_id,
            "source": ctx.source,
            "before": json.dumps(before, default=str) if before is not None else None,
            "after": json.dumps(after, default=str) if after is not None else None,
            "rid": ctx.request_id,
        },
    )


async def _invalidate(ctx: RequestContext, user_id: uuid.UUID) -> None:
    if get_settings().redis_enabled:
        await get_redis().delete(perms_key(ctx.tenant_id, user_id))


async def permissions(db: AsyncSession, ctx: RequestContext) -> list[dict]:
    ctx.require("roles.read")
    async with db.begin():
        rows = (
            (await db.execute(text("SELECT id,code,description FROM permissions ORDER BY code")))
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def list_roles(db: AsyncSession, ctx: RequestContext) -> list[dict]:
    ctx.require("roles.read")
    async with db.begin():
        rows = (
            (
                await db.execute(
                    text("""SELECT r.id,r.name,r.description,r.is_system,r.data_scope,
          COALESCE(array_agg(p.code) FILTER (WHERE p.code IS NOT NULL),'{}') AS permissions
          FROM roles r LEFT JOIN role_permissions rp ON rp.role_id=r.id
          LEFT JOIN permissions p ON p.id=rp.permission_id
          WHERE r.tenant_id IS NULL OR r.tenant_id=:tid GROUP BY r.id ORDER BY r.is_system DESC,r.name"""),
                    {"tid": ctx.tenant_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def create_role(db: AsyncSession, ctx: RequestContext, data: dict) -> dict:
    ctx.require("roles.manage")
    async with db.begin():
        row = (
            (
                await db.execute(
                    text("""INSERT INTO roles(tenant_id,name,description,data_scope)
          VALUES (:tid,:name,:description,:scope) RETURNING id,name,description,is_system,data_scope"""),
                    {
                        "tid": ctx.tenant_id,
                        "name": data["name"],
                        "description": data.get("description", ""),
                        "scope": data.get("data_scope", "all"),
                    },
                )
            )
            .mappings()
            .one()
        )
        await _audit(db, ctx, "role.created", "role", row["id"], after=dict(row))
    return dict(row)


async def get_role(db: AsyncSession, ctx: RequestContext, role_id: uuid.UUID) -> dict:
    ctx.require("roles.read")
    async with db.begin():
        row = (
            (
                await db.execute(
                    text("""SELECT r.id,r.tenant_id,r.name,r.description,r.is_system,r.data_scope,
          COALESCE(array_agg(p.code) FILTER(WHERE p.code IS NOT NULL),'{}') AS permissions
          FROM roles r LEFT JOIN role_permissions rp ON rp.role_id=r.id LEFT JOIN permissions p ON p.id=rp.permission_id
          WHERE r.id=:id AND (r.tenant_id IS NULL OR r.tenant_id=:tid) GROUP BY r.id"""),
                    {"id": role_id, "tid": ctx.tenant_id},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise NotFound("Role not found", code="role.not_found")
    return dict(row)


async def update_role(db: AsyncSession, ctx: RequestContext, role_id: uuid.UUID, changes: dict) -> dict:
    ctx.require("roles.manage")
    changes = {k: v for k, v in changes.items() if v is not None}
    allowed = {"name", "description", "data_scope"}
    if set(changes) - allowed:
        raise ValidationFailed()
    async with db.begin():
        old = (
            (
                await db.execute(
                    text(
                        "SELECT id,name,description,data_scope,is_system FROM roles WHERE id=:id AND tenant_id=:tid FOR UPDATE"
                    ),
                    {"id": role_id, "tid": ctx.tenant_id},
                )
            )
            .mappings()
            .first()
        )
        if old is None:
            system = await db.scalar(
                text("SELECT EXISTS(SELECT 1 FROM roles WHERE id=:id AND tenant_id IS NULL)"), {"id": role_id}
            )
            if system:
                raise Forbidden("System roles are read-only", code="role.system_read_only")
            raise NotFound("Role not found", code="role.not_found")
        if changes:
            cols = ",".join(f"{key}=:{key}" for key in changes)
            await db.execute(text(f"UPDATE roles SET {cols} WHERE id=:id"), {**changes, "id": role_id})
        new = (
            (
                await db.execute(
                    text("SELECT id,name,description,data_scope,is_system FROM roles WHERE id=:id"),
                    {"id": role_id},
                )
            )
            .mappings()
            .one()
        )
        await _audit(db, ctx, "role.updated", "role", role_id, before=dict(old), after=dict(new))
    return dict(new)


async def delete_role(db: AsyncSession, ctx: RequestContext, role_id: uuid.UUID) -> None:
    ctx.require("roles.manage")
    affected: list[uuid.UUID] = []
    async with db.begin():
        exists = await db.scalar(
            text("SELECT EXISTS(SELECT 1 FROM roles WHERE id=:id AND tenant_id=:tid)"),
            {"id": role_id, "tid": ctx.tenant_id},
        )
        if not exists:
            system = await db.scalar(
                text("SELECT EXISTS(SELECT 1 FROM roles WHERE id=:id AND tenant_id IS NULL)"), {"id": role_id}
            )
            if system:
                raise Forbidden("System roles are read-only", code="role.system_read_only")
            raise NotFound("Role not found", code="role.not_found")
        owner = await db.scalar(text("SELECT id FROM roles WHERE tenant_id IS NULL AND name='Tenant Owner'"))
        assigned = await db.scalar(
            text(
                "SELECT EXISTS(SELECT 1 FROM user_roles WHERE tenant_id=:tid AND role_id=:rid AND role_id=:owner)"
            ),
            {"tid": ctx.tenant_id, "rid": role_id, "owner": owner},
        )
        if assigned:
            remaining = await db.scalar(
                text("SELECT count(*) FROM user_roles WHERE tenant_id=:tid AND role_id=:owner"),
                {"tid": ctx.tenant_id, "owner": owner},
            )
            if remaining <= 1:
                raise Conflict("The last Tenant Owner cannot be removed", code="role.last_owner")
        affected = (
            (
                await db.execute(
                    text("SELECT user_id FROM user_roles WHERE tenant_id=:tid AND role_id=:rid"),
                    {"tid": ctx.tenant_id, "rid": role_id},
                )
            )
            .scalars()
            .all()
        )
        await _audit(db, ctx, "role.deleted", "role", role_id)
        await db.execute(
            text("DELETE FROM user_roles WHERE tenant_id=:tid AND role_id=:rid"),
            {"tid": ctx.tenant_id, "rid": role_id},
        )
        await db.execute(
            text("DELETE FROM roles WHERE id=:id AND tenant_id=:tid"), {"id": role_id, "tid": ctx.tenant_id}
        )
    for uid in affected:
        await _invalidate(ctx, uid)


async def set_role_permissions(
    db: AsyncSession, ctx: RequestContext, role_id: uuid.UUID, codes: list[str]
) -> dict:
    ctx.require("roles.manage")
    if not set(codes).issubset(ctx.permissions):
        raise Forbidden("You cannot grant permissions you do not hold", code="role.permission_escalation")
    async with db.begin():
        role = (
            await db.execute(
                text("SELECT id FROM roles WHERE id=:id AND tenant_id=:tid FOR UPDATE"),
                {"id": role_id, "tid": ctx.tenant_id},
            )
        ).first()
        if role is None:
            system = await db.scalar(
                text("SELECT EXISTS(SELECT 1 FROM roles WHERE id=:id AND tenant_id IS NULL)"), {"id": role_id}
            )
            if system:
                raise Forbidden("System roles are read-only", code="role.system_read_only")
            raise NotFound("Role not found", code="role.not_found")
        found = (
            await db.execute(text("SELECT id,code FROM permissions WHERE code=ANY(:codes)"), {"codes": codes})
        ).all()
        if len(found) != len(set(codes)):
            raise ValidationFailed("Unknown permission code", code="role.permission_unknown")
        await db.execute(text("DELETE FROM role_permissions WHERE role_id=:rid"), {"rid": role_id})
        for permission_id, _ in found:
            await db.execute(
                text("INSERT INTO role_permissions(role_id,permission_id) VALUES (:rid,:pid)"),
                {"rid": role_id, "pid": permission_id},
            )
        await _audit(
            db, ctx, "role.permissions_updated", "role", role_id, after={"permissions": sorted(set(codes))}
        )
        affected = (
            (
                await db.execute(
                    text("SELECT user_id FROM user_roles WHERE tenant_id=:tid AND role_id=:rid"),
                    {"tid": ctx.tenant_id, "rid": role_id},
                )
            )
            .scalars()
            .all()
        )
    for uid in affected:
        await _invalidate(ctx, uid)
    return {"role_id": role_id, "permissions": sorted(set(codes))}


async def list_users(db: AsyncSession, ctx: RequestContext) -> list[dict]:
    ctx.require("users.read")
    async with db.begin():
        rows = (
            (
                await db.execute(
                    text("""SELECT u.id,u.email,u.full_name,m.status,m.is_owner,m.joined_at,
          COALESCE(array_agg(DISTINCT r.name) FILTER(WHERE r.name IS NOT NULL),'{}') AS roles
          FROM tenant_memberships m JOIN users u ON u.id=m.user_id
          LEFT JOIN user_roles ur ON ur.tenant_id=m.tenant_id AND ur.user_id=m.user_id
          LEFT JOIN roles r ON r.id=ur.role_id
          WHERE m.tenant_id=:tid GROUP BY u.id,m.id ORDER BY u.email"""),
                    {"tid": ctx.tenant_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def user_roles(db: AsyncSession, ctx: RequestContext, user_id: uuid.UUID) -> list[dict]:
    ctx.require("roles.read")
    async with db.begin():
        member = await db.scalar(
            text("SELECT EXISTS(SELECT 1 FROM tenant_memberships WHERE tenant_id=:tid AND user_id=:uid)"),
            {"tid": ctx.tenant_id, "uid": user_id},
        )
        if not member:
            raise NotFound("User not found", code="user.not_found")
        rows = (
            (
                await db.execute(
                    text("""SELECT r.id,r.name,r.description,r.data_scope,r.is_system
          FROM user_roles ur JOIN roles r ON r.id=ur.role_id
          WHERE ur.tenant_id=:tid AND ur.user_id=:uid ORDER BY r.name"""),
                    {"tid": ctx.tenant_id, "uid": user_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def set_user_roles(
    db: AsyncSession, ctx: RequestContext, user_id: uuid.UUID, role_ids: list[uuid.UUID]
) -> list[dict]:
    ctx.require("roles.manage")
    if user_id == ctx.user_id:
        raise Forbidden("You cannot change your own roles", code="role.self_change")
    async with db.begin():
        member = await db.scalar(
            text(
                "SELECT EXISTS(SELECT 1 FROM tenant_memberships WHERE tenant_id=:tid AND user_id=:uid AND status='active')"
            ),
            {"tid": ctx.tenant_id, "uid": user_id},
        )
        if not member:
            raise NotFound("User not found", code="user.not_found")
        unique_ids = sorted(set(role_ids), key=str)
        found = (
            (
                await db.execute(
                    text(
                        "SELECT id,name FROM roles WHERE id=ANY(:ids) AND (tenant_id IS NULL OR tenant_id=:tid)"
                    ),
                    {"ids": unique_ids, "tid": ctx.tenant_id},
                )
            ).all()
            if unique_ids
            else []
        )
        if len(found) != len(unique_ids):
            raise NotFound("Role not found", code="role.not_found")
        permission_codes = (
            (
                await db.execute(
                    text("""SELECT DISTINCT p.code FROM role_permissions rp
          JOIN permissions p ON p.id=rp.permission_id WHERE rp.role_id=ANY(:ids)"""),
                    {"ids": unique_ids},
                )
            )
            .scalars()
            .all()
            if unique_ids
            else []
        )
        if not set(permission_codes).issubset(ctx.permissions):
            raise Forbidden(
                "You cannot grant a role with permissions you do not hold", code="role.permission_escalation"
            )
        owner = await db.scalar(text("SELECT id FROM roles WHERE tenant_id IS NULL AND name='Tenant Owner'"))
        was_owner = await db.scalar(
            text(
                "SELECT EXISTS(SELECT 1 FROM user_roles WHERE tenant_id=:tid AND user_id=:uid AND role_id=:owner)"
            ),
            {"tid": ctx.tenant_id, "uid": user_id, "owner": owner},
        )
        will_owner = owner in unique_ids
        if was_owner and not will_owner:
            count = await db.scalar(
                text("SELECT count(*) FROM user_roles WHERE tenant_id=:tid AND role_id=:owner"),
                {"tid": ctx.tenant_id, "owner": owner},
            )
            if count <= 1:
                raise Conflict("The last Tenant Owner cannot be removed", code="role.last_owner")
        before = (
            (
                await db.execute(
                    text("SELECT role_id FROM user_roles WHERE tenant_id=:tid AND user_id=:uid"),
                    {"tid": ctx.tenant_id, "uid": user_id},
                )
            )
            .scalars()
            .all()
        )
        await db.execute(
            text("DELETE FROM user_roles WHERE tenant_id=:tid AND user_id=:uid"),
            {"tid": ctx.tenant_id, "uid": user_id},
        )
        for role_id in unique_ids:
            await db.execute(
                text(
                    "INSERT INTO user_roles(tenant_id,user_id,role_id,granted_by) VALUES (:tid,:uid,:rid,:actor)"
                ),
                {"tid": ctx.tenant_id, "uid": user_id, "rid": role_id, "actor": ctx.user_id},
            )
        await _audit(
            db,
            ctx,
            "user.roles_updated",
            "user",
            user_id,
            before={"role_ids": before},
            after={"role_ids": unique_ids},
        )
    await _invalidate(ctx, user_id)
    return await user_roles(db, _read_context(ctx), user_id)


def _read_context(ctx: RequestContext) -> RequestContext:
    """The caller has already passed roles.manage; retain a role-read equivalent for response shaping."""
    from dataclasses import replace

    return replace(ctx, permissions=ctx.permissions | {"roles.read"})
