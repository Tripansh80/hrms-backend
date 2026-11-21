-- =====================================================================
-- AI-First Multi-Tenant HRMS — PHASE 1 (MVP) SCHEMA
-- Target: PostgreSQL 14+
-- Run order: 01_schema.sql -> 02_seed.sql -> 03_rls.sql -> (05_phase2_schema.sql later)
--
-- Conventions
--   * UUID primary keys (gen_random_uuid()).
--   * Every tenant-owned table carries tenant_id NOT NULL.
--   * Tenant-owned parents expose UNIQUE (tenant_id, id) so children can use
--     COMPOSITE foreign keys (tenant_id, x_id) -> parent (tenant_id, id).
--     This makes cross-tenant references impossible at the database layer.
--   * Status columns are TEXT + CHECK (easier to migrate than ENUM types).
--   * created_at/updated_at on mutable entities; updated_at kept by trigger.
--   * deleted_at = soft delete for records with legal/audit retention.
-- =====================================================================

BEGIN;  

CREATE EXTENSION IF NOT EXISTS pgcrypto;    -- gen_random_uuid(), digest()
CREATE EXTENSION IF NOT EXISTS citext;      -- case-insensitive email
CREATE EXTENSION IF NOT EXISTS btree_gist;  -- exclusion constraint on leave overlap

-- ---------------------------------------------------------------------
-- Shared trigger functions
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% on % is not allowed (append-only table)', TG_OP, TG_TABLE_NAME
    USING ERRCODE = '42501';
END $$;

