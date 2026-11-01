"""RBAC API schemas."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RoleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=100)
    description: str = Field(default="", max_length=500)
    data_scope: Literal["all", "department", "team", "self"] = "all"


class RoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=2, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    data_scope: Literal["all", "department", "team", "self"] | None = None


class PermissionSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    permissions: list[str] = Field(max_length=100)


class RoleSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role_ids: list[UUID] = Field(max_length=50)
