"""Attendance, corrections, and nightly absence marking."""

import json
import uuid
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import Conflict, Forbidden, NotFound, ValidationFailed
from app.domains.attendance.schemas import parse_month


async def _audit(db: AsyncSession, ctx: RequestContext, action: str, kind: str, entity_id: uuid.UUID) -> None:
    await db.execute(
        text("""INSERT INTO audit_logs(tenant_id,actor_user_id,action,entity_type,entity_id,source,request_id)
        VALUES (:tid,:uid,:action,:kind,:id,:source,:rid)"""),
        {
            "tid": ctx.tenant_id,
            "uid": ctx.user_id,
            "action": action,
            "kind": kind,
            "id": entity_id,
            "source": ctx.source,
            "rid": ctx.request_id,
        },
    )


async def _employee(db: AsyncSession, ctx: RequestContext) -> dict:
    row = (
        (
            await db.execute(
                text("""SELECT e.*, s.timezone, s.rules_json
        FROM employees e LEFT JOIN work_schedules s ON s.tenant_id=e.tenant_id AND s.id=e.work_schedule_id
        WHERE e.tenant_id=:tid AND e.user_id=:uid AND e.deleted_at IS NULL"""),
                {"tid": ctx.tenant_id, "uid": ctx.user_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Employee profile not found", code="attendance.employee_not_found")
    if not row["timezone"]:
        row = dict(row)
        schedule = (
            (
                await db.execute(
                    text(
                        "SELECT timezone,rules_json FROM work_schedules WHERE tenant_id=:tid AND is_default"
                    ),
                    {"tid": ctx.tenant_id},
                )
            )
            .mappings()
            .first()
        )
        row["timezone"] = schedule["timezone"] if schedule else "UTC"
        row["rules_json"] = schedule["rules_json"] if schedule else {}
    return dict(row)


def _local_date(employee: dict, now: datetime | None = None) -> date:
    try:
        zone = ZoneInfo(employee.get("timezone") or "UTC")
    except ZoneInfoNotFoundError:
        raise ValidationFailed(
            "Employee schedule has an invalid timezone", code="attendance.invalid_timezone"
        ) from None
    return (now or datetime.now(UTC)).astimezone(zone).date()


async def check_in(db: AsyncSession, ctx: RequestContext, notes: str | None) -> dict:
    async with db.begin():
        employee = await _employee(db, ctx)
        work_date = _local_date(employee)
        result = (
            (
                await db.execute(
                    text("""INSERT INTO attendance_records(tenant_id,employee_id,work_date,check_in,status,source,notes)
            VALUES (:tid,:eid,:day,now(),'present',:source,:notes)
            ON CONFLICT (tenant_id,employee_id,work_date) DO NOTHING RETURNING *"""),
                    {
                        "tid": ctx.tenant_id,
                        "eid": employee["id"],
                        "day": work_date,
                        "source": "mobile" if ctx.source == "ai" else "web",
                        "notes": notes,
                    },
                )
            )
            .mappings()
            .first()
        )
        if result is None:
            raise Conflict("Attendance is already recorded for today", code="attendance.already_checked_in")
        await _audit(db, ctx, "attendance.checked_in", "attendance", result["id"])
        return dict(result)


async def check_out(db: AsyncSession, ctx: RequestContext) -> dict:
    async with db.begin():
        employee = await _employee(db, ctx)
        work_date = _local_date(employee)
        row = (
            (
                await db.execute(
                    text("""SELECT * FROM attendance_records WHERE tenant_id=:tid AND employee_id=:eid
            AND work_date=:day FOR UPDATE"""),
                    {"tid": ctx.tenant_id, "eid": employee["id"], "day": work_date},
                )
            )
            .mappings()
            .first()
        )
        if row is None or row["check_in"] is None:
            raise Conflict("Check in before checking out", code="attendance.not_checked_in")
        if row["check_out"] is not None:
            raise Conflict("Attendance is already checked out", code="attendance.already_checked_out")
        rules = employee.get("rules_json") or {}
        worked = max(0, int((datetime.now(UTC) - row["check_in"]).total_seconds() // 60))
        threshold = int(rules.get("half_day_minutes", 240)) if isinstance(rules, dict) else 240
        status = "half_day" if worked < threshold else "present"
        updated = (
            (
                await db.execute(
                    text("""UPDATE attendance_records SET check_out=now(),worked_minutes=:minutes,
            status=:status,updated_at=now() WHERE tenant_id=:tid AND id=:id RETURNING *"""),
                    {"minutes": worked, "status": status, "tid": ctx.tenant_id, "id": row["id"]},
                )
            )
            .mappings()
            .one()
        )
        await _audit(db, ctx, "attendance.checked_out", "attendance", row["id"])
        return dict(updated)


async def my_attendance(db: AsyncSession, ctx: RequestContext, month: str) -> dict:
    start, end = parse_month(month)
    async with db.begin():
        employee = await _employee(db, ctx)
        rows = (
            (
                await db.execute(
                    text("""SELECT * FROM attendance_records WHERE tenant_id=:tid AND employee_id=:eid
            AND work_date>=:start AND work_date<:end ORDER BY work_date"""),
                    {"tid": ctx.tenant_id, "eid": employee["id"], "start": start, "end": end},
                )
            )
            .mappings()
            .all()
        )
        return {"items": [dict(row) for row in rows]}


async def list_attendance(
    db: AsyncSession, ctx: RequestContext, month: str | None, limit: int, cursor: uuid.UUID | None
) -> dict:
    month = month or datetime.now(UTC).strftime("%Y-%m")
    start, end = parse_month(month)
    ctx.require("attendance.read")
    async with db.begin():
        scopes = (
            (
                await db.execute(
                    text("""SELECT r.data_scope FROM user_roles ur JOIN roles r ON r.id=ur.role_id
            JOIN role_permissions rp ON rp.role_id=r.id JOIN permissions p ON p.id=rp.permission_id
            WHERE ur.tenant_id=:tid AND ur.user_id=:uid AND p.code='attendance.read'"""),
                    {"tid": ctx.tenant_id, "uid": ctx.user_id},
                )
            )
            .scalars()
            .all()
        )
        scope = max(scopes, key={"self": 1, "team": 2, "department": 3, "all": 4}.get) if scopes else "self"
        clauses = {
            "all": "TRUE",
            "self": "e.user_id=:uid",
            "team": "e.id IN (WITH RECURSIVE team(id) AS (SELECT id FROM employees WHERE tenant_id=:tid AND user_id=:uid AND deleted_at IS NULL UNION ALL SELECT e2.id FROM employees e2 JOIN team t ON e2.manager_id=t.id WHERE e2.tenant_id=:tid AND e2.deleted_at IS NULL) SELECT id FROM team)",
            "department": "e.department_id IN (WITH RECURSIVE dept(id) AS (SELECT department_id FROM employees WHERE tenant_id=:tid AND user_id=:uid AND deleted_at IS NULL UNION ALL SELECT d.id FROM departments d JOIN dept x ON d.parent_id=x.id WHERE d.tenant_id=:tid AND d.deleted_at IS NULL) SELECT id FROM dept)",
        }
        rows = (
            (
                await db.execute(
                    text(f"""SELECT a.*,e.employee_no,e.first_name,e.last_name FROM attendance_records a
            JOIN employees e ON e.tenant_id=a.tenant_id AND e.id=a.employee_id
            WHERE a.tenant_id=:tid AND a.work_date>=:start AND a.work_date<:end AND {clauses[scope]}
            AND (:cursor IS NULL OR (a.work_date,a.id)>(SELECT work_date,id FROM attendance_records WHERE tenant_id=:tid AND id=:cursor))
            ORDER BY a.work_date,a.id LIMIT :limit"""),
                    {
                        "tid": ctx.tenant_id,
                        "uid": ctx.user_id,
                        "start": start,
                        "end": end,
                        "cursor": cursor,
                        "limit": limit,
                    },
                )
            )
            .mappings()
            .all()
        )
        return {
            "items": [dict(row) for row in rows],
            "next_cursor": str(rows[-1]["id"]) if len(rows) == limit else None,
        }


async def summary(db: AsyncSession, ctx: RequestContext, month: str) -> dict:
    ctx.require("attendance.read")
    start, end = parse_month(month)
    async with db.begin():
        scopes = (
            (
                await db.execute(
                    text("""SELECT r.data_scope FROM user_roles ur JOIN roles r ON r.id=ur.role_id
            JOIN role_permissions rp ON rp.role_id=r.id JOIN permissions p ON p.id=rp.permission_id
            WHERE ur.tenant_id=:tid AND ur.user_id=:uid AND p.code='attendance.read'"""),
                    {"tid": ctx.tenant_id, "uid": ctx.user_id},
                )
            )
            .scalars()
            .all()
        )
        scope = max(scopes, key={"self": 1, "team": 2, "department": 3, "all": 4}.get) if scopes else "self"
        scope_clause = {
            "all": "TRUE",
            "self": "e.user_id=:uid",
            "team": "e.id IN (WITH RECURSIVE team(id) AS (SELECT id FROM employees WHERE tenant_id=:tid AND user_id=:uid AND deleted_at IS NULL UNION ALL SELECT e2.id FROM employees e2 JOIN team t ON e2.manager_id=t.id WHERE e2.tenant_id=:tid AND e2.deleted_at IS NULL) SELECT id FROM team)",
            "department": "e.department_id IN (WITH RECURSIVE dept(id) AS (SELECT department_id FROM employees WHERE tenant_id=:tid AND user_id=:uid AND deleted_at IS NULL UNION ALL SELECT d.id FROM departments d JOIN dept x ON d.parent_id=x.id WHERE d.tenant_id=:tid AND d.deleted_at IS NULL) SELECT id FROM dept)",
        }[scope]
        rows = (
            (
                await db.execute(
                    text(f"""SELECT e.id AS employee_id,e.employee_no,e.first_name,e.last_name,
            count(a.id) FILTER (WHERE a.status='present') AS present_days,
            count(a.id) FILTER (WHERE a.status='half_day') AS half_days,
            count(a.id) FILTER (WHERE a.status='absent') AS absent_days,
            count(a.id) FILTER (WHERE a.status='on_leave') AS leave_days,
            COALESCE(sum(a.worked_minutes),0) AS worked_minutes
            FROM employees e LEFT JOIN attendance_records a ON a.tenant_id=e.tenant_id AND a.employee_id=e.id
            AND a.work_date>=:start AND a.work_date<:end WHERE e.tenant_id=:tid AND e.deleted_at IS NULL
            AND e.status<>'exited' AND {scope_clause} GROUP BY e.id ORDER BY e.employee_no"""),
                    {"tid": ctx.tenant_id, "uid": ctx.user_id, "start": start, "end": end},
                )
            )
            .mappings()
            .all()
        )
        return {"items": [dict(row) for row in rows]}


async def create_correction(db: AsyncSession, ctx: RequestContext, values: dict) -> dict:
    async with db.begin():
        employee = await _employee(db, ctx)
        day = values["work_date"]
        if day >= _local_date(employee):
            raise ValidationFailed(
                "Corrections must be for a past work date", code="attendance.invalid_correction_date"
            )
        record = await db.scalar(
            text(
                "SELECT id FROM attendance_records WHERE tenant_id=:tid AND employee_id=:eid AND work_date=:day"
            ),
            {"tid": ctx.tenant_id, "eid": employee["id"], "day": day},
        )
        correction = (
            (
                await db.execute(
                    text("""INSERT INTO attendance_corrections(tenant_id,employee_id,attendance_record_id,
            work_date,requested_check_in,requested_check_out,reason) VALUES (:tid,:eid,:record,:day,:checkin,:checkout,:reason)
            RETURNING *"""),
                    {
                        "tid": ctx.tenant_id,
                        "eid": employee["id"],
                        "record": record,
                        "day": day,
                        "checkin": values["requested_check_in"],
                        "checkout": values["requested_check_out"],
                        "reason": values["reason"],
                    },
                )
            )
            .mappings()
            .one()
        )
        await _start_correction_workflow(db, ctx, employee, dict(correction))
        await _audit(db, ctx, "attendance.correction_requested", "attendance_correction", correction["id"])
        return dict(correction)


async def _start_correction_workflow(
    db: AsyncSession, ctx: RequestContext, employee: dict, correction: dict
) -> None:
    version = (
        (
            await db.execute(
                text("""SELECT v.id,v.definition_json FROM workflow_definitions d JOIN workflow_versions v
        ON v.tenant_id=d.tenant_id AND v.workflow_definition_id=d.id WHERE d.tenant_id=:tid
        AND d.entity_type='attendance_correction' AND d.status='active' AND v.published_at IS NOT NULL
        ORDER BY v.version DESC LIMIT 1"""),
                {"tid": ctx.tenant_id},
            )
        )
        .mappings()
        .first()
    )
    if version is None:
        return
    definition = version["definition_json"] or {}
    step_key = definition.get("start")
    step = definition.get("steps", {}).get(step_key) if step_key else None
    if not isinstance(step, dict):
        raise ValidationFailed(
            "Active attendance correction workflow has an invalid start step",
            code="workflow.invalid_definition",
        )
    assignee = step.get("assignee", {})
    assignee_id = None
    if assignee.get("kind") == "manager_of_subject":
        assignee_id = (
            await db.scalar(
                text("""SELECT e.user_id FROM employees e JOIN tenant_memberships m ON m.tenant_id=e.tenant_id
                AND m.user_id=e.user_id AND m.status='active' WHERE e.tenant_id=:tid AND e.id=:manager
                AND e.deleted_at IS NULL"""),
                {"tid": ctx.tenant_id, "manager": employee.get("manager_id")},
            )
            if employee.get("manager_id")
            else None
        )
    elif assignee.get("kind") == "department_head":
        assignee_id = await db.scalar(
            text("""SELECT manager.user_id FROM departments d JOIN employees manager
            ON manager.tenant_id=d.tenant_id AND manager.id=d.manager_id JOIN tenant_memberships m
            ON m.tenant_id=manager.tenant_id AND m.user_id=manager.user_id AND m.status='active'
            WHERE d.tenant_id=:tid AND d.id=:department"""),
            {"tid": ctx.tenant_id, "department": employee.get("department_id")},
        )
    elif assignee.get("kind") == "user":
        assignee_id = assignee.get("user_id")
    elif assignee.get("kind") == "permission":
        assignee_id = await db.scalar(
            text("""SELECT ur.user_id FROM user_roles ur JOIN tenant_memberships m
            ON m.tenant_id=ur.tenant_id AND m.user_id=ur.user_id AND m.status='active'
            JOIN role_permissions rp ON rp.role_id=ur.role_id JOIN permissions p ON p.id=rp.permission_id
            WHERE ur.tenant_id=:tid AND p.code=:permission ORDER BY ur.user_id LIMIT 1"""),
            {"tid": ctx.tenant_id, "permission": assignee.get("code")},
        )
    if assignee_id is None:
        fallback = assignee.get("fallback", assignee)
        if fallback.get("kind") == "user":
            assignee_id = fallback.get("user_id")
        elif fallback.get("kind") == "permission":
            assignee_id = await db.scalar(
                text("""SELECT ur.user_id FROM user_roles ur JOIN role_permissions rp ON rp.role_id=ur.role_id
                JOIN permissions p ON p.id=rp.permission_id WHERE ur.tenant_id=:tid AND p.code=:permission ORDER BY ur.user_id LIMIT 1"""),
                {"tid": ctx.tenant_id, "permission": fallback.get("code", "attendance.manage")},
            )
        elif fallback.get("kind") == "role":
            assignee_id = await db.scalar(
                text("""SELECT ur.user_id FROM user_roles ur JOIN tenant_memberships m
                ON m.tenant_id=ur.tenant_id AND m.user_id=ur.user_id AND m.status='active'
                JOIN roles r ON r.id=ur.role_id WHERE ur.tenant_id=:tid AND r.name=:name ORDER BY ur.user_id LIMIT 1"""),
                {"tid": ctx.tenant_id, "name": fallback.get("name")},
            )
    execution = await db.scalar(
        text("""INSERT INTO workflow_executions(tenant_id,workflow_version_id,entity_type,entity_id,
        status,current_step,context_json,started_by) VALUES (:tid,:version,'attendance_correction',:id,'running',:step,
        CAST(:context AS jsonb),:actor) RETURNING id"""),
        {
            "tid": ctx.tenant_id,
            "version": version["id"],
            "id": correction["id"],
            "step": step_key,
            "context": json.dumps({"work_date": str(correction["work_date"])}),
            "actor": ctx.user_id,
        },
    )
    due_hours = step.get("due_in_hours")
    await db.execute(
        text("""INSERT INTO workflow_tasks(tenant_id,execution_id,step_key,assignee_user_id,task_type,due_at)
        VALUES (:tid,:execution,:step,:assignee,:type,CASE WHEN :hours IS NULL THEN NULL ELSE now()+(:hours * interval '1 hour') END)"""),
        {
            "tid": ctx.tenant_id,
            "execution": execution,
            "step": step_key,
            "assignee": assignee_id,
            "type": step.get("type", "approval"),
            "hours": due_hours,
        },
    )


async def decide_correction(
    db: AsyncSession, ctx: RequestContext, correction_id: uuid.UUID, approve: bool, comment: str | None
) -> dict:
    async with db.begin():
        correction = (
            (
                await db.execute(
                    text("SELECT * FROM attendance_corrections WHERE tenant_id=:tid AND id=:id FOR UPDATE"),
                    {"tid": ctx.tenant_id, "id": correction_id},
                )
            )
            .mappings()
            .first()
        )
        if correction is None:
            raise NotFound("Attendance correction not found")
        if correction["status"] != "pending":
            raise Conflict("Attendance correction is no longer pending", code="attendance.correction_decided")
        assigned = await db.scalar(
            text("""SELECT EXISTS(SELECT 1 FROM workflow_tasks t JOIN workflow_executions x
            ON x.tenant_id=t.tenant_id AND x.id=t.execution_id WHERE t.tenant_id=:tid AND x.entity_type='attendance_correction'
            AND x.entity_id=:id AND t.assignee_user_id=:uid AND t.status='pending') OR EXISTS(
            SELECT 1 FROM employees e JOIN employees manager ON manager.tenant_id=e.tenant_id AND manager.id=e.manager_id
            WHERE e.tenant_id=:tid AND e.id=:employee AND manager.user_id=:uid AND e.deleted_at IS NULL)"""),
            {
                "tid": ctx.tenant_id,
                "id": correction_id,
                "employee": correction["employee_id"],
                "uid": ctx.user_id,
            },
        )
        if not ctx.can("attendance.manage") and not assigned:
            raise Forbidden(details={"permission": "attendance.manage", "reason": "not_assigned_approver"})
        now = datetime.now(UTC)
        updated = (
            (
                await db.execute(
                    text("""UPDATE attendance_corrections SET status=:status,decided_by=:actor,
            decided_at=:now,updated_at=:now WHERE tenant_id=:tid AND id=:id RETURNING *"""),
                    {
                        "status": "approved" if approve else "rejected",
                        "actor": ctx.user_id,
                        "now": now,
                        "tid": ctx.tenant_id,
                        "id": correction_id,
                    },
                )
            )
            .mappings()
            .one()
        )
        if approve:
            checkin, checkout = correction["requested_check_in"], correction["requested_check_out"]
            minutes = int((checkout - checkin).total_seconds() // 60) if checkin and checkout else None
            threshold = await db.scalar(
                text("""SELECT COALESCE((s.rules_json->>'half_day_minutes')::int,240)
                FROM employees e LEFT JOIN work_schedules s ON s.tenant_id=e.tenant_id
                AND (s.id=e.work_schedule_id OR (e.work_schedule_id IS NULL AND s.is_default))
                WHERE e.tenant_id=:tid AND e.id=:eid"""),
                {"tid": ctx.tenant_id, "eid": correction["employee_id"]},
            )
            await db.execute(
                text("""INSERT INTO attendance_records(tenant_id,employee_id,work_date,check_in,check_out,
                worked_minutes,status,source) VALUES (:tid,:eid,:day,:checkin,:checkout,:minutes,:status,'correction')
                ON CONFLICT (tenant_id,employee_id,work_date) DO UPDATE SET check_in=EXCLUDED.check_in,
                check_out=EXCLUDED.check_out,worked_minutes=EXCLUDED.worked_minutes,status=EXCLUDED.status,
                source='correction',updated_at=now()"""),
                {
                    "tid": ctx.tenant_id,
                    "eid": correction["employee_id"],
                    "day": correction["work_date"],
                    "checkin": checkin,
                    "checkout": checkout,
                    "minutes": minutes,
                    "status": "present" if minutes is None or minutes >= (threshold or 240) else "half_day",
                },
            )
        task = (
            (
                await db.execute(
                    text("""SELECT t.id,t.execution_id FROM workflow_tasks t JOIN workflow_executions x
            ON x.tenant_id=t.tenant_id AND x.id=t.execution_id WHERE t.tenant_id=:tid AND x.entity_type='attendance_correction'
            AND x.entity_id=:id AND t.status='pending' ORDER BY t.created_at DESC LIMIT 1 FOR UPDATE OF t"""),
                    {"tid": ctx.tenant_id, "id": correction_id},
                )
            )
            .mappings()
            .first()
        )
        if task:
            await db.execute(
                text("""UPDATE workflow_tasks SET status=:status,comment=:comment,completed_by=:actor,
                completed_at=:now,updated_at=:now WHERE tenant_id=:tid AND id=:id"""),
                {
                    "status": "approved" if approve else "rejected",
                    "comment": comment,
                    "actor": ctx.user_id,
                    "now": now,
                    "tid": ctx.tenant_id,
                    "id": task["id"],
                },
            )
            await db.execute(
                text(
                    "UPDATE workflow_executions SET status=:status,completed_at=:now,updated_at=:now WHERE tenant_id=:tid AND id=:id"
                ),
                {
                    "status": "completed" if approve else "rejected",
                    "now": now,
                    "tid": ctx.tenant_id,
                    "id": task["execution_id"],
                },
            )
        await _audit(
            db,
            ctx,
            "attendance.correction_approved" if approve else "attendance.correction_rejected",
            "attendance_correction",
            correction_id,
        )
        return dict(updated)


async def mark_absent_for_tenant(db: AsyncSession, tenant_id: uuid.UUID, work_date: date) -> int:
    result = await db.execute(
        text("""INSERT INTO attendance_records(tenant_id,employee_id,work_date,status,source)
        SELECT e.tenant_id,e.id,:day,'absent','system' FROM employees e
        LEFT JOIN tenants t ON t.id=e.tenant_id
        LEFT JOIN work_schedules s ON s.tenant_id=e.tenant_id
          AND (s.id=e.work_schedule_id OR (e.work_schedule_id IS NULL AND s.is_default))
        WHERE e.tenant_id=:tid AND e.status='active' AND e.deleted_at IS NULL AND e.joining_date<=:day
        AND (now() AT TIME ZONE COALESCE(s.timezone,t.timezone,'UTC'))::date - 1 = :day
        AND EXISTS(SELECT 1 FROM jsonb_array_elements_text(COALESCE(s.rules_json->'working_days','[1,2,3,4,5]'::jsonb)) d
          WHERE d.value=extract(isodow from :day::date)::int::text)
        AND NOT EXISTS(SELECT 1 FROM holidays h WHERE h.tenant_id=e.tenant_id AND h.holiday_date=:day
          AND h.is_optional=false AND (h.location_id IS NULL OR h.location_id=e.location_id))
        AND NOT EXISTS(SELECT 1 FROM leave_requests l WHERE l.tenant_id=e.tenant_id AND l.employee_id=e.id
          AND l.status='approved' AND l.start_date<=:day AND l.end_date>=:day)
        ON CONFLICT (tenant_id,employee_id,work_date) DO NOTHING"""),
        {"tid": tenant_id, "day": work_date},
    )
    return result.rowcount or 0
