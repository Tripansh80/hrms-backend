"""Tenant-scoped employee import parsing, validation and commit logic."""

import csv
import io
import json
import uuid
from datetime import date

from fastapi import UploadFile
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed

HEADERS = {
    "employee number": "employee_no",
    "employee no": "employee_no",
    "employee_id": "employee_no",
    "first name": "first_name",
    "last name": "last_name",
    "email": "email",
    "work email": "email",
    "phone": "phone",
    "designation": "designation",
    "job title": "designation",
    "department": "department",
    "team": "team",
    "reporting manager": "manager",
    "manager": "manager",
    "employment type": "employment_type",
    "joining date": "joining_date",
    "location": "location",
}
REQUIRED = ("employee_no", "first_name", "last_name", "email", "joining_date")
TYPE_MAP = {
    "full-time": "full_time",
    "full time": "full_time",
    "part-time": "part_time",
    "part time": "part_time",
    "contract": "contract",
    "intern": "intern",
}


def enabled() -> bool:
    return get_settings().feature_employee_import


def _check_enabled() -> None:
    if not enabled():
        raise NotFound("Employee bulk import is not enabled", code="imports.disabled")


def _key(value: object) -> str:
    return str(value or "").strip().lower()


async def parse_file(file: UploadFile) -> list[dict]:
    name = (file.filename or "").lower()
    if not name.endswith((".csv", ".xlsx", ".xlsm")):
        raise ValidationFailed("Only CSV and Excel files are supported", code="imports.file_type")
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise ValidationFailed("Import file must be 10 MB or smaller", code="imports.file_too_large")
    try:
        if name.endswith(".csv"):
            text_data = data.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text_data))
            headers = [_key(h) for h in (reader.fieldnames or [])]
            rows = [
                {HEADERS.get(h, h): (value or "").strip() for h, value in zip(headers, values, strict=False)}
                for values in reader
            ]
        else:
            from openpyxl import load_workbook

            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            sheet = workbook.active
            values = list(sheet.values)
            headers = [_key(h) for h in (values[0] if values else [])]
            rows = [
                {HEADERS.get(h, h): str(value or "").strip() for h, value in zip(headers, row, strict=False)}
                for row in values[1:]
            ]
    except Exception as exc:
        raise ValidationFailed("The file could not be parsed", code="imports.parse_error") from exc
    if not rows:
        raise ValidationFailed("The file contains no data rows", code="imports.empty")
    if len(rows) > 10_000:
        raise ValidationFailed("An import may contain at most 10,000 rows", code="imports.too_many_rows")
    return rows


async def _validate(db: AsyncSession, ctx: RequestContext, rows: list[dict]) -> list[dict]:
    employee_numbers: dict[str, int] = {}
    emails: dict[str, int] = {}
    errors: list[dict] = []
    async with db.begin():
        existing = (
            await db.execute(
                text("SELECT employee_no, email FROM employees WHERE tenant_id=:tid AND deleted_at IS NULL"),
                {"tid": ctx.tenant_id},
            )
        ).all()
        existing_numbers = {_key(r[0]) for r in existing}
        existing_emails = {_key(r[1]) for r in existing}
        departments = {
            _key(r[1]): r[0]
            for r in (
                await db.execute(
                    text("SELECT id,name FROM departments WHERE tenant_id=:tid AND deleted_at IS NULL"),
                    {"tid": ctx.tenant_id},
                )
            ).all()
        }
        locations = {
            _key(r[1]): r[0]
            for r in (
                await db.execute(
                    text("SELECT id,name FROM locations WHERE tenant_id=:tid AND is_active"),
                    {"tid": ctx.tenant_id},
                )
            ).all()
        }
        managers = {
            _key(r[0]): r[1]
            for r in (
                await db.execute(
                    text("SELECT employee_no,id FROM employees WHERE tenant_id=:tid AND deleted_at IS NULL"),
                    {"tid": ctx.tenant_id},
                )
            ).all()
        }
        managers.update(
            {
                _key(r[0]): r[1]
                for r in (
                    await db.execute(
                        text("SELECT email,id FROM employees WHERE tenant_id=:tid AND deleted_at IS NULL"),
                        {"tid": ctx.tenant_id},
                    )
                ).all()
            }
        )
    # Make all employee numbers available first so forward references are validated too.
    for row in rows:
        if _key(row.get("employee_no")):
            managers.setdefault(_key(row["employee_no"]), None)
    for number, row in enumerate(rows, 2):
        row_errors: list[dict] = []
        for field in REQUIRED:
            if not _key(row.get(field)):
                row_errors.append(
                    {"row_number": number, "field": field, "message": "Required value is missing"}
                )
        no, email = _key(row.get("employee_no")), _key(row.get("email"))
        if no in existing_numbers or no in employee_numbers:
            row_errors.append(
                {"row_number": number, "field": "employee_no", "message": "Employee number already exists"}
            )
        if email in existing_emails or email in emails:
            row_errors.append({"row_number": number, "field": "email", "message": "Email already exists"})
        if no:
            employee_numbers[no] = number
        if email:
            emails[email] = number
        if row.get("department") and _key(row["department"]) not in departments:
            row_errors.append(
                {"row_number": number, "field": "department", "message": "Department not found"}
            )
        if row.get("location") and _key(row["location"]) not in locations:
            row_errors.append({"row_number": number, "field": "location", "message": "Location not found"})
        if row.get("team"):
            row_errors.append(
                {
                    "row_number": number,
                    "field": "team",
                    "message": "Teams are not configured for this workspace",
                }
            )
        employment = TYPE_MAP.get(_key(row.get("employment_type")) or "full-time")
        if employment is None:
            row_errors.append(
                {"row_number": number, "field": "employment_type", "message": "Unsupported employment type"}
            )
        raw_date = _key(row.get("joining_date"))
        try:
            parsed_date = date.fromisoformat(raw_date)
        except ValueError:
            parsed_date = None
            row_errors.append({"row_number": number, "field": "joining_date", "message": "Use YYYY-MM-DD"})
        if (
            row.get("manager")
            and _key(row["manager"]) not in managers
            and _key(row["manager"]) not in employee_numbers
        ):
            row_errors.append(
                {"row_number": number, "field": "manager", "message": "Reporting manager not found"}
            )
        row["_department_id"] = (
            str(departments.get(_key(row.get("department")))) if row.get("department") else None
        )
        row["_location_id"] = str(locations.get(_key(row.get("location")))) if row.get("location") else None
        row["_manager_id"] = str(managers.get(_key(row.get("manager")))) if row.get("manager") else None
        row["_employment_type"] = employment
        row["_joining_date"] = parsed_date.isoformat() if parsed_date else None
        errors.extend(row_errors)
    return errors


