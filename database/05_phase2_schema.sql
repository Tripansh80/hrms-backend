-- =====================================================================
-- AI-First Multi-Tenant HRMS — PHASE 2 SCHEMA (additive only)
-- Target: PostgreSQL 14+. Run after Phase 1 is in production, then re-run
-- 03_rls.sql so the new tenant-owned tables get their isolation policy.
--
-- Nothing here changes or drops a Phase 1 column: every statement is a new
-- table or a nullable/defaulted column, so it can ship without downtime.
-- Each section maps to one Phase 2 workstream in implement.md and can be
-- applied on its own (split into separate Alembic revisions).
-- =====================================================================

BEGIN;

-- =====================================================================
-- P2-1. SSO (SAML / OIDC) + ADVANCED IDENTITY LIFECYCLE (SCIM)
-- =====================================================================
CREATE TABLE identity_providers (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES tenants(id),
  name             text NOT NULL,
  protocol         text NOT NULL CHECK (protocol IN ('oidc','saml')),
  -- OIDC: issuer, client_id, scopes. SAML: entity_id, sso_url, x509 cert.
  -- Client secrets are NOT stored here: secret_ref points into the secret manager.
  config_json      jsonb NOT NULL,
  secret_ref       text,
  allowed_domains  text[] NOT NULL DEFAULT '{}',     -- email domains allowed to use this IdP
  default_role_id  uuid REFERENCES roles(id),        -- role for just-in-time provisioned users
  jit_provisioning boolean NOT NULL DEFAULT false,
  enforce_sso      boolean NOT NULL DEFAULT false,   -- true = password login disabled for the tenant
  is_active        boolean NOT NULL DEFAULT true,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, name)
);

-- Links a global user to an external subject at one tenant's IdP.
CREATE TABLE user_identities (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            uuid NOT NULL REFERENCES tenants(id),
  user_id              uuid NOT NULL,
  identity_provider_id uuid NOT NULL,
  external_subject     text NOT NULL,                -- OIDC "sub" / SAML NameID
  last_login_at        timestamptz,
  created_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, identity_provider_id, external_subject),
  UNIQUE (tenant_id, identity_provider_id, user_id),
  FOREIGN KEY (tenant_id, user_id)              REFERENCES tenant_memberships (tenant_id, user_id),
  FOREIGN KEY (tenant_id, identity_provider_id) REFERENCES identity_providers (tenant_id, id)
);

-- SCIM bearer tokens for automated provisioning/deprovisioning from the IdP.
CREATE TABLE scim_tokens (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES tenants(id),
  name         text NOT NULL,
  token_hash   text NOT NULL UNIQUE,
  created_by   uuid REFERENCES users(id),
  last_used_at timestamptz,
  revoked_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_scim_tokens_tenant ON scim_tokens (tenant_id) WHERE revoked_at IS NULL;

-- Optional TOTP second factor for password logins.
ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_secret_ref  text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_enabled_at  timestamptz;

-- =====================================================================
-- P2-2. ADVANCED SHIFT / ROSTER PLANNING
-- =====================================================================
CREATE TABLE shifts (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  name          text NOT NULL,
  code          text NOT NULL,
  start_time    time NOT NULL,
  end_time      time NOT NULL,                       -- end < start means the shift crosses midnight
  break_minutes integer NOT NULL DEFAULT 0 CHECK (break_minutes >= 0),
  location_id   uuid,
  is_active     boolean NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, code),
  FOREIGN KEY (tenant_id, location_id) REFERENCES locations (tenant_id, id)
);

CREATE TABLE shift_assignments (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id),
  shift_id    uuid NOT NULL,
  employee_id uuid NOT NULL,
  work_date   date NOT NULL,
  status      text NOT NULL DEFAULT 'planned'
              CHECK (status IN ('planned','published','swapped','cancelled')),
  created_by  uuid REFERENCES users(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, shift_id)    REFERENCES shifts (tenant_id, id),
  FOREIGN KEY (tenant_id, employee_id) REFERENCES employees (tenant_id, id)
);
-- one live shift per employee per day
CREATE UNIQUE INDEX uq_shift_assignments_live ON shift_assignments (tenant_id, employee_id, work_date)
  WHERE status IN ('planned','published');
CREATE INDEX ix_shift_assignments_date ON shift_assignments (tenant_id, work_date, shift_id);

CREATE TABLE shift_swap_requests (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            uuid NOT NULL REFERENCES tenants(id),
  from_assignment_id   uuid NOT NULL,
  to_employee_id       uuid NOT NULL,
  reason               text,
  status               text NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','approved','rejected','cancelled')),
  decided_by           uuid REFERENCES users(id),
  decided_at           timestamptz,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, from_assignment_id) REFERENCES shift_assignments (tenant_id, id),
  FOREIGN KEY (tenant_id, to_employee_id)     REFERENCES employees (tenant_id, id)
);
CREATE INDEX ix_shift_swaps_status ON shift_swap_requests (tenant_id, status);

