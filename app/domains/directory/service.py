"""Directory queries with public-field-only responses."""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound

_PUBLIC_COLUMNS = """e.id,e.employee_no,e.first_name,e.last_name,e.designation,e.email AS work_email,
    d.name AS department_name,t.name AS team_name,e.manager_id,
    CONCAT_WS(' ',m.first_name,m.last_name) AS manager_name"""


async def directory(
    db: AsyncSession,
    ctx: RequestContext,
    query: str | None,
    department_id: uuid.UUID | None,
    team_id: uuid.UUID | None,
    location_id: uuid.UUID | None,
    cursor: uuid.UUID | None,
    limit: int = 50,
) -> dict:
    ctx.require("directory.read")
    filters = ["e.tenant_id=:tid", "e.deleted_at IS NULL", "e.status <> 'exited'"]
    params = {"tid": ctx.tenant_id, "cursor": cursor, "limit": limit + 1}
    if query:
        filters.append("(e.first_name ILIKE :q OR e.last_name ILIKE :q OR e.designation ILIKE :q)")
        params["q"] = f"%{query}%"
    if department_id:
        filters.append("e.department_id=:department_id")
        params["department_id"] = department_id
    if team_id:
        filters.append("e.team_id=:team_id")
        params["team_id"] = team_id
    if location_id:
        filters.append("e.location_id=:location_id")
        params["location_id"] = location_id
    filters.append("(CAST(:cursor AS uuid) IS NULL OR e.id > CAST(:cursor AS uuid))")
    async with db.begin():
        rows = (
            await db.execute(
                text(
                    f"""SELECT {_PUBLIC_COLUMNS}
                    FROM employees e
                    LEFT JOIN departments d ON d.tenant_id=e.tenant_id AND d.id=e.department_id
                    LEFT JOIN teams t ON t.tenant_id=e.tenant_id AND t.id=e.team_id AND t.deleted_at IS NULL
                    LEFT JOIN employees m ON m.tenant_id=e.tenant_id AND m.id=e.manager_id
                    WHERE {' AND '.join(filters)}
                    ORDER BY e.id LIMIT :limit"""
                ),
                params,
            )
        ).mappings().all()
    return {
        "items": [dict(row) for row in rows[:limit]],
        "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None,
    }


async def org_chart(db: AsyncSession, ctx: RequestContext, focus_id: uuid.UUID | None) -> dict:
    ctx.require("directory.read")
    async with db.begin():
        if focus_id is None:
            focus_id = await db.scalar(
                text(
                    "SELECT id FROM employees WHERE tenant_id=:tid AND user_id=:uid "
                    "AND deleted_at IS NULL LIMIT 1"
                ),
                {"tid": ctx.tenant_id, "uid": ctx.user_id},
            )
        focus = (
            await db.execute(
                text(
                    f"""SELECT {_PUBLIC_COLUMNS},
                        (SELECT count(*) FROM employees r WHERE r.tenant_id=e.tenant_id
                         AND r.manager_id=e.id AND r.deleted_at IS NULL) AS direct_reports
                    FROM employees e
                    LEFT JOIN departments d ON d.tenant_id=e.tenant_id AND d.id=e.department_id
                    LEFT JOIN teams t ON t.tenant_id=e.tenant_id AND t.id=e.team_id AND t.deleted_at IS NULL
                    LEFT JOIN employees m ON m.tenant_id=e.tenant_id AND m.id=e.manager_id
                    WHERE e.tenant_id=:tid AND e.id=:focus AND e.deleted_at IS NULL"""
                ),
                {"tid": ctx.tenant_id, "focus": focus_id},
            )
        ).mappings().first()
        if focus is None:
            raise NotFound("Employee not found", code="employee.not_found")
        chain = (
            await db.execute(
                text(
                    """WITH RECURSIVE chain AS (
                      SELECT e.id,e.manager_id,e.first_name,e.last_name,e.designation,0 AS depth
                      FROM employees e WHERE e.tenant_id=:tid AND e.id=:focus AND e.deleted_at IS NULL
                      UNION ALL
                      SELECT m.id,m.manager_id,m.first_name,m.last_name,m.designation,c.depth+1
                      FROM employees m JOIN chain c ON m.id=c.manager_id
                      WHERE m.tenant_id=:tid AND m.deleted_at IS NULL AND c.depth < 19
                    )
                    SELECT id,first_name,last_name,designation FROM chain ORDER BY depth DESC"""
                ),
                {"tid": ctx.tenant_id, "focus": focus_id},
            )
        ).mappings().all()
        reports = (
            await db.execute(
                text(
                    f"""SELECT {_PUBLIC_COLUMNS},
                        (SELECT count(*) FROM employees r WHERE r.tenant_id=e.tenant_id
                         AND r.manager_id=e.id AND r.deleted_at IS NULL) AS direct_reports
                    FROM employees e
                    LEFT JOIN departments d ON d.tenant_id=e.tenant_id AND d.id=e.department_id
                    LEFT JOIN teams t ON t.tenant_id=e.tenant_id AND t.id=e.team_id AND t.deleted_at IS NULL
                    LEFT JOIN employees m ON m.tenant_id=e.tenant_id AND m.id=e.manager_id
                    WHERE e.tenant_id=:tid AND e.manager_id=:focus AND e.deleted_at IS NULL
                    ORDER BY e.last_name,e.first_name"""
                ),
                {"tid": ctx.tenant_id, "focus": focus_id},
            )
        ).mappings().all()
    return {
        "chain": [dict(row) for row in chain],
        "focus": dict(focus),
        "reports": [dict(row) for row in reports],
    }


async def reports(db: AsyncSession, ctx: RequestContext, employee_id: uuid.UUID) -> list[dict]:
    result = await org_chart(db, ctx, employee_id)
    return result["reports"]


async def department_tree(db: AsyncSession, ctx: RequestContext) -> list[dict]:
    ctx.require("directory.read")
    async with db.begin():
        rows = (
            await db.execute(
                text(
                    """SELECT d.id,d.name,d.parent_id,d.manager_id,
                              CONCAT_WS(' ',e.first_name,e.last_name) AS head_name,
                              count(emp.id) FILTER (WHERE emp.deleted_at IS NULL
                                AND emp.status <> 'exited') AS active_headcount
                       FROM departments d
                       LEFT JOIN employees e ON e.tenant_id=d.tenant_id AND e.id=d.manager_id
                         AND e.deleted_at IS NULL
                       LEFT JOIN employees emp ON emp.tenant_id=d.tenant_id
                         AND emp.department_id=d.id
                       WHERE d.tenant_id=:tid AND d.deleted_at IS NULL
                       GROUP BY d.id,e.first_name,e.last_name
                       ORDER BY d.parent_id NULLS FIRST,d.name"""
                ),
                {"tid": ctx.tenant_id},
            )
        ).mappings().all()
    return [dict(row) for row in rows]
