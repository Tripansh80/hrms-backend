"""HTTP layer for the recruitment domain: parse, call the service, shape the response."""

from fastapi import APIRouter

router = APIRouter(prefix="/recruitment", tags=["recruitment"])
