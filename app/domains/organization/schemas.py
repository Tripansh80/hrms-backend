"""Schemas for tenant organization and schedule resources."""

import uuid
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DepartmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    code: str | None = Field(default=None, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    parent_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None

    @field_validator("code", mode="before")
    @classmethod
    def blank_code_is_optional(cls, value: str | None) -> str | None:
        return value.strip() or None if isinstance(value, str) else value


class DepartmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    code: str | None = Field(default=None, min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    parent_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None


class TeamCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    code: str | None = Field(default=None, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    department_id: uuid.UUID
    manager_employee_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=500)
    status: str = Field(default="active", pattern=r"^(active|archived)$")


class TeamUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    code: str | None = Field(default=None, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    department_id: uuid.UUID | None = None
    manager_employee_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=500)
    status: str | None = Field(default=None, pattern=r"^(active|archived)$")


class LocationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    address: str | None = Field(default=None, max_length=500)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    timezone: str = Field(default="UTC", max_length=80)
    is_active: bool = True


class LocationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    address: str | None = Field(default=None, max_length=500)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    timezone: str | None = Field(default=None, max_length=80)
    is_active: bool | None = None


class ScheduleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    timezone: str = Field(default="UTC", max_length=80)
    rules_json: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False


class ScheduleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    timezone: str | None = Field(default=None, max_length=80)
    rules_json: dict[str, Any] | None = None
    is_default: bool | None = None


class HolidayCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    location_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=120)
    holiday_date: date
    is_optional: bool = False


class HolidayUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    location_id: uuid.UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    holiday_date: date | None = None
    is_optional: bool | None = None
