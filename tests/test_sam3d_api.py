import hashlib

from pathlib import Path

import httpx
import pytest

from assetserver.sam3d_server.app import create_app


def _fake_generator(image_path: Path, output_path: Path, **_):
    assert image_path.read_bytes().startswith(b"\x89PNG")
    output_path.write_bytes(b"glTF-test")


@pytest.mark.asyncio
async def test_generate_exposes_backend_specific_asset_url(tmp_path):
    app = create_app(
        asset_root=tmp_path / "assets",
        generator=_fake_generator,
        ready_check=lambda: (True, None),
        backend_version="test-image",
        model_bundle_version="test-models",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/sam3d/generations",
            files={"image": ("object.png", b"\x89PNG\r\n\x1a\nimage", "image/png")},
            data={"mode": "foreground", "threshold": "0.5"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["backend"] == "sam3d"
        assert body["backend_version"] == "test-image"
        assert body["model_bundle_version"] == "test-models"
        assert body["asset"]["download_url"].startswith("/v1/sam3d/assets/")

        asset = await client.get(body["asset"]["download_url"])
        assert asset.status_code == 200
        assert asset.content == b"glTF-test"
        assert (
            asset.headers["etag"].strip('"') == hashlib.sha256(b"glTF-test").hexdigest()
        )


@pytest.mark.asyncio
async def test_object_description_requires_prompt(tmp_path):
    app = create_app(
        asset_root=tmp_path / "assets",
        generator=_fake_generator,
        ready_check=lambda: (True, None),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/sam3d/generations",
            files={"image": ("object.png", b"\x89PNG\r\n\x1a\nimage", "image/png")},
            data={"mode": "object_description"},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_readiness_reports_dependency_failure(tmp_path):
    app = create_app(
        asset_root=tmp_path / "assets",
        generator=_fake_generator,
        ready_check=lambda: (False, "model bundle missing"),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/health/live")).status_code == 200
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["detail"] == "model bundle missing"


@pytest.mark.asyncio
async def test_generation_is_rejected_until_pipeline_is_ready(tmp_path):
    app = create_app(
        asset_root=tmp_path / "assets",
        generator=_fake_generator,
        ready_check=lambda: (False, "pipeline is loading"),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/sam3d/generations",
            files={"image": ("object.png", b"\x89PNG\r\n\x1a\nimage", "image/png")},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "pipeline is loading"
