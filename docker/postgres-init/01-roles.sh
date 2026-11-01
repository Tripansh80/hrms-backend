#!/bin/sh
# Runs once, as superuser, when the postgres volume is first created.
# Group roles match database/03_rls.sql; login roles are what the app connects as.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE hrms_app NOLOGIN;
CREATE ROLE hrms_platform NOLOGIN BYPASSRLS;
CREATE ROLE hrms_api   LOGIN PASSWORD '${HRMS_API_PASSWORD}'   IN ROLE hrms_app;
CREATE ROLE hrms_admin LOGIN PASSWORD '${HRMS_ADMIN_PASSWORD}' IN ROLE hrms_platform;
ALTER ROLE hrms_admin BYPASSRLS;  -- BYPASSRLS is not inherited through membership
SQL
