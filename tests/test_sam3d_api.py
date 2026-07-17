import asyncio
import json

from pathlib import Path

import httpx
import pytest
import trimesh

from assetserver.generation_server.app import create_app
from assetserver.generation_server.runtime import GenerationRuntime


class FakePipeline:
    name = "sam3d"

    def __init__(self):
        self.loads = 0
        self.requests = []
        self.cleanups = 0

    def load(self):
        self.loads += 1

    def generate(self, request, output_path: Path):
        self.requests.append(request)
        trimesh.creation.box().export(output_path)

    def cleanup_request(self):
        self.cleanups += 1

    def tool_versions(self):
        return {"backend": "test"}


def _app(tmp_path, pipeline=None):
    pipeline = pipeline or FakePipeline()
    runtime = GenerationRuntime(pipeline, preload=False)
    return create_app(
        runtime=runtime,
        asset_root=tmp_path / "assets",
        staging_root=tmp_path / "staging",
    ), pipeline, runtime


@pytest.mark.asyncio
async def test_generation_publishes_path_free_asset_ref(tmp_path, monkeypatch):
    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)
    app, pipeline, _ = _app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v2/generations",
            files={"image": ("object.png", b"\x89PNG\r\n\x1a\nimage", "image/png")},
            data={"prompt": "chair", "options": json.dumps({"threshold": 0.4})},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["backend"] == "sam3d"
    assert body["asset_ref"].startswith("asset://sha256/")
    assert not any("path" in key for key in body)
    assert pipeline.loads == 1
    assert pipeline.cleanups == 1
    assert pipeline.requests[0].options == {"threshold": 0.4}


@pytest.mark.asyncio
@pytest.mark.parametrize("options", ["not-json", "[]", "1", '"text"'])
async def test_options_must_be_a_json_object(tmp_path, options):
    app, _, _ = _app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v2/generations",
            files={"image": ("object.png", b"image", "image/png")},
            data={"options": options},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_live_remains_available_when_readiness_failed(tmp_path, monkeypatch):
    app, _, runtime = _app(tmp_path)
    monkeypatch.setattr(runtime, "readiness", lambda: (False, "model bundle missing"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/health/live")).status_code == 200
        ready = await client.get("/health/ready")
        generate = await client.post(
            "/v2/generations",
            files={"image": ("object.png", b"image", "image/png")},
        )
    assert ready.status_code == 503
    assert generate.status_code == 503
