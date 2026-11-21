import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import Forbidden, NotFound
from app.core.permissions import DataScope, resolve_scope


async def _can_manage_employee(db: AsyncSession, ctx: RequestContext, employee_id: uuid.UUID) -> bool:
    if ctx.can("tenant.update") or ctx.can("employees.update"):
        return True
    return bool(await db.scalar(
        text("""SELECT EXISTS(
                    SELECT 1 FROM departments d
                    JOIN employees manager ON manager.tenant_id=d.tenant_id
                      AND manager.id=d.manager_id AND manager.user_id=:uid
                    WHERE d.tenant_id=:tid AND d.deleted_at IS NULL
                      AND d.id=(SELECT department_id FROM employees
                                WHERE tenant_id=:tid AND id=:eid AND deleted_at IS NULL)
                )"""),
        {"tid": ctx.tenant_id, "uid": ctx.user_id, "eid": employee_id},
    ))


async def list_tasks(db: AsyncSession, ctx: RequestContext, assigned_only: bool = False) -> list[dict]:
    ctx.require("employees.read")
    is_employee = (await resolve_scope(db, ctx, "employees.read")) is DataScope.SELF
    visibility_clause = """
        e.user_id=:uid AND (
            EXISTS (SELECT 1 FROM tenant_memberships creator_membership
                   WHERE creator_membership.tenant_id=:tid
                     AND creator_membership.user_id=t.created_by
                     AND creator_membership.status='active'
                     AND creator_membership.is_owner)
            OR EXISTS (SELECT 1 FROM user_roles creator_roles
                       JOIN roles creator_role ON creator_role.id=creator_roles.role_id
                       WHERE creator_roles.tenant_id=:tid
                         AND creator_roles.user_id=t.created_by
                         AND creator_role.data_scope='all'
                         AND creator_role.is_system)
        )
    """ if assigned_only and not is_employee else """
        e.user_id=:uid
    """ if assigned_only else """
        t.created_by=:uid
        OR EXISTS (SELECT 1 FROM departments d
                   JOIN employees m ON m.id=d.manager_id
                   WHERE d.tenant_id=:tid AND d.id=e.department_id
                     AND m.user_id=:uid)
    """
    rows = await db.execute(
        text(f"""SELECT t.*, e.first_name, e.last_name, e.employee_no
                FROM employee_tasks t JOIN employees e
                  ON e.tenant_id=t.tenant_id AND e.id=t.assignee_employee_id
                WHERE t.tenant_id=:tid
                  AND ({visibility_clause})
                ORDER BY t.created_at DESC"""),
        {"tid": ctx.tenant_id, "uid": ctx.user_id},
    )
    return [dict(row) for row in rows.mappings().all()]


async def create_task(db: AsyncSession, ctx: RequestContext, values: dict) -> dict:
    ctx.require("employees.read")
    async with db.begin():
        if not await _can_manage_employee(db, ctx, values["assignee_employee_id"]):
            raise Forbidden(details={"permission": "employees.update"})
        employee = (
            await db.execute(
                text("""SELECT id, user_id FROM employees WHERE tenant_id=:tid AND id=:eid
                        AND deleted_at IS NULL AND status <> 'exited'"""),
                {"tid": ctx.tenant_id, "eid": values["assignee_employee_id"]},
            )
        ).mappings().first()
        if employee is None:
            raise NotFound("Employee not found", code="employee.not_found")
        row = (await db.execute(
            text("""INSERT INTO employee_tasks
                    (tenant_id,title,description,assignee_employee_id,created_by,due_date)
                    VALUES (:tid,:title,:description,:assignee,:uid,:due)
                    RETURNING *"""),
            {**values, "tid": ctx.tenant_id, "assignee": values["assignee_employee_id"],
             "uid": ctx.user_id, "due": values.get("due_date")},
        )).mappings().one()
        if employee.user_id is not None and employee.user_id != ctx.user_id:
            await db.execute(
                text("""INSERT INTO notifications
                        (tenant_id, user_id, channel, type, template, payload_json, dedupe_key)
                        VALUES (:tid, :uid, 'in_app', 'task.assigned', 'task.assigned',
                                CAST(:payload AS jsonb), :dedupe)"""),
                {
                    "tid": ctx.tenant_id,
                    "uid": employee.user_id,
                    "payload": json.dumps({
                        "task_id": str(row["id"]),
                        "title": row["title"],
                        "message": f"New task assigned: {row['title']}",
                    }),
                    "dedupe": f"task.assigned:{row['id']}",
                },
            )
    return dict(row)


async def update_status(db: AsyncSession, ctx: RequestContext, task_id: uuid.UUID, status: str) -> dict:
    ctx.require("employees.read")
    async with db.begin():
        row = (await db.execute(
            text("""UPDATE employee_tasks t SET status=:status, updated_at=now()
                    WHERE t.tenant_id=:tid AND t.id=:id
                      AND (t.created_by=:uid OR EXISTS (
                        SELECT 1 FROM employees e
                        WHERE e.id=t.assignee_employee_id AND e.user_id=:uid))
                    RETURNING t.*"""),
            {"tid": ctx.tenant_id, "id": task_id, "uid": ctx.user_id, "status": status},
        )).mappings().first()
        if row is None:
            raise NotFound("Task not found", code="task.not_found")
        if status == "done" and row["created_by"] != ctx.user_id:
            await db.execute(
                text("""INSERT INTO notifications
                        (tenant_id, user_id, channel, type, template, payload_json, dedupe_key)
                        VALUES (:tid, :uid, 'in_app', 'task.completed', 'task.completed',
                                CAST(:payload AS jsonb), :dedupe)"""),
                {
                    "tid": ctx.tenant_id,
                    "uid": row["created_by"],
                    "payload": json.dumps({
                        "task_id": str(row["id"]),
                        "title": row["title"],
                        "message": f"{row['title']} was marked complete.",
                    }),
                    "dedupe": f"task.completed:{row['id']}:{row['updated_at']}",
                },
            )
    return dict(row)
