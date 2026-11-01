"""Pydantic request/response schemas for the policies domain.

Never add a tenant_id field: the tenant comes from RequestContext only.
"""

from pydantic import BaseModel, Field


class PolicyCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=100000)
