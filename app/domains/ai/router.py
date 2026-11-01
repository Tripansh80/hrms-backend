"""HTTP layer for the ai domain: parse, call the service, shape the response."""

from fastapi import APIRouter

router = APIRouter(prefix="/ai", tags=["ai"])
