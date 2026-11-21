-- =====================================================================
-- AI-First Multi-Tenant HRMS — QUERY COOKBOOK
--
-- Reference queries for the repository layer. This file is NOT meant to be
-- executed top to bottom. Parameters use :name style (SQLAlchemy text() /
-- psycopg named parameters). Never build these with string concatenation.
--
-- Golden rule: every query on a tenant-owned table filters by :tenant_id,
-- and :tenant_id ALWAYS comes from the server-side request context
-- (validated hostname + membership), never from the request body or the LLM.
-- =====================================================================


-- =====================================================================
-- A. REQUEST CONTEXT
-- =====================================================================

-- A1. Resolve tenant from hostname (acme.myhrm.com -> 'acme'). Cache in Redis ~60s.
SELECT id, slug, status, timezone, locale
FROM resolve_tenant(:slug, :custom_domain);          -- defined in 03_rls.sql

-- A2. Bind the tenant to the current transaction (activates RLS policies).
SELECT set_config('app.tenant_id', :tenant_id, true);

-- A3. Login: user + membership in THIS tenant (a valid password for a user who
--     is not an active member of the resolved tenant must still be rejected).
SELECT u.id, u.email, u.password_hash, u.is_active, u.locked_until, u.failed_login_count,
       m.status AS membership_status
FROM users u
JOIN tenant_memberships m ON m.user_id = u.id AND m.tenant_id = :tenant_id
WHERE u.email = :email;

-- A4. Failed / successful login bookkeeping.
UPDATE users
SET failed_login_count = failed_login_count + 1,
    locked_until = CASE WHEN failed_login_count + 1 >= 5 THEN now() + interval '15 minutes' ELSE locked_until END
WHERE id = :user_id;

UPDATE users SET failed_login_count = 0, locked_until = NULL, last_login_at = now()
WHERE id = :user_id;

-- A5. Effective permissions of a user in a tenant. Cache in Redis for ~5 min,
--     key perms:{tenant_id}:{user_id}; delete the key whenever roles change.
SELECT DISTINCT p.code
FROM tenant_memberships m
JOIN user_roles ur       ON ur.tenant_id = m.tenant_id AND ur.user_id = m.user_id
JOIN roles r             ON r.id = ur.role_id AND (r.tenant_id IS NULL OR r.tenant_id = m.tenant_id)
JOIN role_permissions rp ON rp.role_id = r.id
JOIN permissions p       ON p.id = rp.permission_id
WHERE m.tenant_id = :tenant_id AND m.user_id = :user_id AND m.status = 'active';

-- A6. Widest data scope the user holds for one permission
--     (all > department > team > self).
SELECT r.data_scope
FROM user_roles ur
JOIN roles r             ON r.id = ur.role_id
JOIN role_permissions rp ON rp.role_id = r.id
JOIN permissions p       ON p.id = rp.permission_id
WHERE ur.tenant_id = :tenant_id AND ur.user_id = :user_id AND p.code = :permission_code
ORDER BY CASE r.data_scope WHEN 'all' THEN 1 WHEN 'department' THEN 2 WHEN 'team' THEN 3 ELSE 4 END
LIMIT 1;


-- =====================================================================
-- B. REFRESH TOKENS (rotation + reuse detection)
-- =====================================================================

-- B1. Issue.
INSERT INTO refresh_tokens (tenant_id, user_id, family_id, token_hash, expires_at, ip_address, user_agent)
VALUES (:tenant_id, :user_id, :family_id, :token_hash, :expires_at, :ip_address, :user_agent)
RETURNING id;

-- B2. Look up the presented token (lock it so two concurrent refreshes cannot both win).
SELECT id, user_id, family_id, expires_at, revoked_at
FROM refresh_tokens
WHERE tenant_id = :tenant_id AND token_hash = :token_hash
FOR UPDATE;

-- B3. Rotate: revoke the old token and point it at its replacement.
UPDATE refresh_tokens SET revoked_at = now(), replaced_by = :new_token_id
WHERE id = :old_token_id AND revoked_at IS NULL;

-- B4. Reuse detected (a revoked token was presented) or logout-everywhere:
UPDATE refresh_tokens SET revoked_at = now()
WHERE family_id = :family_id AND revoked_at IS NULL;

UPDATE refresh_tokens SET revoked_at = now()
WHERE tenant_id = :tenant_id AND user_id = :user_id AND revoked_at IS NULL;


-- =====================================================================
-- C. EMPLOYEES & ORGANIZATION
-- =====================================================================

-- C1. List with filters + keyset pagination (stable under inserts, no OFFSET scans).
SELECT e.id, e.employee_no, e.first_name, e.last_name, e.email, e.designation, e.status,
       e.joining_date, d.name AS department, l.name AS location,
       m.first_name || ' ' || m.last_name AS manager_name, e.created_at
