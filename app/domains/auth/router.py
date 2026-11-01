"""HTTP endpoints for login and session management."""

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext, get_request_context
from app.core.errors import Unauthorized
from app.core.ratelimit import hit
from app.core.tenant import TenantContext, get_tenant
from app.db.session import get_db
from app.domains.auth import service
from app.domains.auth.schemas import AcceptInviteRequest, LoginRequest

router = APIRouter(prefix="/auth", tags=["auth"])
COOKIE = "hrms_refresh"


def _set_cookie(response: Response, value: str) -> None:
    from app.core.config import get_settings

    response.set_cookie(
        COOKIE,
        value,
        max_age=get_settings().refresh_token_ttl_days * 86400,
        httponly=True,
        secure=get_settings().env == "prod",
        samesite="lax",
        path="/auth",
    )


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    tenant: TenantContext = Depends(get_tenant),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ip = request.client.host if request.client else "unknown"
    await hit(f"auth:ip:{ip}", 10, 60)
    await hit(f"auth:email:{body.email.lower()}", 5, 60)
    access, refresh, _ = await service.login(
        db, tenant, body.email, body.password, ip, request.headers.get("user-agent")
    )
    _set_cookie(response, refresh)
    return {"access_token": access, "token_type": "bearer", "expires_in": 900}


@router.post("/refresh")
async def refresh(
    request: Request,
    response: Response,
    tenant: TenantContext = Depends(get_tenant),
    db: AsyncSession = Depends(get_db),
) -> dict:
    token = request.cookies.get(COOKIE)
    if not token:
        raise Unauthorized()
    access, rotated = await service.rotate_refresh(
        db, tenant, token, request.client.host if request.client else None, request.headers.get("user-agent")
    )
    _set_cookie(response, rotated)
    return {"access_token": access, "token_type": "bearer", "expires_in": 900}


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    tenant: TenantContext = Depends(get_tenant),
    db: AsyncSession = Depends(get_db),
) -> Response:
    token = request.cookies.get(COOKIE)
    if token:
        async with db.begin():
            await db.execute(
                text(
                    "UPDATE refresh_tokens SET revoked_at=now() WHERE tenant_id=:tid AND token_hash=:hash AND revoked_at IS NULL"
                ),
                {
                    "tid": tenant.id,
                    "hash": __import__("app.core.security", fromlist=["hash_token"]).hash_token(token),
                },
            )
    response.delete_cookie(COOKIE, path="/auth", httponly=True, samesite="lax")
    return response


@router.post("/logout-all", status_code=204)
async def logout_all(
    response: Response, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> Response:
    async with db.begin():
        await db.execute(
            text(
                "UPDATE refresh_tokens SET revoked_at=now() WHERE tenant_id=:tid AND user_id=:uid AND revoked_at IS NULL"
            ),
            {"tid": ctx.tenant_id, "uid": ctx.user_id},
        )
    response.delete_cookie(COOKIE, path="/auth", httponly=True, samesite="lax")
    return response


@router.get("/me")
async def me(ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)) -> dict:
    async with db.begin():
        row = (
            (
                await db.execute(
                    text("""SELECT u.id,u.email,u.full_name,u.email_verified_at,m.is_owner,
       e.employee_no AS employee_id,e.designation,
       t.id AS tenant_id,t.name AS tenant_name,t.slug FROM users u
       JOIN tenant_memberships m ON m.user_id=u.id AND m.tenant_id=:tid
       JOIN tenants t ON t.id=m.tenant_id
       LEFT JOIN employees e ON e.user_id=u.id AND e.tenant_id=m.tenant_id
       WHERE u.id=:uid AND m.status='active'"""),
                    {"tid": ctx.tenant_id, "uid": ctx.user_id},
                )
            )
            .mappings()
            .first()
        )
        role_names = (
            (
                await db.execute(
                    text("""SELECT r.name FROM user_roles ur JOIN roles r ON r.id=ur.role_id
          WHERE ur.tenant_id=:tid AND ur.user_id=:uid ORDER BY r.name"""),
                    {"tid": ctx.tenant_id, "uid": ctx.user_id},
                )
            )
            .scalars()
            .all()
        )
    if row is None:
        raise Unauthorized()
    return {**dict(row), "roles": role_names, "permissions": sorted(ctx.permissions)}


@router.post("/accept-invite", status_code=204)
async def accept_invite(
    body: AcceptInviteRequest, tenant: TenantContext = Depends(get_tenant), db: AsyncSession = Depends(get_db)
) -> Response:
    await service.accept_invitation(db, tenant, body.token, body.password)
    return Response(status_code=204)
