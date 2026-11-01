"""Business logic for the policies domain.

Services own authorization (ctx.require), data scope, business rules, transactions and audit.
Routers, the AI layer and workers all call these functions.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed


async def list_policies(db: AsyncSession, ctx: RequestContext) -> list[dict]:
    ctx.require("policies.read")
    rows = await db.execute(
        text("""SELECT p.*, v.id AS version_id, v.version, v.content, v.published_at,
                       EXISTS(SELECT 1 FROM policy_acknowledgements a
                              JOIN employees e ON e.tenant_id=a.tenant_id AND e.id=a.employee_id
                              WHERE a.tenant_id=p.tenant_id AND a.policy_version_id=v.id
                                AND e.user_id=:uid) AS acknowledged
                FROM policies p LEFT JOIN LATERAL
                  (SELECT * FROM policy_versions v0 WHERE v0.tenant_id=p.tenant_id
                   AND v0.policy_id=p.id ORDER BY v0.version DESC LIMIT 1) v ON true
                WHERE p.tenant_id=:tid
                  AND (p.status='published' OR CAST(:manage AS boolean) = true)
                ORDER BY p.updated_at DESC"""),
        {"tid": ctx.tenant_id, "uid": ctx.user_id, "manage": ctx.can("policies.manage")},
    )
    return [dict(row) for row in rows.mappings().all()]


async def create_policy(db: AsyncSession, ctx: RequestContext, values: dict) -> dict:
    ctx.require("policies.manage")
    async with db.begin():
        policy = (
            (
                await db.execute(
                    text("""INSERT INTO policies (tenant_id,title,created_by)
                        VALUES (:tid,:title,:uid) RETURNING *"""),
                    {"tid": ctx.tenant_id, "title": values["title"], "uid": ctx.user_id},
                )
            )
            .mappings()
            .one()
        )
        version = (
            (
                await db.execute(
                    text("""INSERT INTO policy_versions
                        (tenant_id,policy_id,version,content,created_by)
                        VALUES (:tid,:pid,1,:content,:uid) RETURNING *"""),
                    {
                        "tid": ctx.tenant_id,
                        "pid": policy["id"],
                        "content": values["content"],
                        "uid": ctx.user_id,
                    },
                )
            )
            .mappings()
            .one()
        )
    return {**dict(policy), "version": version["version"], "content": version["content"]}


async def publish_policy(db: AsyncSession, ctx: RequestContext, policy_id: uuid.UUID) -> dict:
    ctx.require("policies.manage")
    async with db.begin():
        row = (
            (
                await db.execute(
                    text("""UPDATE policies SET status='published', updated_at=now()
                        WHERE tenant_id=:tid AND id=:pid RETURNING *"""),
                    {"tid": ctx.tenant_id, "pid": policy_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound("Policy not found", code="policy.not_found")
        version_id = (
            await db.execute(
                text("""SELECT id FROM policy_versions
                        WHERE tenant_id=:tid AND policy_id=:pid
                        ORDER BY version DESC LIMIT 1"""),
                {"tid": ctx.tenant_id, "pid": policy_id},
            )
        ).scalar_one()
        await db.execute(
            text("""UPDATE policy_versions SET published_at=COALESCE(published_at, now())
                    WHERE tenant_id=:tid AND policy_id=:pid
                    AND version=(SELECT max(version) FROM policy_versions
                                 WHERE tenant_id=:tid AND policy_id=:pid)"""),
            {"tid": ctx.tenant_id, "pid": policy_id},
        )
        await db.execute(
            text("""INSERT INTO policy_acknowledgements
                        (tenant_id, policy_version_id, employee_id, ip_address)
                    SELECT :tid, :vid, e.id, NULL
                    FROM employees e
                    WHERE e.tenant_id=:tid
                      AND e.deleted_at IS NULL
                      AND e.status='active'
                      AND e.user_id IS NOT NULL
                    ON CONFLICT (tenant_id, policy_version_id, employee_id) DO NOTHING"""),
            {"tid": ctx.tenant_id, "vid": version_id},
        )
    return dict(row)


async def toggle_policy_status(db: AsyncSession, ctx: RequestContext, policy_id: uuid.UUID) -> dict:
    ctx.require("policies.manage")
    async with db.begin():
        row = (
            await db.execute(
                text("""UPDATE policies
                        SET status=CASE WHEN status='published' THEN 'draft' ELSE 'published' END,
                            updated_at=now()
                        WHERE tenant_id=:tid AND id=:pid
                        RETURNING *"""),
                {"tid": ctx.tenant_id, "pid": policy_id},
            )
        ).mappings().first()
        if row is None:
            raise NotFound("Policy not found", code="policy.not_found")
        if row["status"] == "published":
            await db.execute(
                text("""UPDATE policy_versions SET published_at=COALESCE(published_at, now())
                        WHERE tenant_id=:tid AND policy_id=:pid
                          AND version=(SELECT max(version) FROM policy_versions
                                       WHERE tenant_id=:tid AND policy_id=:pid)"""),
                {"tid": ctx.tenant_id, "pid": policy_id},
            )
    return dict(row)


async def acknowledge_policy(db: AsyncSession, ctx: RequestContext, policy_id: uuid.UUID) -> dict:
    ctx.require("policies.read")
    employee = (
        await db.execute(
            text("""SELECT id FROM employees
                    WHERE tenant_id=:tid AND user_id=:uid AND deleted_at IS NULL"""),
            {"tid": ctx.tenant_id, "uid": ctx.user_id},
        )
    ).scalar_one_or_none()
    if employee is None:
        raise ValidationFailed(
            "Only employees can acknowledge policies", code="policy.employee_profile_required"
        )
    version = (
        await db.execute(
            text("""SELECT v.id
                    FROM policies p
                    JOIN policy_versions v ON v.tenant_id=p.tenant_id AND v.policy_id=p.id
                    WHERE p.tenant_id=:tid AND p.id=:pid AND p.status='published'
                    ORDER BY v.version DESC LIMIT 1"""),
            {"tid": ctx.tenant_id, "pid": policy_id},
        )
    ).scalar_one_or_none()
    if version is None:
        raise NotFound("Published policy not found", code="policy.not_published")
    row = (
        await db.execute(
            text("""INSERT INTO policy_acknowledgements
                    (tenant_id, policy_version_id, employee_id, ip_address)
                    VALUES (:tid,:vid,:eid,NULL)
                    ON CONFLICT (tenant_id, policy_version_id, employee_id)
                    DO UPDATE SET acknowledged_at=now()
                    RETURNING *"""),
            {"tid": ctx.tenant_id, "vid": version, "eid": employee},
        )
    ).mappings().one()
    return dict(row)
