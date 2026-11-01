"""Business logic for the leave domain.

Services own authorization (ctx.require), data scope, business rules, transactions and audit.
Routers, the AI layer and workers all call these functions.
"""

import json
import uuid
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import Conflict, Forbidden, NotFound, ValidationFailed


async def _notify(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None,
    event_type: str,
    payload: dict,
) -> None:
    if user_id is None:
        return
    await db.execute(
        text("""INSERT INTO notifications
                (tenant_id, user_id, channel, type, template, payload_json, dedupe_key)
                VALUES (:tid, :uid, 'in_app', :type, :template, CAST(:payload AS jsonb), :dedupe)"""),
        {
            "tid": tenant_id,
            "uid": user_id,
            "type": event_type,
            "template": event_type,
            "payload": json.dumps(payload, default=str),
            "dedupe": f"{event_type}:{uuid.uuid4()}",
        },
    )


def _working_days(start: date, end: date, holidays: set[date]) -> int:
    return sum(
        1
        for ordinal in range((end - start).days + 1)
        if (current := date.fromordinal(start.toordinal() + ordinal)).weekday() < 5
        and current not in holidays
    )


async def list_leave_types(db: AsyncSession, ctx: RequestContext) -> list[dict]:
    ctx.require("leave.read")
    result = await db.execute(
        text("SELECT * FROM leave_types WHERE tenant_id=:tid AND is_active ORDER BY name"),
        {"tid": ctx.tenant_id},
    )
    return [dict(row) for row in result.mappings().all()]


async def update_leave_type(
    db: AsyncSession, ctx: RequestContext, leave_type_id: uuid.UUID, values: dict
) -> dict:
    ctx.require("tenant.update")
    if not values:
        raise ValidationFailed("At least one leave policy field is required", code="leave.policy_empty")
    rules = {
        "annual_allocation": values.pop("annual_allocation", None),
        "min_notice_days": values.pop("min_notice_days", None),
        "max_consecutive_days": values.pop("max_consecutive_days", None),
        "carry_forward_max": values.pop("carry_forward_max", None),
        "allow_negative": values.pop("allow_negative", None),
    }
    rules = {key: value for key, value in rules.items() if value is not None}
    assignments = []
    params = {"tid": ctx.tenant_id, "lid": leave_type_id}
    for key in ("name", "is_paid", "is_active"):
        if key in values:
            assignments.append(f"{key}=:{key}")
            params[key] = values[key]
    if rules:
        assignments.append("rules_json=rules_json || CAST(:rules AS jsonb)")
        params["rules"] = json.dumps(rules)
    assignments.append("updated_at=now()")
    row = (
        await db.execute(
            text(f"""UPDATE leave_types SET {", ".join(assignments)}
                     WHERE tenant_id=:tid AND id=:lid RETURNING *"""),  # noqa: S608
            params,
        )
    ).mappings().first()
    if row is None:
        raise NotFound("Leave type not found", code="leave.type_not_found")
    return dict(row)


