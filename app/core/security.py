"""Password hashing (argon2id), JWT access tokens, opaque token generation and hashing."""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import get_settings
from app.core.errors import Unauthorized

_hasher = PasswordHasher()  # argon2id with library defaults


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def generate_opaque_token() -> str:
    """Refresh / reset / invite tokens. Only hash_token(value) is ever stored."""
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class AccessClaims:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    jti: str


def create_access_token(user_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "typ": "access",
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + timedelta(seconds=s.access_token_ttl_seconds),
    }
    return jwt.encode(payload, s.jwt_secret.get_secret_value(), algorithm=s.jwt_algorithm)


def decode_access_token(token: str) -> AccessClaims:
    s = get_settings()
    try:
        payload = jwt.decode(
            token,
            s.jwt_secret.get_secret_value(),
            algorithms=[s.jwt_algorithm],
            options={"require": ["sub", "tid", "exp", "iat", "jti"]},
        )
        if payload.get("typ") != "access":
            raise Unauthorized(code="auth.invalid_token")
        return AccessClaims(uuid.UUID(payload["sub"]), uuid.UUID(payload["tid"]), payload["jti"])
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("Token expired", code="auth.token_expired") from exc
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise Unauthorized(code="auth.invalid_token") from exc
