import pytest

from app.core.errors import NotFound
from app.core.tenant import parse_host

BASE = "myhrm.com"


@pytest.mark.parametrize(
    ("host", "slug", "domain"),
    [
        ("acme.myhrm.com", "acme", None),
        ("ACME.myhrm.com:443", "acme", None),
        ("hr.acme-corp.com", None, "hr.acme-corp.com"),
    ],
)
def test_tenant_hosts(host: str, slug: str | None, domain: str | None) -> None:
    target = parse_host(host, BASE)
    assert (target.slug, target.custom_domain) == (slug, domain)


@pytest.mark.parametrize("host", ["myhrm.com", "www.myhrm.com", "myhrm.com:8000", ""])
def test_root_hosts(host: str) -> None:
    assert parse_host(host, BASE).is_root


def test_nested_subdomain_is_not_a_tenant() -> None:
    with pytest.raises(NotFound):
        parse_host("a.b.myhrm.com", BASE)
