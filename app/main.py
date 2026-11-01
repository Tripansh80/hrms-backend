"""App factory: middleware, exception handlers, router registration."""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import health
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.redis import close_redis
from app.db.session import dispose_engines
from app.domains.ai.router import router as ai_router
from app.domains.attendance.router import router as attendance_router
from app.domains.audit.router import router as audit_router
from app.domains.auth.router import router as auth_router
from app.domains.documents.router import router as documents_router
from app.domains.employees.router import router as employees_router
from app.domains.imports.router import router as imports_router
from app.domains.leave.router import router as leave_router
from app.domains.notifications.router import router as notifications_router
from app.domains.organization.router import router as organization_router
from app.domains.policies.router import router as policies_router
from app.domains.rbac.router import router as rbac_router
from app.domains.recruitment.router import router as recruitment_router
from app.domains.reports.router import router as reports_router
from app.domains.subscriptions.router import pricing_router
from app.domains.subscriptions.router import router as subscriptions_router
from app.domains.tenants.router import memberships_router
from app.domains.tenants.router import router as tenants_router
from app.domains.tasks.router import router as tasks_router
from app.domains.workflows.router import router as workflows_router

DOMAIN_ROUTERS = [
    tenants_router,
    memberships_router,
    auth_router,
    rbac_router,
    organization_router,
    employees_router,
    attendance_router,
    leave_router,
    workflows_router,
    notifications_router,
    audit_router,
    imports_router,
    recruitment_router,
    documents_router,
    policies_router,
    ai_router,
    subscriptions_router,
    pricing_router,
    reports_router,
    tasks_router,
]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engines()
    await close_redis()


def create_app() -> FastAPI:
    settings = get_settings()  # fails fast if a required setting is missing
    configure_logging(settings.log_level, json=settings.env != "dev")

    app = FastAPI(
        title="HRMS API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.env != "prod" else None,
        redoc_url=None,
    )

    # CORS: explicit origins + any https://<tenant>.<base_domain>. Never "*" with credentials.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_origin_regex=rf"https://([a-z0-9-]+\.)?{re.escape(settings.base_domain)}",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestContextMiddleware)  # outermost: every log line has request_id
    register_exception_handlers(app)

    app.include_router(health.router)
    for router in DOMAIN_ROUTERS:
        app.include_router(router)
    return app


app = create_app()