-- =====================================================================
-- P2-3. ADVANCED ATS AUTOMATION
-- =====================================================================
-- Configurable pipeline stages replace the fixed applications.status list.
-- applications.status stays as the coarse state; stage_id is the fine-grained one.
CREATE TABLE pipeline_stages (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  uuid NOT NULL REFERENCES tenants(id),
  job_id     uuid,                                   -- NULL = tenant default pipeline
  name       text NOT NULL,
  position   integer NOT NULL CHECK (position >= 0),
  maps_to    text NOT NULL
             CHECK (maps_to IN ('applied','screening','interview','offer','hired','rejected','withdrawn')),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, job_id) REFERENCES jobs (tenant_id, id)
);
CREATE UNIQUE INDEX uq_pipeline_stages_position ON pipeline_stages
  (tenant_id, COALESCE(job_id, '00000000-0000-0000-0000-000000000000'::uuid), position);

ALTER TABLE applications ADD COLUMN IF NOT EXISTS stage_id uuid;
ALTER TABLE applications ADD COLUMN IF NOT EXISTS score numeric(5,2) CHECK (score BETWEEN 0 AND 100);
ALTER TABLE applications
  ADD CONSTRAINT fk_applications_stage
  FOREIGN KEY (tenant_id, stage_id) REFERENCES pipeline_stages (tenant_id, id);

-- "When X happens, if conditions match, do Y": e.g. on stage change to
-- 'Interview' -> send email template + create scheduling task.
CREATE TABLE automation_rules (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id),
  name            text NOT NULL,
  trigger_event   text NOT NULL,                     -- application.stage_changed, interview.completed ...
  conditions_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  actions_json    jsonb NOT NULL,                    -- allowlisted action types only
  is_active       boolean NOT NULL DEFAULT true,
  created_by      uuid REFERENCES users(id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, name)
);
CREATE INDEX ix_automation_rules_trigger ON automation_rules (tenant_id, trigger_event) WHERE is_active;

CREATE TABLE automation_runs (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id),
  rule_id     uuid NOT NULL,
  event_id    uuid NOT NULL,                         -- outbox_events.id that triggered the run
  status      text NOT NULL CHECK (status IN ('succeeded','failed','skipped')),
  result_json jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, rule_id, event_id),             -- a rule fires at most once per event
  FOREIGN KEY (tenant_id, rule_id) REFERENCES automation_rules (tenant_id, id)
);

-- =====================================================================
-- P2-4. DOCUMENT OCR / CLASSIFICATION
-- =====================================================================
CREATE TABLE document_extractions (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL REFERENCES tenants(id),
  document_id         uuid NOT NULL,
  status              text NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued','processing','completed','failed','needs_review')),
  engine              text,                          -- ocr / model identifier
  detected_type       text,                          -- classifier output, e.g. 'passport'
  confidence          numeric(4,3) CHECK (confidence BETWEEN 0 AND 1),
  extracted_json      jsonb,                         -- structured fields: number, name, expiry ...
  text_storage_key    text,                          -- full OCR text lives in object storage
  reviewed_by         uuid REFERENCES users(id),     -- human confirms before fields are applied
  reviewed_at         timestamptz,
  error               text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, id)
);
CREATE INDEX ix_document_extractions_doc    ON document_extractions (tenant_id, document_id);
CREATE INDEX ix_document_extractions_status ON document_extractions (tenant_id, status);

-- =====================================================================
-- P2-5. VISUAL WORKFLOW BUILDER
-- =====================================================================
-- The engine keeps reading definition_json. The builder only adds canvas
-- layout (node positions) which never affects execution.
ALTER TABLE workflow_versions ADD COLUMN IF NOT EXISTS layout_json jsonb;

-- =====================================================================
-- P2-6. ADVANCED COMPLIANCE
-- =====================================================================
CREATE TABLE retention_policies (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      uuid NOT NULL REFERENCES tenants(id),
  entity_type    text NOT NULL,                      -- employees, candidates, documents, audit_logs
  retain_days    integer NOT NULL CHECK (retain_days > 0),
  action         text NOT NULL DEFAULT 'anonymize' CHECK (action IN ('anonymize','delete')),
  is_active      boolean NOT NULL DEFAULT true,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, entity_type)
);

-- Data-subject requests (GDPR / DPDP style): export my data, erase my data.
CREATE TABLE data_subject_requests (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  subject_type  text NOT NULL CHECK (subject_type IN ('employee','candidate')),
  subject_id    uuid NOT NULL,
  request_type  text NOT NULL CHECK (request_type IN ('export','erasure','rectification')),
  status        text NOT NULL DEFAULT 'received'
                CHECK (status IN ('received','in_progress','completed','rejected')),
  requested_by  uuid REFERENCES users(id),
  handled_by    uuid REFERENCES users(id),
  result_key    text,                                -- export archive in object storage
  due_at        timestamptz,
  completed_at  timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_dsr_status ON data_subject_requests (tenant_id, status, due_at);

-- A legal hold blocks retention jobs from touching the entity.
CREATE TABLE legal_holds (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id),
  entity_type text NOT NULL,
  entity_id   uuid NOT NULL,
  reason      text NOT NULL,
  placed_by   uuid REFERENCES users(id),
  placed_at   timestamptz NOT NULL DEFAULT now(),
  released_at timestamptz
);
CREATE UNIQUE INDEX uq_legal_holds_live ON legal_holds (tenant_id, entity_type, entity_id)
  WHERE released_at IS NULL;