-- =====================================================================
-- 1. SUBSCRIPTION PLANS + TENANTS
-- =====================================================================
CREATE TABLE plans (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code             text NOT NULL UNIQUE,
  name             text NOT NULL,
  price            numeric(10,2) NOT NULL DEFAULT 0 CHECK (price >= 0),
  currency         char(3) NOT NULL DEFAULT 'USD',
  billing_interval text NOT NULL DEFAULT 'monthly'
                   CHECK (billing_interval IN ('monthly','yearly')),
  limits_json      jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {"employees": 25, "ai_commands_per_month": 100}
  features_json    jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {"recruitment": true, "ai": false}
  is_active        boolean NOT NULL DEFAULT true,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tenants (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name          text NOT NULL,
  slug          text NOT NULL UNIQUE
                CHECK (slug ~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$')
                CHECK (slug NOT IN ('www','api','app','admin','mail','static','assets','status','docs')),
  domain        citext UNIQUE,                            -- optional custom domain
  status        text NOT NULL DEFAULT 'active'
                CHECK (status IN ('pending','active','suspended','cancelled')),
  timezone      text NOT NULL DEFAULT 'UTC',
  locale        text NOT NULL DEFAULT 'en',
  branding_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  plan_id       uuid NOT NULL REFERENCES plans(id),
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  deleted_at    timestamptz
);

CREATE TABLE tenant_settings (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  uuid NOT NULL REFERENCES tenants(id),
  key        text NOT NULL,
  value_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, key)
);

CREATE TABLE subscriptions (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id          uuid NOT NULL REFERENCES tenants(id),
  plan_id            uuid NOT NULL REFERENCES plans(id),
  status             text NOT NULL DEFAULT 'trialing'
                     CHECK (status IN ('trialing','active','past_due','cancelled','expired')),
  started_at         timestamptz NOT NULL DEFAULT now(),
  current_period_end timestamptz,
  cancelled_at       timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
-- Only one live subscription per tenant.
CREATE UNIQUE INDEX uq_subscriptions_one_live
  ON subscriptions (tenant_id) WHERE status IN ('trialing','active','past_due');
CREATE INDEX ix_subscriptions_tenant ON subscriptions (tenant_id);

CREATE TABLE usage_counters (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  uuid NOT NULL REFERENCES tenants(id),
  metric     text NOT NULL,                 -- 'employees', 'ai_commands', 'imports'
  period     text NOT NULL,                 -- 'lifetime' or 'YYYY-MM'
  value      bigint NOT NULL DEFAULT 0 CHECK (value >= 0),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, metric, period)
);

-- =====================================================================
-- 2. IDENTITY & AUTHENTICATION
-- =====================================================================
CREATE TABLE users (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email              citext NOT NULL UNIQUE,          -- global identity, many tenants
  password_hash      text,                            -- NULL = SSO-only / not yet set
  full_name          text,
  is_active          boolean NOT NULL DEFAULT true,
  is_platform_admin  boolean NOT NULL DEFAULT false,  -- Platform Super Admin
  email_verified_at  timestamptz,
  failed_login_count integer NOT NULL DEFAULT 0 CHECK (failed_login_count >= 0),
  locked_until       timestamptz,
  last_login_at      timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tenant_memberships (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  uuid NOT NULL REFERENCES tenants(id),
  user_id    uuid NOT NULL REFERENCES users(id),
  status     text NOT NULL DEFAULT 'active'
             CHECK (status IN ('invited','active','suspended','removed')),
  is_owner   boolean NOT NULL DEFAULT false,
  invited_by uuid REFERENCES users(id),
  joined_at  timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, user_id)
);
CREATE INDEX ix_memberships_user ON tenant_memberships (user_id);

-- Refresh tokens = sessions. Only the SHA-256 hash of the token is stored.
-- family_id supports rotation + reuse detection (revoke the whole family).
CREATE TABLE refresh_tokens (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL,
  user_id     uuid NOT NULL,
  family_id   uuid NOT NULL,
  token_hash  text NOT NULL UNIQUE,
  issued_at   timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL,
  revoked_at  timestamptz,
  replaced_by uuid REFERENCES refresh_tokens(id),
  ip_address  inet,
  user_agent  text,
  FOREIGN KEY (tenant_id, user_id) REFERENCES tenant_memberships (tenant_id, user_id)
);
CREATE INDEX ix_refresh_tokens_user   ON refresh_tokens (tenant_id, user_id) WHERE revoked_at IS NULL;
CREATE INDEX ix_refresh_tokens_family ON refresh_tokens (family_id);

-- One-time tokens: password reset, email verification, invitations.
CREATE TABLE auth_tokens (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES users(id),
  tenant_id  uuid REFERENCES tenants(id),
  purpose    text NOT NULL CHECK (purpose IN ('password_reset','email_verification','invitation')),
  token_hash text NOT NULL UNIQUE,
  expires_at timestamptz NOT NULL,
  used_at    timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_auth_tokens_user ON auth_tokens (user_id, purpose);

-- =====================================================================
-- 3. RBAC
-- =====================================================================
CREATE TABLE permissions (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code        text NOT NULL UNIQUE,        -- 'employees.create'
  description text NOT NULL DEFAULT ''
);

-- tenant_id NULL  = system role, shared by every tenant, immutable by tenants.
-- tenant_id set   = custom role owned by that tenant.
-- data_scope narrows WHICH rows a permission applies to (enforced in services).
CREATE TABLE roles (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid REFERENCES tenants(id),
  name        text NOT NULL,
  description text NOT NULL DEFAULT '',
  is_system   boolean NOT NULL DEFAULT false,
  data_scope  text NOT NULL DEFAULT 'all'
              CHECK (data_scope IN ('all','department','team','self')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  CHECK ((tenant_id IS NULL) = is_system)
);
CREATE UNIQUE INDEX uq_roles_system_name ON roles (name) WHERE tenant_id IS NULL;
CREATE UNIQUE INDEX uq_roles_tenant_name ON roles (tenant_id, name) WHERE tenant_id IS NOT NULL;

CREATE TABLE role_permissions (
  role_id       uuid NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
  permission_id uuid NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
  PRIMARY KEY (role_id, permission_id)
);
CREATE INDEX ix_role_permissions_permission ON role_permissions (permission_id);

CREATE TABLE user_roles (
  tenant_id  uuid NOT NULL,
  user_id    uuid NOT NULL,
  role_id    uuid NOT NULL REFERENCES roles(id),
  granted_by uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, user_id, role_id),
  -- a role can only be granted to someone who is a member of that tenant
  FOREIGN KEY (tenant_id, user_id)
    REFERENCES tenant_memberships (tenant_id, user_id) ON DELETE CASCADE
);
CREATE INDEX ix_user_roles_role ON user_roles (role_id);

-- A user may only hold system roles or roles owned by the SAME tenant.
CREATE OR REPLACE FUNCTION check_user_role_tenant() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_role_tenant uuid;
BEGIN
  SELECT tenant_id INTO v_role_tenant FROM roles WHERE id = NEW.role_id;
  IF NOT FOUND OR (v_role_tenant IS NOT NULL AND v_role_tenant <> NEW.tenant_id) THEN
    RAISE EXCEPTION 'role % is not assignable in tenant %', NEW.role_id, NEW.tenant_id
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER trg_user_roles_tenant
  BEFORE INSERT OR UPDATE ON user_roles
  FOR EACH ROW EXECUTE FUNCTION check_user_role_tenant();

-- =====================================================================
-- 4. ORGANIZATION
-- =====================================================================
CREATE TABLE locations (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES tenants(id),
  name         text NOT NULL,
  address      text,
  country_code char(2),
  timezone     text NOT NULL DEFAULT 'UTC',
  is_active    boolean NOT NULL DEFAULT true,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, name)
);

CREATE TABLE departments (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  uuid NOT NULL REFERENCES tenants(id),
  name       text NOT NULL,
  code       text NOT NULL,
  parent_id  uuid,
  manager_id uuid,                          -- FK to employees added below
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, code),
  CHECK (parent_id IS NULL OR parent_id <> id),
  FOREIGN KEY (tenant_id, parent_id) REFERENCES departments (tenant_id, id)
);
CREATE INDEX ix_departments_parent ON departments (tenant_id, parent_id);

