-- =====================================================================
-- AI-First Multi-Tenant HRMS — ROW LEVEL SECURITY (defence in depth)
-- Run after 01_schema.sql and 02_seed.sql. Re-run after 05_phase2_schema.sql
-- (it is idempotent and picks up any new table that has a tenant_id column).
--
-- The application already scopes every query by tenant_id. RLS is the second
-- lock: if a query ever forgets its WHERE tenant_id = ..., PostgreSQL returns
-- zero rows instead of another tenant's data.
--
-- How the app uses it (once per transaction, right after tenant resolution):
--     SELECT set_config('app.tenant_id', '<tenant uuid>', true);   -- true = transaction-local
--
-- Roles
--   hrms_app      : what the API and tenant-scoped workers connect as. RLS applies.
--   hrms_platform : migrations, provision_tenant(), platform-admin operations and
--                   cross-tenant scheduled jobs. BYPASSRLS. Never used for
--                   ordinary tenant requests.
--
-- IMPORTANT: RLS does not apply to the table OWNER or to superusers. The API
-- must therefore connect as a login role that is a member of hrms_app and is
-- NOT the owner of the tables.
-- =====================================================================

BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hrms_app') THEN
    CREATE ROLE hrms_app NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hrms_platform') THEN
    CREATE ROLE hrms_platform NOLOGIN BYPASSRLS;   -- requires superuser to create
  END IF;
END $$;

-- Current tenant for this transaction. NULL when unset => policies match nothing (fail closed).
CREATE OR REPLACE FUNCTION app_tenant_id() RETURNS uuid
LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid
$$;

-- ---------------------------------------------------------------------
-- Privileges
-- ---------------------------------------------------------------------
GRANT USAGE ON SCHEMA public TO hrms_app, hrms_platform;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO hrms_app;
GRANT ALL ON ALL TABLES IN SCHEMA public TO hrms_platform;
GRANT EXECUTE ON FUNCTION provision_tenant(text, text, text, text, text, text, text, text) TO hrms_platform;
REVOKE EXECUTE ON FUNCTION provision_tenant(text, text, text, text, text, text, text, text) FROM PUBLIC;

-- Least privilege for the app role:
REVOKE UPDATE, DELETE, TRUNCATE ON audit_logs FROM hrms_app;          -- append-only
REVOKE INSERT, UPDATE, DELETE ON permissions, plans FROM hrms_app;    -- platform-managed catalogs
REVOKE INSERT, DELETE ON tenants FROM hrms_app;                       -- tenants are created via hrms_platform

-- ---------------------------------------------------------------------
-- Tenant isolation policy on every table that has a tenant_id column
-- ---------------------------------------------------------------------
DO $$
DECLARE
  t text;
BEGIN
  FOR t IN
    SELECT c.table_name
    FROM information_schema.columns c
    JOIN information_schema.tables tb
      ON tb.table_schema = c.table_schema AND tb.table_name = c.table_name
    WHERE c.table_schema = 'public'
      AND c.column_name  = 'tenant_id'
      AND tb.table_type  = 'BASE TABLE'
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);

    IF t IN ('roles', 'notification_templates') THEN
      -- shared system rows (tenant_id IS NULL) are readable by everyone,
      -- but a tenant can only write rows it owns
      EXECUTE format(
        'CREATE POLICY tenant_isolation ON %I
           USING (tenant_id IS NULL OR tenant_id = app_tenant_id())
           WITH CHECK (tenant_id = app_tenant_id())', t);
    ELSE
      EXECUTE format(
        'CREATE POLICY tenant_isolation ON %I
           USING (tenant_id = app_tenant_id())
           WITH CHECK (tenant_id = app_tenant_id())', t);
    END IF;
  END LOOP;
END $$;

-- role_permissions has no tenant_id: derive visibility from the parent role.
-- (The sub-select on roles is itself filtered by the roles policy above.)
ALTER TABLE role_permissions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON role_permissions;
CREATE POLICY tenant_isolation ON role_permissions
  USING      (EXISTS (SELECT 1 FROM roles r WHERE r.id = role_id))
  WITH CHECK (EXISTS (SELECT 1 FROM roles r WHERE r.id = role_id AND r.tenant_id = app_tenant_id()));

-- tenants: the app role may only see / update the tenant it is currently scoped to.
-- Hostname -> tenant resolution happens BEFORE the tenant is known, so it goes
-- through this SECURITY DEFINER function instead of a direct SELECT.
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON tenants;
CREATE POLICY tenant_isolation ON tenants
  USING (id = app_tenant_id()) WITH CHECK (id = app_tenant_id());

CREATE OR REPLACE FUNCTION resolve_tenant(p_host_slug text, p_custom_domain text DEFAULT NULL)
RETURNS TABLE (id uuid, slug text, status text, timezone text, locale text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT t.id, t.slug, t.status, t.timezone, t.locale
  FROM tenants t
  WHERE t.deleted_at IS NULL
    AND (t.slug = lower(p_host_slug)
         OR (p_custom_domain IS NOT NULL AND t.domain = p_custom_domain::citext))
  LIMIT 1
$$;
REVOKE EXECUTE ON FUNCTION resolve_tenant(text, text) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION resolve_tenant(text, text) TO hrms_app, hrms_platform;

-- Tables intentionally WITHOUT RLS (global, not tenant-owned):
--   users, permissions, plans
-- users is global identity; the service layer only ever exposes users through
-- tenant_memberships, which IS tenant-scoped.

COMMIT;

-- ---------------------------------------------------------------------
-- Deployment (run once per environment, as a superuser; choose real passwords
-- from your secret manager — never commit them):
--
--   CREATE ROLE hrms_api    LOGIN PASSWORD '...' IN ROLE hrms_app;
--   CREATE ROLE hrms_admin  LOGIN PASSWORD '...' IN ROLE hrms_platform;
--   ALTER ROLE hrms_admin BYPASSRLS;   -- BYPASSRLS is not inherited through membership
--
-- Smoke test (as hrms_api):
--   SELECT count(*) FROM employees;                                  -- 0 rows: no tenant set
--   BEGIN; SELECT set_config('app.tenant_id', '<tenant A>', true);
--          SELECT count(*) FROM employees;                           -- only tenant A
--          INSERT INTO locations (tenant_id, name) VALUES ('<tenant B>', 'x');  -- fails: RLS
--   ROLLBACK;
-- ---------------------------------------------------------------------
