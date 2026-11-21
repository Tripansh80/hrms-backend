-- =====================================================================
-- AI-First Multi-Tenant HRMS — SEED DATA + TENANT PROVISIONING
-- Run after 01_schema.sql. Safe to re-run (every insert is idempotent).
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 1. Plans
-- ---------------------------------------------------------------------
INSERT INTO plans (code, name, price, billing_interval, limits_json, features_json) VALUES
  ('free',       'Free',       0,   'monthly',
   '{"employees": 10,   "ai_commands_per_month": 25,   "imports_per_month": 2,  "storage_mb": 250}',
   '{"recruitment": false, "workflows": true, "ai": true, "audit_export": false}'),
  ('growth',     'Growth',     49,  'monthly',
   '{"employees": 100,  "ai_commands_per_month": 500,  "imports_per_month": 20, "storage_mb": 5000}',
   '{"recruitment": true,  "workflows": true, "ai": true, "audit_export": true}'),
  ('enterprise', 'Enterprise', 299, 'monthly',
   '{"employees": 5000, "ai_commands_per_month": 10000,"imports_per_month": 500,"storage_mb": 100000}',
   '{"recruitment": true,  "workflows": true, "ai": true, "audit_export": true, "sso": true}')
ON CONFLICT (code) DO NOTHING;

-- ---------------------------------------------------------------------
-- 2. Permission catalog (PRD Appendix A)
-- ---------------------------------------------------------------------
INSERT INTO permissions (code, description) VALUES
  ('tenant.read',          'View company profile and settings'),
  ('tenant.update',        'Update company profile and settings'),
  ('users.read',           'View users and memberships'),
  ('users.manage',         'Invite, suspend and remove users'),
  ('roles.read',           'View roles and permissions'),
  ('roles.manage',         'Create roles and assign them to users'),
  ('employees.read',       'View employee records'),
  ('employees.create',     'Create employees'),
  ('employees.update',     'Update employee records'),
  ('employees.delete',     'Soft-delete / offboard employees'),
  ('attendance.read',      'View attendance records'),
  ('attendance.manage',    'Correct attendance and manage schedules'),
  ('leave.read',           'View leave requests and balances'),
  ('leave.request',        'Request leave for self'),
  ('leave.approve',        'Approve or reject leave requests'),
  ('recruitment.read',     'View jobs, candidates and applications'),
  ('recruitment.manage',   'Manage jobs, candidates, interviews and offers'),
  ('documents.read',       'View documents'),
  ('documents.manage',     'Upload, verify and delete documents'),
  ('policies.read',        'View and acknowledge policies'),
  ('policies.manage',      'Create and publish policies'),
  ('workflows.read',       'View workflow definitions and executions'),
  ('workflows.manage',     'Create and publish workflow definitions'),
  ('notifications.manage', 'Manage notification templates'),
  ('imports.create',       'Start bulk imports'),
  ('imports.read',         'View import jobs and errors'),
  ('audit.read',           'View audit logs'),
  ('subscriptions.read',   'View plan and usage'),
  ('subscriptions.manage', 'Change plan and billing'),
  ('reports.read',         'View and export reports'),
  ('ai.execute',           'Use the AI assistant')
ON CONFLICT (code) DO NOTHING;

-- ---------------------------------------------------------------------
-- 3. System roles (tenant_id NULL, shared by all tenants)
--    "Platform Super Admin" is NOT a tenant role: it is users.is_platform_admin.
-- ---------------------------------------------------------------------
INSERT INTO roles (tenant_id, name, description, is_system, data_scope) VALUES
  (NULL, 'Tenant Owner',       'Owns the company workspace',          true, 'all'),
  (NULL, 'HR Admin',           'Runs HR operations',                  true, 'all'),
  (NULL, 'HR Manager',         'Manages assigned HR areas',           true, 'department'),
  (NULL, 'Department Manager', 'Manages a department / team',         true, 'team'),
  (NULL, 'People Manager',      'Manages people and employment details', true, 'department'),
  (NULL, 'Recruiter',          'Runs hiring',                         true, 'all'),
  (NULL, 'Employee',           'Self-service user',                   true, 'self'),
  (NULL, 'Auditor',            'Read-only compliance access',         true, 'all')
ON CONFLICT (name) WHERE tenant_id IS NULL DO NOTHING;

-- ---------------------------------------------------------------------
-- 4. Role -> permission mapping
-- ---------------------------------------------------------------------
-- Tenant Owner: everything
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
WHERE r.tenant_id IS NULL AND r.name = 'Tenant Owner'
ON CONFLICT DO NOTHING;

-- HR Admin: everything except billing changes
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
WHERE r.tenant_id IS NULL AND r.name = 'HR Admin'
  AND p.code <> 'subscriptions.manage'
ON CONFLICT DO NOTHING;

-- HR Manager (scope: department)
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM roles r JOIN permissions p ON p.code = ANY (ARRAY[
  'tenant.read','users.read','roles.read',
  'employees.read','employees.create','employees.update',
  'attendance.read','attendance.manage',
  'leave.read','leave.request','leave.approve',
  'documents.read','documents.manage',
  'policies.read','workflows.read',
  'imports.create','imports.read','reports.read','ai.execute'])
