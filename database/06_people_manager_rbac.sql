-- Align existing installations with the department and people manager workflow.
-- Safe to run more than once.

INSERT INTO roles (tenant_id, name, description, is_system, data_scope)
VALUES (NULL, 'People Manager', 'Manages people and employment details', true, 'department')
ON CONFLICT (name) WHERE tenant_id IS NULL DO NOTHING;

UPDATE roles
SET data_scope = 'department'
WHERE tenant_id IS NULL AND name = 'Department Manager';

INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r
CROSS JOIN permissions p
WHERE r.tenant_id IS NULL
  AND r.name = 'Department Manager'
  AND p.code = 'employees.update'
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r
JOIN permissions p ON p.code = ANY (ARRAY[
  'tenant.read','employees.read','employees.create','employees.update',
  'attendance.read','leave.read','leave.request','leave.approve',
  'documents.read','policies.read','reports.read'])
WHERE r.tenant_id IS NULL
  AND r.name = 'People Manager'
ON CONFLICT DO NOTHING;
