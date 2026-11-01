"""HTTP layer for the subscriptions domain: parse, call the service, shape the response."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_platform_db
from app.domains.subscriptions import service

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])
pricing_router = APIRouter(prefix="/pricing", tags=["pricing"])


@pricing_router.get("")
async def get_public_pricing(db: AsyncSession = Depends(get_platform_db)) -> dict:
    return await service.public_plans(db)
