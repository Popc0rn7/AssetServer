import httpx
import pytest
from omegaconf import OmegaConf

from assetserver.asset_acquisition_server.server_app import AssetAcquisitionApp
from assetserver.config import load_assetserver_config


class FakeRetrieval:
    def __init__(self) -> None:
        self.sources = {"materials": object(), "articulated": object()}


class MutableStatusProvider:
    def __init__(self) -> None:
        self.states = {
            "materials": {
                "status": "ready",
                "available": True,
                "healthy": True,
                "queue_depth": 0,
                "capacity": 4,
                "estimated_wait_seconds": 0,
            },
            "articulated": {
                "status": "ready",
                "available": True,
                "healthy": True,
                "queue_depth": 0,
                "capacity": 4,
                "estimated_wait_seconds": 0,
            },
        }

    def __call__(self, backend):
        return dict(self.states[backend.name])


@pytest.fixture
def retrieve_only_gateway(tmp_path):
    provider = MutableStatusProvider()
    config = load_assetserver_config()
    config.server.storage.data_root = str(tmp_path / "data")
    config.server.storage.output_root = str(tmp_path / "outputs")
    gateway = AssetAcquisitionApp(
        config=config,
        retrieval_engine=FakeRetrieval(),
        backend_status_provider=provider,
    )
    return gateway, provider


def test_tools_publish_complete_path_free_agent_profiles(retrieve_only_gateway):
    gateway, _ = retrieve_only_gateway
    payload = gateway._tools_endpoint()
    enabled = {(item["role"], item["name"]): item for item in payload["enabled"]}
    assert set(enabled) == {("retrieve", "materials"), ("retrieve", "articulated")}
    for item in enabled.values():
        profile = item["config"]
        assert profile["description"]
        assert profile["best_for"]
        assert profile["avoid_for"]
        assert profile["output_kind"] in {"object", "material"}
        assert "server" not in profile
        assert "source_path" not in profile
    assert enabled[("retrieve", "materials")]["config"]["output_kind"] == "material"
    assert enabled[("retrieve", "articulated")]["config"]["output_kind"] == "object"
    serialized = str(payload)
    assert "/home/" not in serialized
    assert "data/materials" not in serialized
    assert "127.0.0.1" not in serialized


@pytest.mark.asyncio
async def test_backends_match_enabled_tools_and_include_dynamic_health(
    retrieve_only_gateway,
):
    gateway, _ = retrieve_only_gateway
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.app), base_url="http://test"
    ) as client:
        backends = await client.get("/backends")

    tool_keys = {
        (item["role"], item["name"]) for item in gateway._tools_endpoint()["enabled"]
    }
    statuses = backends.json()["enabled"]
    assert {(item["role"], item["name"]) for item in statuses} == tool_keys
    for status in statuses:
        assert status["status"] == "ready"
        assert status["available"] is True
        assert status["healthy"] is True
        assert status["queue_depth"] == 0
        assert status["updated_at"] > 0

    disabled = {item["name"]: item for item in backends.json()["all"]}
    assert disabled["sam3d"]["status"] == "disabled"
    assert disabled["sam3d"]["available"] is False
    assert disabled["sam3d"]["healthy"] is False


@pytest.mark.asyncio
async def test_backend_state_refreshes_without_gateway_restart(retrieve_only_gateway):
    gateway, provider = retrieve_only_gateway
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.app), base_url="http://test"
    ) as client:
        first = (await client.get("/backends")).json()
        provider.states["materials"] = {
            "status": "maintenance",
            "available": False,
            "healthy": False,
            "maintenance": True,
            "last_error": "scheduled maintenance",
        }
        second = (await client.get("/backends")).json()

    before = next(item for item in first["enabled"] if item["name"] == "materials")
    after = next(item for item in second["enabled"] if item["name"] == "materials")
    assert before["available"] is True
    assert after["status"] == "maintenance"
    assert after["available"] is False
    assert after["maintenance"] is True
    assert after["updated_at"] >= before["updated_at"]


@pytest.mark.asyncio
async def test_unavailable_enabled_route_returns_structured_503(tmp_path):
    config = OmegaConf.create(
        {
            "server": {"storage": {"data_root": str(tmp_path / "data")}},
            "runtime": {},
            "openclip": {},
            "backends": {
                "sam3d": {
                    "name": "sam3d",
                    "type": "geometry_generation",
                    "role": "generate",
                    "enabled": True,
                    "server": {"host": "127.0.0.1", "port": 7000},
                    "profile": {
                        "description": "Generate an object from a reference image.",
                        "best_for": ["single objects"],
                        "avoid_for": ["articulated objects"],
                        "output_kind": "object",
                    },
                }
            },
        }
    )

    def offline(_backend):
        return {
            "status": "offline",
            "available": False,
            "healthy": False,
            "last_error": "backend process is not running",
        }

    gateway = AssetAcquisitionApp(
        config=config,
        retrieval_engine=FakeRetrieval(),
        backend_status_provider=offline,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.app), base_url="http://test"
    ) as client:
        response = await client.post("/v2/generate/sam3d", content=b"unused")

    assert response.status_code == 503
    assert response.json() == {
        "error": "backend_unavailable",
        "message": "backend process is not running",
        "retryable": True,
    }


def test_every_available_capability_has_a_public_v2_route(retrieve_only_gateway):
    gateway, _ = retrieve_only_gateway
    paths = gateway.app.openapi()["paths"]
    assert "/v2/retrieve/{source}" in paths
    assert "/v2/generate/{backend}" in paths
