"""Shared fixtures. Unit tests need no services; integration tests need `docker compose up`.

TODO(M0/M1): real-PostgreSQL fixtures — each test in a rolled-back transaction,
factories make_tenant() and make_user(tenant, role).
"""

import os
from collections.abc import AsyncIterator

# Settings are read from the environment at import time; give tests safe defaults.
_TEST_ENV = {
    "ENV": "test",
    "DATABASE_URL": "postgresql+asyncpg://hrms_api:hrms_api_dev@localhost:5432/hrms_test",
    "PLATFORM_DATABASE_URL": "postgresql+asyncpg://hrms_admin:hrms_admin_dev@localhost:5432/hrms_test",
    "REDIS_URL": "redis://localhost:6379/15",
    "JWT_SECRET": "test-secret-test-secret-test-secret-0123",
    "BASE_DOMAIN": "myhrm.test",
    "S3_BUCKET": "hrms-test",
    "S3_ACCESS_KEY": "test",
    "S3_SECRET_KEY": "test",
}
for _k, _v in _TEST_ENV.items():
    os.environ.setdefault(_k, _v)

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.main import create_app  # noqa: E402


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://myhrm.test") as c:
        yield c
