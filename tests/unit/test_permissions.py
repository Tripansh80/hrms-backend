from app.core.permissions import DataScope, scope_filter, widest_scope


def test_widest_scope_wins() -> None:
    assert widest_scope(["self", "team"]) is DataScope.TEAM
    assert widest_scope(["department", "all", "self"]) is DataScope.ALL
    assert widest_scope([]) is None


def test_managed_scope_is_wider_than_self() -> None:
    assert widest_scope(["self", "managed"]) is DataScope.MANAGED
    assert "managed_employee_ids" in scope_filter(DataScope.MANAGED)
