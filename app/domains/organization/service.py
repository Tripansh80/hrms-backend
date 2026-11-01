"""Organization, location, schedule and holiday services."""

import json
import re
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.config import get_settings
from app.core.errors import Conflict, NotFound, ValidationFailed


def _require_teams() -> None:
    if not get_settings().feature_teams:
        raise NotFound("Teams are disabled", code="feature.teams_disabled")


async def _validate_team_refs(db: AsyncSession, tenant_id: uuid.UUID, data: dict) -> None:
    department_id = data.get("department_id")
    manager_id = data.get("manager_employee_id")
    if department_id is not None:
        exists = await db.scalar(text(
            "SELECT EXISTS(SELECT 1 FROM departments WHERE tenant_id=:tid AND id=:id AND deleted_at IS NULL)"
        ), {"tid": tenant_id, "id": department_id})
        if not exists:
            raise NotFound("Department not found", code="team.department_not_found")
    if manager_id is not None:
        exists = await db.scalar(text(
            """SELECT EXISTS(SELECT 1 FROM employees e
               WHERE e.tenant_id=:tid AND e.id=:id AND e.deleted_at IS NULL
                 AND e.status <> 'exited'
                 AND e.department_id=:department_id)"""
        ), {"tid": tenant_id, "id": manager_id, "department_id": department_id})
        if not exists:
            raise ValidationFailed(
                "Team manager must be an active employee in the team department",
                code="team.manager_invalid",
            )


async def teams(db: AsyncSession, ctx: RequestContext) -> list[dict]:
    _require_teams()
    ctx.require("teams.read")
    async with db.begin():
        rows = (await db.execute(text(
            """SELECT t.*, d.name AS department_name,
                      e.first_name AS manager_first_name, e.last_name AS manager_last_name
               FROM teams t
               JOIN departments d ON d.tenant_id=t.tenant_id AND d.id=t.department_id
                 AND d.deleted_at IS NULL
               LEFT JOIN employees e ON e.tenant_id=t.tenant_id AND e.id=t.manager_employee_id
                 AND e.deleted_at IS NULL
               WHERE t.tenant_id=:tid AND t.deleted_at IS NULL
               ORDER BY d.name, t.name"""
        ), {"tid": ctx.tenant_id})).mappings().all()
    return [dict(row) for row in rows]


async def get_team(db: AsyncSession, ctx: RequestContext, team_id: uuid.UUID) -> dict:
    _require_teams()
    ctx.require("teams.read")
    async with db.begin():
        row = (await db.execute(text(
            """SELECT t.*, d.name AS department_name,
                      e.first_name AS manager_first_name, e.last_name AS manager_last_name
               FROM teams t JOIN departments d ON d.tenant_id=t.tenant_id AND d.id=t.department_id
               LEFT JOIN employees e ON e.tenant_id=t.tenant_id AND e.id=t.manager_employee_id
               WHERE t.tenant_id=:tid AND t.id=:id AND t.deleted_at IS NULL"""
        ), {"tid": ctx.tenant_id, "id": team_id})).mappings().first()
    if row is None:
        raise NotFound("Team not found", code="team.not_found")
    return dict(row)


async def create_team(db: AsyncSession, ctx: RequestContext, data: dict) -> dict:
    _require_teams()
    ctx.require("teams.manage")
    data["code"] = data.get("code") or None
    async with db.begin():
        await _validate_team_refs(db, ctx.tenant_id, data)
        try:
            row = (await db.execute(text(
                """INSERT INTO teams(tenant_id,name,code,department_id,manager_employee_id,description,status)
                   VALUES (:tid,:name,:code,:department_id,:manager_employee_id,:description,:status)
                   RETURNING *"""
            ), {"tid": ctx.tenant_id, **data})).mappings().one()
        except Exception as exc:
            if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
                raise Conflict("Team name or code already exists", code="team.duplicate") from exc
            raise
    return dict(row)


