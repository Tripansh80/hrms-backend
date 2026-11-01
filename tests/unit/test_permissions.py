from app.core.permissions import DataScope, widest_scope


def test_widest_scope_wins() -> None:
    assert widest_scope(["self", "team"]) is DataScope.TEAM
    assert widest_scope(["department", "all", "self"]) is DataScope.ALL
    assert widest_scope([]) is None