FROM employees e
LEFT JOIN departments d ON d.tenant_id = e.tenant_id AND d.id = e.department_id
LEFT JOIN locations   l ON l.tenant_id = e.tenant_id AND l.id = e.location_id
LEFT JOIN employees   m ON m.tenant_id = e.tenant_id AND m.id = e.manager_id
WHERE e.tenant_id = :tenant_id
  AND e.deleted_at IS NULL
  AND (CAST(:status AS text) IS NULL OR e.status = :status)
  AND (CAST(:department_id AS uuid) IS NULL OR e.department_id = :department_id)
  AND (CAST(:search AS text) IS NULL
       OR e.first_name ILIKE '%' || :search || '%'
       OR e.last_name  ILIKE '%' || :search || '%'
       OR e.employee_no ILIKE :search || '%')
  AND (CAST(:cursor_created_at AS timestamptz) IS NULL
       OR (e.created_at, e.id) < (:cursor_created_at, :cursor_id))
ORDER BY e.created_at DESC, e.id DESC
LIMIT :limit;

-- C2. Create employee. Relies on UNIQUE(tenant_id, employee_no) / UNIQUE(tenant_id, email);
--     translate unique_violation (23505) into a 409 with the offending field.
INSERT INTO employees (tenant_id, employee_no, first_name, last_name, email, phone, department_id,
                       manager_id, location_id, work_schedule_id, designation, employment_type,
                       status, joining_date)
VALUES (:tenant_id, :employee_no, :first_name, :last_name, :email, :phone, :department_id,
        :manager_id, :location_id, :work_schedule_id, :designation, :employment_type,
        'onboarding', :joining_date)
RETURNING *;

-- C3. Next employee number, race-free (row lock on the settings row).
UPDATE tenant_settings
SET value_json = jsonb_set(value_json, '{next}', to_jsonb((value_json->>'next')::int + 1))
WHERE tenant_id = :tenant_id AND key = 'employee_no.format'
RETURNING value_json->>'prefix'            AS prefix,
          (value_json->>'pad')::int        AS pad,
          (value_json->>'next')::int - 1   AS issued_number;

-- C4. All direct + indirect reports of a manager ("team" data scope).
WITH RECURSIVE reports AS (
  SELECT e.id, e.manager_id, 1 AS depth
  FROM employees e
  WHERE e.tenant_id = :tenant_id AND e.manager_id = :manager_employee_id AND e.deleted_at IS NULL
  UNION ALL
  SELECT e.id, e.manager_id, r.depth + 1
  FROM employees e
  JOIN reports r ON e.manager_id = r.id
  WHERE e.tenant_id = :tenant_id AND e.deleted_at IS NULL AND r.depth < 25
)
SELECT id, depth FROM reports;

-- C5. Manager-cycle guard: before setting :employee_id's manager to :new_manager_id,
--     walk UP from the new manager. If :employee_id appears, reject (would create a loop).
WITH RECURSIVE chain AS (
  SELECT e.id, e.manager_id, 1 AS depth
  FROM employees e
  WHERE e.tenant_id = :tenant_id AND e.id = :new_manager_id
  UNION ALL
  SELECT e.id, e.manager_id, c.depth + 1
  FROM employees e
  JOIN chain c ON e.id = c.manager_id
  WHERE e.tenant_id = :tenant_id AND c.depth < 25
)
SELECT EXISTS (SELECT 1 FROM chain WHERE id = :employee_id) AS would_create_cycle;

-- C6. Department tree with headcount.
WITH RECURSIVE tree AS (
  SELECT d.id, d.name, d.code, d.parent_id, 0 AS depth, ARRAY[d.name] AS path
  FROM departments d
  WHERE d.tenant_id = :tenant_id AND d.parent_id IS NULL AND d.deleted_at IS NULL
  UNION ALL
  SELECT d.id, d.name, d.code, d.parent_id, t.depth + 1, t.path || d.name
  FROM departments d
  JOIN tree t ON d.parent_id = t.id
  WHERE d.tenant_id = :tenant_id AND d.deleted_at IS NULL AND t.depth < 25
)
SELECT t.id, t.name, t.code, t.parent_id, t.depth, t.path,
       (SELECT count(*) FROM employees e
        WHERE e.tenant_id = :tenant_id AND e.department_id = t.id
          AND e.deleted_at IS NULL AND e.status <> 'exited') AS headcount
FROM tree t
ORDER BY t.path;