async def create_job(db: AsyncSession, ctx: RequestContext, file: UploadFile) -> dict:
    _check_enabled()
    ctx.require("imports.create")
    rows = await parse_file(file)
    errors = await _validate(db, ctx, rows)
    job_id = uuid.uuid4()
    async with db.begin():
        await db.execute(
            text("""INSERT INTO import_jobs
            (id,tenant_id,created_by,entity_type,file_key,file_name,status,total_rows,processed_rows,failed_rows,rows_json)
            VALUES (:id,:tid,:uid,'employees',:key,:name,:status,:total,:processed,:failed,
                    CAST(:rows AS jsonb))"""),
            {
                "id": job_id,
                "tid": ctx.tenant_id,
                "uid": ctx.user_id,
                "key": f"inline:{job_id}",
                "name": file.filename,
                "status": "queued",
                "total": len(rows),
                "processed": len(rows),
                "failed": len({e["row_number"] for e in errors}),
                "rows": json.dumps(rows),
            },
        )
        for error in errors:
            await db.execute(
                text("""INSERT INTO import_errors
                (tenant_id,import_job_id,row_number,field,message,raw_data)
                VALUES (:tid,:job,:row,:field,:message,CAST(:raw AS jsonb))"""),
                {
                    "tid": ctx.tenant_id,
                    "job": job_id,
                    "row": error["row_number"],
                    "field": error["field"],
                    "message": error["message"],
                    "raw": json.dumps(rows[error["row_number"] - 2]),
                },
            )
    return await get_job(db, ctx, job_id)


