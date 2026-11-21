"""Permission dependency and data-scope helpers.

A permission says WHAT you may do; the role's data_scope says TO WHOM.
`require()` on a route is a convenience — the service check is the guarantee.
"""

from collections.abc import Awaitable, Callable, Iterable
from enum import StrEnum

from fastapi import Depends
from sqlalchemy import text

from app.core.context import RequestContext, get_request_context
from app.core.errors import NotFound


class DataScope(StrEnum):
    ALL = "all"
    MANAGED = "managed"
    DEPARTMENT = "department"
    TEAM = "team"
    SELF = "self"


_WIDTH = {
    DataScope.ALL: 5,
    DataScope.MANAGED: 4,
    DataScope.DEPARTMENT: 3,
    DataScope.TEAM: 2,
    DataScope.SELF: 1,
}


def widest_scope(scopes: Iterable[str]) -> DataScope | None:
    """A user with several roles gets the widest scope among the roles granting the permission (A6)."""
    parsed = [DataScope(s) for s in scopes]
    return max(parsed, key=_WIDTH.__getitem__) if parsed else None


def require(code: str) -> Callable[[RequestContext], Awaitable[RequestContext]]:
    """Route dependency: `ctx: RequestContext = Depends(require("employees.create"))`."""

    async def dependency(ctx: RequestContext = Depends(get_request_context)) -> RequestContext:
        ctx.require(code)
        return ctx

    return dependency


async def resolve_scope(db, ctx: RequestContext, permission: str) -> DataScope | None:
    """Return the widest scope granted for ``permission`` or ``None``."""
    scopes = (
        (
            await db.execute(
                text(
                    """SELECT r.data_scope
                       FROM user_roles ur
                       JOIN roles r ON r.id=ur.role_id
                       JOIN role_permissions rp ON rp.role_id=r.id
                       JOIN permissions p ON p.id=rp.permission_id
                       WHERE ur.tenant_id=:tid AND ur.user_id=:uid AND p.code=:permission"""
                ),
                {"tid": ctx.tenant_id, "uid": ctx.user_id, "permission": permission},
            )
        )
        .scalars()
        .all()
    )
    if not scopes:
        return None
    return widest_scope(scopes)


def scope_filter(scope: DataScope | str, alias: str = "e") -> str:
    """Build the employee predicate; callers bind ``:tid`` and ``:uid``."""
    value = DataScope(scope)
    if value is DataScope.ALL:
        return "TRUE"
    if value is DataScope.MANAGED:
        return f"{alias}.id IN (SELECT employee_id FROM managed_employee_ids(:tid, :uid))"
    if value is DataScope.SELF:
        return f"{alias}.user_id=:uid"
    if value is DataScope.TEAM:
        return (
            f"{alias}.id IN (WITH RECURSIVE team(id) AS "
            "(SELECT id FROM employees WHERE tenant_id=:tid AND user_id=:uid AND deleted_at IS NULL "
            "UNION SELECT e2.id FROM employees e2 JOIN team t ON e2.manager_id=t.id "
            "WHERE e2.tenant_id=:tid AND e2.deleted_at IS NULL) SELECT id FROM team)"
        )
    return (
        f"{alias}.id IN (WITH RECURSIVE dept(id) AS "
        "(SELECT department_id FROM employees WHERE tenant_id=:tid AND user_id=:uid AND deleted_at IS NULL "
        "UNION SELECT d.id FROM departments d JOIN employees m ON m.id=d.manager_id "
        "WHERE d.tenant_id=:tid AND m.user_id=:uid AND d.deleted_at IS NULL "
        "UNION SELECT child.id FROM departments child JOIN dept parent ON child.parent_id=parent.id "
        "WHERE child.tenant_id=:tid AND child.deleted_at IS NULL) "
        f"SELECT member.id FROM employees member JOIN dept ON dept.id=member.department_id "
        "WHERE member.tenant_id=:tid AND member.deleted_at IS NULL)"
    )


async def assert_in_scope(db, ctx: RequestContext, permission: str, employee_id) -> None:
    """Raise 404 when an employee is outside the caller's permitted scope."""
    scope = await resolve_scope(db, ctx, permission)
    if scope is None:
        ctx.require(permission)
        return
    exists = await db.scalar(
        text(
            f"SELECT EXISTS(SELECT 1 FROM employees e WHERE e.tenant_id=:tid "
            f"AND e.id=:employee_id AND e.deleted_at IS NULL AND ({scope_filter(scope)}))"
        ),
        {"tid": ctx.tenant_id, "uid": ctx.user_id, "employee_id": employee_id},
    )
    if not exists:
        raise NotFound("Employee not found", code="employee.not_found")
