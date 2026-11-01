"""RequestContext: who is calling, in which tenant, with which permissions.

Built by `get_request_context` in pipeline order:
tenant (from host) -> JWT -> tid == tenant -> active membership + permissions (cached).
Services receive the context and re-check permissions themselves; the AI layer and
workers build a RequestContext and call the same services.
"""

import json
import uuid
from dataclasses import dataclass, field
from typing import Literal

import structlog
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import Forbidden, Unauthorized
from app.core.redis import get_redis, perms_key
from app.core.security import decode_access_token
from app.core.tenant import TenantContext, get_tenant
from app.db.session import get_db

PERMS_CACHE_TTL_SECONDS = 5 * 60

Source = Literal["api", "ai", "system", "worker", "import"]

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class RequestContext:
    tenant_id: uuid.UUID
    user_id: uuid.UUID | None
    permissions: frozenset[str] = field(default_factory=frozenset)
    request_id: str | None = None
    ip: str | None = None
    source: Source = "api"

    def can(self, code: str) -> bool:
        return code in self.permissions

    def require(self, code: str) -> None:
        if not self.can(code):
            # TODO(M2): audit `authz.denied` here
            raise Forbidden(details={"permission": code})


async def load_permissions(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> frozenset[str]:
    from app.domains.rbac.repository import fetch_effective_permissions

    if get_settings().redis_enabled:
        redis = get_redis()
        key = perms_key(tenant_id, user_id)
        cached = await redis.get(key)
        if cached is not None:
            perms = json.loads(cached)
        else:
            async with db.begin():
                perms = await fetch_effective_permissions(db, tenant_id, user_id)
            await redis.set(key, json.dumps(perms), ex=PERMS_CACHE_TTL_SECONDS)
    else:
        async with db.begin():
            perms = await fetch_effective_permissions(db, tenant_id, user_id)
    if perms is None:  # no active membership in this tenant
        raise Unauthorized(code="auth.not_a_member")
    return frozenset(perms)


async def get_request_context(
    request: Request,
    tenant: TenantContext = Depends(get_tenant),
    db: AsyncSession = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> RequestContext:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise Unauthorized()
    claims = decode_access_token(credentials.credentials)
    # A token minted for tenant A must never work on tenant B's host.
    if claims.tenant_id != tenant.id:
        raise Unauthorized(code="auth.tenant_mismatch")

    permissions = await load_permissions(db, tenant.id, claims.user_id)
    structlog.contextvars.bind_contextvars(tenant_id=str(tenant.id), user_id=str(claims.user_id))
    return RequestContext(
        tenant_id=tenant.id,
        user_id=claims.user_id,
        permissions=permissions,
        request_id=getattr(request.state, "request_id", None),
        ip=request.client.host if request.client else None,
    )
