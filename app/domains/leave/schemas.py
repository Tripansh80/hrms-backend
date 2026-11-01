"""Pydantic request/response schemas for the leave domain.

Never add a tenant_id field: the tenant comes from RequestContext only.
"""

import uuid
from datetime import date

from pydantic import BaseModel, Field, model_validator


class LeaveRequestCreate(BaseModel):
    employee_id: uuid.UUID | None = None
    leave_type_id: uuid.UUID
    approver_user_id: uuid.UUID | None = None
    start_date: date
    end_date: date
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_dates(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        return self


class LeaveDecision(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class LeaveTypeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    is_paid: bool | None = None
    is_active: bool | None = None
    annual_allocation: float | None = Field(default=None, ge=0)
    min_notice_days: int | None = Field(default=None, ge=0)
    max_consecutive_days: int | None = Field(default=None, ge=0)
    carry_forward_max: float | None = Field(default=None, ge=0)
    allow_negative: bool | None = None
