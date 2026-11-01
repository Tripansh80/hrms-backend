"""Public tenant signup and workspace lookup endpoints."""

import re
import uuid

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext, get_request_context
from app.core.errors import ValidationFailed
from app.core.ratelimit import hit
from app.core.tenant import require_root_domain
from app.db.session import get_db, get_platform_db
from app.domains.auth.schemas import SignupRequest
from app.domains.auth.service import signup
from app.domains.tenants import service
from app.domains.tenants.schemas import InviteMember, MembershipUpdate, TenantUpdate

router = APIRouter(prefix="/tenants", tags=["tenants"])
memberships_router = APIRouter(prefix="/memberships", tags=["memberships"])
RESERVED = {"www", "api", "app", "admin", "mail", "static", "assets", "status", "docs"}


@router.get("/check-slug")
async def check_slug(slug: str, request: Request, db: AsyncSession = Depends(get_platform_db)) -> dict:
    require_root_domain(request)
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", slug) or slug in RESERVED:
        return {"available": False}
    exists = await db.scalar(
        text("SELECT EXISTS(SELECT 1 FROM tenants WHERE slug=:slug AND deleted_at IS NULL)"), {"slug": slug}
    )
    return {"available": not bool(exists)}


@router.post("/signup", status_code=201)
async def tenant_signup(body: SignupRequest, request: Request) -> dict:
    require_root_domain(request)
    if body.slug in RESERVED:
        raise ValidationFailed("This workspace address is reserved", code="tenant.slug_reserved")
    ip = request.client.host if request.client else "unknown"
    await hit(f"tenant-signup:{ip}", 5, 3600)
    tenant_id = await signup(body.name, body.slug, body.owner_email, body.owner_full_name, body.password)
    return {"id": tenant_id, "slug": body.slug, "status": "active"}


@router.get("/current")
async def current_tenant(
    ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> dict:
    return await service.get_current(db, ctx)


@router.patch("/current")
async def patch_current_tenant(
    body: TenantUpdate, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> dict:
    return await service.update_current(db, ctx, body.model_dump(exclude_unset=True))


@memberships_router.get("")
async def get_memberships(
    ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> dict:
    return {"items": await service.memberships(db, ctx)}


@memberships_router.post("/invite", status_code=201)
async def post_membership_invite(
    body: InviteMember, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> dict:
    return await service.invite_member(db, ctx, body.email, body.full_name)


@memberships_router.patch("/{user_id}")
async def patch_membership(
    user_id: uuid.UUID,
    body: MembershipUpdate,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.update_membership(db, ctx, user_id, body.status)


@memberships_router.delete("/{user_id}", status_code=204)
async def delete_membership(
    user_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> Response:
    await service.remove_membership(db, ctx, user_id)
    return Response(status_code=204)
