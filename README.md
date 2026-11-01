# HRMS backend

FastAPI modular monolith for the AI-first multi-tenant HRMS. The build plan is in [../implement.md](../implement.md), and the schema is in [../database/](../database/).

## Layout

```
app/
  main.py            app factory, middleware, router registration
  health.py          GET /health, GET /ready
  core/              config, security, tenant, context, permissions, errors, logging,
                     middleware, redis, ratelimit, idempotency
  db/                engines + RLS-scoped sessions, declarative base, Alembic migrations
  domains/<name>/    models.py  schemas.py  repository.py  service.py  router.py
  workers/           arq WorkerSettings + tasks/
tests/               unit/  integration/  security/  ai/
docker/              Dockerfile, compose.yml, nginx.conf, postgres-init/
scripts/             create_platform_admin.py, seed_demo.py
```

Rules:
- Routers only handle HTTP.
- Services own authorization, business rules, transactions and audit.
- Repositories own the SQL and always take `tenant_id` first.
- A domain calls another domain only through that domain's service.

## Tenant isolation in the request pipeline

1. `core/tenant.py` reads the tenant from the `Host` header and calls `resolve_tenant()`. The result is cached in Redis for 60 s.
2. `core/context.py` verifies the JWT, then checks that the token's `tid` equals the host's tenant (otherwise 401). It then loads the active membership and permissions, cached for 5 min.
3. `db/session.py` runs `set_config('app.tenant_id', …, true)` at the start of every transaction, so RLS is always on. A session with no tenant sees no rows.

## Run with Docker (recommended)

```bash
cp .env.example .env
docker compose -f docker/compose.yml up --build
```

The `migrate` service runs `alembic upgrade head`. Baseline `0001` applies `database/01_schema.sql`, `02_seed.sql` and `03_rls.sql`. The API is then at http://localhost:8000/docs.

## Run locally

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows; use bin/activate elsewhere
pip install -r requirements-dev.txt
cp .env.example .env
# PostgreSQL 16 + Redis must be running, with roles created as in docker/postgres-init/01-roles.sh
alembic upgrade head
uvicorn app.main:app --reload
arq app.workers.settings.WorkerSettings              # worker, separate terminal
```

## Tests and lint

```bash
pytest tests/unit tests/integration/test_health.py   # no services needed
ruff check . && ruff format --check . && mypy app
```

## Database logins

| Login | Group role | Used for |
|---|---|---|
| `hrms_owner` | table owner | Alembic only |
| `hrms_api` | `hrms_app` (RLS enforced) | every tenant request (`DATABASE_URL`) |
| `hrms_admin` | `hrms_platform` (BYPASSRLS) | signup, platform admin, cross-tenant jobs (`PLATFORM_DATABASE_URL`) |

The API must never connect as the table owner, because RLS does not apply to the owner.
