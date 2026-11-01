"""SQL for the rbac domain.

Every method takes tenant_id as its first argument. See database/04_queries.sql.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# A5 plus the membership check in one round trip.
_EFFECTIVE_PERMISSIONS = text(
    """
    SELECT m.status,
           COALESCE(
             array_agg(DISTINCT p.code) FILTER (WHERE p.code IS NOT NULL),
             '{}'
           ) AS codes
    FROM tenant_memberships m
    LEFT JOIN user_roles ur       ON ur.tenant_id = m.tenant_id AND ur.user_id = m.user_id
    LEFT JOIN roles r             ON r.id = ur.role_id AND (r.tenant_id IS NULL OR r.tenant_id = m.tenant_id)
    LEFT JOIN role_permissions rp ON rp.role_id = r.id
    LEFT JOIN permissions p       ON p.id = rp.permission_id
    WHERE m.tenant_id = :tenant_id AND m.user_id = :user_id
    GROUP BY m.status
    """
)


async def fetch_effective_permissions(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> list[str] | None:
    """Permission codes of an ACTIVE member, or None when the user is not an active member."""
    row = (
        await session.execute(_EFFECTIVE_PERMISSIONS, {"tenant_id": tenant_id, "user_id": user_id})
    ).first()
    if row is None or row.status != "active":
        return None
    return sorted(row.codes)
