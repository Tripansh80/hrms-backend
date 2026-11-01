"""Add optional tenant-scoped teams and employee team assignments."""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE teams (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          department_id uuid NOT NULL,
          name text NOT NULL,
          code text,
          manager_employee_id uuid,
          description text,
          status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          deleted_at timestamptz,
          UNIQUE (tenant_id, id),
          UNIQUE (tenant_id, name),
          UNIQUE (tenant_id, code),
          FOREIGN KEY (tenant_id, department_id) REFERENCES departments(tenant_id, id),
          FOREIGN KEY (tenant_id, manager_employee_id) REFERENCES employees(tenant_id, id)
        );
        CREATE INDEX ix_teams_department ON teams (tenant_id, department_id);
        ALTER TABLE employees ADD COLUMN IF NOT EXISTS team_id uuid;
        ALTER TABLE employees ADD CONSTRAINT fk_employees_team
          FOREIGN KEY (tenant_id, team_id) REFERENCES teams(tenant_id, id);
        CREATE INDEX ix_employees_team ON employees (tenant_id, team_id);
        INSERT INTO permissions(code, description) VALUES
          ('teams.read', 'View teams'),
          ('teams.manage', 'Create, update and archive teams')
        ON CONFLICT (code) DO NOTHING;
        INSERT INTO role_permissions(role_id, permission_id)
        SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
        WHERE r.tenant_id IS NULL AND r.name IN ('Tenant Owner','HR Admin')
          AND p.code IN ('teams.read','teams.manage')
        ON CONFLICT DO NOTHING;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM role_permissions
        WHERE permission_id IN (SELECT id FROM permissions WHERE code IN ('teams.read','teams.manage'));
        DELETE FROM permissions WHERE code IN ('teams.read','teams.manage');
        DROP INDEX IF EXISTS ix_employees_team;
        ALTER TABLE employees DROP CONSTRAINT IF EXISTS fk_employees_team;
        ALTER TABLE employees DROP COLUMN IF EXISTS team_id;
        DROP TABLE IF EXISTS teams;
        """
    )