CREATE TABLE work_schedules (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  uuid NOT NULL REFERENCES tenants(id),
  name       text NOT NULL,
  timezone   text NOT NULL DEFAULT 'UTC',
  -- {"working_days":[1,2,3,4,5],"start":"09:00","end":"18:00","grace_minutes":15,"half_day_minutes":240}
  rules_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  is_default boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, name)
);
CREATE UNIQUE INDEX uq_work_schedules_default ON work_schedules (tenant_id) WHERE is_default;

CREATE TABLE holidays (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES tenants(id),
  location_id  uuid,                        -- NULL = applies to all locations
  name         text NOT NULL,
  holiday_date date NOT NULL,
  is_optional  boolean NOT NULL DEFAULT false,
  created_at   timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, location_id) REFERENCES locations (tenant_id, id)
);
CREATE UNIQUE INDEX uq_holidays ON holidays
  (tenant_id, holiday_date, COALESCE(location_id, '00000000-0000-0000-0000-000000000000'::uuid), name);

-- =====================================================================
-- 5. EMPLOYEES
-- =====================================================================
CREATE TABLE employees (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES tenants(id),
  user_id          uuid,                    -- optional login account
  employee_no      text NOT NULL,
  first_name       text NOT NULL,
  last_name        text NOT NULL DEFAULT '',
  email            citext NOT NULL,
  phone            text,
  department_id    uuid,
  manager_id       uuid,
  location_id      uuid,
  work_schedule_id uuid,
  designation      text,
  employment_type  text NOT NULL DEFAULT 'full_time'
                   CHECK (employment_type IN ('full_time','part_time','contract','intern')),
  status           text NOT NULL DEFAULT 'onboarding'
                   CHECK (status IN ('onboarding','active','on_notice','suspended','exited')),
  joining_date     date NOT NULL,
  exit_date        date,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  deleted_at       timestamptz,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, employee_no),
  UNIQUE (tenant_id, email),
  CHECK (manager_id IS NULL OR manager_id <> id),
  CHECK (exit_date IS NULL OR exit_date >= joining_date),
  -- composite FKs: every reference is forced to stay inside the same tenant
  FOREIGN KEY (tenant_id, user_id)          REFERENCES tenant_memberships (tenant_id, user_id),
  FOREIGN KEY (tenant_id, department_id)    REFERENCES departments (tenant_id, id),
  FOREIGN KEY (tenant_id, manager_id)       REFERENCES employees (tenant_id, id),
  FOREIGN KEY (tenant_id, location_id)      REFERENCES locations (tenant_id, id),
  FOREIGN KEY (tenant_id, work_schedule_id) REFERENCES work_schedules (tenant_id, id)
);
CREATE UNIQUE INDEX uq_employees_user     ON employees (tenant_id, user_id) WHERE user_id IS NOT NULL;
CREATE INDEX ix_employees_department      ON employees (tenant_id, department_id);
CREATE INDEX ix_employees_manager         ON employees (tenant_id, manager_id);
CREATE INDEX ix_employees_status          ON employees (tenant_id, status);
CREATE INDEX ix_employees_name            ON employees (tenant_id, lower(last_name), lower(first_name));

ALTER TABLE departments
  ADD CONSTRAINT fk_departments_manager
  FOREIGN KEY (tenant_id, manager_id) REFERENCES employees (tenant_id, id);

CREATE TABLE employee_history (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  employee_id   uuid NOT NULL,
  event_type    text NOT NULL,   -- hired, promoted, transferred, manager_changed, status_changed, exited
  effective_at  timestamptz NOT NULL DEFAULT now(),
  old_data      jsonb,
  new_data      jsonb,
  actor_user_id uuid REFERENCES users(id),
  created_at    timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, employee_id) REFERENCES employees (tenant_id, id)
);
CREATE INDEX ix_employee_history ON employee_history (tenant_id, employee_id, effective_at DESC);