async def update_team(
    db: AsyncSession, ctx: RequestContext, team_id: uuid.UUID, changes: dict
) -> dict:
    _require_teams()
    ctx.require("teams.manage")
    async with db.begin():
        current = (await db.execute(text(
            "SELECT * FROM teams WHERE tenant_id=:tid AND id=:id AND deleted_at IS NULL FOR UPDATE"
        ), {"tid": ctx.tenant_id, "id": team_id})).mappings().first()
        if current is None:
            raise NotFound("Team not found", code="team.not_found")
        merged = {**dict(current), **changes}
        await _validate_team_refs(db, ctx.tenant_id, merged)
        if changes:
            sets = ",".join(f"{key}=:{key}" for key in changes)
            row = (await db.execute(text(
                f"UPDATE teams SET {sets},updated_at=now() WHERE tenant_id=:tid AND id=:id RETURNING *"
            ), {**changes, "tid": ctx.tenant_id, "id": team_id})).mappings().one()
        else:
            row = current
    return dict(row)


async def delete_team(db: AsyncSession, ctx: RequestContext, team_id: uuid.UUID) -> None:
    _require_teams()
    ctx.require("teams.manage")
    async with db.begin():
        exists = await db.scalar(text(
            "SELECT EXISTS(SELECT 1 FROM teams WHERE tenant_id=:tid AND id=:id AND deleted_at IS NULL)"
        ), {"tid": ctx.tenant_id, "id": team_id})
        if not exists:
            raise NotFound("Team not found", code="team.not_found")
        assigned = await db.scalar(text(
            "SELECT EXISTS(SELECT 1 FROM employees WHERE tenant_id=:tid AND team_id=:id AND deleted_at IS NULL AND status <> 'exited')"
        ), {"tid": ctx.tenant_id, "id": team_id})
        if assigned:
            raise Conflict("Team has active employees", code="team.has_employees")
        await db.execute(text(
            "UPDATE teams SET deleted_at=now(),status='archived',updated_at=now() WHERE tenant_id=:tid AND id=:id"
        ), {"tid": ctx.tenant_id, "id": team_id})


