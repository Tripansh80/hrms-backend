import uuid

import jwt
import pytest

from app.core.config import get_settings
from app.core.errors import Unauthorized
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    hash_token,
    verify_password,
)


def test_password_roundtrip() -> None:
    h = hash_password("correct horse")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "correct horse")
    assert not verify_password(h, "wrong")
    assert not verify_password(None, "anything")


def test_token_hash_is_stable_and_not_the_token() -> None:
    assert hash_token("abc") == hash_token("abc")
    assert hash_token("abc") != "abc"


def test_access_token_carries_user_and_tenant() -> None:
    user_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    claims = decode_access_token(create_access_token(user_id, tenant_id))
    assert claims.user_id == user_id
    assert claims.tenant_id == tenant_id


def test_tampered_or_foreign_token_rejected() -> None:
    forged = jwt.encode({"sub": str(uuid.uuid4()), "tid": str(uuid.uuid4())}, "x" * 40, algorithm="HS256")
    with pytest.raises(Unauthorized):
        decode_access_token(forged)


def test_expired_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "access_token_ttl_seconds", -1)
    token = create_access_token(uuid.uuid4(), uuid.uuid4())
    with pytest.raises(Unauthorized) as exc:
        decode_access_token(token)
    assert exc.value.code == "auth.token_expired"
