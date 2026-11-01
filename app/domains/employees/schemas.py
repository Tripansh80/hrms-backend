"""Employee API schemas."""

import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_no: str | None = Field(default=None, min_length=1, max_length=40)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(default="", max_length=100)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=40)
    department_id: uuid.UUID | None = None
    team_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    work_schedule_id: uuid.UUID | None = None
    designation: str | None = Field(default=None, max_length=120)
    employment_type: Literal["full_time", "part_time", "contract", "intern"] = "full_time"
    status: Literal["onboarding", "active", "on_notice", "suspended", "exited"] = "onboarding"
    joining_date: date
    exit_date: date | None = None
    # Selecting roles also creates the employee's workspace account and invitation.
    role_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    send_invitation: bool = False


class EmployeeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=40)
    department_id: uuid.UUID | None = None
    team_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    work_schedule_id: uuid.UUID | None = None
    designation: str | None = Field(default=None, max_length=120)
    employment_type: Literal["full_time", "part_time", "contract", "intern"] | None = None
    status: Literal["onboarding", "active", "on_notice", "suspended", "exited"] | None = None
    joining_date: date | None = None
    exit_date: date | None = None
    role_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)


class EmployeeTransfer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    department_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None
    designation: str | None = Field(default=None, max_length=120)
    effective_date: date | None = None
