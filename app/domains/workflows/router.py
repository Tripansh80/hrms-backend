"""HTTP layer for the workflows domain: parse, call the service, shape the response."""

from fastapi import APIRouter

router = APIRouter(prefix="/workflows", tags=["workflows"])
