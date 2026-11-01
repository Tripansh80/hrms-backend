"""Pydantic request/response schemas for the documents domain.

Never add a tenant_id field: the tenant comes from RequestContext only.
"""

import uuid

from pydantic import BaseModel, Field


class DocumentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    document_type: str = Field(min_length=1, max_length=80)
    file_name: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=120)
    size_bytes: int = Field(ge=0)
    storage_key: str = Field(min_length=1, max_length=500)
    employee_id: uuid.UUID | None = None
    expires_at: str | None = None