-- C7. Update + lifecycle history in one statement.
WITH prev AS (
  SELECT * FROM employees WHERE tenant_id = :tenant_id AND id = :employee_id FOR UPDATE
), upd AS (
  UPDATE employees e
  SET department_id = :department_id, manager_id = :manager_id, designation = :designation
  FROM prev
  WHERE e.tenant_id = :tenant_id AND e.id = prev.id
  RETURNING e.*
)
INSERT INTO employee_history (tenant_id, employee_id, event_type, old_data, new_data, actor_user_id)
SELECT :tenant_id, upd.id, :event_type,
       jsonb_build_object('department_id', prev.department_id, 'manager_id', prev.manager_id, 'designation', prev.designation),
       jsonb_build_object('department_id', upd.department_id, 'manager_id', upd.manager_id, 'designation', upd.designation),
       :actor_user_id
FROM upd, prev
RETURNING employee_id;

-- C8. Soft delete / offboard.
UPDATE employees
SET status = 'exited', exit_date = :exit_date, deleted_at = now()
WHERE tenant_id = :tenant_id AND id = :employee_id AND deleted_at IS NULL
RETURNING id;


-- =====================================================================
-- D. ATTENDANCE
-- =====================================================================

-- D1. Check in. Idempotent: a second check-in the same day returns no row
--     (service answers 409 "already checked in"). work_date is computed in the
--     employee's schedule timezone by the service, not by the client.
INSERT INTO attendance_records (tenant_id, employee_id, work_date, check_in, status, source)
VALUES (:tenant_id, :employee_id, :work_date, now(), 'present', :source)
ON CONFLICT (tenant_id, employee_id, work_date) DO NOTHING
RETURNING id, check_in;

-- D2. Check out.
UPDATE attendance_records
SET check_out = now(),
    worked_minutes = floor(extract(epoch FROM (now() - check_in)) / 60)::int
WHERE tenant_id = :tenant_id AND employee_id = :employee_id AND work_date = :work_date
  AND check_in IS NOT NULL AND check_out IS NULL
RETURNING id, check_in, check_out, worked_minutes;

-- D3. Apply an approved correction (upsert).
INSERT INTO attendance_records (tenant_id, employee_id, work_date, check_in, check_out, worked_minutes, status, source)
VALUES (:tenant_id, :employee_id, :work_date, :check_in, :check_out, :worked_minutes, 'present', 'correction')
ON CONFLICT (tenant_id, employee_id, work_date) DO UPDATE
SET check_in = EXCLUDED.check_in, check_out = EXCLUDED.check_out,
    worked_minutes = EXCLUDED.worked_minutes, status = 'present', source = 'correction';

-- D4. Monthly summary per employee.
SELECT e.id AS employee_id, e.employee_no, e.first_name, e.last_name,
       count(*) FILTER (WHERE a.status = 'present')  AS present_days,
       count(*) FILTER (WHERE a.status = 'half_day') AS half_days,
       count(*) FILTER (WHERE a.status = 'absent')   AS absent_days,
       count(*) FILTER (WHERE a.status = 'on_leave') AS leave_days,
       COALESCE(sum(a.worked_minutes), 0)            AS worked_minutes
FROM employees e
LEFT JOIN attendance_records a
       ON a.tenant_id = e.tenant_id AND a.employee_id = e.id
      AND a.work_date >= :month_start AND a.work_date < :next_month_start
WHERE e.tenant_id = :tenant_id AND e.deleted_at IS NULL AND e.status <> 'exited'
GROUP BY e.id, e.employee_no, e.first_name, e.last_name
ORDER BY e.employee_no;

-- D5. Nightly job: mark employees with no record for :work_date as absent
--     (service skips weekends/holidays/approved leave before calling this).
INSERT INTO attendance_records (tenant_id, employee_id, work_date, status, source)
SELECT e.tenant_id, e.id, :work_date, 'absent', 'system'
FROM employees e
WHERE e.tenant_id = :tenant_id AND e.status = 'active' AND e.deleted_at IS NULL
  AND e.joining_date <= :work_date
ON CONFLICT (tenant_id, employee_id, work_date) DO NOTHING;


-- =====================================================================
-- E. LEAVE  (all balance changes are single atomic UPDATEs — no read-then-write)
-- =====================================================================

-- E1. Holidays in a range, for the working-day calculation.
SELECT holiday_date
FROM holidays
WHERE tenant_id = :tenant_id
  AND holiday_date BETWEEN :start_date AND :end_date
  AND is_optional = false
  AND (location_id IS NULL OR location_id = :location_id);

-- E2. Balance view.
SELECT lt.id AS leave_type_id, lt.name, lt.code, lt.is_paid,
       b.allocated, b.used, b.pending, (b.allocated - b.used - b.pending) AS available
FROM leave_types lt
LEFT JOIN leave_balances b
       ON b.tenant_id = lt.tenant_id AND b.leave_type_id = lt.id
      AND b.employee_id = :employee_id AND b.year = :year
WHERE lt.tenant_id = :tenant_id AND lt.is_active
ORDER BY lt.name;

