"""Consolidate system roles, scopes and authorization surfaces.

The application is pre-production with demo data only. Existing HR Manager and
Recruiter assignments are intentionally remapped to HR, and Auditor assignments
to Employee, because the old roles have no production data contract yet.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE roles DROP CONSTRAINT IF EXISTS roles_data_scope_check;
        ALTER TABLE roles ADD CONSTRAINT roles_data_scope_check
          CHECK (data_scope IN ('all','managed','department','team','self'));

        INSERT INTO permissions(code, description) VALUES
          ('departments.manage','Create, edit and archive departments'),
          ('attendance.approve','Approve attendance corrections'),
          ('directory.read','View the public company directory'),
          ('dashboard.admin','Open the Admin dashboard'),
          ('dashboard.hr','Open the HR dashboard'),
          ('dashboard.manager','Open the Manager dashboard'),
          ('dashboard.employee','Open the Employee dashboard'),
          ('reports.executive','View company-wide aggregate reports')
        ON CONFLICT (code) DO NOTHING;

        UPDATE roles SET name='Admin' WHERE tenant_id IS NULL AND name='Tenant Owner';
        UPDATE roles SET name='HR' WHERE tenant_id IS NULL AND name='HR Admin';
        UPDATE roles SET name='Manager' WHERE tenant_id IS NULL AND name='Department Manager';

        INSERT INTO user_roles(tenant_id,user_id,role_id,granted_by)
        SELECT ur.tenant_id,ur.user_id,target.id,ur.granted_by
        FROM user_roles ur
        JOIN roles old ON old.id=ur.role_id
        JOIN roles target ON target.tenant_id IS NULL AND target.name='HR'
        WHERE old.tenant_id IS NULL AND old.name IN ('HR Manager','Recruiter')
        ON CONFLICT DO NOTHING;
        INSERT INTO user_roles(tenant_id,user_id,role_id,granted_by)
        SELECT ur.tenant_id,ur.user_id,target.id,ur.granted_by
        FROM user_roles ur
        JOIN roles old ON old.id=ur.role_id
        JOIN roles target ON target.tenant_id IS NULL AND target.name='Manager'
        WHERE old.tenant_id IS NULL AND old.name='People Manager'
        ON CONFLICT DO NOTHING;
        INSERT INTO user_roles(tenant_id,user_id,role_id,granted_by)
        SELECT ur.tenant_id,ur.user_id,target.id,ur.granted_by
        FROM user_roles ur
        JOIN roles old ON old.id=ur.role_id
        JOIN roles target ON target.tenant_id IS NULL AND target.name='Employee'
        WHERE old.tenant_id IS NULL AND old.name='Auditor'
        ON CONFLICT DO NOTHING;
        DELETE FROM user_roles
        WHERE role_id IN (
          SELECT id FROM roles WHERE tenant_id IS NULL
            AND name IN ('HR Manager','Recruiter','People Manager','Auditor')
        );
        DELETE FROM roles WHERE tenant_id IS NULL
          AND name IN ('HR Manager','Recruiter','People Manager','Auditor');

        UPDATE roles SET data_scope = CASE name
          WHEN 'Admin' THEN 'all'
          WHEN 'HR' THEN 'all'
          WHEN 'Manager' THEN 'managed'
          WHEN 'Employee' THEN 'self'
        END
        WHERE tenant_id IS NULL AND name IN ('Admin','HR','Manager','Employee');

        DO $migration$
        DECLARE
          definition text;
        BEGIN
          SELECT pg_get_functiondef(p.oid) INTO definition
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid=p.pronamespace
          WHERE p.proname='provision_tenant' AND n.nspname=current_schema()
          LIMIT 1;
          IF definition IS NOT NULL THEN
            definition := replace(definition, 'name = ''Tenant Owner''', 'name = ''Admin''');
            definition := replace(
              definition,
              'VALUES (v_tenant_id, v_user_id, v_role_id, v_user_id);',
              'VALUES (v_tenant_id, v_user_id, v_role_id, v_user_id);
               INSERT INTO user_roles (tenant_id, user_id, role_id, granted_by)
               SELECT v_tenant_id, v_user_id, id, v_user_id
               FROM roles WHERE tenant_id IS NULL AND name = ''Employee'';'
            );
            EXECUTE definition;
          END IF;
        END
        $migration$;

        INSERT INTO user_roles(tenant_id,user_id,role_id)
        SELECT m.tenant_id,m.user_id,r.id
        FROM tenant_memberships m
        JOIN users u ON u.id=m.user_id AND u.is_active
        CROSS JOIN roles r
        WHERE m.status='active' AND r.tenant_id IS NULL AND r.name='Employee'
        ON CONFLICT DO NOTHING;
        INSERT INTO user_roles(tenant_id,user_id,role_id)
        SELECT m.tenant_id,m.user_id,r.id
        FROM tenant_memberships m
        JOIN roles r ON r.tenant_id IS NULL AND r.name='Admin'
        WHERE m.status='active' AND m.is_owner
        ON CONFLICT DO NOTHING;

        DELETE FROM role_permissions
        WHERE role_id IN (SELECT id FROM roles WHERE tenant_id IS NULL);
        INSERT INTO role_permissions(role_id,permission_id)
        SELECT r.id,p.id FROM roles r CROSS JOIN permissions p
        WHERE r.tenant_id IS NULL AND r.name='Admin';
        INSERT INTO role_permissions(role_id,permission_id)
        SELECT r.id,p.id FROM roles r JOIN permissions p ON p.code = ANY(ARRAY[
          'tenant.read','users.read','users.manage','roles.read','departments.manage',
          'teams.read','teams.manage','directory.read','employees.read','employees.create',
          'employees.delete','employees.update','attendance.read','attendance.approve',
          'attendance.manage','leave.read','leave.request','leave.approve','documents.read',
          'documents.manage','policies.read','policies.manage','imports.read','imports.create',
          'recruitment.read','recruitment.manage','workflows.read','ai.execute','dashboard.hr',
          'dashboard.employee','reports.read'
        ]) WHERE r.tenant_id IS NULL AND r.name='HR';
        INSERT INTO role_permissions(role_id,permission_id)
        SELECT r.id,p.id FROM roles r JOIN permissions p ON p.code = ANY(ARRAY[
          'tenant.read','teams.read','directory.read','employees.read','attendance.read',
          'attendance.approve','leave.read','leave.request','leave.approve','policies.read',
          'ai.execute','dashboard.manager','dashboard.employee','reports.read'
        ]) WHERE r.tenant_id IS NULL AND r.name='Manager';
        INSERT INTO role_permissions(role_id,permission_id)
        SELECT r.id,p.id FROM roles r JOIN permissions p ON p.code = ANY(ARRAY[
          'tenant.read','teams.read','directory.read','employees.read','employees.update',
          'attendance.read','leave.read','leave.request','documents.read','policies.read',
          'ai.execute','dashboard.employee'
        ]) WHERE r.tenant_id IS NULL AND r.name='Employee';

        CREATE OR REPLACE FUNCTION managed_employee_ids(p_tenant uuid, p_user uuid)
        RETURNS TABLE (employee_id uuid)
        LANGUAGE sql STABLE SECURITY INVOKER AS $$
          WITH RECURSIVE
          me AS (
            SELECT id FROM employees
            WHERE tenant_id=p_tenant AND user_id=p_user AND deleted_at IS NULL
          ),
          reports(id) AS (
            SELECT e.id FROM employees e JOIN me ON e.manager_id=me.id
            WHERE e.tenant_id=p_tenant AND e.deleted_at IS NULL
            UNION
            SELECT e.id FROM employees e JOIN reports r ON e.manager_id=r.id
            WHERE e.tenant_id=p_tenant AND e.deleted_at IS NULL
          ),
          depts(id) AS (
            SELECT d.id FROM departments d JOIN me ON d.manager_id=me.id
            WHERE d.tenant_id=p_tenant AND d.deleted_at IS NULL
            UNION
            SELECT d.id FROM departments d JOIN depts x ON d.parent_id=x.id
            WHERE d.tenant_id=p_tenant AND d.deleted_at IS NULL
          )
          SELECT id FROM me
          UNION SELECT id FROM reports
          UNION SELECT e.id FROM employees e JOIN depts ON e.department_id=depts.id
            WHERE e.tenant_id=p_tenant AND e.deleted_at IS NULL
          UNION SELECT e.id FROM employees e
            JOIN teams t ON t.tenant_id=e.tenant_id AND t.id=e.team_id
            JOIN me ON t.manager_employee_id=me.id
            WHERE e.tenant_id=p_tenant AND e.deleted_at IS NULL AND t.deleted_at IS NULL;
        $$;
        CREATE INDEX IF NOT EXISTS ix_employees_manager_active
          ON employees (tenant_id, manager_id) WHERE deleted_at IS NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS managed_employee_ids(uuid, uuid);")
    op.execute("DROP INDEX IF EXISTS ix_employees_manager_active;")
    raise NotImplementedError("Role consolidation is irreversible; drop and recreate the database.")
