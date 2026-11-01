"""Engines and sessions.

Two pools, two PostgreSQL logins:
  * api      -> hrms_api   (member of hrms_app, RLS enforced)   — every tenant request
  * platform -> hrms_admin (hrms_platform, BYPASSRLS)           — signup, platform admin, cross-tenant jobs

Every transaction opened on a tenant session runs
    SELECT set_config('app.tenant_id', :tid, true)
first (transaction-local, so it is PgBouncer-safe). A session without a tenant
matches no rows under RLS — it fails closed.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Depends
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, SessionTransaction

from app.core.config import get_settings
from app.core.tenant import TenantContext, get_tenant

TENANT_INFO_KEY = "tenant_id"


class TenantScopedSession(Session):
    """Sync session class behind AsyncSession; carries the tenant id in session.info."""


@event.listens_for(TenantScopedSession, "after_begin")
def _bind_tenant(session: Session, transaction: SessionTransaction, connection: Any) -> None:
    tenant_id = session.info.get(TENANT_INFO_KEY)
    if tenant_id is not None:
        connection.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})


_engines: dict[str, AsyncEngine] = {}
_api_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_platform_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _engine(name: str, url: str) -> AsyncEngine:
    if name not in _engines:
        s = get_settings()
        _engines[name] = create_async_engine(url, pool_size=s.db_pool_size, pool_pre_ping=True)
    return _engines[name]


def api_engine() -> AsyncEngine:
    return _engine("api", get_settings().database_url)


def platform_engine() -> AsyncEngine:
    return _engine("platform", get_settings().platform_database_url)


def tenant_session(tenant_id: uuid.UUID) -> AsyncSession:
    """A session whose every transaction is scoped to tenant_id. Also used by workers."""
    global _api_sessionmaker
    if _api_sessionmaker is None:
        _api_sessionmaker = async_sessionmaker(
            api_engine(), sync_session_class=TenantScopedSession, expire_on_commit=False
        )
    session = _api_sessionmaker()
    session.info[TENANT_INFO_KEY] = tenant_id
    return session


def platform_session() -> AsyncSession:
    """BYPASSRLS. Only for signup, platform admin and cross-tenant sweeps — never ordinary requests."""
    global _platform_sessionmaker
    if _platform_sessionmaker is None:
        _platform_sessionmaker = async_sessionmaker(platform_engine(), expire_on_commit=False)
    return _platform_sessionmaker()


async def get_db(tenant: TenantContext = Depends(get_tenant)) -> AsyncIterator[AsyncSession]:
    """Request-scoped tenant session. Services open their own `async with db.begin():` transactions."""
    async with tenant_session(tenant.id) as session:
        yield session


async def get_platform_db() -> AsyncIterator[AsyncSession]:
    async with platform_session() as session:
        yield session


async def dispose_engines() -> None:
    for engine in _engines.values():
        await engine.dispose()
    _engines.clear()