-- E3. Reserve balance when a request is submitted.
--     0 rows updated => insufficient balance => reject (422) and roll back.
--     Pass :allow_negative = true for leave types whose rules allow it.
UPDATE leave_balances
SET pending = pending + :days
WHERE tenant_id = :tenant_id AND employee_id = :employee_id
  AND leave_type_id = :leave_type_id AND year = :year
  AND (:allow_negative OR allocated - used - pending >= :days)
RETURNING id, allocated, used, pending;

-- E4. Create the request (same transaction as E3). The exclusion constraint
--     ex_leave_requests_no_overlap raises 23P01 on overlapping live requests -> 409.
INSERT INTO leave_requests (tenant_id, employee_id, leave_type_id, start_date, end_date, days,
                            reason, status, current_approver_id, source)
VALUES (:tenant_id, :employee_id, :leave_type_id, :start_date, :end_date, :days,
        :reason, 'pending', :approver_user_id, :source)
RETURNING id;

-- E5. Approve: state guard in the WHERE clause makes double-approval a no-op.
UPDATE leave_requests
SET status = 'approved', decided_by = :user_id, decided_at = now(),
    decision_note = :note, current_approver_id = NULL
WHERE tenant_id = :tenant_id AND id = :leave_request_id AND status = 'pending'
RETURNING employee_id, leave_type_id, days, start_date, end_date;

UPDATE leave_balances
SET pending = pending - :days, used = used + :days
WHERE tenant_id = :tenant_id AND employee_id = :employee_id
  AND leave_type_id = :leave_type_id AND year = :year;

-- E6. Reject, or cancel while still pending: release the reservation.
UPDATE leave_requests
SET status = :new_status,                     -- 'rejected' | 'cancelled'
    decided_by = :user_id, decided_at = now(), decision_note = :note, current_approver_id = NULL
WHERE tenant_id = :tenant_id AND id = :leave_request_id AND status = 'pending'
RETURNING employee_id, leave_type_id, days;

UPDATE leave_balances
SET pending = pending - :days
WHERE tenant_id = :tenant_id AND employee_id = :employee_id
  AND leave_type_id = :leave_type_id AND year = :year;

-- E7. Cancel an already-approved request (only if it has not started): give days back.
UPDATE leave_requests
SET status = 'cancelled', decided_by = :user_id, decided_at = now(), decision_note = :note
WHERE tenant_id = :tenant_id AND id = :leave_request_id
  AND status = 'approved' AND start_date > CURRENT_DATE
RETURNING employee_id, leave_type_id, days, start_date, end_date;

UPDATE leave_balances
SET used = used - :days
WHERE tenant_id = :tenant_id AND employee_id = :employee_id
  AND leave_type_id = :leave_type_id AND year = :year;

-- E8. Attendance impact of an approved leave (one row per working day; the
--     service passes the working dates it already calculated).
INSERT INTO attendance_records (tenant_id, employee_id, work_date, status, source)
SELECT :tenant_id, :employee_id, d, 'on_leave', 'system'
FROM unnest(CAST(:working_dates AS date[])) AS d
ON CONFLICT (tenant_id, employee_id, work_date) DO UPDATE
SET status = 'on_leave', source = 'system';

-- E9. Approver inbox.
SELECT lr.id, lr.start_date, lr.end_date, lr.days, lr.reason, lr.created_at,
       e.employee_no, e.first_name, e.last_name, lt.name AS leave_type
FROM leave_requests lr
JOIN employees e    ON e.tenant_id = lr.tenant_id AND e.id = lr.employee_id
JOIN leave_types lt ON lt.tenant_id = lr.tenant_id AND lt.id = lr.leave_type_id
WHERE lr.tenant_id = :tenant_id AND lr.current_approver_id = :user_id AND lr.status = 'pending'
ORDER BY lr.created_at;

-- E10. Yearly allocation job (idempotent thanks to the unique key).
INSERT INTO leave_balances (tenant_id, employee_id, leave_type_id, year, allocated)
SELECT e.tenant_id, e.id, lt.id, :year, COALESCE((lt.rules_json->>'annual_allocation')::numeric, 0)
FROM employees e
JOIN leave_types lt ON lt.tenant_id = e.tenant_id AND lt.is_active
WHERE e.tenant_id = :tenant_id AND e.deleted_at IS NULL AND e.status IN ('onboarding','active')
ON CONFLICT (tenant_id, employee_id, leave_type_id, year) DO NOTHING;


-- =====================================================================
-- F. WORKFLOW ENGINE
-- =====================================================================

-- F1. Active published version for an entity type.
SELECT wv.id, wv.version, wv.definition_json
FROM workflow_definitions wd
JOIN workflow_versions wv ON wv.tenant_id = wd.tenant_id AND wv.workflow_definition_id = wd.id
WHERE wd.tenant_id = :tenant_id AND wd.entity_type = :entity_type
  AND wd.status = 'active' AND wv.published_at IS NOT NULL
ORDER BY wv.version DESC
LIMIT 1;

