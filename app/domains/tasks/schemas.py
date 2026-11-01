import uuid
from datetime import date

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    assignee_employee_id: uuid.UUID
    due_date: date | None = None


class TaskStatusUpdate(BaseModel):
    status: str = Field(pattern="^(todo|in_progress|done|cancelled)$")
