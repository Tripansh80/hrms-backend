"""HTTP layer for the reports domain: parse, call the service, shape the response."""

from fastapi import APIRouter

router = APIRouter(prefix="/reports", tags=["reports"])
