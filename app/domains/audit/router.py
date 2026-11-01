"""HTTP layer for the audit domain: parse, call the service, shape the response."""

from fastapi import APIRouter

router = APIRouter(prefix="/audit-logs", tags=["audit"])
