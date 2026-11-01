"""Pydantic request/response schemas for the audit domain.

Never add a tenant_id field: the tenant comes from RequestContext only.
"""