-- =====================================================================
-- 6. ATTENDANCE
-- =====================================================================
CREATE TABLE attendance_records (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      uuid NOT NULL REFERENCES tenants(id),
  employee_id    uuid NOT NULL,
  work_date      date NOT NULL,
  check_in       timestamptz,
  check_out      timestamptz,
  worked_minutes integer CHECK (worked_minutes IS NULL OR worked_minutes >= 0),
  status         text NOT NULL DEFAULT 'present'
                 CHECK (status IN ('present','absent','half_day','on_leave','holiday','weekend')),
  source         text NOT NULL DEFAULT 'web'
                 CHECK (source IN ('web','mobile','biometric','import','correction','system','ai')),
  notes          text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, employee_id, work_date),
  CHECK (check_out IS NULL OR (check_in IS NOT NULL AND check_out >= check_in)),
  FOREIGN KEY (tenant_id, employee_id) REFERENCES employees (tenant_id, id)
);
CREATE INDEX ix_attendance_work_date ON attendance_records (tenant_id, work_date);

CREATE TABLE attendance_corrections (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            uuid NOT NULL REFERENCES tenants(id),
  employee_id          uuid NOT NULL,
  attendance_record_id uuid,
  work_date            date NOT NULL,
  requested_check_in   timestamptz,
  requested_check_out  timestamptz,
  reason               text NOT NULL,
  status               text NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','approved','rejected','cancelled')),
  decided_by           uuid REFERENCES users(id),
  decided_at           timestamptz,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  CHECK (requested_check_out IS NULL OR requested_check_in IS NULL
         OR requested_check_out >= requested_check_in),
  FOREIGN KEY (tenant_id, employee_id)          REFERENCES employees (tenant_id, id),
  FOREIGN KEY (tenant_id, attendance_record_id) REFERENCES attendance_records (tenant_id, id)
);
CREATE INDEX ix_attendance_corrections ON attendance_corrections (tenant_id, status, created_at);

-- =====================================================================
-- 7. LEAVE
-- =====================================================================
CREATE TABLE leave_types (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  uuid NOT NULL REFERENCES tenants(id),
  name       text NOT NULL,
  code       text NOT NULL,
  unit       text NOT NULL DEFAULT 'day' CHECK (unit IN ('day','half_day','hour')),
  -- {"annual_allocation":18,"max_consecutive_days":10,"min_notice_days":2,
  --  "carry_forward_max":5,"allow_negative":false,"requires_document_after_days":3}
  rules_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  is_paid    boolean NOT NULL DEFAULT true,
  is_active  boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, code)
);

CREATE TABLE leave_balances (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  employee_id   uuid NOT NULL,
  leave_type_id uuid NOT NULL,
  year          integer NOT NULL CHECK (year BETWEEN 2000 AND 2100),
  allocated     numeric(6,2) NOT NULL DEFAULT 0 CHECK (allocated >= 0),
  used          numeric(6,2) NOT NULL DEFAULT 0 CHECK (used >= 0),
  pending       numeric(6,2) NOT NULL DEFAULT 0 CHECK (pending >= 0),
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, employee_id, leave_type_id, year),
  FOREIGN KEY (tenant_id, employee_id)   REFERENCES employees (tenant_id, id),
  FOREIGN KEY (tenant_id, leave_type_id) REFERENCES leave_types (tenant_id, id)
);

CREATE TABLE leave_requests (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL REFERENCES tenants(id),
  employee_id         uuid NOT NULL,
  leave_type_id       uuid NOT NULL,
  start_date          date NOT NULL,
  end_date            date NOT NULL,
  days                numeric(5,2) NOT NULL CHECK (days > 0),   -- working days, computed server-side
  reason              text,
  status              text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','approved','rejected','cancelled')),
  current_approver_id uuid,                -- user id of whoever must act next
  decided_by          uuid REFERENCES users(id),
  decided_at          timestamptz,
  decision_note       text,
  source              text NOT NULL DEFAULT 'api' CHECK (source IN ('api','ai','import')),
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  CHECK (end_date >= start_date),
  FOREIGN KEY (tenant_id, employee_id)         REFERENCES employees (tenant_id, id),
  FOREIGN KEY (tenant_id, leave_type_id)       REFERENCES leave_types (tenant_id, id),
  FOREIGN KEY (tenant_id, current_approver_id) REFERENCES tenant_memberships (tenant_id, user_id),
  -- an employee can never have two live requests covering the same day
  CONSTRAINT ex_leave_requests_no_overlap EXCLUDE USING gist (
    tenant_id   WITH =,
    employee_id WITH =,
    daterange(start_date, end_date, '[]') WITH &&
  ) WHERE (status IN ('pending','approved'))
);
CREATE INDEX ix_leave_requests_employee ON leave_requests (tenant_id, employee_id, start_date);
CREATE INDEX ix_leave_requests_status   ON leave_requests (tenant_id, status);
CREATE INDEX ix_leave_requests_approver ON leave_requests (tenant_id, current_approver_id)
  WHERE status = 'pending';

