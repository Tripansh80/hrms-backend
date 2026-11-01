"""Identity and session operations."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import Conflict, Unauthorized
from app.core.security import (
    create_access_token,
    generate_opaque_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.core.tenant import TenantContext
from app.db.session import platform_session

_DUMMY_HASH = hash_password("dummy password used only for timing")


async def login(
    db: AsyncSession, tenant: TenantContext, email: str, password: str, ip: str | None, user_agent: str | None
) -> tuple[str, str, uuid.UUID]:
    async with db.begin():
        row = (
            (
                await db.execute(
                    text("""
            SELECT u.id, u.password_hash, u.is_active, u.failed_login_count, u.locked_until,
                   m.status AS membership_status
            FROM users u JOIN tenant_memberships m ON m.user_id=u.id
            WHERE u.email=:email AND m.tenant_id=:tenant_id FOR UPDATE OF u
        """),
                    {"email": email, "tenant_id": tenant.id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            verify_password(_DUMMY_HASH, password)
            raise Unauthorized("Invalid email or password", code="auth.invalid_credentials")
        if not row["is_active"] or row["membership_status"] != "active":
            raise Unauthorized("Invalid email or password", code="auth.invalid_credentials")
        if row["locked_until"] and row["locked_until"] > datetime.now(UTC):
            raise Unauthorized("Invalid email or password", code="auth.invalid_credentials")
        if not verify_password(row["password_hash"], password):
            attempts = row["failed_login_count"] + 1
            await db.execute(
                text("UPDATE users SET failed_login_count=:n, locked_until=:until WHERE id=:id"),
                {
                    "n": attempts,
                    "until": datetime.now(UTC) + timedelta(minutes=15) if attempts >= 5 else None,
                    "id": row["id"],
                },
            )
            await db.execute(
                text("""INSERT INTO audit_logs(tenant_id,actor_user_id,action,source,ip_address)
                                     VALUES (:tid,:uid,'auth.login_failed','api',CAST(:ip AS inet))"""),
                {"tid": tenant.id, "uid": row["id"], "ip": ip},
            )
            await db.commit()  # retain the failed attempt and its audit record
            raise Unauthorized("Invalid email or password", code="auth.invalid_credentials")
        await db.execute(
            text(
                "UPDATE users SET failed_login_count=0, locked_until=NULL, last_login_at=now() WHERE id=:id"
            ),
            {"id": row["id"]},
        )
        refresh = generate_opaque_token()
        await db.execute(
            text("""INSERT INTO refresh_tokens(tenant_id,user_id,family_id,token_hash,expires_at,ip_address,user_agent)
            VALUES (:tid,:uid,:family,:hash,:exp,CAST(:ip AS inet),:ua)"""),
            {
                "tid": tenant.id,
                "uid": row["id"],
                "family": uuid.uuid4(),
                "hash": hash_token(refresh),
                "exp": datetime.now(UTC) + timedelta(days=get_settings().refresh_token_ttl_days),
                "ip": ip,
                "ua": user_agent,
            },
        )
        await db.execute(
            text("""INSERT INTO audit_logs(tenant_id,actor_user_id,action,source,ip_address)
            VALUES (:tid,:uid,'auth.login','api',CAST(:ip AS inet))"""),
            {"tid": tenant.id, "uid": row["id"], "ip": ip},
        )
        uid = row["id"]
    return create_access_token(uid, tenant.id), refresh, uid


async def rotate_refresh(
    db: AsyncSession, tenant: TenantContext, token: str, ip: str | None, user_agent: str | None
) -> tuple[str, str]:
    async with db.begin():
        row = (
            (
                await db.execute(
                    text("SELECT * FROM refresh_tokens WHERE tenant_id=:tid AND token_hash=:hash FOR UPDATE"),
                    {"tid": tenant.id, "hash": hash_token(token)},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise Unauthorized()
        if row["revoked_at"] is not None:
            await db.execute(
                text(
                    "UPDATE refresh_tokens SET revoked_at=coalesce(revoked_at,now()) WHERE family_id=:family"
                ),
                {"family": row["family_id"]},
            )
            await db.execute(
                text(
                    "INSERT INTO audit_logs(tenant_id,actor_user_id,action,source) VALUES (:tid,:uid,'auth.token_reuse','api')"
                ),
                {"tid": tenant.id, "uid": row["user_id"]},
            )
            await db.commit()  # family revocation must survive the 401 response
            raise Unauthorized(code="auth.refresh_reuse")
        if row["expires_at"] <= datetime.now(UTC):
            raise Unauthorized(code="auth.refresh_expired")
        new = generate_opaque_token()
        result = await db.execute(
            text("""INSERT INTO refresh_tokens(tenant_id,user_id,family_id,token_hash,expires_at,ip_address,user_agent)
          VALUES (:tid,:uid,:family,:hash,:exp,CAST(:ip AS inet),:ua) RETURNING id"""),
            {
                "tid": tenant.id,
                "uid": row["user_id"],
                "family": row["family_id"],
                "hash": hash_token(new),
                "exp": datetime.now(UTC) + timedelta(days=get_settings().refresh_token_ttl_days),
                "ip": ip,
                "ua": user_agent,
            },
        )
        replacement_id = result.scalar_one()
        await db.execute(
            text("UPDATE refresh_tokens SET revoked_at=now(),replaced_by=:replacement WHERE id=:id"),
            {"replacement": replacement_id, "id": row["id"]},
        )
        uid = row["user_id"]
    return create_access_token(uid, tenant.id), new


async def signup(name: str, slug: str, email: str, full_name: str, password: str) -> uuid.UUID:
    async with platform_session() as db:
        async with db.begin():
            try:
                result = await db.execute(
                    text("SELECT provision_tenant(:name,:slug,:email,:password,:full_name)"),
                    {
                        "name": name,
                        "slug": slug,
                        "email": email,
                        "password": hash_password(password),
                        "full_name": full_name,
                    },
                )
            except Exception as exc:
                if getattr(getattr(exc, "orig", None), "sqlstate", None) == "23505":
                    raise Conflict(
                        "Email or workspace slug is already in use", code="tenant.signup_conflict"
                    ) from exc
                raise
            return result.scalar_one()


async def accept_invitation(db: AsyncSession, tenant: TenantContext, token: str, password: str) -> None:
    async with db.begin():
        invitation = (
            (
                await db.execute(
                    text("""SELECT id,user_id FROM auth_tokens WHERE tenant_id=:tid AND token_hash=:hash
          AND purpose='invitation' AND used_at IS NULL AND expires_at>now() FOR UPDATE"""),
                    {"tid": tenant.id, "hash": hash_token(token)},
                )
            )
            .mappings()
            .first()
        )
        if invitation is None:
            raise Unauthorized("Invitation token is invalid or expired", code="auth.invitation_invalid")
        await db.execute(
            text(
                "UPDATE users SET password_hash=coalesce(password_hash,:password),is_active=true,email_verified_at=coalesce(email_verified_at,now()) WHERE id=:uid"
            ),
            {"password": hash_password(password), "uid": invitation["user_id"]},
        )
        await db.execute(
            text(
                "UPDATE tenant_memberships SET status='active',joined_at=now() WHERE tenant_id=:tid AND user_id=:uid AND status='invited'"
            ),
            {"tid": tenant.id, "uid": invitation["user_id"]},
        )
        await db.execute(text("UPDATE auth_tokens SET used_at=now() WHERE id=:id"), {"id": invitation["id"]})
        await db.execute(
            text(
                "INSERT INTO audit_logs(tenant_id,actor_user_id,action,entity_type,entity_id,source) VALUES (:tid,:uid,'membership.accepted','user',:uid,'api')"
            ),
            {"tid": tenant.id, "uid": invitation["user_id"]},
        )
