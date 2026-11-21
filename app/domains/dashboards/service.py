from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext

_DASHBOARDS = (
    ("admin", "dashboard.admin"),
    ("hr", "dashboard.hr"),
    ("manager", "dashboard.manager"),
    ("employee", "dashboard.employee"),
)


async def _is_owner(db: AsyncSession, ctx: RequestContext) -> bool:
    return bool(
        await db.scalar(
            text(
                "SELECT EXISTS(SELECT 1 FROM tenant_memberships "
                "WHERE tenant_id=:tid AND user_id=:uid AND status='active' AND is_owner)"
            ),
            {"tid": ctx.tenant_id, "uid": ctx.user_id},
        )
    )


async def available(db: AsyncSession, ctx: RequestContext) -> dict:
    owner = await _is_owner(db, ctx)
    items = [
        name
        for name, permission in _DASHBOARDS
        if ctx.can(permission) or (owner and permission == "dashboard.admin")
    ]
    return {"dashboards": items, "default": items[0] if items else None}


async def get_dashboard(db: AsyncSession, ctx: RequestContext, view: str) -> dict:
    permission = dict(_DASHBOARDS)[view]
    if not (permission == "dashboard.admin" and await _is_owner(db, ctx)):
        ctx.require(permission)
    widgets: dict[str, dict] = {
        "hiring_funnel": {"status": "not_available"},
        "people_cost": {"status": "not_available"},
    }
    if view in {"admin", "hr"}:
        active = await db.scalar(
            text(
                "SELECT count(*) FROM employees WHERE tenant_id=:tid "
                "AND status='active' AND deleted_at IS NULL"
            ),
            {"tid": ctx.tenant_id},
        )
        widgets["kpis"] = {"active_employees": active or 0}
    elif view == "manager":
        managed = await db.scalar(
            text("SELECT count(*) FROM managed_employee_ids(:tid,:uid)"),
            {"tid": ctx.tenant_id, "uid": ctx.user_id},
        )
        widgets["kpis"] = {"managed_people": managed or 0}
    else:
        widgets["check_in"] = {"status": "not_available"}
    return {"generated_at": datetime.now(UTC).isoformat(), "widgets": widgets}