-- F2. Start an execution. The partial unique index (one 'running' per entity)
--     turns an accidental double-start into a unique_violation.
INSERT INTO workflow_executions (tenant_id, workflow_version_id, entity_type, entity_id,
                                 status, current_step, context_json, started_by)
VALUES (:tenant_id, :workflow_version_id, :entity_type, :entity_id,
        'running', :start_step, CAST(:context_json AS jsonb), :user_id)
RETURNING id;

-- F3. Create a task.
INSERT INTO workflow_tasks (tenant_id, execution_id, step_key, assignee_user_id, task_type, due_at)
VALUES (:tenant_id, :execution_id, :step_key, :assignee_user_id, :task_type, :due_at)
RETURNING id;

-- F4. My open tasks.
SELECT t.id, t.step_key, t.task_type, t.due_at, t.created_at, x.entity_type, x.entity_id
FROM workflow_tasks t
JOIN workflow_executions x ON x.tenant_id = t.tenant_id AND x.id = t.execution_id
WHERE t.tenant_id = :tenant_id AND t.assignee_user_id = :user_id AND t.status = 'pending'
ORDER BY t.due_at NULLS LAST, t.created_at;

-- F5. Decide a task. Only the assignee, only while pending: 0 rows => 404/409.
UPDATE workflow_tasks
SET status = :decision,                       -- 'approved' | 'rejected' | 'completed'
    comment = :comment, completed_by = :user_id, completed_at = now()
WHERE tenant_id = :tenant_id AND id = :task_id
  AND assignee_user_id = :user_id AND status = 'pending'
RETURNING execution_id, step_key;

-- F6. Advance or finish the execution (lock first so parallel decisions serialize).
SELECT id, current_step, status, context_json, workflow_version_id
FROM workflow_executions
WHERE tenant_id = :tenant_id AND id = :execution_id
FOR UPDATE;

UPDATE workflow_executions
SET current_step = :next_step,
    status       = :status,
    completed_at = CASE WHEN :status IN ('completed','rejected','cancelled','failed') THEN now() END,
    error        = :error
WHERE tenant_id = :tenant_id AND id = :execution_id AND status = 'running';

-- F7. Timeout sweep (worker, hrms_platform connection, all tenants):
--     claim overdue tasks without blocking other workers.
UPDATE workflow_tasks
SET status = 'expired', completed_at = now()
WHERE id IN (
  SELECT id FROM workflow_tasks
  WHERE status = 'pending' AND due_at < now()
  ORDER BY due_at
  LIMIT :batch_size
  FOR UPDATE SKIP LOCKED
)
RETURNING id, tenant_id, execution_id, step_key;

-- F8. Publish a draft version (after this the trigger makes it immutable).
UPDATE workflow_versions SET published_at = now()
WHERE tenant_id = :tenant_id AND id = :workflow_version_id AND published_at IS NULL;


-- =====================================================================
-- G. AUDIT
-- =====================================================================

-- G1. Write an audit event (always in the SAME transaction as the change).
INSERT INTO audit_logs (tenant_id, actor_user_id, action, entity_type, entity_id, source,
                        before_json, after_json, metadata_json, ip_address, request_id)
VALUES (:tenant_id, :actor_user_id, :action, :entity_type, :entity_id, :source,
        CAST(:before_json AS jsonb), CAST(:after_json AS jsonb), CAST(:metadata_json AS jsonb),
        CAST(:ip_address AS inet), :request_id);

-- G2. Browse (keyset pagination on created_at).
SELECT a.id, a.created_at, a.action, a.entity_type, a.entity_id, a.source,
       a.before_json, a.after_json, a.metadata_json, a.ip_address, u.email AS actor_email
FROM audit_logs a
LEFT JOIN users u ON u.id = a.actor_user_id
WHERE a.tenant_id = :tenant_id
  AND (CAST(:action AS text) IS NULL OR a.action = :action)
  AND (CAST(:entity_type AS text) IS NULL OR a.entity_type = :entity_type)
  AND (CAST(:entity_id AS uuid) IS NULL OR a.entity_id = :entity_id)
  AND a.created_at >= :from_ts AND a.created_at < :to_ts
  AND (CAST(:cursor_created_at AS timestamptz) IS NULL OR a.created_at < :cursor_created_at)
ORDER BY a.created_at DESC
LIMIT :limit;

-- G3. Everything the AI did for a tenant.
SELECT created_at, actor_user_id, action, entity_type, entity_id,
       metadata_json->>'intent' AS intent, metadata_json->>'ai_command_id' AS ai_command_id
FROM audit_logs
WHERE tenant_id = :tenant_id AND source = 'ai'
ORDER BY created_at DESC
LIMIT :limit;


-- =====================================================================
-- H. BULK IMPORT
-- =====================================================================

