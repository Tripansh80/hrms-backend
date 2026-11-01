"""Baseline: database/01_schema.sql, 02_seed.sql, 03_rls.sql.

These SQL files are frozen after this revision. Every later schema change is a new
Alembic revision — never an edit to the SQL files.

03_rls.sql creates the NOLOGIN roles hrms_app / hrms_platform (needs CREATEROLE /
superuser). In docker compose they already exist from docker/postgres-init.

Revision ID: 0001
Revises:
Create Date: 2026-10-04
"""

import os
import re
from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# backend/app/db/migrations/versions/ -> repo root
SQL_DIR = Path(os.environ.get("HRMS_SQL_DIR", Path(__file__).resolve().parents[5] / "database"))
FILES = ("01_schema.sql", "02_seed.sql", "03_rls.sql")

# The files wrap themselves in BEGIN; ... COMMIT; — Alembic already owns the transaction.
_TX = re.compile(r"^\s*(BEGIN|COMMIT)\s*;\s*$", re.IGNORECASE | re.MULTILINE)


def _run_file(name: str) -> None:
    sql = _TX.sub("", (SQL_DIR / name).read_text(encoding="utf-8"))
    # Raw DBAPI cursor, no parameters: the SQL contains '%' and ':' that must not be parsed as binds.
    cursor = op.get_bind().connection.cursor()
    try:
        cursor.execute(sql)
    finally:
        cursor.close()


def upgrade() -> None:
    for name in FILES:
        _run_file(name)


def downgrade() -> None:
    raise NotImplementedError("The baseline cannot be downgraded; drop and recreate the database.")
