from types import SimpleNamespace

import httpx
import pytest
from omegaconf import OmegaConf

from assetserver.asset_acquisition_server.server_app import AssetAcquisitionApp


class FakeRetrievalEngine:
    def __init__(self, asset_path):
        self.asset_path = asset_path

    async def retrieve(self, source, request):
        return {
            "source": source,
            "query": request["description"],
            "results": [
                {
                    "asset_id": "asset-1",
                    "score": 0.9,
                    "metadata": {"category": "Wood"},
                    "download_url": f"/v1/assets/{source}/asset-1",
                }
            ],
        }

    def package(self, source, asset_id):
        if source != "materials" or asset_id != "asset-1":
            raise KeyError(asset_id)
        return SimpleNamespace(
            asset_id=asset_id,
            path=self.asset_path,
            sha256="abc123",
            size_bytes=self.asset_path.stat().st_size,
        )


@pytest.fixture
def retrieve_app(tmp_path):
    archive = tmp_path / "asset.zip"
    archive.write_bytes(b"PK\x03\x04archive")
    config = OmegaConf.create({
        "gateway": {"request_timeout_s": 30, "rate_limit": {"enabled": False}},
        "docker": {"launch_backend": False},
        "runtime": {},
        "tool_dirs": [],
        "backends": {},
    })
    return AssetAcquisitionApp(
        config=config, retrieval_engine=FakeRetrievalEngine(archive)
    ).app


@pytest.mark.asyncio
async def test_gateway_retrieve_returns_candidate_metadata(retrieve_app):
    transport = httpx.ASGITransport(app=retrieve_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/retrieve/materials",
            json={"description": "warm wood", "num_candidates": 1, "download": False},
        )

    assert response.status_code == 200
    assert response.json()["results"][0]["download_url"] == "/v1/assets/materials/asset-1"


@pytest.mark.asyncio
async def test_gateway_retrieve_can_return_single_asset_zip(retrieve_app):
    transport = httpx.ASGITransport(app=retrieve_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/retrieve/materials",
            json={"description": "warm wood", "num_candidates": 1, "download": True},
        )

    assert response.status_code == 200
    assert response.content == b"PK\x03\x04archive"
    assert response.headers["x-asset-id"] == "asset-1"


@pytest.mark.asyncio
async def test_gateway_retrieve_rejects_direct_download_of_multiple_candidates(retrieve_app):
    transport = httpx.ASGITransport(app=retrieve_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/retrieve/materials",
            json={"description": "wood", "num_candidates": 2, "download": True},
        )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_gateway_retrieve_rejects_non_boolean_download_flag(retrieve_app):
    transport = httpx.ASGITransport(app=retrieve_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/retrieve/materials",
            json={"description": "wood", "download": "false"},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_gateway_downloads_existing_retrieval_asset(retrieve_app):
    transport = httpx.ASGITransport(app=retrieve_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/assets/materials/asset-1")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"


@pytest.mark.asyncio
async def test_legacy_retrieve_route_is_not_public(retrieve_app):
    transport = httpx.ASGITransport(app=retrieve_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/retrieve/materials", json=[])

    assert response.status_code == 404


def test_gateway_tools_advertises_only_v1_retrieve_routes(tmp_path):
    archive = tmp_path / "asset.zip"
    archive.write_bytes(b"zip")
    server = AssetAcquisitionApp(
        config=OmegaConf.create(
            {"gateway": {}, "docker": {"launch_backend": False}, "backends": {}}
        ),
        retrieval_engine=FakeRetrievalEngine(archive),
    )

    routes = server._tools_endpoint()["routes"]
    assert routes["retrieve"] == "/v1/retrieve/{source}"
    assert routes["assets"] == "/v1/assets/{source}/{asset_id}"