-- =====================================================================
-- 8. RECRUITMENT
-- =====================================================================
CREATE TABLE candidates (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id          uuid NOT NULL REFERENCES tenants(id),
  name               text NOT NULL,
  email              citext NOT NULL,
  phone              text,
  resume_document_id uuid,                 -- FK to documents added below
  source             text,                 -- referral, linkedin, careers_page ...
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  deleted_at         timestamptz,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, email)
);

CREATE TABLE jobs (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id),
  department_id   uuid,
  location_id     uuid,
  title           text NOT NULL,
  description     text NOT NULL DEFAULT '',
  status          text NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft','open','on_hold','closed','filled')),
  employment_type text NOT NULL DEFAULT 'full_time'
                  CHECK (employment_type IN ('full_time','part_time','contract','intern')),
  openings        integer NOT NULL DEFAULT 1 CHECK (openings > 0),
  created_by      uuid NOT NULL REFERENCES users(id),
  opened_at       timestamptz,
  closed_at       timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, department_id) REFERENCES departments (tenant_id, id),
  FOREIGN KEY (tenant_id, location_id)   REFERENCES locations (tenant_id, id)
);
CREATE INDEX ix_jobs_status ON jobs (tenant_id, status);

CREATE TABLE applications (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id),
  job_id            uuid NOT NULL,
  candidate_id      uuid NOT NULL,
  status            text NOT NULL DEFAULT 'applied'
                    CHECK (status IN ('applied','screening','interview','offer','hired','rejected','withdrawn')),
  source            text,
  hired_employee_id uuid,                  -- set on hiring conversion
  applied_at        timestamptz NOT NULL DEFAULT now(),
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, job_id, candidate_id),      -- no duplicate application to the same job
  CHECK (hired_employee_id IS NULL OR status = 'hired'),
  FOREIGN KEY (tenant_id, job_id)            REFERENCES jobs (tenant_id, id),
  FOREIGN KEY (tenant_id, candidate_id)      REFERENCES candidates (tenant_id, id),
  FOREIGN KEY (tenant_id, hired_employee_id) REFERENCES employees (tenant_id, id)
);
CREATE INDEX ix_applications_job       ON applications (tenant_id, job_id, status);
CREATE INDEX ix_applications_candidate ON applications (tenant_id, candidate_id);

CREATE TABLE interviews (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES tenants(id),
  application_id   uuid NOT NULL,
  scheduled_at     timestamptz NOT NULL,
  duration_minutes integer NOT NULL DEFAULT 60 CHECK (duration_minutes > 0),
  interviewer_id   uuid NOT NULL,          -- user id
  status           text NOT NULL DEFAULT 'scheduled'
                   CHECK (status IN ('scheduled','completed','cancelled','no_show')),
  rating           smallint CHECK (rating BETWEEN 1 AND 5),
  feedback         text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, application_id) REFERENCES applications (tenant_id, id),
  FOREIGN KEY (tenant_id, interviewer_id) REFERENCES tenant_memberships (tenant_id, user_id)
);
CREATE INDEX ix_interviews_application ON interviews (tenant_id, application_id);
CREATE INDEX ix_interviews_interviewer ON interviews (tenant_id, interviewer_id, scheduled_at);

CREATE TABLE offers (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      uuid NOT NULL REFERENCES tenants(id),
  application_id uuid NOT NULL,
  salary_json    jsonb NOT NULL,           -- {"currency":"INR","base":1800000,"bonus":200000}
  joining_date   date,
  status         text NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft','sent','accepted','declined','expired','withdrawn')),
  offered_at     timestamptz,
  expires_at     timestamptz,
  created_by     uuid REFERENCES users(id),
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  CHECK (expires_at IS NULL OR offered_at IS NULL OR expires_at > offered_at),
  FOREIGN KEY (tenant_id, application_id) REFERENCES applications (tenant_id, id)
);
-- at most one live offer per application
CREATE UNIQUE INDEX uq_offers_one_live ON offers (tenant_id, application_id)
  WHERE status IN ('draft','sent','accepted');
CREATE INDEX ix_offers_application ON offers (tenant_id, application_id);

