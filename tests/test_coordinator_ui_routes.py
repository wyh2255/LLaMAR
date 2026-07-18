"""Regression tests for optional, application-owned coordinator UI assets."""

from pathlib import Path

import httpx
import pytest

from a2a.coordinator.server import create_server


async def _get(server, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=server._app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "expected_marker"),
    [
        ("/ui", "Task Console"),
        ("/ui/map", "SAR Map"),
        ("/ui/debug", "Debug Viewer"),
        ("/dashboard", "SAR Dashboard"),
    ],
)
async def test_sar_ui_routes_serve_injected_assets(path: str, expected_marker: str):
    ui_dir = Path(__file__).parents[1] / "sar_orch" / "ui"
    server = create_server(ui_dir=str(ui_dir), verifier_enabled=False)

    response = await _get(server, path)

    assert response.status_code == 200
    assert expected_marker in response.text


@pytest.mark.asyncio
async def test_ui_routes_are_hidden_without_an_injected_directory():
    server = create_server(verifier_enabled=False)

    response = await _get(server, "/ui")

    assert response.status_code == 404
