"""Browser modules must load even with broken Windows MIME registry entries."""

import mimetypes

import pytest
from httpx import ASGITransport, AsyncClient

from apps.admin_console import server


@pytest.mark.asyncio
@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize(
    "filename, expected",
    [
        ("main.js", "text/javascript"),
        ("chunk.mjs", "text/javascript"),
        ("styles.css", "text/css"),
        ("icon.svg", "image/svg+xml"),
    ],
)
async def test_spa_assets_ignore_bad_mime_registry(
    tmp_path, monkeypatch, public, filename, expected
):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<app-root></app-root>", encoding="utf-8")
    root = tmp_path / "apps" / "showcase_ui" / "public" if public else dist
    root.mkdir(parents=True, exist_ok=True)
    (root / filename).write_text("asset", encoding="utf-8")
    monkeypatch.setattr(server, "_get_showcase_dist", lambda: dist)
    monkeypatch.setattr(server, "_workspace_root", tmp_path)
    monkeypatch.setattr(mimetypes, "guess_type", lambda *a, **k: ("text/plain", None))
    async with AsyncClient(
        transport=ASGITransport(app=server.app), base_url="http://localhost"
    ) as ac:
        response = await ac.get("/" + filename)
        assert response.status_code == 200
        assert response.headers["content-type"].split(";")[0] == expected
        assert response.text == "asset"
        page = await ac.get("/workspace")
        assert page.headers["content-type"].startswith("text/html")