async def _audit(
    db: AsyncSession,
    ctx: RequestContext,
    action: str,
    kind: str,
    entity_id: uuid.UUID,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    await db.execute(
        text("""INSERT INTO audit_logs(tenant_id,actor_user_id,action,entity_type,entity_id,source,before_json,after_json,request_id)
      VALUES (:tid,:uid,:action,:kind,:id,:source,CAST(:before AS jsonb),CAST(:after AS jsonb),:rid)"""),
        {
            "tid": ctx.tenant_id,
            "uid": ctx.user_id,
            "action": action,
            "kind": kind,
            "id": entity_id,
            "source": ctx.source,
            "before": json.dumps(before, default=str) if before else None,
            "after": json.dumps(after, default=str) if after else None,
            "rid": ctx.request_id,
        },
    )


async def _validate_department_refs(db: AsyncSession, tenant_id: uuid.UUID, data: dict) -> None:
    for key, table, condition in (
        ("parent_id", "departments", "deleted_at IS NULL"),
        ("manager_id", "employees", "deleted_at IS NULL"),
    ):
        value = data.get(key)
        if value is not None:
            valid = await db.scalar(
                text(f"SELECT EXISTS(SELECT 1 FROM {table} WHERE tenant_id=:tid AND id=:id AND {condition})"),
                {"tid": tenant_id, "id": value},
            )
            if not valid:
                raise NotFound(
                    f"Department {key.removesuffix('_id').replace('_', ' ')} not found",
                    code="department.reference_not_found",
                )


async def _list(db: AsyncSession, ctx: RequestContext, table: str, permission: str) -> list[dict]:
    ctx.require(permission)
    where = (
        " AND deleted_at IS NULL"
        if table == "departments"
        else (" AND is_active" if table == "locations" else "")
    )
    async with db.begin():
        rows = (
            (
                await db.execute(
                    text(f"SELECT * FROM {table} WHERE tenant_id=:tid{where} ORDER BY name"),
                    {"tid": ctx.tenant_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def get_resource(
    db: AsyncSession, ctx: RequestContext, table: str, entity_id: uuid.UUID, permission: str
) -> dict:
    allowed = {"departments", "locations", "work_schedules", "holidays"}
    if table not in allowed:
        raise ValueError("Unsupported organization resource")
    ctx.require(permission)
    extra = " AND deleted_at IS NULL" if table == "departments" else ""
    async with db.begin():
        row = (
            (
                await db.execute(
                    text(f"SELECT * FROM {table} WHERE tenant_id=:tid AND id=:id{extra}"),
                    {"tid": ctx.tenant_id, "id": entity_id},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise NotFound(
            f"{table.rstrip('s').replace('_', ' ').title()} not found", code="organization.not_found"
        )
    return dict(row)


async def departments(db: AsyncSession, ctx: RequestContext) -> list[dict]:
    ctx.require("tenant.read")
    if ctx.can("tenant.update"):
        return await _list(db, ctx, "departments", "tenant.read")
    async with db.begin():
        rows = (
            await db.execute(
                text("""SELECT d.*
                        FROM departments d
                        JOIN employees manager
                          ON manager.tenant_id=d.tenant_id
                         AND manager.id=d.manager_id
                         AND manager.user_id=:uid
                         AND manager.deleted_at IS NULL
                        WHERE d.tenant_id=:tid AND d.deleted_at IS NULL
                        ORDER BY d.name"""),
                {"tid": ctx.tenant_id, "uid": ctx.user_id},
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def locations(db: AsyncSession, ctx: RequestContext) -> list[dict]:
    return await _list(db, ctx, "locations", "tenant.read")


async def department_tree(db: AsyncSession, ctx: RequestContext) -> list[dict]:
    ctx.require("tenant.read")
    rows = await departments(db, ctx)
    items = {r["id"]: {**r, "children": []} for r in rows}
    roots = []
    for row in items.values():
        parent = items.get(row["parent_id"])
        (parent["children"] if parent else roots).append(row)
    return roots


async def department_analytics(db: AsyncSession, ctx: RequestContext) -> list[dict]:
    if not get_settings().feature_analytics:
        raise NotFound("Department analytics is disabled", code="feature.analytics_disabled")
    ctx.require("tenant.read")
    async with db.begin():
        rows = (
            await db.execute(
                text("""SELECT d.id, d.name, d.code, d.manager_id,
                               COUNT(e.id) FILTER (WHERE e.deleted_at IS NULL AND e.status <> 'exited') AS employee_count,
                               COUNT(e.id) FILTER (WHERE e.deleted_at IS NULL AND e.status = 'active') AS active_count
                        FROM departments d
                        LEFT JOIN employees e
                          ON e.tenant_id=d.tenant_id AND e.department_id=d.id
                        WHERE d.tenant_id=:tid AND d.deleted_at IS NULL
                        GROUP BY d.id, d.name, d.code, d.manager_id
                        ORDER BY d.name"""),
                {"tid": ctx.tenant_id},
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def department_profile(db: AsyncSession, ctx: RequestContext, department_id: uuid.UUID) -> dict:
    if not get_settings().feature_department_profiles:
        raise NotFound("Department profiles are disabled", code="feature.department_profiles_disabled")
    ctx.require("tenant.read")
    async with db.begin():
        department = (
            await db.execute(
                text("""SELECT d.*, manager.first_name AS manager_first_name,
                               manager.last_name AS manager_last_name,
                               COUNT(employee.id) FILTER (WHERE employee.deleted_at IS NULL
                                 AND employee.status <> 'exited') AS employee_count
                        FROM departments d
                        LEFT JOIN employees manager
                          ON manager.tenant_id=d.tenant_id AND manager.id=d.manager_id
                        LEFT JOIN employees employee
                          ON employee.tenant_id=d.tenant_id AND employee.department_id=d.id
                        WHERE d.tenant_id=:tid AND d.id=:did AND d.deleted_at IS NULL
                        GROUP BY d.id, manager.first_name, manager.last_name"""),
                {"tid": ctx.tenant_id, "did": department_id},
            )
        ).mappings().first()
        if department is None:
            raise NotFound("Department not found", code="department.not_found")
        history = (
            await db.execute(
                text("""SELECT id, action, actor_user_id, before_json, after_json, created_at
                        FROM audit_logs
                        WHERE tenant_id=:tid AND entity_type='department' AND entity_id=:did
                        ORDER BY created_at DESC LIMIT 50"""),
                {"tid": ctx.tenant_id, "did": department_id},
            )
        ).mappings().all()
    return {"department": dict(department), "history": [dict(row) for row in history]}


async def _can_manage_department(
    db: AsyncSession,
    ctx: RequestContext,
    department_id: uuid.UUID,
    require_update: bool = False,
) -> None:
    if ctx.can("tenant.update"):
        return
    if require_update and not ctx.can("employees.update"):
        manager_role = await db.scalar(
            text("""SELECT EXISTS(
                        SELECT 1
                        FROM user_roles ur
                        JOIN roles r ON r.id=ur.role_id
                        WHERE ur.tenant_id=:tid AND ur.user_id=:uid
                          AND r.tenant_id IS NULL
                          AND r.name IN ('Department Manager', 'People Manager')
                    )"""),
            {"tid": ctx.tenant_id, "uid": ctx.user_id},
        )
        if not manager_role:
            ctx.require("employees.update")
    else:
        ctx.require("employees.read")
    allowed = await db.scalar(
        text("""SELECT EXISTS(
                    SELECT 1
                    FROM departments d
                    JOIN employees manager
                      ON manager.tenant_id=d.tenant_id
                     AND manager.id=d.manager_id
                     AND manager.user_id=:uid
                     AND manager.deleted_at IS NULL
                    WHERE d.tenant_id=:tid
                      AND d.id=:did
                      AND d.deleted_at IS NULL
                )"""),
        {"tid": ctx.tenant_id, "uid": ctx.user_id, "did": department_id},
    )
    if not allowed:
        raise ValidationFailed(
            "You can only manage employees in your department",
            code="department.scope_denied",
        )


async def department_employees(
    db: AsyncSession, ctx: RequestContext, department_id: uuid.UUID
) -> dict:
    ctx.require("employees.read")
    async with db.begin():
        await _can_manage_department(db, ctx, department_id)
        assigned = (
            await db.execute(
                text("""SELECT id, employee_no, first_name, last_name, email, designation,
                               status, department_id, manager_id
                        FROM employees
                        WHERE tenant_id=:tid AND department_id=:did AND deleted_at IS NULL
                        ORDER BY last_name, first_name"""),
                {"tid": ctx.tenant_id, "did": department_id},
            )
        ).mappings().all()
        available = (
            await db.execute(
                text("""SELECT id, employee_no, first_name, last_name, email, designation,
                               status, department_id, manager_id
                        FROM employees
                        WHERE tenant_id=:tid AND department_id IS NULL
                          AND deleted_at IS NULL AND status <> 'exited'
                        ORDER BY last_name, first_name"""),
                {"tid": ctx.tenant_id},
            )
        ).mappings().all()
    return {"assigned": [dict(row) for row in assigned], "available": [dict(row) for row in available]}


async def assign_department_employee(
    db: AsyncSession, ctx: RequestContext, department_id: uuid.UUID, employee_id: uuid.UUID
) -> dict:
    ctx.require("employees.update")
    async with db.begin():
        await _can_manage_department(db, ctx, department_id, require_update=True)
        employee = (
            await db.execute(
                text("""SELECT * FROM employees
                        WHERE tenant_id=:tid AND id=:eid AND deleted_at IS NULL
                        FOR UPDATE"""),
                {"tid": ctx.tenant_id, "eid": employee_id},
            )
        ).mappings().first()
        if employee is None:
            raise NotFound("Employee not found", code="employee.not_found")
        if employee["department_id"] is not None:
            raise Conflict("Employee already belongs to a department", code="employee.already_assigned")
        row = (
            await db.execute(
                text("""UPDATE employees SET department_id=:did, updated_at=now()
                        WHERE tenant_id=:tid AND id=:eid RETURNING id, employee_no,
                        first_name, last_name, email, designation, status,
                        department_id, manager_id"""),
                {"tid": ctx.tenant_id, "did": department_id, "eid": employee_id},
            )
        ).mappings().one()
    return dict(row)


async def remove_department_employee(
    db: AsyncSession, ctx: RequestContext, department_id: uuid.UUID, employee_id: uuid.UUID
) -> None:
    ctx.require("employees.update")
    async with db.begin():
        await _can_manage_department(db, ctx, department_id, require_update=True)
        result = await db.execute(
            text("""UPDATE employees SET department_id=NULL, updated_at=now()
                    WHERE tenant_id=:tid AND id=:eid AND department_id=:did
                    AND deleted_at IS NULL"""),
            {"tid": ctx.tenant_id, "eid": employee_id, "did": department_id},
        )
        if result.rowcount == 0:
            raise NotFound("Employee is not assigned to this department", code="employee.not_assigned")


async def create_department(db: AsyncSession, ctx: RequestContext, data: dict) -> dict:
    ctx.require("tenant.update")
    data["code"] = data.get("code") or None
    if not data.get("code"):
        words = re.findall(r"[A-Za-z0-9]+", data["name"].upper())
        data["code"] = "".join(word[0] for word in words)[:8] or "DEPT"
    async with db.begin():
        await _validate_department_refs(db, ctx.tenant_id, data)
        base_code = data["code"].upper()
        data["code"] = base_code
        suffix = 2
        while await db.scalar(
            text("SELECT EXISTS(SELECT 1 FROM departments WHERE tenant_id=:tid AND code=:code)"),
            {"tid": ctx.tenant_id, "code": data["code"]},
        ):
            data["code"] = f"{base_code[:35]}-{suffix}"
            suffix += 1
        row = (
            (
                await db.execute(
                    text("""INSERT INTO departments(tenant_id,name,code,parent_id,manager_id)
          VALUES (:tid,:name,:code,:parent,:manager) RETURNING *"""),
                    {
                        "tid": ctx.tenant_id,
                        "name": data["name"],
                        "code": data["code"],
                        "parent": data.get("parent_id"),
                        "manager": data.get("manager_id"),
                    },
                )
            )
            .mappings()
            .one()
        )
        await _audit(db, ctx, "department.created", "department", row["id"], after=dict(row))
    return dict(row)


async def update_department(
    db: AsyncSession, ctx: RequestContext, entity_id: uuid.UUID, changes: dict
) -> dict:
    ctx.require("tenant.update")
    changes = {k: v for k, v in changes.items() if v is not None or k in {"parent_id", "manager_id"}}
    if set(changes) - {"name", "code", "parent_id", "manager_id"}:
        raise ValidationFailed()
    async with db.begin():
        old = (
            (
                await db.execute(
                    text(
                        "SELECT * FROM departments WHERE tenant_id=:tid AND id=:id AND deleted_at IS NULL FOR UPDATE"
                    ),
                    {"tid": ctx.tenant_id, "id": entity_id},
                )
            )
            .mappings()
            .first()
        )
        if old is None:
            raise NotFound("Department not found", code="department.not_found")
        await _validate_department_refs(db, ctx.tenant_id, changes)
        parent = changes.get("parent_id")
        if parent:
            cycle = await db.scalar(
                text("""WITH RECURSIVE ancestors(id) AS (SELECT :parent::uuid UNION ALL
              SELECT d.parent_id FROM departments d JOIN ancestors a ON d.id=a.id WHERE d.tenant_id=:tid AND d.parent_id IS NOT NULL)
              SELECT EXISTS(SELECT 1 FROM ancestors WHERE id=:id)"""),
                {"parent": parent, "tid": ctx.tenant_id, "id": entity_id},
            )
            if cycle:
                raise ValidationFailed(
                    "Department parent would create a cycle", code="department.parent_cycle"
                )
        if changes:
            sets = ",".join(f"{k}=:{k}" for k in changes)
            await db.execute(
                text(f"UPDATE departments SET {sets} WHERE tenant_id=:tid AND id=:id"),
                {**changes, "tid": ctx.tenant_id, "id": entity_id},
            )
        new = (
            (
                await db.execute(
                    text("SELECT * FROM departments WHERE tenant_id=:tid AND id=:id"),
                    {"tid": ctx.tenant_id, "id": entity_id},
                )
            )
            .mappings()
            .one()
        )
        await _audit(db, ctx, "department.updated", "department", entity_id, dict(old), dict(new))
    return dict(new)


async def delete_department(db: AsyncSession, ctx: RequestContext, entity_id: uuid.UUID) -> None:
    ctx.require("tenant.update")
    async with db.begin():
        row = (
            (
                await db.execute(
                    text(
                        "SELECT * FROM departments WHERE tenant_id=:tid AND id=:id AND deleted_at IS NULL FOR UPDATE"
                    ),
                    {"tid": ctx.tenant_id, "id": entity_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound("Department not found", code="department.not_found")
        children = await db.scalar(
            text(
                "SELECT EXISTS(SELECT 1 FROM departments WHERE tenant_id=:tid AND parent_id=:id AND deleted_at IS NULL)"
            ),
            {"tid": ctx.tenant_id, "id": entity_id},
        )
        if children:
            raise Conflict("Department has active sub-departments", code="department.has_children")
        await db.execute(
            text("UPDATE departments SET deleted_at=now() WHERE tenant_id=:tid AND id=:id"),
            {"tid": ctx.tenant_id, "id": entity_id},
        )
        await _audit(db, ctx, "department.deleted", "department", entity_id, dict(row))


async def create_location(db: AsyncSession, ctx: RequestContext, data: dict) -> dict:
    ctx.require("tenant.update")
    async with db.begin():
        row = (
            (
                await db.execute(
                    text("""INSERT INTO locations(tenant_id,name,address,country_code,timezone,is_active)
          VALUES (:tid,:name,:address,:country,:timezone,:active) RETURNING *"""),
                    {
                        "tid": ctx.tenant_id,
                        "name": data["name"],
                        "address": data.get("address"),
                        "country": data.get("country_code"),
                        "timezone": data.get("timezone", "UTC"),
                        "active": data.get("is_active", True),
                    },
                )
            )
            .mappings()
            .one()
        )
        await _audit(db, ctx, "location.created", "location", row["id"], after=dict(row))
    return dict(row)


async def update_location(db: AsyncSession, ctx: RequestContext, entity_id: uuid.UUID, changes: dict) -> dict:
    ctx.require("tenant.update")
    changes = {k: v for k, v in changes.items() if v is not None or k in {"address", "country_code"}}
    if set(changes) - {"name", "address", "country_code", "timezone", "is_active"}:
        raise ValidationFailed()
    async with db.begin():
        old = (
            (
                await db.execute(
                    text("SELECT * FROM locations WHERE tenant_id=:tid AND id=:id FOR UPDATE"),
                    {"tid": ctx.tenant_id, "id": entity_id},
                )
            )
            .mappings()
            .first()
        )
        if old is None:
            raise NotFound("Location not found", code="location.not_found")
        if changes:
            sets = ",".join(f"{k}=:{k}" for k in changes)
            await db.execute(
                text(f"UPDATE locations SET {sets} WHERE tenant_id=:tid AND id=:id"),
                {**changes, "tid": ctx.tenant_id, "id": entity_id},
            )
        new = (
            (
                await db.execute(
                    text("SELECT * FROM locations WHERE tenant_id=:tid AND id=:id"),
                    {"tid": ctx.tenant_id, "id": entity_id},
                )
            )
            .mappings()
            .one()
        )
        await _audit(db, ctx, "location.updated", "location", entity_id, dict(old), dict(new))
    return dict(new)


async def delete_location(db: AsyncSession, ctx: RequestContext, entity_id: uuid.UUID) -> None:
    ctx.require("tenant.update")
    async with db.begin():
        row = (
            (
                await db.execute(
                    text("SELECT * FROM locations WHERE tenant_id=:tid AND id=:id FOR UPDATE"),
                    {"tid": ctx.tenant_id, "id": entity_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound("Location not found", code="location.not_found")
        await db.execute(
            text("UPDATE locations SET is_active=false WHERE tenant_id=:tid AND id=:id"),
            {"tid": ctx.tenant_id, "id": entity_id},
        )
        await _audit(db, ctx, "location.deactivated", "location", entity_id, dict(row))


def _validate_rules(rules: dict) -> None:
    days = rules.get("working_days", [1, 2, 3, 4, 5])
    if not isinstance(days, list) or any(not isinstance(day, int) or day not in range(1, 8) for day in days):
        raise ValidationFailed(
            "working_days must contain integers 1 through 7", code="schedule.invalid_rules"
        )
    for field in ("start", "end"):
        if field in rules and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(rules[field])):
            raise ValidationFailed(f"{field} must use HH:MM", code="schedule.invalid_rules")
    for field in ("grace_minutes", "half_day_minutes"):
        if field in rules and (not isinstance(rules[field], int) or rules[field] < 0):
            raise ValidationFailed(f"{field} must be a non-negative integer", code="schedule.invalid_rules")


async def schedules(db: AsyncSession, ctx: RequestContext) -> list[dict]:
    return await _list(db, ctx, "work_schedules", "attendance.read")


async def create_schedule(db: AsyncSession, ctx: RequestContext, data: dict) -> dict:
    ctx.require("attendance.manage")
    _validate_rules(data.get("rules_json", {}))
    async with db.begin():
        if data.get("is_default"):
            await db.execute(
                text("UPDATE work_schedules SET is_default=false WHERE tenant_id=:tid AND is_default"),
                {"tid": ctx.tenant_id},
            )
        row = (
            (
                await db.execute(
                    text("""INSERT INTO work_schedules(tenant_id,name,timezone,rules_json,is_default)
          VALUES (:tid,:name,:timezone,CAST(:rules AS jsonb),:default) RETURNING *"""),
                    {
                        "tid": ctx.tenant_id,
                        "name": data["name"],
                        "timezone": data.get("timezone", "UTC"),
                        "rules": json.dumps(data.get("rules_json", {})),
                        "default": data.get("is_default", False),
                    },
                )
            )
            .mappings()
            .one()
        )
        await _audit(db, ctx, "work_schedule.created", "work_schedule", row["id"], after=dict(row))
    return dict(row)


async def update_schedule(db: AsyncSession, ctx: RequestContext, entity_id: uuid.UUID, changes: dict) -> dict:
    ctx.require("attendance.manage")
    changes = {k: v for k, v in changes.items() if v is not None}
    if "rules_json" in changes:
        _validate_rules(changes["rules_json"])
        changes["rules_json"] = json.dumps(changes["rules_json"])
    if set(changes) - {"name", "timezone", "rules_json", "is_default"}:
        raise ValidationFailed()
    async with db.begin():
        old = (
            (
                await db.execute(
                    text("SELECT * FROM work_schedules WHERE tenant_id=:tid AND id=:id FOR UPDATE"),
                    {"tid": ctx.tenant_id, "id": entity_id},
                )
            )
            .mappings()
            .first()
        )
        if old is None:
            raise NotFound("Work schedule not found", code="schedule.not_found")
        if changes.get("is_default"):
            await db.execute(
                text("UPDATE work_schedules SET is_default=false WHERE tenant_id=:tid AND is_default"),
                {"tid": ctx.tenant_id},
            )
        if changes:
            sets = ",".join(f"{k}=CAST(:{k} AS jsonb)" if k == "rules_json" else f"{k}=:{k}" for k in changes)
            await db.execute(
                text(f"UPDATE work_schedules SET {sets} WHERE tenant_id=:tid AND id=:id"),
                {**changes, "tid": ctx.tenant_id, "id": entity_id},
            )
        new = (
            (
                await db.execute(
                    text("SELECT * FROM work_schedules WHERE tenant_id=:tid AND id=:id"),
                    {"tid": ctx.tenant_id, "id": entity_id},
                )
            )
            .mappings()
            .one()
        )
        await _audit(db, ctx, "work_schedule.updated", "work_schedule", entity_id, dict(old), dict(new))
    return dict(new)


async def delete_schedule(db: AsyncSession, ctx: RequestContext, entity_id: uuid.UUID) -> None:
    ctx.require("attendance.manage")
    async with db.begin():
        row = (
            (
                await db.execute(
                    text("SELECT * FROM work_schedules WHERE tenant_id=:tid AND id=:id FOR UPDATE"),
                    {"tid": ctx.tenant_id, "id": entity_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound("Work schedule not found", code="schedule.not_found")
        used = await db.scalar(
            text(
                "SELECT EXISTS(SELECT 1 FROM employees WHERE tenant_id=:tid AND work_schedule_id=:id AND deleted_at IS NULL)"
            ),
            {"tid": ctx.tenant_id, "id": entity_id},
        )
        if used:
            raise Conflict("Work schedule is assigned to employees", code="schedule.in_use")
        await db.execute(
            text("DELETE FROM work_schedules WHERE tenant_id=:tid AND id=:id"),
            {"tid": ctx.tenant_id, "id": entity_id},
        )
        await _audit(db, ctx, "work_schedule.deleted", "work_schedule", entity_id, dict(row))


async def holidays(db: AsyncSession, ctx: RequestContext) -> list[dict]:
    ctx.require("attendance.read")
    async with db.begin():
        rows = (
            (
                await db.execute(
                    text("SELECT * FROM holidays WHERE tenant_id=:tid ORDER BY holiday_date,name"),
                    {"tid": ctx.tenant_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def create_holiday(db: AsyncSession, ctx: RequestContext, data: dict) -> dict:
    ctx.require("attendance.manage")
    async with db.begin():
        if data.get("location_id"):
            valid = await db.scalar(
                text("SELECT EXISTS(SELECT 1 FROM locations WHERE tenant_id=:tid AND id=:id AND is_active)"),
                {"tid": ctx.tenant_id, "id": data["location_id"]},
            )
            if not valid:
                raise NotFound("Location not found", code="location.not_found")
        row = (
            (
                await db.execute(
                    text("""INSERT INTO holidays(tenant_id,location_id,name,holiday_date,is_optional)
          VALUES (:tid,:location,:name,:date,:optional) RETURNING *"""),
                    {
                        "tid": ctx.tenant_id,
                        "location": data.get("location_id"),
                        "name": data["name"],
                        "date": data["holiday_date"],
                        "optional": data.get("is_optional", False),
                    },
                )
            )
            .mappings()
            .one()
        )
        await _audit(db, ctx, "holiday.created", "holiday", row["id"], after=dict(row))
    return dict(row)


async def update_holiday(db: AsyncSession, ctx: RequestContext, entity_id: uuid.UUID, changes: dict) -> dict:
    ctx.require("attendance.manage")
    changes = {k: v for k, v in changes.items() if v is not None or k == "location_id"}
    allowed = {"location_id", "name", "holiday_date", "is_optional"}
    if set(changes) - allowed:
        raise ValidationFailed()
    async with db.begin():
        old = (
            (
                await db.execute(
                    text("SELECT * FROM holidays WHERE tenant_id=:tid AND id=:id FOR UPDATE"),
                    {"tid": ctx.tenant_id, "id": entity_id},
                )
            )
            .mappings()
            .first()
        )
        if old is None:
            raise NotFound("Holiday not found", code="holiday.not_found")
        if changes.get("location_id"):
            valid = await db.scalar(
                text("SELECT EXISTS(SELECT 1 FROM locations WHERE tenant_id=:tid AND id=:id AND is_active)"),
                {"tid": ctx.tenant_id, "id": changes["location_id"]},
            )
            if not valid:
                raise NotFound("Location not found", code="location.not_found")
        if changes:
            sets = ",".join(f"{k}=:{k}" for k in changes)
            await db.execute(
                text(f"UPDATE holidays SET {sets} WHERE tenant_id=:tid AND id=:id"),
                {**changes, "tid": ctx.tenant_id, "id": entity_id},
            )
        new = (
            (
                await db.execute(
                    text("SELECT * FROM holidays WHERE tenant_id=:tid AND id=:id"),
                    {"tid": ctx.tenant_id, "id": entity_id},
                )
            )
            .mappings()
            .one()
        )
        await _audit(db, ctx, "holiday.updated", "holiday", entity_id, dict(old), dict(new))
    return dict(new)


async def delete_holiday(db: AsyncSession, ctx: RequestContext, entity_id: uuid.UUID) -> None:
    ctx.require("attendance.manage")
    async with db.begin():
        row = (
            (
                await db.execute(
                    text("SELECT * FROM holidays WHERE tenant_id=:tid AND id=:id FOR UPDATE"),
                    {"tid": ctx.tenant_id, "id": entity_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound("Holiday not found", code="holiday.not_found")
        await db.execute(
            text("DELETE FROM holidays WHERE tenant_id=:tid AND id=:id"),
            {"tid": ctx.tenant_id, "id": entity_id},
        )
        await _audit(db, ctx, "holiday.deleted", "holiday", entity_id, dict(row))
