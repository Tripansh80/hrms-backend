"""HTTP endpoints for permissions, roles and tenant users."""

import uuid

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext, get_request_context
from app.db.session import get_db
from app.domains.rbac import service
from app.domains.rbac.schemas import PermissionSet, RoleCreate, RoleSet, RoleUpdate

router = APIRouter(tags=["rbac"])


@router.get("/permissions")
async def get_permissions(
    ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> dict:
    return {"items": await service.permissions(db, ctx)}


@router.get("/roles")
async def get_roles(
    ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> dict:
    return {"items": await service.list_roles(db, ctx)}


@router.post("/roles", status_code=201)
async def post_role(
    body: RoleCreate, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> dict:
    return await service.create_role(db, ctx, body.model_dump())


@router.get("/roles/{role_id}")
async def get_role(
    role_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> dict:
    return await service.get_role(db, ctx, role_id)


@router.patch("/roles/{role_id}")
async def patch_role(
    role_id: uuid.UUID,
    body: RoleUpdate,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.update_role(db, ctx, role_id, body.model_dump(exclude_unset=True))


@router.delete("/roles/{role_id}", status_code=204)
async def remove_role(
    role_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> Response:
    await service.delete_role(db, ctx, role_id)
    return Response(status_code=204)


@router.put("/roles/{role_id}/permissions")
async def put_role_permissions(
    role_id: uuid.UUID,
    body: PermissionSet,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.set_role_permissions(db, ctx, role_id, body.permissions)


@router.get("/users")
async def get_users(
    ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> dict:
    return {"items": await service.list_users(db, ctx)}


@router.get("/users/{user_id}/roles")
async def get_user_roles(
    user_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> dict:
    return {"items": await service.user_roles(db, ctx, user_id)}


@router.put("/users/{user_id}/roles")
async def put_user_roles(
    user_id: uuid.UUID,
    body: RoleSet,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return {"items": await service.set_user_roles(db, ctx, user_id, body.role_ids)}
