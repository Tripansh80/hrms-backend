CREATE TABLE IF NOT EXISTS employee_tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  title text NOT NULL,
  description text,
  assignee_employee_id uuid NOT NULL,
  created_by uuid NOT NULL REFERENCES users(id),
  status text NOT NULL DEFAULT 'todo' CHECK (status IN ('todo','in_progress','done','cancelled')),
  due_date date,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, assignee_employee_id) REFERENCES employees (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS ix_employee_tasks_assignee
  ON employee_tasks (tenant_id, assignee_employee_id, status);
