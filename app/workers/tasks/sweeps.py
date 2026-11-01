"""Scheduled database sweeps."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from app.db.session import platform_session, tenant_session


async def mark_absent(_ctx: dict[str, Any]) -> None:
    """Mark the previous local workday absent for each active tenant (D5)."""
    async with platform_session() as platform_db:
        tenant_ids = (
            (
                await platform_db.execute(
                    text("SELECT id FROM tenants WHERE status='active' AND deleted_at IS NULL")
                )
            )
            .scalars()
            .all()
        )
    # Tenant and schedule timezones span UTC-12 through UTC+14. The service filters
    # each employee to their own local yesterday, so these candidate dates cover all.
    utc_today = datetime.now(UTC).date()
    for tenant_id in tenant_ids:
        async with tenant_session(tenant_id) as db:
            async with db.begin():
                from app.domains.attendance.service import mark_absent_for_tenant

                for day_offset in (-1, 0, 1):
                    await mark_absent_for_tenant(db, tenant_id, utc_today + timedelta(days=day_offset))
