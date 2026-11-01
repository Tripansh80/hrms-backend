"""Request schemas for attendance endpoints."""

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator, model_validator


class CheckIn(BaseModel):
    notes: str | None = Field(default=None, max_length=2000)


class CorrectionCreate(BaseModel):
    work_date: date
    requested_check_in: datetime | None = None
    requested_check_out: datetime | None = None
    reason: str = Field(min_length=3, max_length=2000)

    @field_validator("requested_check_in", "requested_check_out")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Attendance correction timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_times(self):
        if not self.requested_check_in and not self.requested_check_out:
            raise ValueError("At least one requested attendance time is required")
        if self.requested_check_out and not self.requested_check_in:
            raise ValueError("requested_check_in is required when requesting check-out")
        if self.requested_check_in and self.requested_check_out:
            if self.requested_check_out < self.requested_check_in:
                raise ValueError("requested_check_out must not precede requested_check_in")
        return self


class Decision(BaseModel):
    comment: str | None = Field(default=None, max_length=2000)


def parse_month(value: str) -> tuple[date, date]:
    try:
        year, month = (int(part) for part in value.split("-"))
        start = date(year, month, 1)
    except (ValueError, TypeError):
        raise ValueError("month must be in YYYY-MM format") from None
    end = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return start, end
