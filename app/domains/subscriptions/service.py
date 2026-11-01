"""Business logic for the subscriptions domain.

Services own authorization (ctx.require), data scope, business rules, transactions and audit.
Routers, the AI layer and workers all call these functions.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def public_plans(db: AsyncSession) -> dict:
    """Return the active plan catalog without requiring a tenant or login."""
    rows = (
        (
            await db.execute(
                text("""SELECT id,code,name,price,currency,billing_interval,limits_json,features_json
            FROM plans WHERE is_active ORDER BY price,code""")
            )
        )
        .mappings()
        .all()
    )
    return {"plans": [dict(row) for row in rows]}
