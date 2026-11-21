"""Employee lifecycle, permission scopes and audit."""

import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.errors import Conflict, FeatureDisabled, Forbidden, NotFound, PlanLimitReached, ValidationFailed
from app.core.permissions import DataScope, resolve_scope, scope_filter
from app.core.security import generate_opaque_token, hash_password, hash_token
from app.domains.notifications import service as notification_service

_FIELDS = {
    "first_name",
    "last_name",
    "email",
    "phone",
    "department_id",
    "team_id",
    "manager_id",
    "location_id",
    "work_schedule_id",
    "designation",
    "employment_type",
    "status",
    "joining_date",
    "exit_date",
}


def _require_feature(name: str, enabled: bool) -> None:
    if not enabled:
        raise FeatureDisabled(details={"feature": name})


async def _audit(
    db: AsyncSession,
    ctx: RequestContext,
    action: str,
    employee_id: uuid.UUID,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    await db.execute(
        text("""INSERT INTO audit_logs(tenant_id,actor_user_id,action,entity_type,entity_id,source,before_json,after_json,request_id)
      VALUES (:tid,:uid,:action,'employee',:eid,:source,CAST(:before AS jsonb),CAST(:after AS jsonb),:rid)"""),
        {
            "tid": ctx.tenant_id,
            "uid": ctx.user_id,
            "action": action,
            "eid": employee_id,
            "source": ctx.source,
            "before": json.dumps(before, default=str) if before else None,
            "after": json.dumps(after, default=str) if after else None,
            "rid": ctx.request_id,
        },
    )


async def _validate_employee_refs(db: AsyncSession, tenant_id: uuid.UUID, values: dict) -> None:
    references = (
        ("department_id", "departments", "deleted_at IS NULL"),
        ("manager_id", "employees", "deleted_at IS NULL"),
        ("location_id", "locations", "is_active"),
        ("work_schedule_id", "work_schedules", "TRUE"),
    )
    for key, table, active in references:
        value = values.get(key)
        if value is None:
            continue
        exists = await db.scalar(
            text(f"SELECT EXISTS(SELECT 1 FROM {table} WHERE tenant_id=:tid AND id=:id AND {active})"),
            {"tid": tenant_id, "id": value},
        )
        if not exists:
            raise NotFound(
                f"Referenced {key.removesuffix('_id').replace('_', ' ')} not found",
                code="employee.reference_not_found",
            )
    if values.get("team_id") is not None:
        if not get_settings().feature_teams:
            raise ValidationFailed("Teams are disabled", code="feature.teams_disabled")
        valid = await db.scalar(text(
            """SELECT EXISTS(SELECT 1 FROM teams
               WHERE tenant_id=:tid AND id=:team_id AND department_id=:department_id
                 AND deleted_at IS NULL AND status='active')"""
        ), {"tid": tenant_id, "team_id": values["team_id"], "department_id": values.get("department_id")})
        if not valid:
            raise ValidationFailed(
                "Team must belong to the selected department and be active",
                code="employee.team_department_mismatch",
            )


async def _start_onboarding_workflow(db: AsyncSession, ctx: RequestContext, employee: dict) -> None:
    version = (
        (
            await db.execute(
                text("""SELECT v.id,v.definition_json FROM workflow_definitions d
              JOIN workflow_versions v ON v.tenant_id=d.tenant_id AND v.workflow_definition_id=d.id
              WHERE d.tenant_id=:tid AND d.entity_type='employee_onboarding' AND d.status='active'
                AND v.published_at IS NOT NULL ORDER BY v.version DESC LIMIT 1"""),
                {"tid": ctx.tenant_id},
            )
        )
        .mappings()
        .first()
    )
    if version is None:
        return
    definition = version["definition_json"]
    start = definition.get("start")
    steps = definition.get("steps", {})
    terminal = start in {"END_APPROVED", "END_REJECTED", "END_COMPLETED"}
    step = steps.get(start) if isinstance(steps, dict) else None
    if not terminal and not isinstance(step, dict):
        raise ValidationFailed(
            "Active onboarding workflow has an invalid start step", code="workflow.invalid_definition"
        )
    step = step or {}
    execution = await db.scalar(
        text("""INSERT INTO workflow_executions(tenant_id,workflow_version_id,entity_type,entity_id,status,
          current_step,context_json,started_by,completed_at)
          VALUES (:tid,:version,'employee_onboarding',:employee,:status,:step,CAST(:context AS jsonb),:actor,:completed)
          RETURNING id"""),
        {
            "tid": ctx.tenant_id,
            "version": version["id"],
            "employee": employee["id"],
            "status": "completed" if terminal else "running",
            "step": None if terminal else start,
            "context": json.dumps({"employee_id": str(employee["id"]), "email": str(employee["email"])}),
            "actor": ctx.user_id,
            "completed": datetime.now(UTC) if terminal else None,
        },
    )
    step_type = step.get("type")
    if terminal or step_type not in {"approval", "action", "notification"}:
        return
    assignee = None
    assignee_spec = step.get("assignee") or {}
    if assignee_spec.get("kind") == "manager_of_subject" and employee.get("manager_id"):
        assignee = await db.scalar(
            text("""SELECT e.user_id FROM employees e JOIN tenant_memberships m
              ON m.tenant_id=e.tenant_id AND m.user_id=e.user_id AND m.status='active'
              WHERE e.tenant_id=:tid AND e.id=:manager AND e.user_id IS NOT NULL"""),
            {"tid": ctx.tenant_id, "manager": employee["manager_id"]},
        )
    if assignee is None:
        fallback = assignee_spec.get("fallback") or {}
        if fallback.get("kind") == "permission":
            assignee = await db.scalar(
                text("""SELECT ur.user_id FROM user_roles ur JOIN tenant_memberships m
                  ON m.tenant_id=ur.tenant_id AND m.user_id=ur.user_id AND m.status='active'
                  JOIN roles r ON r.id=ur.role_id JOIN role_permissions rp ON rp.role_id=r.id
                  JOIN permissions p ON p.id=rp.permission_id
                  WHERE ur.tenant_id=:tid AND p.code=:permission AND r.data_scope='all'
                  ORDER BY ur.user_id LIMIT 1"""),
                {"tid": ctx.tenant_id, "permission": fallback.get("code", "")},
            )
    hours = step.get("due_in_hours", 72)
    due_at = datetime.now(UTC) + timedelta(hours=max(1, int(hours)))
    await db.execute(
        text("""INSERT INTO workflow_tasks(tenant_id,execution_id,step_key,assignee_user_id,task_type,due_at)
          VALUES (:tid,:execution,:step,:assignee,:type,:due)"""),
        {
            "tid": ctx.tenant_id,
            "execution": execution,
            "step": start,
            "assignee": assignee,
            "type": step_type,
            "due": due_at,
        },
    )


async def list_employees(db: AsyncSession, ctx: RequestContext, limit: int, cursor: uuid.UUID | None) -> dict:
    ctx.require("employees.read")
    async with db.begin():
        scope = await resolve_scope(db, ctx, "employees.read") or DataScope.SELF
        rows = (
            (
                await db.execute(
                    text(f"""SELECT e.id,e.employee_no,e.first_name,e.last_name,e.email,e.phone,e.department_id,
          e.manager_id,e.team_id,e.location_id,e.work_schedule_id,e.designation,e.employment_type,e.status,e.joining_date,e.exit_date,e.user_id,
          COALESCE(tm.is_owner, FALSE) AS is_owner,
          CASE WHEN e.user_id IS NULL THEN 'not_invited'
               WHEN tm.status = 'invited' THEN 'invited'
               WHEN tm.status = 'active' THEN 'active'
               ELSE 'not_invited' END AS invitation_status,
          d.name AS department_name, d.manager_id AS department_manager_id,
          manager.first_name AS manager_first_name, manager.last_name AS manager_last_name
          FROM employees e
          LEFT JOIN departments d ON d.tenant_id=e.tenant_id AND d.id=e.department_id AND d.deleted_at IS NULL
          LEFT JOIN employees manager ON manager.tenant_id=e.tenant_id AND manager.id=e.manager_id AND manager.deleted_at IS NULL
          LEFT JOIN tenant_memberships tm ON tm.tenant_id=e.tenant_id AND tm.user_id=e.user_id
          WHERE e.tenant_id=:tid AND e.deleted_at IS NULL AND ({scope_filter(scope)})
          AND (CAST(:cursor AS uuid) IS NULL OR e.id>CAST(:cursor AS uuid)) ORDER BY e.id LIMIT :limit"""),
                    {"tid": ctx.tenant_id, "uid": ctx.user_id, "cursor": cursor, "limit": limit + 1},
                )
            )
            .mappings()
            .all()
        )
    next_cursor = str(rows[limit - 1]["id"]) if len(rows) > limit else None
    return {"items": [dict(r) for r in rows[:limit]], "next_cursor": next_cursor}


async def get_employee(db: AsyncSession, ctx: RequestContext, employee_id: uuid.UUID) -> dict:
    ctx.require("employees.read")
    async with db.begin():
        scope = await resolve_scope(db, ctx, "employees.read") or DataScope.SELF
        row = (
            (
                await db.execute(
                    text(f"""SELECT e.id,e.employee_no,e.first_name,e.last_name,e.email,e.phone,e.department_id,
          e.manager_id,e.team_id,e.location_id,e.work_schedule_id,e.designation,e.employment_type,e.status,e.joining_date,e.exit_date,e.user_id,
          COALESCE(tm.is_owner, FALSE) AS is_owner,
          CASE WHEN e.user_id IS NULL THEN 'not_invited'
               WHEN tm.status = 'invited' THEN 'invited'
               WHEN tm.status = 'active' THEN 'active'
               ELSE 'not_invited' END AS invitation_status,
          d.name AS department_name, d.manager_id AS department_manager_id,
          manager.first_name AS manager_first_name, manager.last_name AS manager_last_name
          FROM employees e
          LEFT JOIN departments d ON d.tenant_id=e.tenant_id AND d.id=e.department_id AND d.deleted_at IS NULL
          LEFT JOIN employees manager ON manager.tenant_id=e.tenant_id AND manager.id=e.manager_id AND manager.deleted_at IS NULL
          LEFT JOIN tenant_memberships tm ON tm.tenant_id=e.tenant_id AND tm.user_id=e.user_id
          WHERE e.tenant_id=:tid AND e.id=:id AND e.deleted_at IS NULL AND ({scope_filter(scope)})"""),
                    {"tid": ctx.tenant_id, "uid": ctx.user_id, "id": employee_id},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise NotFound("Employee not found", code="employee.not_found")
    return dict(row)


async def get_profile(db: AsyncSession, ctx: RequestContext, employee_id: uuid.UUID) -> dict:
    _require_feature("employee_profiles", get_settings().feature_employee_profiles)
    employee = await get_employee(db, ctx, employee_id)
    history_items = await history(db, ctx, employee_id)
    async with db.begin():
        if employee.get("department_id") and not employee.get("department_name"):
            employee["department_name"] = await db.scalar(
                text("SELECT name FROM departments WHERE tenant_id=:tid AND id=:did"),
                {"tid": ctx.tenant_id, "did": employee["department_id"]},
            )
        roles = (
            await db.execute(
                text("""SELECT r.id,r.name FROM user_roles ur JOIN roles r ON r.id=ur.role_id
                        WHERE ur.tenant_id=:tid AND ur.user_id=:uid ORDER BY r.name"""),
                {"tid": ctx.tenant_id, "uid": employee.get("user_id")},
            )
        ).mappings().all() if employee.get("user_id") else []
        membership = await db.scalar(
            text("SELECT status FROM tenant_memberships WHERE tenant_id=:tid AND user_id=:uid"),
            {"tid": ctx.tenant_id, "uid": employee.get("user_id")},
        ) if employee.get("user_id") else None
    return {
        **employee,
        "access": {
            "linked": employee.get("user_id") is not None,
            "membership_status": membership,
            "roles": [dict(role) for role in roles],
        },
        "history": history_items,
    }


async def transfer_employee(
    db: AsyncSession, ctx: RequestContext, employee_id: uuid.UUID, changes: dict
) -> dict:
    _require_feature("employee_transfers", get_settings().feature_employee_transfers)
    if not ctx.can("employees.update"):
        ctx.require("employees.update")
    changes.pop("effective_date", None)
    changes = {key: value for key, value in changes.items() if key in {"department_id", "manager_id", "designation"}}
    if not changes:
        raise ValidationFailed("At least one employment detail must change", code="employee.transfer_empty")
    async with db.begin():
        old = (
            await db.execute(
                text("SELECT * FROM employees WHERE tenant_id=:tid AND id=:id AND deleted_at IS NULL FOR UPDATE"),
                {"tid": ctx.tenant_id, "id": employee_id},
            )
        ).mappings().first()
        if old is None:
            raise NotFound("Employee not found", code="employee.not_found")
        validation_values = {
            **changes,
            "department_id": changes.get("department_id", old["department_id"]),
            "team_id": changes.get("team_id", old.get("team_id")),
        }
        await _validate_employee_refs(db, ctx.tenant_id, validation_values)
        if changes.get("manager_id") == employee_id:
            raise ValidationFailed("An employee cannot report to themselves", code="employee.manager_self")
        if changes.get("manager_id"):
            eligible = await db.scalar(
                text("SELECT EXISTS(SELECT 1 FROM employees WHERE tenant_id=:tid AND id=:id AND deleted_at IS NULL AND status <> 'exited')"),
                {"tid": ctx.tenant_id, "id": changes["manager_id"]},
            )
            if not eligible:
                raise ValidationFailed("Reporting manager must be an active employee", code="employee.manager_ineligible")
            cycle = await db.scalar(
                text("""WITH RECURSIVE chain(id) AS
                    (SELECT :manager::uuid UNION ALL SELECT e.manager_id FROM employees e
                     JOIN chain c ON e.id=c.id WHERE e.tenant_id=:tid AND e.manager_id IS NOT NULL)
                    SELECT EXISTS(SELECT 1 FROM chain WHERE id=:id)"""),
                {"manager": changes["manager_id"], "tid": ctx.tenant_id, "id": employee_id},
            )
            if cycle:
                raise ValidationFailed("Manager relationship would create a cycle", code="employee.manager_cycle")
        sets = ",".join(f"{key}=:{key}" for key in changes)
        await db.execute(text(f"UPDATE employees SET {sets} WHERE tenant_id=:tid AND id=:id"),
                         {**changes, "tid": ctx.tenant_id, "id": employee_id})
        new = (await db.execute(text("SELECT * FROM employees WHERE tenant_id=:tid AND id=:id"),
                                {"tid": ctx.tenant_id, "id": employee_id})).mappings().one()
        event = "manager_changed" if set(changes) == {"manager_id"} else "transferred"
        await db.execute(
            text("""INSERT INTO employee_history(tenant_id,employee_id,event_type,old_data,new_data,actor_user_id)
                    VALUES (:tid,:id,:event,CAST(:old AS jsonb),CAST(:new AS jsonb),:uid)"""),
            {"tid": ctx.tenant_id, "id": employee_id, "event": event,
             "old": json.dumps({key: old[key] for key in changes}, default=str),
             "new": json.dumps({key: new[key] for key in changes}, default=str), "uid": ctx.user_id},
        )
        if "manager_id" in changes:
            from app.domains.organization.service import _ensure_manager_role

            await _ensure_manager_role(db, ctx, changes["manager_id"])
    return await get_employee(db, ctx, employee_id)


async def create_employee(db: AsyncSession, ctx: RequestContext, data: dict) -> dict:
    ctx.require("employees.create")
    invitations_enabled = get_settings().feature_employee_invitations
    data.pop("send_invitation", False)
    role_ids = list(dict.fromkeys(data.pop("role_ids", [])))
    if invitations_enabled and role_ids:
        raise ValidationFailed(
            "Role assignment requires sending an employee invitation",
            code="employee.invitation_required",
        )
    if invitations_enabled:
        role_ids = []
    if role_ids:
        ctx.require("roles.manage")
    async with db.begin():
        scope = await resolve_scope(db, ctx, "employees.create")
        if scope is not DataScope.ALL:
            raise Forbidden("Only all-scope users may create employees", code="employee.scope")
        await db.execute(text("SELECT id FROM tenants WHERE id=:tid FOR UPDATE"), {"tid": ctx.tenant_id})
        await _validate_employee_refs(db, ctx.tenant_id, data)
        current = await db.scalar(
            text("SELECT count(*) FROM employees WHERE tenant_id=:tid AND deleted_at IS NULL"),
            {"tid": ctx.tenant_id},
        )
        max_count = await db.scalar(
            text(
                """SELECT (p.limits_json->>'employees')::int FROM tenants t JOIN plans p ON p.id=t.plan_id WHERE t.id=:tid"""
            ),
            {"tid": ctx.tenant_id},
        )
        if max_count is not None and current >= max_count:
            raise PlanLimitReached(details={"metric": "employees", "limit": max_count})

        user_id = None
        invitation_token = None
        if not invitations_enabled and not role_ids:
            employee_role = await db.scalar(
                text("SELECT id FROM roles WHERE tenant_id IS NULL AND name='Employee'")
            )
            if employee_role is None:
                raise NotFound("Employee role not found", code="role.employee_not_found")
            role_ids = [employee_role]
        if not invitations_enabled and role_ids:
            role_rows = (
                (
                    await db.execute(
                        text(
                            "SELECT id FROM roles WHERE id=ANY(:ids) AND (tenant_id IS NULL OR tenant_id=:tid)"
                        ),
                        {"ids": role_ids, "tid": ctx.tenant_id},
                    )
                )
                .scalars()
                .all()
            )
            if len(role_rows) != len(role_ids):
                raise NotFound("Role not found", code="role.not_found")
            role_permissions = (
                (
                    await db.execute(
                        text("""SELECT DISTINCT p.code FROM role_permissions rp
                      JOIN permissions p ON p.id=rp.permission_id WHERE rp.role_id=ANY(:ids)"""),
                        {"ids": role_ids},
                    )
                )
                .scalars()
                .all()
            )
            if not set(role_permissions).issubset(ctx.permissions):
                raise Forbidden(
                    "You cannot grant a role with permissions you do not hold",
                    code="role.permission_escalation",
                )

            user_id = await db.scalar(
                text("""INSERT INTO users(email,full_name,password_hash,is_active,email_verified_at)
                  VALUES (:email,:name,:password,true,now())
                  ON CONFLICT(email) DO UPDATE SET
                    full_name=coalesce(users.full_name,EXCLUDED.full_name),
                    password_hash=coalesce(users.password_hash,EXCLUDED.password_hash),
                    is_active=coalesce(users.is_active,true),
                    email_verified_at=coalesce(users.email_verified_at,now())
                  RETURNING id"""),
                {
                    "email": data["email"],
                    "name": f"{data['first_name']} {data['last_name']}".strip(),
                    "password": hash_password("Testing@1234"),
                },
            )
            if user_id == ctx.user_id:
                raise Forbidden("You cannot change your own roles", code="role.self_change")
            membership = await db.scalar(
                text(
                    "SELECT status FROM tenant_memberships WHERE tenant_id=:tid AND user_id=:uid FOR UPDATE"
                ),
                {"tid": ctx.tenant_id, "uid": user_id},
            )
            if membership == "suspended":
                raise Conflict("This user has a suspended workspace membership", code="membership.suspended")
            if membership is None or membership in {"removed", "invited"}:
                await db.execute(
                    text("""INSERT INTO tenant_memberships(tenant_id,user_id,status,invited_by)
                      VALUES (:tid,:uid,'active',:actor) ON CONFLICT(tenant_id,user_id)
                      DO UPDATE SET status='active',invited_by=:actor,joined_at=COALESCE(tenant_memberships.joined_at,now())"""),
                    {"tid": ctx.tenant_id, "uid": user_id, "actor": ctx.user_id},
                )
                membership = "active"
            await db.execute(
                text("DELETE FROM user_roles WHERE tenant_id=:tid AND user_id=:uid"),
                {"tid": ctx.tenant_id, "uid": user_id},
            )
            for role_id in role_ids:
                await db.execute(
                    text("""INSERT INTO user_roles(tenant_id,user_id,role_id,granted_by)
                      VALUES (:tid,:uid,:role,:actor)"""),
                    {"tid": ctx.tenant_id, "uid": user_id, "role": role_id, "actor": ctx.user_id},
                )
        number = data.pop("employee_no", None)
        if not number:
            setting = (
                await db.execute(
                    text("""UPDATE tenant_settings SET value_json=jsonb_set(value_json,'{next}',
              to_jsonb((value_json->>'next')::int+1)) WHERE tenant_id=:tid AND key='employee_no.format'
              RETURNING value_json"""),
                    {"tid": ctx.tenant_id},
                )
            ).scalar_one_or_none()
            if setting is None:
                raise ValidationFailed(
                    "Employee number format is not configured", code="employee.number_config_missing"
                )
            number = f"{setting['prefix']}{int(setting['next']) - 1:0{int(setting.get('pad', 4))}d}"
        columns = {**data, "tenant_id": ctx.tenant_id, "employee_no": number, "user_id": user_id}
        names = ",".join(columns)
        binds = ",".join(f":{name}" for name in columns)
        row = (
            (await db.execute(text(f"INSERT INTO employees({names}) VALUES ({binds}) RETURNING *"), columns))
            .mappings()
            .one()
        )
        new = {
            k: row[k]
            for k in ("id", "employee_no", "first_name", "last_name", "email", "status", "joining_date")
        }
        await db.execute(
            text(
                "INSERT INTO employee_history(tenant_id,employee_id,event_type,new_data,actor_user_id) VALUES (:tid,:id,'hired',CAST(:data AS jsonb),:uid)"
            ),
            {"tid": ctx.tenant_id, "id": row["id"], "data": json.dumps(new, default=str), "uid": ctx.user_id},
        )
        await _start_onboarding_workflow(db, ctx, dict(row))
        await db.execute(
            text("""INSERT INTO usage_counters(tenant_id,metric,period,value)
          VALUES (:tid,'employees','lifetime',1) ON CONFLICT(tenant_id,metric,period)
          DO UPDATE SET value=usage_counters.value+1,updated_at=now()"""),
            {"tid": ctx.tenant_id},
        )
        await _audit(db, ctx, "employee.created", row["id"], after=new)
        response = {
            key: row[key]
            for key in (
                "id",
                "employee_no",
                "first_name",
                "last_name",
                "email",
                "phone",
                "department_id",
                "team_id",
                "manager_id",
                "location_id",
                "work_schedule_id",
                "designation",
                "employment_type",
                "status",
                "joining_date",
                "exit_date",
                "user_id",
            )
        }
        response["roles_assigned"] = bool(role_ids)
        response["invitation_status"] = "active" if row["user_id"] else "not_invited"
        if invitation_token and get_settings().env != "prod":
            response["invitation_token"] = invitation_token
    return response


async def update_employee(
    db: AsyncSession, ctx: RequestContext, employee_id: uuid.UUID, changes: dict
) -> dict:
    role_ids = changes.pop("role_ids", None)
    if role_ids is not None:
        ctx.require("roles.manage")
    if changes.get("team_id") is not None and not get_settings().feature_teams:
        raise ValidationFailed("Teams are disabled", code="feature.teams_disabled")
    if ctx.can("employees.update"):
        ctx.require("employees.update")
    elif ctx.can("employees.read"):
        async with db.begin():
            own = await db.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM employees WHERE tenant_id=:tid AND id=:id AND user_id=:uid AND deleted_at IS NULL)"
                ),
                {"tid": ctx.tenant_id, "id": employee_id, "uid": ctx.user_id},
            )
        if not own:
            raise NotFound("Employee not found", code="employee.not_found")
        if set(changes) - {"phone"}:
            raise Forbidden("You may only update your phone number", code="employee.self_update_restricted")
    else:
        ctx.require("employees.update")
    nullable = {
        "phone",
        "department_id",
        "manager_id",
        "location_id",
        "work_schedule_id",
        "designation",
        "exit_date",
    }
    changes = {k: v for k, v in changes.items() if v is not None or k in nullable}
    if set(changes) - _FIELDS:
        raise ValidationFailed()
    async with db.begin():
        scope = (
            await resolve_scope(db, ctx, "employees.update") if ctx.can("employees.update") else DataScope.SELF
        ) or DataScope.SELF
        old = (
            (
                await db.execute(
                    text(
                        "SELECT e.* FROM employees e WHERE e.tenant_id=:tid AND e.id=:id AND e.deleted_at IS NULL AND ("
                        + scope_filter(scope)
                        + ") FOR UPDATE"
                    ),
                    {"tid": ctx.tenant_id, "id": employee_id, "uid": ctx.user_id},
                )
            )
            .mappings()
            .first()
        )
        if old is None:
            raise NotFound("Employee not found", code="employee.not_found")
        if role_ids is not None:
            role_ids = list(dict.fromkeys(role_ids))
            role_rows = (
                (
                    await db.execute(
                        text(
                            "SELECT id FROM roles WHERE id=ANY(:ids) AND (tenant_id IS NULL OR tenant_id=:tid)"
                        ),
                        {"ids": role_ids, "tid": ctx.tenant_id},
                    )
                )
                .scalars()
                .all()
                if role_ids
                else []
            )
            if len(role_rows) != len(role_ids):
                raise NotFound("Role not found", code="role.not_found")
            role_permissions = (
                (
                    await db.execute(
                        text("""SELECT DISTINCT p.code FROM role_permissions rp
                      JOIN permissions p ON p.id=rp.permission_id WHERE rp.role_id=ANY(:ids)"""),
                        {"ids": role_ids},
                    )
                )
                .scalars()
                .all()
                if role_ids
                else []
            )
            if not set(role_permissions).issubset(ctx.permissions):
                raise Forbidden(
                    "You cannot grant a role with permissions you do not hold",
                    code="role.permission_escalation",
                )
        validation_values = {
            **changes,
            "department_id": changes.get("department_id", old["department_id"]),
            "team_id": changes.get("team_id", old.get("team_id")),
        }
        await _validate_employee_refs(db, ctx.tenant_id, validation_values)
        if changes.get("manager_id"):
            cycle = await db.scalar(
                text(
                    """WITH RECURSIVE chain(id) AS (SELECT :manager::uuid UNION ALL SELECT e.manager_id FROM employees e JOIN chain c ON e.id=c.id WHERE e.tenant_id=:tid AND e.manager_id IS NOT NULL) SELECT EXISTS(SELECT 1 FROM chain WHERE id=:id)"""
                ),
                {"manager": changes["manager_id"], "tid": ctx.tenant_id, "id": employee_id},
            )
            if cycle:
                raise ValidationFailed(
                    "Manager relationship would create a cycle", code="employee.manager_cycle"
                )
        if changes:
            sets = ",".join(f"{k}=:{k}" for k in changes)
            await db.execute(
                text(f"UPDATE employees SET {sets} WHERE id=:id AND tenant_id=:tid"),
                {**changes, "id": employee_id, "tid": ctx.tenant_id},
            )
        new = (
            (
                await db.execute(
                    text("SELECT * FROM employees WHERE tenant_id=:tid AND id=:id"),
                    {"tid": ctx.tenant_id, "id": employee_id},
                )
            )
            .mappings()
            .one()
        )
        if role_ids is not None:
            user_id = new["user_id"]
            if user_id is None and role_ids:
                user_id = await db.scalar(
                    text("""INSERT INTO users(email,full_name) VALUES (:email,:name)
                      ON CONFLICT(email) DO UPDATE SET full_name=coalesce(users.full_name,EXCLUDED.full_name) RETURNING id"""),
                    {"email": new["email"], "name": f"{new['first_name']} {new['last_name']}".strip()},
                )
                if user_id == ctx.user_id:
                    raise Forbidden("You cannot change your own roles", code="role.self_change")
                await db.execute(
                    text("""INSERT INTO tenant_memberships(tenant_id,user_id,status,invited_by)
                      VALUES (:tid,:uid,'invited',:actor) ON CONFLICT(tenant_id,user_id)
                      DO UPDATE SET status='invited',invited_by=:actor"""),
                    {"tid": ctx.tenant_id, "uid": user_id, "actor": ctx.user_id},
                )
                token = generate_opaque_token()
                await db.execute(
                    text("""INSERT INTO auth_tokens(user_id,tenant_id,purpose,token_hash,expires_at)
                      VALUES (:uid,:tid,'invitation',:hash,now()+interval '7 days')"""),
                    {"uid": user_id, "tid": ctx.tenant_id, "hash": hash_token(token)},
                )
                tenant_slug = await db.scalar(
                    text("SELECT slug FROM tenants WHERE id=:tid"), {"tid": ctx.tenant_id}
                )
                await notification_service.queue_invitation(
                    db,
                    ctx.tenant_id,
                    user_id,
                    tenant_slug,
                    f"{new['first_name']} {new['last_name']}".strip(),
                    token,
                )
                await db.execute(
                    text("UPDATE employees SET user_id=:uid WHERE tenant_id=:tid AND id=:id"),
                    {"uid": user_id, "tid": ctx.tenant_id, "id": employee_id},
                )
            if user_id is not None:
                if user_id == ctx.user_id:
                    raise Forbidden("You cannot change your own roles", code="role.self_change")
                await db.execute(
                    text("DELETE FROM user_roles WHERE tenant_id=:tid AND user_id=:uid"),
                    {"tid": ctx.tenant_id, "uid": user_id},
                )
                for role_id in role_ids:
                    await db.execute(
                        text(
                            "INSERT INTO user_roles(tenant_id,user_id,role_id,granted_by) VALUES (:tid,:uid,:role,:actor)"
                        ),
                        {"tid": ctx.tenant_id, "uid": user_id, "role": role_id, "actor": ctx.user_id},
                    )
                new = (
                    (
                        await db.execute(
                            text("SELECT * FROM employees WHERE tenant_id=:tid AND id=:id"),
                            {"tid": ctx.tenant_id, "id": employee_id},
                        )
                    )
                    .mappings()
                    .one()
                )
        await _audit(db, ctx, "employee.updated", employee_id, before=dict(old), after=dict(new))
        if changes:
            if "status" in changes:
                event = "status_changed"
            elif "manager_id" in changes:
                event = "manager_changed"
            elif "department_id" in changes or "location_id" in changes:
                event = "transferred"
            elif "designation" in changes:
                event = "promoted"
            else:
                event = "updated"
            await db.execute(
                text(
                    "INSERT INTO employee_history(tenant_id,employee_id,event_type,old_data,new_data,actor_user_id) VALUES (:tid,:id,:event,CAST(:old AS jsonb),CAST(:new AS jsonb),:uid)"
                ),
                {
                    "tid": ctx.tenant_id,
                    "id": employee_id,
                    "event": event,
                    "old": json.dumps({key: old[key] for key in changes}, default=str),
                    "new": json.dumps({key: new[key] for key in changes}, default=str),
                    "uid": ctx.user_id,
                },
            )
        response = {
            key: new[key]
            for key in (
                "id",
                "employee_no",
                "first_name",
                "last_name",
                "email",
                "phone",
                "department_id",
                "manager_id",
                "location_id",
                "work_schedule_id",
                "designation",
                "employment_type",
                "status",
                "joining_date",
                "exit_date",
                "user_id",
            )
        }
    return response


async def delete_employee(db: AsyncSession, ctx: RequestContext, employee_id: uuid.UUID) -> None:
    ctx.require("employees.delete")
    async with db.begin():
        scope = await resolve_scope(db, ctx, "employees.delete") or DataScope.SELF
        row = (
            (
                await db.execute(
                    text(
                        "SELECT e.id,e.status FROM employees e WHERE e.tenant_id=:tid AND e.id=:id AND e.deleted_at IS NULL AND ("
                        + scope_filter(scope)
                        + ") FOR UPDATE"
                    ),
                    {"tid": ctx.tenant_id, "id": employee_id, "uid": ctx.user_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound("Employee not found", code="employee.not_found")
        await db.execute(
            text("UPDATE employees SET deleted_at=now(),status='exited' WHERE tenant_id=:tid AND id=:id"),
            {"tid": ctx.tenant_id, "id": employee_id},
        )
        await db.execute(
            text(
                "INSERT INTO employee_history(tenant_id,employee_id,event_type,old_data,actor_user_id) VALUES (:tid,:id,'exited',CAST(:old AS jsonb),:uid)"
            ),
            {
                "tid": ctx.tenant_id,
                "id": employee_id,
                "old": json.dumps({"status": row["status"]}),
                "uid": ctx.user_id,
            },
        )
        await _audit(db, ctx, "employee.deleted", employee_id, before=dict(row))


async def invite_employee(db: AsyncSession, ctx: RequestContext, employee_id: uuid.UUID) -> dict:
    ctx.require("employees.update")
    token = generate_opaque_token()
    async with db.begin():
        scope = await resolve_scope(db, ctx, "employees.update") or DataScope.SELF
        employee = (
            (
                await db.execute(
                    text("""SELECT id,user_id,email,first_name,last_name FROM employees
          WHERE tenant_id=:tid AND id=:id AND deleted_at IS NULL AND (""" + scope_filter(scope) + """) FOR UPDATE"""),
                    {"tid": ctx.tenant_id, "id": employee_id, "uid": ctx.user_id},
                )
            )
            .mappings()
            .first()
        )
        if employee is None:
            raise NotFound("Employee not found", code="employee.not_found")
        if employee["user_id"] is not None:
            raise Conflict("Employee already has a login account", code="employee.account_exists")
        uid = await db.scalar(
            text("""INSERT INTO users(email,full_name) VALUES (:email,:name)
          ON CONFLICT(email) DO UPDATE SET full_name=coalesce(users.full_name,EXCLUDED.full_name) RETURNING id"""),
            {"email": employee["email"], "name": f"{employee['first_name']} {employee['last_name']}".strip()},
        )
        membership = await db.scalar(
            text("SELECT status FROM tenant_memberships WHERE tenant_id=:tid AND user_id=:uid FOR UPDATE"),
            {"tid": ctx.tenant_id, "uid": uid},
        )
        if membership == "suspended":
            raise Conflict("This user has a suspended workspace membership", code="membership.suspended")
        invite_needed = membership != "active"
        if membership is None or membership == "removed":
            await db.execute(
                text("""INSERT INTO tenant_memberships(tenant_id,user_id,status,invited_by)
              VALUES (:tid,:uid,'invited',:actor) ON CONFLICT(tenant_id,user_id) DO UPDATE SET status='invited',invited_by=:actor"""),
                {"tid": ctx.tenant_id, "uid": uid, "actor": ctx.user_id},
            )
            await db.execute(
                text("DELETE FROM user_roles WHERE tenant_id=:tid AND user_id=:uid"),
                {"tid": ctx.tenant_id, "uid": uid},
            )
            await db.execute(
                text("""INSERT INTO user_roles(tenant_id,user_id,role_id,granted_by)
              SELECT :tid,:uid,id,:actor FROM roles WHERE tenant_id IS NULL AND name='Employee' ON CONFLICT DO NOTHING"""),
                {"tid": ctx.tenant_id, "uid": uid, "actor": ctx.user_id},
            )
        if invite_needed:
            await db.execute(
                text(
                    "UPDATE auth_tokens SET used_at=now() WHERE user_id=:uid AND tenant_id=:tid AND purpose='invitation' AND used_at IS NULL"
                ),
                {"uid": uid, "tid": ctx.tenant_id},
            )
            await db.execute(
                text("""INSERT INTO auth_tokens(user_id,tenant_id,purpose,token_hash,expires_at)
              VALUES (:uid,:tid,'invitation',:hash,now()+interval '7 days')"""),
                {"uid": uid, "tid": ctx.tenant_id, "hash": hash_token(token)},
            )
            tenant_slug = await db.scalar(
                text("SELECT slug FROM tenants WHERE id=:tid"), {"tid": ctx.tenant_id}
            )
            await notification_service.queue_invitation(
                db,
                ctx.tenant_id,
                uid,
                tenant_slug,
                f"{employee['first_name']} {employee['last_name']}".strip(),
                token,
            )
        await db.execute(
            text("UPDATE employees SET user_id=:uid WHERE tenant_id=:tid AND id=:id"),
            {"uid": uid, "tid": ctx.tenant_id, "id": employee_id},
        )
        await _audit(
            db,
            ctx,
            "employee.invited",
            employee_id,
            after={"user_id": uid, "membership_status": membership or "invited"},
        )
    result = {"employee_id": employee_id, "user_id": uid, "status": membership or "invited"}
    if invite_needed and get_settings().env != "prod":
        result["invitation_token"] = token
    return result


async def history(db: AsyncSession, ctx: RequestContext, employee_id: uuid.UUID) -> list[dict]:
    ctx.require("employees.read")
    async with db.begin():
        employee = await db.scalar(
            text(
                "SELECT EXISTS(SELECT 1 FROM employees e WHERE e.tenant_id=:tid AND e.id=:id AND ("
                + scope_filter(await resolve_scope(db, ctx, "employees.read") or DataScope.SELF)
                + "))"
            ),
            {"tid": ctx.tenant_id, "uid": ctx.user_id, "id": employee_id},
        )
        if not employee:
            raise NotFound("Employee not found", code="employee.not_found")
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT id,event_type,effective_at,old_data,new_data,actor_user_id FROM employee_history WHERE tenant_id=:tid AND employee_id=:id ORDER BY effective_at DESC"
                    ),
                    {"tid": ctx.tenant_id, "id": employee_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def reports(db: AsyncSession, ctx: RequestContext, employee_id: uuid.UUID) -> list[dict]:
    ctx.require("employees.read")
    async with db.begin():
        scope = await resolve_scope(db, ctx, "employees.read") or DataScope.SELF
        row = await db.scalar(
            text(
                "SELECT EXISTS(SELECT 1 FROM employees e WHERE e.tenant_id=:tid AND e.id=:id AND e.deleted_at IS NULL AND ("
                + scope_filter(scope)
                + "))"
            ),
            {"tid": ctx.tenant_id, "uid": ctx.user_id, "id": employee_id},
        )
        if not row:
            raise NotFound("Employee not found", code="employee.not_found")
        rows = (
            (
                await db.execute(
                    text(
                        """WITH RECURSIVE team AS (
          SELECT id,manager_id,first_name,last_name,email,status,1 AS depth FROM employees WHERE tenant_id=:tid AND manager_id=:id AND deleted_at IS NULL
          UNION ALL SELECT e.id,e.manager_id,e.first_name,e.last_name,e.email,e.status,t.depth+1 FROM employees e JOIN team t ON e.manager_id=t.id WHERE e.tenant_id=:tid AND e.deleted_at IS NULL)
          SELECT t.id,t.manager_id,t.first_name,t.last_name,t.email,t.status,t.depth FROM team t
          JOIN employees e ON e.id=t.id WHERE """
                        + scope_filter(scope)
                        + " ORDER BY t.depth,t.last_name,t.first_name"
                    ),
                    {"tid": ctx.tenant_id, "uid": ctx.user_id, "id": employee_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]
