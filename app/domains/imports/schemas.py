"""Schemas for the employee bulk import API."""

from pydantic import BaseModel, Field


class ImportExecute(BaseModel):
    confirm: bool = Field(default=False)