async def _employee_for_user(db: AsyncSession, ctx: RequestContext) -> dict:
    row = (
        (
            await db.execute(
                text("SELECT * FROM employees WHERE tenant_id=:tid AND user_id=:uid AND deleted_at IS NULL"),
                {"tid": ctx.tenant_id, "uid": ctx.user_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Employee profile not found", code="leave.employee_not_found")
    return dict(row)


async def _is_owner(db: AsyncSession, ctx: RequestContext) -> bool:
    return bool(
        (
            await db.execute(
                text("""SELECT 1 FROM tenant_memberships
                        WHERE tenant_id=:tid AND user_id=:uid
                          AND status='active' AND is_owner"""),
                {"tid": ctx.tenant_id, "uid": ctx.user_id},
            )
        ).scalar_one_or_none()
    )


async def list_approvers(db: AsyncSession, ctx: RequestContext) -> list[dict]:
    ctx.require("leave.request")
    employee = (
        (
            await db.execute(
                text(
                    "SELECT id FROM employees "
                    "WHERE tenant_id=:tid AND user_id=:uid AND deleted_at IS NULL"
                ),
                {"tid": ctx.tenant_id, "uid": ctx.user_id},
            )
        )
        .mappings()
        .first()
    )
    if employee is None:
        return []
    result = await db.execute(
        text("""WITH eligible AS (
                    SELECT manager.user_id, 'Department Manager' AS role_name, 1 AS priority
                    FROM employees subject
                    JOIN departments d ON d.tenant_id=subject.tenant_id
                      AND d.id=subject.department_id AND d.deleted_at IS NULL
                    JOIN employees manager ON manager.tenant_id=d.tenant_id
                      AND manager.id=d.manager_id AND manager.deleted_at IS NULL
                    WHERE subject.tenant_id=:tid AND subject.id=:eid
                    UNION
                    SELECT ur.user_id, role.name, 2
                    FROM user_roles ur
                    JOIN roles role ON role.id=ur.role_id
                    JOIN role_permissions rp ON rp.role_id=role.id
                    JOIN permissions permission ON permission.id=rp.permission_id
                    WHERE ur.tenant_id=:tid
                      AND permission.code='leave.approve'
                      AND role.name IN ('HR Admin', 'HR Manager')
                    UNION
                    SELECT membership.user_id, 'Company Owner', 3
                    FROM tenant_memberships membership
                    WHERE membership.tenant_id=:tid AND membership.status='active'
                      AND membership.is_owner
                )
                SELECT DISTINCT ON (eligible.user_id)
                       eligible.user_id AS id, u.full_name, u.email, eligible.role_name
                FROM eligible
                JOIN users u ON u.id=eligible.user_id
                JOIN tenant_memberships membership
                  ON membership.tenant_id=:tid AND membership.user_id=eligible.user_id
                 AND membership.status='active'
                WHERE eligible.user_id <> :uid
                ORDER BY eligible.user_id, eligible.priority"""),
        {"tid": ctx.tenant_id, "eid": employee["id"], "uid": ctx.user_id},
    )
    return [dict(row) for row in result.mappings().all()]


async def _check_employee(db: AsyncSession, ctx: RequestContext, employee_id: uuid.UUID) -> dict:
    row = (
        (
            await db.execute(
                text("SELECT * FROM employees WHERE tenant_id=:tid AND id=:eid AND deleted_at IS NULL"),
                {"tid": ctx.tenant_id, "eid": employee_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Employee not found", code="leave.employee_not_found")
    return dict(row)


async def list_balances(
    db: AsyncSession, ctx: RequestContext, employee_id: uuid.UUID | None, year: int | None
) -> list[dict]:
    ctx.require("leave.read")
    employee = None if employee_id is None and await _is_owner(db, ctx) else (
        await _employee_for_user(db, ctx)
        if employee_id is None
        else await _check_employee(db, ctx, employee_id)
    )
    target_year = year or datetime.now().year
    result = await db.execute(
        text("""SELECT b.employee_id, CONCAT(e.first_name, ' ', e.last_name) AS employee_name,
                       lt.id AS leave_type_id, lt.name, lt.code, lt.unit, lt.is_paid,
                       COALESCE(b.allocated, 0) AS allocated, COALESCE(b.used, 0) AS used,
                       COALESCE(b.pending, 0) AS pending,
                       COALESCE(b.allocated, 0) - COALESCE(b.used, 0) - COALESCE(b.pending, 0) AS available
                FROM leave_types lt
                JOIN employees e ON e.tenant_id=lt.tenant_id AND e.deleted_at IS NULL
                LEFT JOIN leave_balances b ON b.tenant_id=lt.tenant_id AND b.leave_type_id=lt.id
                  AND b.employee_id=e.id AND b.year=:year
                WHERE lt.tenant_id=:tid AND lt.is_active
                  AND (CAST(:eid AS uuid) IS NULL OR e.id=CAST(:eid AS uuid))
                ORDER BY e.last_name, e.first_name, lt.name"""),
        {"tid": ctx.tenant_id, "eid": employee["id"] if employee else employee_id, "year": target_year},
    )
    return [dict(row) for row in result.mappings().all()]


async def list_requests(
    db: AsyncSession, ctx: RequestContext, employee_id: uuid.UUID | None, status: str | None, limit: int
) -> list[dict]:
    ctx.require("leave.read")
    params = {"tid": ctx.tenant_id, "limit": limit}
    clauses = ["lr.tenant_id=:tid"]
    if ctx.can("leave.approve") and await _is_owner(db, ctx):
        clauses.append("1=1")
    elif ctx.can("leave.approve"):
        clauses.append("(e.user_id=:uid OR lr.current_approver_id=:uid)")
        params["uid"] = ctx.user_id
    else:
        clauses.append("e.user_id=:uid")
        params["uid"] = ctx.user_id
    if employee_id:
        clauses.append("lr.employee_id=:eid")
        params["eid"] = employee_id
    if status:
        clauses.append("lr.status=:status")
        params["status"] = status
    result = await db.execute(
        text(f"""SELECT lr.*, lt.name AS leave_type_name, lt.code AS leave_type_code,
                        e.employee_no, e.first_name, e.last_name
                 FROM leave_requests lr
                 JOIN leave_types lt ON lt.tenant_id=lr.tenant_id AND lt.id=lr.leave_type_id
                 JOIN employees e ON e.tenant_id=lr.tenant_id AND e.id=lr.employee_id
                 WHERE {" AND ".join(clauses)}
                 ORDER BY lr.start_date DESC, lr.created_at DESC LIMIT :limit"""),  # noqa: S608
        params,
    )
    return [dict(row) for row in result.mappings().all()]


async def create_request(db: AsyncSession, ctx: RequestContext, values: dict) -> dict:
    ctx.require("leave.request")
    async with db.begin():
        actor_employee = await _employee_for_user(db, ctx)
        employee = (
            actor_employee
            if values.get("employee_id") is None
            else await _check_employee(db, ctx, values["employee_id"])
        )
        if employee["id"] != actor_employee["id"] and not ctx.can("leave.approve"):
            raise Forbidden(details={"permission": "leave.approve"})
        leave_type = (
            (
                await db.execute(
                    text("SELECT * FROM leave_types WHERE tenant_id=:tid AND id=:lid AND is_active"),
                    {"tid": ctx.tenant_id, "lid": values["leave_type_id"]},
                )
            )
            .mappings()
            .first()
        )
        if leave_type is None:
            raise NotFound("Leave type not found", code="leave.type_not_found")
        holidays = set(
            (
                await db.execute(
                    text("""SELECT holiday_date FROM holidays
                            WHERE tenant_id=:tid AND holiday_date BETWEEN :start_date AND :end_date
                              AND is_optional=false"""),
                    {
                        "tid": ctx.tenant_id,
                        "start_date": values["start_date"],
                        "end_date": values["end_date"],
                    },
                )
            )
            .scalars()
            .all()
        )
        days = _working_days(values["start_date"], values["end_date"], holidays)
        if days <= 0:
            raise ValidationFailed(
                "Leave request must include at least one working day", code="leave.no_working_days"
            )
        await db.execute(
            text("""INSERT INTO leave_balances
                    (tenant_id, employee_id, leave_type_id, year, allocated)
                    SELECT :tid, :eid, id, :year,
                           COALESCE((rules_json->>'annual_allocation')::numeric, 0)
                    FROM leave_types
                    WHERE tenant_id=:tid AND id=:lid
                    ON CONFLICT (tenant_id, employee_id, leave_type_id, year) DO NOTHING"""),
            {
                "tid": ctx.tenant_id,
                "eid": employee["id"],
                "lid": leave_type["id"],
                "year": values["start_date"].year,
            },
        )
        balance = (
            (
                await db.execute(
                    text("""SELECT * FROM leave_balances WHERE tenant_id=:tid AND employee_id=:eid
                        AND leave_type_id=:lid AND year=:year FOR UPDATE"""),
                    {
                        "tid": ctx.tenant_id,
                        "eid": employee["id"],
                        "lid": leave_type["id"],
                        "year": values["start_date"].year,
                    },
                )
            )
            .mappings()
            .first()
        )
        allow_negative = bool((leave_type["rules_json"] or {}).get("allow_negative", False))
        if balance is None:
            raise ValidationFailed(
                "No leave balance exists for this employee and year", code="leave.balance_not_found"
            )
        if not allow_negative and balance["allocated"] - balance["used"] - balance["pending"] < days:
            raise ValidationFailed("Insufficient leave balance", code="leave.insufficient_balance")
        await db.execute(
            text("UPDATE leave_balances SET pending=pending+:days, updated_at=now() WHERE id=:id"),
            {"days": days, "id": balance["id"]},
        )
        approver = values.get("approver_user_id")
        eligible_approver = None
        if approver is not None:
            eligible_approver = await db.scalar(
                text("""SELECT 1
                        FROM (
                            SELECT manager.user_id
                            FROM employees subject
                            JOIN departments d ON d.tenant_id=subject.tenant_id
                              AND d.id=subject.department_id AND d.deleted_at IS NULL
                            JOIN employees manager ON manager.tenant_id=d.tenant_id
                              AND manager.id=d.manager_id AND manager.deleted_at IS NULL
                            WHERE subject.tenant_id=:tid AND subject.id=:eid
                            UNION
                            SELECT ur.user_id
                            FROM user_roles ur
                            JOIN roles role ON role.id=ur.role_id
                            JOIN role_permissions rp ON rp.role_id=role.id
                            JOIN permissions permission ON permission.id=rp.permission_id
                            WHERE ur.tenant_id=:tid
                              AND permission.code='leave.approve'
                              AND role.name IN ('HR Admin', 'HR Manager')
                            UNION
                            SELECT membership.user_id
                            FROM tenant_memberships membership
                            WHERE membership.tenant_id=:tid AND membership.status='active'
                              AND membership.is_owner
                        ) eligible
                        JOIN tenant_memberships membership
                          ON membership.tenant_id=:tid AND membership.user_id=eligible.user_id
                         AND membership.status='active'
                        WHERE eligible.user_id=:approver AND eligible.user_id <> :uid"""),
                {"tid": ctx.tenant_id, "eid": employee["id"], "approver": approver, "uid": ctx.user_id},
            )
            if eligible_approver is None:
                raise ValidationFailed(
                    "Selected leave approver is not an eligible manager, HR user, or company owner",
                    code="leave.invalid_approver",
                )
        if approver is None:
            approver = (
            await db.execute(
                text("""SELECT manager.user_id FROM employees subject
                    JOIN departments d
                      ON d.tenant_id=subject.tenant_id AND d.id=subject.department_id
                     AND d.deleted_at IS NULL
                    JOIN employees manager
                      ON manager.tenant_id=d.tenant_id AND manager.id=d.manager_id
                     AND manager.deleted_at IS NULL
                    JOIN tenant_memberships membership
                      ON membership.tenant_id=d.tenant_id
                     AND membership.user_id=manager.user_id
                     AND membership.status='active'
                    WHERE subject.tenant_id=:tid AND subject.id=:eid
                      AND manager.user_id IS NOT NULL"""),
                {"tid": ctx.tenant_id, "eid": employee["id"]},
            )
        ).scalar_one_or_none()
        if approver is None:
            approver = (
                await db.execute(
                    text("""SELECT hr.user_id
                            FROM employees subject
                            JOIN employees hr
                              ON hr.tenant_id=subject.tenant_id
                             AND hr.department_id=subject.department_id
                             AND hr.deleted_at IS NULL
                            JOIN tenant_memberships membership
                              ON membership.tenant_id=hr.tenant_id
                             AND membership.user_id=hr.user_id
                             AND membership.status='active'
                            JOIN user_roles ur
                              ON ur.tenant_id=hr.tenant_id AND ur.user_id=hr.user_id
                            JOIN roles role ON role.id=ur.role_id
                            JOIN role_permissions rp ON rp.role_id=role.id
                            JOIN permissions permission ON permission.id=rp.permission_id
                            WHERE subject.tenant_id=:tid AND subject.id=:eid
                              AND hr.user_id IS NOT NULL
                              AND permission.code='leave.approve'
                              AND role.data_scope IN ('department','all')
                            ORDER BY CASE WHEN role.name='HR Manager' THEN 0 ELSE 1 END
                            LIMIT 1"""),
                    {"tid": ctx.tenant_id, "eid": employee["id"]},
                )
            ).scalar_one_or_none()
        if approver is None:
            approver = (
                await db.execute(
                    text("""SELECT membership.user_id
                            FROM tenant_memberships membership
                            JOIN user_roles ur
                              ON ur.tenant_id=membership.tenant_id
                             AND ur.user_id=membership.user_id
                            JOIN roles role ON role.id=ur.role_id
                            JOIN role_permissions rp ON rp.role_id=role.id
                            JOIN permissions permission ON permission.id=rp.permission_id
                            WHERE membership.tenant_id=:tid
                              AND membership.status='active'
                              AND membership.is_owner
                              AND permission.code='leave.approve'
                            LIMIT 1"""),
                    {"tid": ctx.tenant_id},
                )
            ).scalar_one_or_none()
        if approver is None:
            raise ValidationFailed(
                "No leave approver is configured for this workspace",
                code="leave.approver_not_configured",
            )
        row = (
            (
                await db.execute(
                    text("""INSERT INTO leave_requests
                        (tenant_id, employee_id, leave_type_id, start_date, end_date, days,
                         reason, current_approver_id)
                        VALUES (:tid,:eid,:lid,:start_date,:end_date,:days,:reason,:approver)
                        RETURNING *"""),
                    {
                        **values,
                        "tid": ctx.tenant_id,
                        "eid": employee["id"],
                        "lid": leave_type["id"],
                        "days": days,
                        "approver": approver,
                    },
                )
            )
            .mappings()
            .one()
        )
        await _notify(
            db,
            ctx.tenant_id,
            approver,
            "leave.submitted",
            {
                "leave_type": leave_type["name"],
                "message": f"{days} day(s) requested from {values['start_date']} to {values['end_date']}.",
            },
        )
    return dict(row)


async def _get_request(
    db: AsyncSession, ctx: RequestContext, request_id: uuid.UUID, lock: bool = False
) -> dict:
    query = (
        "SELECT * FROM leave_requests WHERE tenant_id=:tid AND id=:rid FOR UPDATE"
        if lock
        else "SELECT * FROM leave_requests WHERE tenant_id=:tid AND id=:rid"
    )
    row = (
        (
            await db.execute(
                text(query),
                {"tid": ctx.tenant_id, "rid": request_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Leave request not found", code="leave.request_not_found")
    return dict(row)


async def decide_request(
    db: AsyncSession, ctx: RequestContext, request_id: uuid.UUID, decision: str, note: str | None
) -> dict:
    ctx.require("leave.approve")
    async with db.begin():
        request = await _get_request(db, ctx, request_id, lock=True)
        if request["status"] != "pending":
            raise Conflict("Only pending leave requests can be decided", code="leave.request_not_pending")
        if request["current_approver_id"] and request["current_approver_id"] != ctx.user_id:
            raise Forbidden(details={"permission": "leave.approve"})
        row = (
            (
                await db.execute(
                    text("""UPDATE leave_requests
                        SET status=:status, decided_by=:uid, decided_at=now(),
                            decision_note=:note, current_approver_id=NULL, updated_at=now()
                        WHERE tenant_id=:tid AND id=:rid RETURNING *"""),
                    {
                        "status": decision,
                        "uid": ctx.user_id,
                        "note": note,
                        "tid": ctx.tenant_id,
                        "rid": request_id,
                    },
                )
            )
            .mappings()
            .one()
        )
        await db.execute(
            text("""UPDATE leave_balances SET pending=pending-:days,
                    used=used+:used, updated_at=now()
                    WHERE tenant_id=:tid AND employee_id=:eid AND leave_type_id=:lid AND year=:year"""),
            {
                "days": request["days"],
                "used": request["days"] if decision == "approved" else 0,
                "tid": ctx.tenant_id,
                "eid": request["employee_id"],
                "lid": request["leave_type_id"],
                "year": request["start_date"].year,
            },
        )
        employee_user_id = (
            await db.execute(
                text("SELECT user_id FROM employees WHERE tenant_id=:tid AND id=:eid"),
                {"tid": ctx.tenant_id, "eid": request["employee_id"]},
            )
        ).scalar_one_or_none()
        await _notify(
            db,
            ctx.tenant_id,
            employee_user_id,
            f"leave.{decision}",
            {
                "leave_type": str(request["leave_type_id"]),
                "message": f"Your leave request was {decision}.",
            },
        )
    return dict(row)


async def cancel_request(
    db: AsyncSession, ctx: RequestContext, request_id: uuid.UUID, note: str | None
) -> dict:
    ctx.require("leave.request")
    async with db.begin():
        request = await _get_request(db, ctx, request_id, lock=True)
        employee = await _employee_for_user(db, ctx)
        if request["employee_id"] != employee["id"] and not ctx.can("leave.approve"):
            raise Forbidden(details={"permission": "leave.approve"})
        if request["status"] not in {"pending", "approved"}:
            raise Conflict(
                "Only pending or approved leave requests can be cancelled",
                code="leave.request_not_cancellable",
            )
        if request["status"] == "approved" and request["start_date"] <= date.today():
            raise ValidationFailed("Started leave cannot be cancelled", code="leave.started")
        row = (
            (
                await db.execute(
                    text("""UPDATE leave_requests SET status='cancelled', decided_by=:uid, decided_at=now(),
                        decision_note=:note, current_approver_id=NULL, updated_at=now()
                        WHERE tenant_id=:tid AND id=:rid RETURNING *"""),
                    {"uid": ctx.user_id, "note": note, "tid": ctx.tenant_id, "rid": request_id},
                )
            )
            .mappings()
            .one()
        )
        await db.execute(
            text("""UPDATE leave_balances SET pending=pending-:pending, used=used-:used, updated_at=now()
                    WHERE tenant_id=:tid AND employee_id=:eid AND leave_type_id=:lid AND year=:year"""),
            {
                "pending": request["days"] if request["status"] == "pending" else 0,
                "used": request["days"] if request["status"] == "approved" else 0,
                "tid": ctx.tenant_id,
                "eid": request["employee_id"],
                "lid": request["leave_type_id"],
                "year": request["start_date"].year,
            },
        )
    return dict(row)
