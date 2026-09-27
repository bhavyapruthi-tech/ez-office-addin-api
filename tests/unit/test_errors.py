import pytest
from httpx import ASGITransport, AsyncClient

from app.core.errors import (
    InsufficientBalanceError,
    NotFoundError,
    UnauthorizedError,
    WorkspaceUnprovisionedError,
)
from app.main import create_app


def _test_app():
    app = create_app()

    @app.get("/_test/raise/{name}")
    async def _raise(name: str):
        errors = {
            "unauthorized": UnauthorizedError,
            "insufficient_balance": InsufficientBalanceError,
            "unprovisioned": WorkspaceUnprovisionedError,
            "not_found": NotFoundError,
        }
        raise errors[name]()

    @app.get("/_test/raise-generic")
    async def _raise_generic():
        raise RuntimeError("some internal detail that must never leak")

    return app


@pytest.mark.anyio
async def test_app_boots_and_openapi_is_reachable():
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/openapi.json")
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("name", "status", "error_code"),
    [
        ("unauthorized", 401, "unauthorized"),
        ("insufficient_balance", 402, "insufficient_balance"),
        ("unprovisioned", 503, "workspace_account_unprovisioned"),
        ("not_found", 404, "not_found"),
    ],
)
@pytest.mark.anyio
async def test_typed_exceptions_return_shared_envelope(name, status, error_code):
    app = _test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/_test/raise/{name}")
    assert response.status_code == status
    body = response.json()
    assert body["error_code"] == error_code
    assert isinstance(body["message"], str) and body["message"]


@pytest.mark.anyio
async def test_request_validation_error_returns_shared_envelope():
    app = _test_app()

    @app.post("/_test/validate")
    async def _validate(payload: dict[str, str]):  # noqa: ARG001
        return {"ok": True}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/_test/validate", content=b"not json")
    assert response.status_code == 422
    body = response.json()
    assert body == {
        "error_code": "validation_error",
        "message": "The request could not be validated.",
    }


@pytest.mark.anyio
async def test_unrecognized_exception_returns_well_formed_envelope_no_leak():
    # Starlette's ServerErrorMiddleware sends the handler's response to the
    # client, then re-raises the original exception for server-side
    # visibility (e.g. uvicorn logging) -- expected, not a bug. httpx's
    # ASGITransport must be told not to propagate that re-raise into the
    # test process itself.
    app = _test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get("/_test/raise-generic")
    assert response.status_code == 500
    body = response.json()
    assert body == {"error_code": "internal_error", "message": "Something went wrong."}
    assert "internal detail" not in response.text


@pytest.mark.anyio
async def test_cors_allows_configured_origin_and_blocks_others():
    app = _test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        allowed = await client.get(
            "/openapi.json", headers={"Origin": "https://localhost:3000"}
        )
        blocked = await client.get(
            "/openapi.json", headers={"Origin": "https://evil.example"}
        )
    assert (
        allowed.headers.get("access-control-allow-origin") == "https://localhost:3000"
    )
    assert "access-control-allow-origin" not in blocked.headers