-- H1. Enqueue.
INSERT INTO import_jobs (tenant_id, created_by, entity_type, file_key, file_name)
VALUES (:tenant_id, :user_id, :entity_type, :file_key, :file_name)
RETURNING id;

-- H2. Worker claims the job. If 0 rows come back another worker already has it
--     (or it is a redelivered message for a finished job) -> exit quietly.
UPDATE import_jobs
SET status = 'processing', started_at = now(), total_rows = :total_rows
WHERE tenant_id = :tenant_id AND id = :import_job_id AND status = 'queued'
RETURNING id;

-- H3. Progress, flushed once per chunk (e.g. every 200 rows).
UPDATE import_jobs
SET processed_rows = processed_rows + :processed_delta,
    failed_rows    = failed_rows + :failed_delta
WHERE tenant_id = :tenant_id AND id = :import_job_id;

-- H4. Row-level error.
INSERT INTO import_errors (tenant_id, import_job_id, row_number, field, message, raw_data)
VALUES (:tenant_id, :import_job_id, :row_number, :field, :message, CAST(:raw_data AS jsonb));

-- H5. Finish.
UPDATE import_jobs
SET status = CASE WHEN failed_rows = 0 THEN 'completed' ELSE 'completed_with_errors' END,
    finished_at = now(), error_report_key = :error_report_key
WHERE tenant_id = :tenant_id AND id = :import_job_id AND status = 'processing';

-- H6. Status + first page of errors for the UI.
SELECT id, entity_type, status, total_rows, processed_rows, failed_rows,
       CASE WHEN total_rows > 0 THEN round(100.0 * processed_rows / total_rows, 1) ELSE 0 END AS percent,
       started_at, finished_at, error
FROM import_jobs
WHERE tenant_id = :tenant_id AND id = :import_job_id;

SELECT row_number, field, message, raw_data
FROM import_errors
WHERE tenant_id = :tenant_id AND import_job_id = :import_job_id AND row_number > :after_row
ORDER BY row_number
LIMIT :limit;


-- =====================================================================
-- I. SUBSCRIPTIONS & LIMITS
-- =====================================================================

-- I1. Current plan limits + features.
SELECT p.code, p.limits_json, p.features_json, s.status, s.current_period_end
FROM subscriptions s
JOIN plans p ON p.id = s.plan_id
WHERE s.tenant_id = :tenant_id AND s.status IN ('trialing','active','past_due');

-- I2. Consume one unit atomically. 0 rows => limit reached => 402/429.
--     :period is 'lifetime' or 'YYYY-MM'. Requires :limit >= 1.
INSERT INTO usage_counters (tenant_id, metric, period, value)
VALUES (:tenant_id, :metric, :period, 1)
ON CONFLICT (tenant_id, metric, period) DO UPDATE
SET value = usage_counters.value + 1
WHERE usage_counters.value < :limit
RETURNING value;

-- I3. Release a unit (e.g. employee offboarded).
UPDATE usage_counters SET value = GREATEST(value - 1, 0)
WHERE tenant_id = :tenant_id AND metric = :metric AND period = :period;


-- =====================================================================
-- J. RECRUITMENT
-- =====================================================================

-- J1. Pipeline counts for a job.
SELECT status, count(*) AS applications
FROM applications
WHERE tenant_id = :tenant_id AND job_id = :job_id
GROUP BY status;

-- J2. Move an application between stages with an expected-state guard.
UPDATE applications
SET status = :to_status
WHERE tenant_id = :tenant_id AND id = :application_id AND status = :from_status
RETURNING id;

-- J3. Hiring conversion: candidate -> employee (run in one transaction with C2).
UPDATE applications
SET status = 'hired', hired_employee_id = :employee_id
WHERE tenant_id = :tenant_id AND id = :application_id AND status = 'offer'
RETURNING job_id, candidate_id;

-- Mark the job filled once hires reach the number of openings.
UPDATE jobs j
SET status = 'filled', closed_at = now()
WHERE j.tenant_id = :tenant_id AND j.id = :job_id AND j.status = 'open'
  AND (SELECT count(*) FROM applications a
       WHERE a.tenant_id = j.tenant_id AND a.job_id = j.id AND a.status = 'hired') >= j.openings;

-- J4. Interviewer calendar clash check.
SELECT id, scheduled_at, duration_minutes
FROM interviews
WHERE tenant_id = :tenant_id AND interviewer_id = :interviewer_id AND status = 'scheduled'
  AND tstzrange(scheduled_at, scheduled_at + make_interval(mins => duration_minutes))
      && tstzrange(:starts_at, :ends_at);

-- J5. Expire stale offers (worker).
UPDATE offers SET status = 'expired'
WHERE status = 'sent' AND expires_at < now()
RETURNING id, tenant_id, application_id;


-- =====================================================================
-- K. DOCUMENTS & POLICIES
-- =====================================================================

