import httpx


async def test_health(client: httpx.AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert r.headers["x-request-id"]


async def test_unknown_route_uses_error_shape(client: httpx.AsyncClient) -> None:
    r = await client.get("/does-not-exist")
    assert r.status_code == 404
