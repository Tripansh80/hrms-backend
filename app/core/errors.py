"""AppError hierarchy and the global exception handlers.

Every failure leaves the API in one shape:
    {"error": {"code": "...", "message": "...", "details": {}, "request_id": "..."}}
"""

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError

log = structlog.get_logger(__name__)


class AppError(Exception):
    status_code = 400
    code = "error"
    message = "Request failed"

    def __init__(
        self, message: str | None = None, *, code: str | None = None, details: dict[str, Any] | None = None
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details or {}
        super().__init__(self.message)


class Unauthorized(AppError):
    status_code, code, message = 401, "auth.unauthorized", "Not authenticated"


class Forbidden(AppError):
    status_code, code, message = 403, "authz.forbidden", "You do not have permission to do this"


class NotFound(AppError):
    """Also used for rows of another tenant or outside the caller's data scope (never 403)."""

    status_code, code, message = 404, "not_found", "Not found"


class FeatureDisabled(NotFound):
    code, message = "feature.disabled", "This feature is not enabled"


class Conflict(AppError):
    status_code, code, message = 409, "conflict", "Conflicts with the current state"


class ValidationFailed(AppError):
    status_code, code, message = 422, "validation_error", "Validation failed"


class PlanLimitReached(AppError):
    status_code, code, message = 402, "plan.limit_reached", "Your plan limit has been reached"


class RateLimited(AppError):
    status_code, code, message = 429, "rate_limited", "Too many requests"


# PostgreSQL SQLSTATEs that mean "conflicts with existing data".
_CONFLICT_SQLSTATES = {"23505": "conflict.duplicate", "23P01": "conflict.overlap"}


def _body(request: Request, code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": getattr(request.state, "request_id", None),
        }
    }


def _sqlstate(exc: DBAPIError) -> str | None:
    orig = exc.orig
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


async def _app_error(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(_body(request, exc.code, exc.message, exc.details), status_code=exc.status_code)


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = {"errors": [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]}
    return JSONResponse(_body(request, "validation_error", "Validation failed", details), status_code=422)


async def _integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
    state = _sqlstate(exc)
    if state in _CONFLICT_SQLSTATES:
        return JSONResponse(
            _body(request, _CONFLICT_SQLSTATES[state], "Conflicts with existing data"), status_code=409
        )
    log.exception("integrity_error", sqlstate=state)
    return JSONResponse(_body(request, "internal_error", "Internal server error"), status_code=500)


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_error")
    return JSONResponse(_body(request, "internal_error", "Internal server error"), status_code=500)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(IntegrityError, _integrity_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled)