-- K1. Documents expiring within N days (reminder job).
SELECT d.id, d.tenant_id, d.title, d.document_type, d.expires_at, d.employee_id
FROM documents d
WHERE d.tenant_id = :tenant_id AND d.deleted_at IS NULL AND d.status <> 'expired'
  AND d.expires_at BETWEEN now() AND now() + make_interval(days => :days_ahead)
ORDER BY d.expires_at;

-- K2. Latest published version of each published policy + whether I acknowledged it.
SELECT p.id AS policy_id, p.title, pv.id AS policy_version_id, pv.version, pv.published_at,
       (ack.id IS NOT NULL) AS acknowledged, ack.acknowledged_at
FROM policies p
JOIN LATERAL (
  SELECT v.id, v.version, v.published_at
  FROM policy_versions v
  WHERE v.tenant_id = p.tenant_id AND v.policy_id = p.id AND v.published_at IS NOT NULL
  ORDER BY v.version DESC
  LIMIT 1
) pv ON true
LEFT JOIN policy_acknowledgements ack
       ON ack.tenant_id = p.tenant_id AND ack.policy_version_id = pv.id AND ack.employee_id = :employee_id
WHERE p.tenant_id = :tenant_id AND p.status = 'published'
ORDER BY p.title;

-- K3. Acknowledge (idempotent).
INSERT INTO policy_acknowledgements (tenant_id, policy_version_id, employee_id, ip_address)
VALUES (:tenant_id, :policy_version_id, :employee_id, CAST(:ip_address AS inet))
ON CONFLICT (tenant_id, policy_version_id, employee_id) DO NOTHING;

-- K4. Who has NOT acknowledged a policy version yet.
SELECT e.id, e.employee_no, e.first_name, e.last_name, e.email
FROM employees e
WHERE e.tenant_id = :tenant_id AND e.deleted_at IS NULL AND e.status = 'active'
  AND NOT EXISTS (
    SELECT 1 FROM policy_acknowledgements a
    WHERE a.tenant_id = e.tenant_id AND a.employee_id = e.id AND a.policy_version_id = :policy_version_id)
ORDER BY e.employee_no;


-- =====================================================================
-- L. NOTIFICATIONS
-- =====================================================================

-- L1. Enqueue, idempotent on dedupe_key (e.g. 'leave.approved:{leave_request_id}:{user_id}:email').
INSERT INTO notifications (tenant_id, user_id, channel, type, template, payload_json, dedupe_key)
SELECT :tenant_id, :user_id, :channel, :type, :template, CAST(:payload_json AS jsonb), :dedupe_key
WHERE NOT EXISTS (
  SELECT 1 FROM notification_preferences np
  WHERE np.tenant_id = :tenant_id AND np.user_id = :user_id
    AND np.channel = :channel AND np.event_type = :type AND np.enabled = false)