-- Tamper evidence for the audit trail: each row stores the hash of the
-- previous row in the same tenant (hash chain), verified by a nightly job.
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS prev_hash text;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS row_hash  text;

-- =====================================================================
-- P2-7. REPORTING / BI
-- =====================================================================
CREATE TABLE report_definitions (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id),
  name        text NOT NULL,
  -- A declarative spec (dataset + dimensions + measures + filters) compiled by
  -- the server into parameterized SQL. Never raw SQL from the user.
  spec_json   jsonb NOT NULL,
  created_by  uuid REFERENCES users(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, name)
);

CREATE TABLE report_schedules (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            uuid NOT NULL REFERENCES tenants(id),
  report_definition_id uuid NOT NULL,
  cron                 text NOT NULL,
  recipients_json      jsonb NOT NULL DEFAULT '[]'::jsonb,   -- user ids, never free-form emails
  is_active            boolean NOT NULL DEFAULT true,
  last_run_at          timestamptz,
  next_run_at          timestamptz,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, report_definition_id) REFERENCES report_definitions (tenant_id, id)
);
CREATE INDEX ix_report_schedules_due ON report_schedules (next_run_at) WHERE is_active;

-- Daily headcount snapshot: cheap trend charts without scanning employee_history.
CREATE TABLE headcount_snapshots (
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  snapshot_date date NOT NULL,
  department_id uuid,
  status        text NOT NULL,
  employees     integer NOT NULL CHECK (employees >= 0),
  FOREIGN KEY (tenant_id, department_id) REFERENCES departments (tenant_id, id)
);
CREATE UNIQUE INDEX uq_headcount_snapshots ON headcount_snapshots
  (tenant_id, snapshot_date, COALESCE(department_id, '00000000-0000-0000-0000-000000000000'::uuid), status);

-- =====================================================================
-- P2-8. AGENT ORCHESTRATION (multi-turn AI)
-- =====================================================================
CREATE TABLE ai_conversations (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  uuid NOT NULL REFERENCES tenants(id),
  user_id    uuid NOT NULL,
  title      text,
  status     text NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, user_id) REFERENCES tenant_memberships (tenant_id, user_id)
);
CREATE INDEX ix_ai_conversations_user ON ai_conversations (tenant_id, user_id, updated_at DESC);

CREATE TABLE ai_messages (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id),
  conversation_id uuid NOT NULL,
  role            text NOT NULL CHECK (role IN ('user','assistant','tool')),
  content_json    jsonb NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, conversation_id) REFERENCES ai_conversations (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX ix_ai_messages_conversation ON ai_messages (tenant_id, conversation_id, created_at);

-- Every step of a multi-step plan is still one ai_commands row, so each step
-- passes the same schema / permission / validation / audit gates as Phase 1.
ALTER TABLE ai_commands ADD COLUMN IF NOT EXISTS conversation_id uuid;
ALTER TABLE ai_commands ADD COLUMN IF NOT EXISTS plan_step integer;
ALTER TABLE ai_commands
  ADD CONSTRAINT fk_ai_commands_conversation
  FOREIGN KEY (tenant_id, conversation_id) REFERENCES ai_conversations (tenant_id, id);

-- =====================================================================
-- P2-9. SCALE: TRANSACTIONAL OUTBOX
-- =====================================================================
-- Domain events are written in the SAME transaction as the business change and
-- relayed by a worker. This is what lets modules (and later, services) react
-- to each other without direct calls, and it feeds automation_rules and BI.
CREATE TABLE outbox_events (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  event_type    text NOT NULL,                       -- employee.created, leave.approved ...
  aggregate_type text NOT NULL,
  aggregate_id  uuid NOT NULL,
  payload_json  jsonb NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  published_at  timestamptz,
  attempts      integer NOT NULL DEFAULT 0
);
CREATE INDEX ix_outbox_unpublished ON outbox_events (created_at) WHERE published_at IS NULL;
CREATE INDEX ix_outbox_aggregate   ON outbox_events (tenant_id, aggregate_type, aggregate_id);

-- =====================================================================
-- Attach the updated_at trigger to the new tables
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

-- Next: re-run 03_rls.sql (adds tenant_isolation to every table created above
-- and grants hrms_app access to them).
--
-- Large tenants only, when audit_logs passes ~50M rows: convert it to a table
-- partitioned by RANGE (created_at) monthly, in a planned maintenance window.
-- It is deliberately not done here because it requires rewriting the table.
