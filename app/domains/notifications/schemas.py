"""Pydantic request/response schemas for the notifications domain.

Never add a tenant_id field: the tenant comes from RequestContext only.
"""

import uuid

from pydantic import BaseModel


class NotificationRead(BaseModel):
    id: uuid.UUID
