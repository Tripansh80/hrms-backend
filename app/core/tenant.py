"""Host -> tenant resolution.

The tenant comes ONLY from the Host header (acme.<base_domain> -> "acme", or a custom domain).
tenant_id in a body, query string or header is ignored.
"""

import json
import uuid
from dataclasses import asdict, dataclass

from fastapi import Request
from sqlalchemy import text

from app.core.config import get_settings
from app.core.errors import AppError, NotFound
from app.core.redis import get_redis, tenant_slug_key

TENANT_CACHE_TTL_SECONDS = 60


@dataclass(frozen=True, slots=True)
class TenantContext:
    id: uuid.UUID
    slug: str
    status: str
    timezone: str
    locale: str


class TenantSuspended(AppError):
    status_code, code, message = 403, "tenant.suspended", "This workspace is suspended"


@dataclass(frozen=True, slots=True)
class HostTarget:
    slug: str | None = None
    custom_domain: str | None = None

    @property
    def is_root(self) -> bool:
        return self.slug is None and self.custom_domain is None


def parse_host(host: str, base_domain: str) -> HostTarget:
    """`acme.myhrm.com` -> slug; `myhrm.com` / `www.myhrm.com` -> root; anything else -> custom domain."""
    host = host.strip().lower().rstrip(".").rsplit(":", 1)[0] if host else ""
    if not host or host in (base_domain, f"www.{base_domain}"):
        return HostTarget()
    suffix = f".{base_domain}"
    if host.endswith(suffix):
        label = host[: -len(suffix)]
        if not label or "." in label:  # nested subdomains are not tenants
            raise NotFound(code="tenant.not_found")
        return HostTarget(slug=label)
    return HostTarget(custom_domain=host)


async def _lookup(target: HostTarget) -> TenantContext | None:
    # Imported here to avoid a cycle: db.session depends on this module.
    from app.db.session import api_engine

    async with api_engine().connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT id, slug, status, timezone, locale FROM resolve_tenant(:slug, :domain)"),
                    {"slug": target.slug or "", "domain": target.custom_domain},
                )
            )
            .mappings()
            .first()
        )
    return TenantContext(**row) if row else None


async def resolve_host(host: str) -> TenantContext:
    target = parse_host(host, get_settings().base_domain)
    if target.is_root:
        raise NotFound("This endpoint is only available on a workspace domain", code="tenant.required")

    if get_settings().redis_enabled:
        redis = get_redis()
        key = tenant_slug_key(target.slug or f"domain:{target.custom_domain}")
        cached = await redis.get(key)
        if cached:
            data = json.loads(cached)
            tenant = TenantContext(**{**data, "id": uuid.UUID(data["id"])}) if data else None
        else:
            tenant = await _lookup(target)
            payload = {**asdict(tenant), "id": str(tenant.id)} if tenant else {}
            await redis.set(key, json.dumps(payload), ex=TENANT_CACHE_TTL_SECONDS)
    else:
        tenant = await _lookup(target)

    if tenant is None or tenant.status in ("pending", "cancelled"):
        raise NotFound(code="tenant.not_found")
    if tenant.status == "suspended":
        raise TenantSuspended()
    return tenant


async def get_tenant(request: Request) -> TenantContext:
    """FastAPI dependency. Cached on request.state so it resolves once per request."""
    tenant: TenantContext | None = getattr(request.state, "tenant", None)
    if tenant is None:
        tenant = await resolve_host(request.headers.get("host", ""))
        request.state.tenant = tenant
    return tenant


def require_root_domain(request: Request) -> None:
    """Signup and platform-admin endpoints live on the bare base domain only."""
    if not parse_host(request.headers.get("host", ""), get_settings().base_domain).is_root:
        raise NotFound()
