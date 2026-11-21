"""Provision a local Infosys demo workspace with all four application roles."""

import asyncio
import sys
from datetime import date

from sqlalchemy import text

from app.core.security import hash_password
from app.db.session import platform_session

TENANT = "infosys"
PASSWORD = "Demo@12345"  # noqa: S105 - local demo credential only

USERS = (
    ("admin@infosys.demo", "Infosys Admin", "Admin", True),
    ("hr@infosys.demo", "Infosys HR", "HR", False),
    ("manager@infosys.demo", "Infosys Manager", "Manager", False),
    ("employee@infosys.demo", "Infosys Employee", "Employee", False),
)


async def main() -> None:
    async with platform_session() as db:
        async with db.begin():
            plan_id = await db.scalar(text("SELECT id FROM plans WHERE code='growth' AND is_active"))
            if plan_id is None:
                raise RuntimeError("Growth plan is missing. Run migrations/seeds first.")

            tenant_id = await db.scalar(
                text(
                    """INSERT INTO tenants(name,slug,status,timezone,locale,plan_id)
                    VALUES ('Infosys','infosys','active','Asia/Kolkata','en',:plan)
                    ON CONFLICT(slug) DO UPDATE SET name=EXCLUDED.name,
                      status='active',plan_id=EXCLUDED.plan_id
                    RETURNING id"""
                ),
                {"plan": plan_id},
            )
            await db.execute(
                text(
                    """INSERT INTO subscriptions(tenant_id,plan_id,status,current_period_end)
                    VALUES (:tid,:plan,'active',now()+interval '1 year')
                    ON CONFLICT (tenant_id) WHERE status IN ('trialing','active','past_due')
                    DO UPDATE SET plan_id=EXCLUDED.plan_id,status='active'"""
                ),
                {"tid": tenant_id, "plan": plan_id},
            )
            await db.execute(
                text(
                    """INSERT INTO tenant_settings(tenant_id,key,value_json)
                    VALUES (:tid,'employee_no.format',CAST(:format_json AS jsonb))
                    ON CONFLICT (tenant_id,key) DO NOTHING"""
                ),
                {"tid": tenant_id, "format_json": '{"prefix":"INF","pad":4,"next":1}'},
            )

            role_ids: dict[str, object] = {}
            for role_name in ("Admin", "HR", "Manager", "Employee"):
                role_ids[role_name] = await db.scalar(
                    text("SELECT id FROM roles WHERE tenant_id IS NULL AND name=:name"),
                    {"name": role_name},
                )
                if role_ids[role_name] is None:
                    raise RuntimeError("Roles are not migrated. Run: alembic upgrade head")

            user_ids: dict[str, object] = {}
            for email, full_name, role_name, is_owner in USERS:
                user_id = await db.scalar(
                    text(
                        """INSERT INTO users(email,full_name,password_hash,is_active,email_verified_at)
                        VALUES (:email,:name,:password,true,now())
                        ON CONFLICT(email) DO UPDATE SET full_name=EXCLUDED.full_name,
                          password_hash=EXCLUDED.password_hash,is_active=true,email_verified_at=now()
                        RETURNING id"""
                    ),
                    {"email": email, "name": full_name, "password": hash_password(PASSWORD)},
                )
                user_ids[role_name] = user_id
                await db.execute(
                    text(
                        """INSERT INTO tenant_memberships(tenant_id,user_id,status,is_owner,joined_at)
                        VALUES (:tid,:uid,'active',:owner,now())
                        ON CONFLICT(tenant_id,user_id) DO UPDATE SET
                          status='active',is_owner=EXCLUDED.is_owner"""
                    ),
                    {"tid": tenant_id, "uid": user_id, "owner": is_owner},
                )
                await db.execute(
                    text("DELETE FROM user_roles WHERE tenant_id=:tid AND user_id=:uid"),
                    {"tid": tenant_id, "uid": user_id},
                )
                await db.execute(
                    text(
                        """INSERT INTO user_roles(tenant_id,user_id,role_id,granted_by)
                        VALUES (:tid,:uid,:role,:uid)"""
                    ),
                    {"tid": tenant_id, "uid": user_id, "role": role_ids[role_name]},
                )

            departments = (
                ("Engineering", "ENG"),
                ("Human Resources", "HR"),
                ("Operations", "OPS"),
            )
            department_ids: dict[str, object] = {}
            for name, code in departments:
                department_ids[code] = await db.scalar(
                    text(
                        """INSERT INTO departments(tenant_id,name,code)
                        VALUES (:tid,:name,:code)
                        ON CONFLICT(tenant_id,code) DO UPDATE SET name=EXCLUDED.name
                        RETURNING id"""
                    ),
                    {"tid": tenant_id, "name": name, "code": code},
                )

            rows = (
                ("INF0001", "Infosys", "Admin", "Chief Executive Officer", "ENG", "active", None),
                ("INF0002", "Infosys", "HR", "HR Business Partner", "HR", "active", None),
                ("INF0003", "Infosys", "Manager", "Engineering Manager", "ENG", "active", None),
                ("INF0004", "Infosys", "Employee", "Software Engineer", "ENG", "active", "Manager"),
            )
            employee_ids: dict[str, object] = {}
            for number, first, last, title, dept, status, manager_role in rows:
                user_id = user_ids[last]
                manager_id = employee_ids.get(manager_role)
                employee_ids[last] = await db.scalar(
                    text(
                        """INSERT INTO employees(
                          tenant_id,user_id,employee_no,first_name,last_name,email,department_id,manager_id,
                          designation,employment_type,status,joining_date)
                        VALUES (:tid,:uid,:number,:first,:last,
                          (SELECT email FROM users WHERE id=:uid),:dept,:manager,:title,
                          'full_time',:status,:joining)
                        ON CONFLICT(tenant_id,employee_no) DO UPDATE SET
                          user_id=EXCLUDED.user_id,first_name=EXCLUDED.first_name,last_name=EXCLUDED.last_name,
                          email=EXCLUDED.email,department_id=EXCLUDED.department_id,manager_id=EXCLUDED.manager_id,
                          designation=EXCLUDED.designation,status=EXCLUDED.status
                        RETURNING id"""
                    ),
                    {
                        "tid": tenant_id,
                        "uid": user_id,
                        "number": number,
                        "first": first,
                        "last": last,
                        "dept": department_ids[dept],
                        "manager": manager_id,
                        "title": title,
                        "status": status,
                        "joining": date(2026, 1, 15),
                    },
                )

            await db.execute(
                text(
                    """UPDATE departments SET manager_id=:manager
                    WHERE tenant_id=:tid AND code='ENG'"""
                ),
                {"tid": tenant_id, "manager": employee_ids["Manager"]},
            )
            await db.execute(
                text("UPDATE departments SET manager_id=:manager WHERE tenant_id=:tid AND code='HR'"),
                {"tid": tenant_id, "manager": employee_ids["HR"]},
            )

    print("Infosys demo workspace seeded.")
    print("Workspace: infosys")
    print(f"Password for all demo users: {PASSWORD}")
    for email, _, role, _ in USERS:
        print(f"{role}: {email}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
