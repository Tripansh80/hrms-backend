"""Pydantic request/response schemas for the tenants domain."""

from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class TenantUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=160)
    timezone: str | None = Field(default=None, min_length=1, max_length=80)
    locale: str | None = Field(default=None, min_length=2, max_length=20)
    branding_json: dict[str, Any] | None = None


class InviteMember(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=160)


class MembershipUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern="^(active|suspended)$")