WHERE r.tenant_id IS NULL AND r.name = 'HR Manager'
ON CONFLICT DO NOTHING;

-- Department Manager (scope: department)
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM roles r JOIN permissions p ON p.code = ANY (ARRAY[
  'tenant.read','employees.read',
  'employees.update',
  'attendance.read',
  'leave.read','leave.request','leave.approve',
  'documents.read','policies.read','workflows.read',
  'reports.read','ai.execute'])
WHERE r.tenant_id IS NULL AND r.name = 'Department Manager'
ON CONFLICT DO NOTHING;

-- People Manager (scope: department)
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM roles r JOIN permissions p ON p.code = ANY (ARRAY[
  'tenant.read','employees.read','employees.create','employees.update',
  'attendance.read','leave.read','leave.request','leave.approve',
  'documents.read','policies.read','reports.read'])
WHERE r.tenant_id IS NULL AND r.name = 'People Manager'
ON CONFLICT DO NOTHING;

-- Recruiter
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM roles r JOIN permissions p ON p.code = ANY (ARRAY[
  'tenant.read','employees.read',
  'recruitment.read','recruitment.manage',
  'documents.read','documents.manage',
  'leave.request','policies.read','ai.execute'])
WHERE r.tenant_id IS NULL AND r.name = 'Recruiter'
ON CONFLICT DO NOTHING;

-- Employee (scope: self)
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM roles r JOIN permissions p ON p.code = ANY (ARRAY[
  'tenant.read','employees.read','attendance.read',
  'leave.read','leave.request',
  'documents.read','policies.read','ai.execute'])
WHERE r.tenant_id IS NULL AND r.name = 'Employee'
ON CONFLICT DO NOTHING;

-- Auditor: every *.read permission, nothing else
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM roles r JOIN permissions p ON p.code LIKE '%.read'
WHERE r.tenant_id IS NULL AND r.name = 'Auditor'
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------
-- 5. Platform default notification templates
-- ---------------------------------------------------------------------
INSERT INTO notification_templates (tenant_id, code, channel, subject, body) VALUES
  (NULL, 'user.invited',          'email',  'You have been invited to {{tenant_name}}',
         'Hi {{name}}, accept your invitation: {{invite_url}}'),
  (NULL, 'auth.password_reset',   'email',  'Reset your password',
         'Use this link to reset your password: {{reset_url}} (valid for 30 minutes)'),
  (NULL, 'leave.submitted',       'in_app', '', '{{employee_name}} requested {{days}} day(s) of {{leave_type}}'),
  (NULL, 'leave.submitted',       'email',  'Leave request awaiting your approval',
         '{{employee_name}} requested {{days}} day(s) of {{leave_type}} from {{start_date}} to {{end_date}}.'),
  (NULL, 'leave.approved',        'in_app', '', 'Your {{leave_type}} request was approved'),
  (NULL, 'leave.approved',        'email',  'Your leave request was approved',
         'Your {{leave_type}} from {{start_date}} to {{end_date}} was approved.'),
  (NULL, 'leave.rejected',        'in_app', '', 'Your {{leave_type}} request was rejected'),
  (NULL, 'leave.rejected',        'email',  'Your leave request was rejected',
         'Your {{leave_type}} from {{start_date}} to {{end_date}} was rejected. Note: {{note}}'),
  (NULL, 'task.assigned',         'in_app', '', 'New task: {{task_title}}'),
  (NULL, 'import.finished',       'in_app', '', 'Import finished: {{processed}} rows, {{failed}} failed'),
  (NULL, 'document.expiring',     'email',  'A document is about to expire',
         '{{document_title}} expires on {{expires_at}}.'),
  (NULL, 'policy.published',      'in_app', '', 'New policy to acknowledge: {{policy_title}}')
ON CONFLICT (code, channel) WHERE tenant_id IS NULL DO NOTHING;

-- ---------------------------------------------------------------------
-- 6. provision_tenant(): the whole tenant-onboarding flow in ONE transaction
--    tenant -> owner user -> membership -> owner role -> subscription
--    -> default settings / leave types / work schedule / workflow -> audit event
--
--    The application hashes the password (argon2id) and passes the HASH in.
--    Returns the new tenant id. Raises unique_violation if the slug is taken.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION provision_tenant(
  p_name                text,
  p_slug                text,
  p_owner_email         text,
  p_owner_password_hash text,
  p_owner_full_name     text DEFAULT NULL,
  p_plan_code           text DEFAULT 'free',
  p_timezone            text DEFAULT 'UTC',
  p_locale              text DEFAULT 'en'
) RETURNS uuid
LANGUAGE plpgsql AS $$
DECLARE
  v_plan_id   uuid;
  v_tenant_id uuid;
  v_user_id   uuid;
  v_role_id   uuid;
  v_def_id    uuid;
