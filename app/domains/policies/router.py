"""HTTP layer for the policies domain: parse, call the service, shape the response."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext, get_request_context
from app.db.session import get_db
from app.domains.policies import service
from app.domains.policies.schemas import PolicyCreate

router = APIRouter(prefix="/policies", tags=["policies"])


@router.get("")
async def get_policies(
    ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
):
    return {"items": await service.list_policies(db, ctx)}


@router.post("", status_code=201)
async def post_policy(
    body: PolicyCreate, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
):
    return await service.create_policy(db, ctx, body.model_dump())


@router.post("/{policy_id}/publish")
async def post_publish(
    policy_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
):
    return await service.publish_policy(db, ctx, policy_id)


@router.post("/{policy_id}/toggle-status")
async def post_toggle_status(
    policy_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.toggle_policy_status(db, ctx, policy_id)


@router.post("/{policy_id}/acknowledge")
async def post_acknowledge(
    policy_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.acknowledge_policy(db, ctx, policy_id)
