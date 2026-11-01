"""Permission dependency and data-scope helpers.

A permission says WHAT you may do; the role's data_scope says TO WHOM.
`require()` on a route is a convenience — the service check is the guarantee.
"""

from collections.abc import Awaitable, Callable, Iterable
from enum import StrEnum

from fastapi import Depends

from app.core.context import RequestContext, get_request_context


class DataScope(StrEnum):
    ALL = "all"
    DEPARTMENT = "department"
    TEAM = "team"
    SELF = "self"


_WIDTH = {DataScope.ALL: 4, DataScope.DEPARTMENT: 3, DataScope.TEAM: 2, DataScope.SELF: 1}


def widest_scope(scopes: Iterable[str]) -> DataScope | None:
    """A user with several roles gets the widest scope among the roles granting the permission (A6)."""
    parsed = [DataScope(s) for s in scopes]
    return max(parsed, key=_WIDTH.__getitem__) if parsed else None


def require(code: str) -> Callable[[RequestContext], Awaitable[RequestContext]]:
    """Route dependency: `ctx: RequestContext = Depends(require("employees.create"))`."""

    async def dependency(ctx: RequestContext = Depends(get_request_context)) -> RequestContext:
        ctx.require(code)
        return ctx

    return dependency


# TODO(M2): scope_filter(ctx, code) -> SQL predicate for the caller's data scope (queries A6 + C4).