-- =====================================================================
-- 9. DOCUMENTS & POLICIES
-- =====================================================================
-- Metadata only. File bytes live in object storage under storage_key.
CREATE TABLE documents (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  employee_id   uuid,
  candidate_id  uuid,
  document_type text NOT NULL,             -- id_proof, contract, resume, certificate, tenant_doc ...
  title         text NOT NULL,
  storage_key   text NOT NULL UNIQUE,      -- always prefixed with tenants/{tenant_id}/
  file_name     text NOT NULL,
  mime_type     text NOT NULL,
  size_bytes    bigint NOT NULL CHECK (size_bytes >= 0),
  checksum      text,
  status        text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','verified','rejected','expired')),
  expires_at    timestamptz,
  uploaded_by   uuid REFERENCES users(id),
  verified_by   uuid REFERENCES users(id),
  verified_at   timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  deleted_at    timestamptz,
  UNIQUE (tenant_id, id),
  CHECK (NOT (employee_id IS NOT NULL AND candidate_id IS NOT NULL)),
  FOREIGN KEY (tenant_id, employee_id)  REFERENCES employees (tenant_id, id),
  FOREIGN KEY (tenant_id, candidate_id) REFERENCES candidates (tenant_id, id)
);
CREATE INDEX ix_documents_employee  ON documents (tenant_id, employee_id);
CREATE INDEX ix_documents_candidate ON documents (tenant_id, candidate_id);
CREATE INDEX ix_documents_expiry    ON documents (tenant_id, expires_at) WHERE expires_at IS NOT NULL;

ALTER TABLE candidates
  ADD CONSTRAINT fk_candidates_resume
  FOREIGN KEY (tenant_id, resume_document_id) REFERENCES documents (tenant_id, id);

CREATE TABLE policies (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  uuid NOT NULL REFERENCES tenants(id),
  title      text NOT NULL,
  status     text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','archived')),
  created_by uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, title)
);

CREATE TABLE policy_versions (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES tenants(id),
  policy_id    uuid NOT NULL,
  version      integer NOT NULL CHECK (version > 0),
  content      text NOT NULL,
  published_at timestamptz,
  created_by   uuid REFERENCES users(id),
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, policy_id, version),
  FOREIGN KEY (tenant_id, policy_id) REFERENCES policies (tenant_id, id)
);

CREATE TABLE policy_acknowledgements (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id),
  policy_version_id uuid NOT NULL,
  employee_id       uuid NOT NULL,
  acknowledged_at   timestamptz NOT NULL DEFAULT now(),
  ip_address        inet,
  UNIQUE (tenant_id, policy_version_id, employee_id),
  FOREIGN KEY (tenant_id, policy_version_id) REFERENCES policy_versions (tenant_id, id),
  FOREIGN KEY (tenant_id, employee_id)       REFERENCES employees (tenant_id, id)
);
CREATE INDEX ix_policy_ack_employee ON policy_acknowledgements (tenant_id, employee_id);

-- =====================================================================
-- 10. WORKFLOW ENGINE
-- =====================================================================
CREATE TABLE workflow_definitions (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id),
  name        text NOT NULL,
  entity_type text NOT NULL,               -- leave_request, attendance_correction, employee_onboarding, offer
  status      text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active','archived')),
  created_by  uuid REFERENCES users(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, name)
);
-- one ACTIVE definition per entity type per tenant
CREATE UNIQUE INDEX uq_workflow_definitions_active
  ON workflow_definitions (tenant_id, entity_type) WHERE status = 'active';

CREATE TABLE workflow_versions (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id              uuid NOT NULL REFERENCES tenants(id),
  workflow_definition_id uuid NOT NULL,
  version                integer NOT NULL CHECK (version > 0),
  definition_json        jsonb NOT NULL,   -- see implement.md "Workflow definition format"
  published_at           timestamptz,      -- NULL = still a draft, editable
  created_by             uuid REFERENCES users(id),
  created_at             timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, workflow_definition_id, version),
  FOREIGN KEY (tenant_id, workflow_definition_id) REFERENCES workflow_definitions (tenant_id, id)
);

-- Published workflow versions are immutable: create a new version instead.
CREATE OR REPLACE FUNCTION protect_published_workflow_version() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.published_at IS NOT NULL THEN
    RAISE EXCEPTION 'workflow version % is published and immutable', OLD.id
      USING ERRCODE = '42501';
  END IF;
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER trg_workflow_versions_immutable
  BEFORE UPDATE OR DELETE ON workflow_versions
  FOR EACH ROW EXECUTE FUNCTION protect_published_workflow_version();

CREATE TABLE workflow_executions (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL REFERENCES tenants(id),
  workflow_version_id uuid NOT NULL,
  entity_type         text NOT NULL,
  entity_id           uuid NOT NULL,       -- polymorphic: validated by the service layer
  status              text NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running','completed','rejected','cancelled','failed')),
  current_step        text,
  context_json        jsonb NOT NULL DEFAULT '{}'::jsonb,
  error               text,
  started_by          uuid REFERENCES users(id),
  started_at          timestamptz NOT NULL DEFAULT now(),
  completed_at        timestamptz,
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, workflow_version_id) REFERENCES workflow_versions (tenant_id, id)
);
-- one running execution per business entity (prevents double-start on retry)
CREATE UNIQUE INDEX uq_workflow_executions_running
  ON workflow_executions (tenant_id, entity_type, entity_id) WHERE status = 'running';
