-- Persist parsed, tenant-scoped employee import rows between preview and commit.
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS rows_json jsonb NOT NULL DEFAULT '[]'::jsonb;