async def get_job(db: AsyncSession, ctx: RequestContext, job_id: uuid.UUID) -> dict:
    _check_enabled()
    ctx.require("imports.read")
    async with db.begin():
        row = (
            (
                await db.execute(
                    text("""SELECT id,file_name,status,total_rows,processed_rows,failed_rows,
            error,created_at,finished_at FROM import_jobs WHERE tenant_id=:tid AND id=:id"""),
                    {"tid": ctx.tenant_id, "id": job_id},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise NotFound("Import job not found", code="imports.not_found")
    return dict(row)


async def list_jobs(db: AsyncSession, ctx: RequestContext) -> dict:
    _check_enabled()
    ctx.require("imports.read")
    async with db.begin():
        rows = (
            (
                await db.execute(
                    text("""SELECT id,file_name,status,total_rows,processed_rows,failed_rows,
            created_at,finished_at FROM import_jobs
            WHERE tenant_id=:tid ORDER BY created_at DESC LIMIT 100"""),
                    {"tid": ctx.tenant_id},
                )
            )
            .mappings()
            .all()
        )
    return {"items": [dict(row) for row in rows]}


async def errors(db: AsyncSession, ctx: RequestContext, job_id: uuid.UUID) -> dict:
    _check_enabled()
    ctx.require("imports.read")
    async with db.begin():
        rows = (
            (
                await db.execute(
                    text("""SELECT row_number,field,message,raw_data FROM import_errors
            WHERE tenant_id=:tid AND import_job_id=:id ORDER BY row_number,id"""),
                    {"tid": ctx.tenant_id, "id": job_id},
                )
            )
            .mappings()
            .all()
        )
    return {"items": [dict(row) for row in rows]}


async def preview(db: AsyncSession, ctx: RequestContext, job_id: uuid.UUID) -> dict:
    _check_enabled()
    ctx.require("imports.read")
    async with db.begin():
        rows = await db.scalar(
            text("SELECT rows_json FROM import_jobs WHERE tenant_id=:tid AND id=:id"),
            {"tid": ctx.tenant_id, "id": job_id},
        )
    if rows is None:
        raise NotFound("Import job not found", code="imports.not_found")
    return {"items": [{key: value for key, value in row.items() if not key.startswith("_")} for row in rows]}


async def execute(db: AsyncSession, ctx: RequestContext, job_id: uuid.UUID) -> dict:
    _check_enabled()
    ctx.require("imports.create")
    async with db.begin():
        job = (
            (
                await db.execute(
                    text("SELECT * FROM import_jobs WHERE tenant_id=:tid AND id=:id FOR UPDATE"),
                    {"tid": ctx.tenant_id, "id": job_id},
                )
            )
            .mappings()
            .first()
        )
        if job is None:
            raise NotFound("Import job not found", code="imports.not_found")
        rows = job["rows_json"] or []
    validation_errors = await _validate(db, ctx, rows)
    valid_rows = {i for i in range(len(rows)) if not any(e["row_number"] == i + 2 for e in validation_errors)}
    imported = 0
    inserted: dict[str, uuid.UUID] = {}
    async with db.begin():
        # Re-read with the lock and only commit rows that passed the complete validation.
        for index in sorted(valid_rows):
            row = rows[index]
            manager_id = row.get("_manager_id")
            if manager_id is None and row.get("manager"):
                manager_id = await db.scalar(
                    text(
                        """SELECT id FROM employees
                        WHERE tenant_id=:tid AND (employee_no=:ref OR email=:ref)
                        AND deleted_at IS NULL"""
                    ),
                    {"tid": ctx.tenant_id, "ref": row["manager"].strip()},
                )
            await db.execute(
                text("""INSERT INTO employees
                (tenant_id,employee_no,first_name,last_name,email,phone,designation,department_id,manager_id,
                 location_id,employment_type,joining_date,status)
                VALUES (:tid,:no,:first,:last,:email,:phone,:designation,:department,:manager,:location,
                        :employment,:joining,'onboarding')"""),
                {
                    "tid": ctx.tenant_id,
                    "no": row["employee_no"].strip(),
                    "first": row["first_name"].strip(),
                    "last": row["last_name"].strip(),
                    "email": row["email"].strip(),
                    "phone": row.get("phone") or None,
                    "designation": row.get("designation") or None,
                    "department": row.get("_department_id"),
                    "manager": manager_id,
                    "location": row.get("_location_id"),
                    "employment": row["_employment_type"],
                    "joining": row["_joining_date"],
                },
            )
            inserted[row["employee_no"].strip().lower()] = await db.scalar(
                text("SELECT id FROM employees WHERE tenant_id=:tid AND employee_no=:no"),
                {"tid": ctx.tenant_id, "no": row["employee_no"].strip()},
            )
            imported += 1
        # Resolve managers that refer to another row in this same import after all inserts exist.
        for index in sorted(valid_rows):
            row = rows[index]
            ref = _key(row.get("manager"))
            if ref in inserted:
                await db.execute(
                    text("""UPDATE employees SET manager_id=:manager
                    WHERE tenant_id=:tid AND employee_no=:no"""),
                    {"manager": inserted[ref], "tid": ctx.tenant_id, "no": row["employee_no"].strip()},
                )
        status = "completed" if not validation_errors else ("completed_with_errors" if imported else "failed")
        await db.execute(
            text("""UPDATE import_jobs SET status=:status,processed_rows=:processed,failed_rows=:failed,
            started_at=COALESCE(started_at,now()),finished_at=now(),updated_at=now()
            WHERE tenant_id=:tid AND id=:id"""),
            {
                "status": status,
                "processed": len(rows),
                "failed": len(rows) - imported,
                "tid": ctx.tenant_id,
                "id": job_id,
            },
        )
    return await get_job(db, ctx, job_id)