CREATE INDEX ix_workflow_executions_entity ON workflow_executions (tenant_id, entity_type, entity_id);
CREATE INDEX ix_workflow_executions_status ON workflow_executions (tenant_id, status);

CREATE TABLE workflow_tasks (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES tenants(id),
  execution_id     uuid NOT NULL,
  step_key         text NOT NULL,
  assignee_user_id uuid,
  task_type        text NOT NULL DEFAULT 'approval'
                   CHECK (task_type IN ('approval','action','notification')),
  status           text NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','approved','rejected','completed','skipped','cancelled','expired')),
  comment          text,
  due_at           timestamptz,
  completed_by     uuid REFERENCES users(id),
  completed_at     timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, execution_id)     REFERENCES workflow_executions (tenant_id, id),
  FOREIGN KEY (tenant_id, assignee_user_id) REFERENCES tenant_memberships (tenant_id, user_id)
);
CREATE INDEX ix_workflow_tasks_assignee  ON workflow_tasks (tenant_id, assignee_user_id, status);
CREATE INDEX ix_workflow_tasks_execution ON workflow_tasks (tenant_id, execution_id);
CREATE INDEX ix_workflow_tasks_due       ON workflow_tasks (due_at) WHERE status = 'pending';

-- =====================================================================
-- 11. NOTIFICATIONS
-- =====================================================================
-- tenant_id NULL = platform default template; a tenant row with the same code overrides it.
CREATE TABLE notification_templates (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  uuid REFERENCES tenants(id),
  code       text NOT NULL,                -- 'leave.approved'
  channel    text NOT NULL CHECK (channel IN ('email','in_app')),
  subject    text NOT NULL DEFAULT '',
  body       text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_notification_templates_system
  ON notification_templates (code, channel) WHERE tenant_id IS NULL;
CREATE UNIQUE INDEX uq_notification_templates_tenant
  ON notification_templates (tenant_id, code, channel) WHERE tenant_id IS NOT NULL;

CREATE TABLE notifications (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES tenants(id),
  user_id      uuid NOT NULL,
  channel      text NOT NULL DEFAULT 'in_app' CHECK (channel IN ('email','in_app')),
  type         text NOT NULL,              -- event type, e.g. 'leave.approved'
  template     text NOT NULL,              -- notification_templates.code
  payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  status       text NOT NULL DEFAULT 'queued'
               CHECK (status IN ('queued','sending','sent','failed','read')),
  attempts     integer NOT NULL DEFAULT 0,
  last_error   text,
  dedupe_key   text,                       -- makes worker retries idempotent
  sent_at      timestamptz,
  read_at      timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, user_id) REFERENCES tenant_memberships (tenant_id, user_id)
);
CREATE UNIQUE INDEX uq_notifications_dedupe ON notifications (tenant_id, dedupe_key)
  WHERE dedupe_key IS NOT NULL;
CREATE INDEX ix_notifications_user  ON notifications (tenant_id, user_id, status, created_at DESC);
CREATE INDEX ix_notifications_queue ON notifications (created_at) WHERE status = 'queued';

CREATE TABLE notification_preferences (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  uuid NOT NULL REFERENCES tenants(id),
  user_id    uuid NOT NULL,
  channel    text NOT NULL CHECK (channel IN ('email','in_app')),
  event_type text NOT NULL,
  enabled    boolean NOT NULL DEFAULT true,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, user_id, channel, event_type),
  FOREIGN KEY (tenant_id, user_id) REFERENCES tenant_memberships (tenant_id, user_id)
);