BEGIN
  SELECT id INTO v_plan_id FROM plans WHERE code = p_plan_code AND is_active;
  IF v_plan_id IS NULL THEN
    RAISE EXCEPTION 'unknown or inactive plan: %', p_plan_code;
  END IF;

  SELECT id INTO v_role_id FROM roles WHERE tenant_id IS NULL AND name = 'Tenant Owner';
  IF v_role_id IS NULL THEN
    RAISE EXCEPTION 'system roles are not seeded (run 02_seed.sql)';
  END IF;

  INSERT INTO tenants (name, slug, status, timezone, locale, plan_id)
  VALUES (p_name, lower(p_slug), 'active', p_timezone, p_locale, v_plan_id)
  RETURNING id INTO v_tenant_id;

  -- One global identity may own/belong to several tenants: never overwrite
  -- an existing user's password here.
  INSERT INTO users (email, password_hash, full_name)
  VALUES (p_owner_email::citext, p_owner_password_hash, p_owner_full_name)
  ON CONFLICT (email) DO NOTHING;
  SELECT id INTO v_user_id FROM users WHERE email = p_owner_email::citext;

  INSERT INTO tenant_memberships (tenant_id, user_id, status, is_owner, joined_at)
  VALUES (v_tenant_id, v_user_id, 'active', true, now());

  INSERT INTO user_roles (tenant_id, user_id, role_id, granted_by)
  VALUES (v_tenant_id, v_user_id, v_role_id, v_user_id);

  INSERT INTO subscriptions (tenant_id, plan_id, status, current_period_end)
  VALUES (v_tenant_id, v_plan_id, 'trialing', now() + interval '14 days');

  INSERT INTO usage_counters (tenant_id, metric, period, value)
  VALUES (v_tenant_id, 'employees', 'lifetime', 0);

  INSERT INTO tenant_settings (tenant_id, key, value_json) VALUES
    (v_tenant_id, 'leave.year_start_month',     '1'),
    (v_tenant_id, 'attendance.allow_web_checkin','true'),
    (v_tenant_id, 'ai.enabled',                 'true'),
    (v_tenant_id, 'employee_no.format',         '{"prefix": "EMP", "pad": 4, "next": 1}');

  INSERT INTO work_schedules (tenant_id, name, timezone, rules_json, is_default)
  VALUES (v_tenant_id, 'Standard (Mon-Fri)', p_timezone,
          '{"working_days":[1,2,3,4,5],"start":"09:00","end":"18:00","grace_minutes":15,"half_day_minutes":240}',
          true);

  INSERT INTO leave_types (tenant_id, name, code, unit, is_paid, rules_json) VALUES
    (v_tenant_id, 'Annual Leave', 'ANNUAL', 'day', true,
     '{"annual_allocation":18,"max_consecutive_days":15,"min_notice_days":2,"carry_forward_max":5,"allow_negative":false}'),
    (v_tenant_id, 'Sick Leave',   'SICK',   'day', true,
     '{"annual_allocation":10,"max_consecutive_days":10,"min_notice_days":0,"requires_document_after_days":3,"allow_negative":false}'),
    (v_tenant_id, 'Unpaid Leave', 'UNPAID', 'day', false,
     '{"annual_allocation":0,"allow_negative":true}');

  -- Default leave approval workflow: single step, approver = employee's manager,
  -- falling back to anyone holding leave.approve with scope "all".
  INSERT INTO workflow_definitions (tenant_id, name, entity_type, status, created_by)
  VALUES (v_tenant_id, 'Leave approval', 'leave_request', 'active', v_user_id)
  RETURNING id INTO v_def_id;

  INSERT INTO workflow_versions (tenant_id, workflow_definition_id, version, definition_json, published_at, created_by)
  VALUES (v_tenant_id, v_def_id, 1,
    '{
       "schema": 1,
       "start": "manager_approval",
       "steps": {
         "manager_approval": {
           "type": "approval",
           "assignee": {"kind": "manager_of_subject", "fallback": {"kind": "permission", "code": "leave.approve"}},
           "due_in_hours": 72,
           "on_approve": "END_APPROVED",
           "on_reject": "END_REJECTED",
           "on_timeout": {"action": "escalate", "to": {"kind": "permission", "code": "leave.approve"}}
         }
       }
     }'::jsonb,
    now(), v_user_id);

  INSERT INTO audit_logs (tenant_id, actor_user_id, action, entity_type, entity_id, source, after_json)
  VALUES (v_tenant_id, v_user_id, 'tenant.provisioned', 'tenant', v_tenant_id, 'system',
          jsonb_build_object('name', p_name, 'slug', lower(p_slug), 'plan', p_plan_code));

  RETURN v_tenant_id;
END $$;

COMMIT;

-- ---------------------------------------------------------------------
-- Local development only: create a demo tenant.
-- Replace the hash with a real argon2id hash generated by the application.
-- ---------------------------------------------------------------------
-- SELECT provision_tenant('Acme Inc', 'acme', 'owner@acme.test',
--                         '$argon2id$v=19$m=65536,t=3,p=4$REPLACE_ME', 'Acme Owner', 'growth', 'Asia/Kolkata', 'en');