ON CONFLICT (tenant_id, dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
RETURNING id;

-- L2. Email worker claims a batch (hrms_platform connection).
UPDATE notifications
SET status = 'sending', attempts = attempts + 1
WHERE id IN (
  SELECT id FROM notifications
  WHERE status = 'queued' AND channel = 'email'
  ORDER BY created_at
  LIMIT :batch_size
  FOR UPDATE SKIP LOCKED
)
RETURNING id, tenant_id, user_id, template, payload_json, attempts;

-- L3. Result. Bounded retries: back to 'queued' until attempts reach the cap.
UPDATE notifications SET status = 'sent', sent_at = now(), last_error = NULL WHERE id = :id;

UPDATE notifications
SET status = CASE WHEN attempts >= :max_attempts THEN 'failed' ELSE 'queued' END,
    last_error = :error
WHERE id = :id;

-- L4. Template lookup: the tenant override wins over the platform default.
SELECT subject, body
FROM notification_templates
WHERE code = :code AND channel = :channel AND (tenant_id = :tenant_id OR tenant_id IS NULL)
ORDER BY tenant_id NULLS LAST
LIMIT 1;

-- L5. In-app inbox + mark as read.
SELECT id, type, template, payload_json, status, created_at
FROM notifications
WHERE tenant_id = :tenant_id AND user_id = :user_id AND channel = 'in_app'
ORDER BY created_at DESC
LIMIT :limit;

UPDATE notifications SET status = 'read', read_at = now()
WHERE tenant_id = :tenant_id AND user_id = :user_id AND id = :id AND channel = 'in_app';


-- =====================================================================
-- M. IDEMPOTENCY + AI COMMANDS
-- =====================================================================

-- M1. Claim an idempotency key. A row back => first time, run the operation.
--     No row => look it up (M2): same request_hash -> replay the stored
--     response; different hash -> 409.
INSERT INTO idempotency_keys (tenant_id, user_id, key, request_hash)
VALUES (:tenant_id, :user_id, :key, :request_hash)
ON CONFLICT (tenant_id, user_id, key) DO NOTHING
RETURNING id;

-- M2.
SELECT request_hash, response_status, response_json
FROM idempotency_keys
WHERE tenant_id = :tenant_id AND user_id = :user_id AND key = :key;

-- M3. Store the response (same transaction as the operation itself).
UPDATE idempotency_keys
SET response_status = :response_status, response_json = CAST(:response_json AS jsonb)
WHERE tenant_id = :tenant_id AND user_id = :user_id AND key = :key;

-- M4. Record the incoming AI request BEFORE calling the model.
INSERT INTO ai_commands (tenant_id, user_id, message, status)
VALUES (:tenant_id, :user_id, :message, 'received')
RETURNING id;

-- M5. Record the outcome of each gate (schema / permission / validation / execution).
UPDATE ai_commands
SET intent = :intent, schema_version = :schema_version,
    arguments_json = CAST(:arguments_json AS jsonb),
    requires_confirmation = :requires_confirmation,
    status = :status, result_json = CAST(:result_json AS jsonb), error = :error,
    model = :model, input_tokens = :input_tokens, output_tokens = :output_tokens,
    confirm_expires_at = :confirm_expires_at
WHERE tenant_id = :tenant_id AND id = :ai_command_id AND user_id = :user_id;

-- M6. Confirm a high-risk command. Only its author, only once, only before expiry.
--     Permissions and business rules are RE-CHECKED after this, at execution time.
UPDATE ai_commands
SET status = 'received'
WHERE tenant_id = :tenant_id AND id = :ai_command_id AND user_id = :user_id
  AND status = 'awaiting_confirmation' AND confirm_expires_at > now()
RETURNING intent, schema_version, arguments_json;


-- =====================================================================
-- N. REPORTS
-- =====================================================================

-- N1. Headcount by department and status.
SELECT COALESCE(d.name, '(unassigned)') AS department, e.status, count(*) AS employees
FROM employees e
LEFT JOIN departments d ON d.tenant_id = e.tenant_id AND d.id = e.department_id
WHERE e.tenant_id = :tenant_id AND e.deleted_at IS NULL
GROUP BY d.name, e.status
ORDER BY department, e.status;

-- N2. Joiners vs leavers per month.
SELECT month,
       count(*) FILTER (WHERE kind = 'joined') AS joined,
       count(*) FILTER (WHERE kind = 'exited') AS exited
FROM (
  SELECT date_trunc('month', joining_date)::date AS month, 'joined' AS kind
  FROM employees WHERE tenant_id = :tenant_id AND joining_date >= :from_date
  UNION ALL
  SELECT date_trunc('month', exit_date)::date, 'exited'
  FROM employees WHERE tenant_id = :tenant_id AND exit_date >= :from_date
) x
GROUP BY month
ORDER BY month;

-- N3. Leave taken by type for a year.
SELECT lt.name AS leave_type, sum(b.used) AS days_used, sum(b.pending) AS days_pending,
       sum(b.allocated) AS days_allocated
FROM leave_balances b
JOIN leave_types lt ON lt.tenant_id = b.tenant_id AND lt.id = b.leave_type_id
WHERE b.tenant_id = :tenant_id AND b.year = :year
GROUP BY lt.name
ORDER BY lt.name;

-- N4. Dashboard tiles in one round trip.
SELECT
  (SELECT count(*) FROM employees
    WHERE tenant_id = :tenant_id AND deleted_at IS NULL AND status = 'active')            AS active_employees,
  (SELECT count(*) FROM attendance_records
    WHERE tenant_id = :tenant_id AND work_date = :today AND status = 'present')           AS present_today,
  (SELECT count(*) FROM leave_requests
    WHERE tenant_id = :tenant_id AND status = 'pending')                                  AS pending_leave_requests,
  (SELECT count(*) FROM jobs
    WHERE tenant_id = :tenant_id AND status = 'open')                                     AS open_jobs,
  (SELECT count(*) FROM workflow_tasks
    WHERE tenant_id = :tenant_id AND assignee_user_id = :user_id AND status = 'pending')  AS my_open_tasks;


-- =====================================================================
-- O. MAINTENANCE (scheduled worker, hrms_platform connection)
-- =====================================================================
DELETE FROM idempotency_keys WHERE expires_at < now();
DELETE FROM auth_tokens      WHERE expires_at < now() - interval '7 days';
DELETE FROM refresh_tokens   WHERE expires_at < now() - interval '30 days';

UPDATE ai_commands SET status = 'expired'
WHERE status = 'awaiting_confirmation' AND confirm_expires_at < now();

UPDATE documents SET status = 'expired'
WHERE status <> 'expired' AND expires_at < now() AND deleted_at IS NULL;

UPDATE subscriptions SET status = 'expired'
WHERE status = 'trialing' AND current_period_end < now();