-- =====================================================================
-- 12. AUDIT (append-only)
-- =====================================================================
CREATE TABLE audit_logs (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid REFERENCES tenants(id),   -- NULL only for platform-level events
  actor_user_id uuid REFERENCES users(id),     -- NULL for system/worker actions
  action        text NOT NULL,                 -- 'employee.created', 'auth.login_failed'
  entity_type   text,
  entity_id     uuid,
  source        text NOT NULL DEFAULT 'api'
                CHECK (source IN ('api','ai','system','worker','import')),
  before_json   jsonb,
  after_json    jsonb,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,   -- AI: intent, command id, result
  ip_address    inet,
  request_id    text,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_logs_created ON audit_logs (tenant_id, created_at DESC);
CREATE INDEX ix_audit_logs_entity  ON audit_logs (tenant_id, entity_type, entity_id);
CREATE INDEX ix_audit_logs_actor   ON audit_logs (tenant_id, actor_user_id, created_at DESC);

CREATE TRIGGER trg_audit_logs_append_only
  BEFORE UPDATE OR DELETE ON audit_logs
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- =====================================================================
-- 13. BULK IMPORT
-- =====================================================================
CREATE TABLE import_jobs (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES tenants(id),
  created_by       uuid NOT NULL REFERENCES users(id),
  entity_type      text NOT NULL CHECK (entity_type IN ('employees','attendance','leave_balances','candidates')),
  file_key         text NOT NULL,
  file_name        text,
  status           text NOT NULL DEFAULT 'queued'
                   CHECK (status IN ('queued','processing','completed','completed_with_errors','failed','cancelled')),
  total_rows       integer NOT NULL DEFAULT 0 CHECK (total_rows >= 0),
  processed_rows   integer NOT NULL DEFAULT 0 CHECK (processed_rows >= 0),
  failed_rows      integer NOT NULL DEFAULT 0 CHECK (failed_rows >= 0),
  error_report_key text,                   -- downloadable CSV of failed rows
  error            text,                   -- fatal job-level error
  started_at       timestamptz,
  finished_at      timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  CHECK (failed_rows <= processed_rows)
);
CREATE INDEX ix_import_jobs_status ON import_jobs (tenant_id, status, created_at);

CREATE TABLE import_errors (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  import_job_id uuid NOT NULL,
  row_number    integer NOT NULL,
  field         text,
  message       text NOT NULL,
  raw_data      jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, import_job_id) REFERENCES import_jobs (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX ix_import_errors_job ON import_errors (tenant_id, import_job_id, row_number);

-- =====================================================================
-- 14. IDEMPOTENCY + AI COMMANDS + REPORT EXPORTS
-- =====================================================================
CREATE TABLE idempotency_keys (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id),
  user_id         uuid NOT NULL REFERENCES users(id),
  key             text NOT NULL,
  request_hash    text NOT NULL,           -- same key + different body => 409
  response_status integer,
  response_json   jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  expires_at      timestamptz NOT NULL DEFAULT now() + interval '24 hours',
  UNIQUE (tenant_id, user_id, key)
);
CREATE INDEX ix_idempotency_expiry ON idempotency_keys (expires_at);

-- Every AI request is recorded, including the ones that were refused.
-- tenant_id / user_id always come from the authenticated request, never the model.
CREATE TABLE ai_commands (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id             uuid NOT NULL REFERENCES tenants(id),
  user_id               uuid NOT NULL,
  message               text NOT NULL,     -- the user's natural-language request
  intent                text,              -- CREATE_EMPLOYEE, REQUEST_LEAVE ...
  schema_version        text,
  arguments_json        jsonb,
  requires_confirmation boolean NOT NULL DEFAULT false,
  status                text NOT NULL DEFAULT 'received'
                        CHECK (status IN ('received','rejected_schema','rejected_permission',
                                          'rejected_validation','awaiting_confirmation',
                                          'executed','failed','cancelled','expired')),
  result_json           jsonb,
  error                 text,
  model                 text,
  input_tokens          integer,
  output_tokens         integer,
  confirm_expires_at    timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, user_id) REFERENCES tenant_memberships (tenant_id, user_id)
);
CREATE INDEX ix_ai_commands_user   ON ai_commands (tenant_id, user_id, created_at DESC);
CREATE INDEX ix_ai_commands_status ON ai_commands (tenant_id, status);

CREATE TABLE report_exports (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES tenants(id),
  requested_by uuid NOT NULL REFERENCES users(id),
  report_type  text NOT NULL,              -- headcount, attendance_summary, leave_summary, hiring_pipeline
  params_json  jsonb NOT NULL DEFAULT '{}'::jsonb,
  status       text NOT NULL DEFAULT 'queued'
               CHECK (status IN ('queued','processing','completed','failed')),
  file_key     text,
  error        text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz
);
CREATE INDEX ix_report_exports ON report_exports (tenant_id, requested_by, created_at DESC);

-- =====================================================================
-- 15. Attach the updated_at trigger to every table that has the column
-- =====================================================================
DO $$
DECLARE
  t text;
BEGIN
  FOR t IN
    SELECT c.table_name
    FROM information_schema.columns c
    JOIN information_schema.tables tb
      ON tb.table_schema = c.table_schema AND tb.table_name = c.table_name
    WHERE c.table_schema = current_schema()
      AND c.column_name  = 'updated_at'
      AND tb.table_type  = 'BASE TABLE'
  LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS trg_set_updated_at ON %I', t);
    EXECUTE format(
      'CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON %I
         FOR EACH ROW EXECUTE FUNCTION set_updated_at()', t);
  END LOOP;
END $$;

COMMIT;
