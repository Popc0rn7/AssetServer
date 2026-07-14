import io
import zipfile

import httpx
import pytest
from omegaconf import OmegaConf

from assetserver.asset_acquisition_server.server_app import AssetAcquisitionApp
from assetserver.scenes import SceneStore


SDF = (
    b'<sdf version="1.10"><model name="room"><link name="chair">'
    b'<visual name="visual"><geometry><mesh><uri>meshes/chair.glb</uri></mesh>'
    b"</geometry></visual></link></model></sdf>"
)


def _package() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("scene.sdf", SDF)
        archive.writestr("meshes/chair.glb", b"mesh")
    return output.getvalue()


def _render_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("top.webp", b"image")
    return output.getvalue()


class FakeRenderer:
    def __init__(self):
        self.calls = []

    async def render(self, package, options):
        self.calls.append((package, options))
        return _render_zip()


@pytest.fixture
def scene_client(tmp_path):
    renderer = FakeRenderer()
    config = OmegaConf.create(
        {
            "server": {"scenes": {"legacy_sdf_api_enabled": True}},
            "backend": [],
            "backends": {},
        }
    )
    app = AssetAcquisitionApp(
        config=config,
        retrieval_engine=False,
        scene_store=SceneStore(tmp_path / "scenes"),
        scene_renderer=renderer,
    ).app
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ), renderer


@pytest.mark.asyncio
async def test_scene_upload_read_update_and_final_download(scene_client):
    client, _ = scene_client
    async with client:
        created = await client.post(
            "/v1/scenes",
            files={"package": ("scene.zip", _package(), "application/zip")},
        )
        assert created.status_code == 201
        scene_id = created.json()["scene_id"]

        current = await client.get(f"/v1/scenes/{scene_id}/sdf")
        assert current.content == SDF
        assert current.headers["x-scene-revision"] == "1"

        changed = SDF.replace(b'name="room"', b'name="changed"')
        updated = await client.put(
            f"/v1/scenes/{scene_id}/sdf",
            content=changed,
            headers={"content-type": "application/xml", "x-base-revision": "1"},
        )
        assert updated.status_code == 201
        assert updated.json()["revision"] == 2

        old = await client.get(f"/v1/scenes/{scene_id}/sdf?revision=1")
        assert old.content == SDF

        final = await client.get(f"/v1/scenes/{scene_id}/final?revision=2")
        assert final.status_code == 200
        assert final.headers["x-scene-revision"] == "2"
        with zipfile.ZipFile(io.BytesIO(final.content)) as archive:
            assert archive.read("scene.sdf") == changed
            assert archive.read("meshes/chair.glb") == b"mesh"


@pytest.mark.asyncio
async def test_scene_update_conflict_is_structured(scene_client):
    client, _ = scene_client
    async with client:
        created = await client.post(
            "/v1/scenes", files={"package": ("scene.zip", _package())}
        )
        scene_id = created.json()["scene_id"]
        await client.put(
            f"/v1/scenes/{scene_id}/sdf",
            content=SDF,
            headers={"content-type": "application/xml", "x-base-revision": "1"},
        )
        conflict = await client.put(
            f"/v1/scenes/{scene_id}/sdf",
            content=SDF,
            headers={"content-type": "application/xml", "x-base-revision": "1"},
        )

    assert conflict.status_code == 409
    assert conflict.json()["error"] == "scene_revision_conflict"


@pytest.mark.asyncio
async def test_scene_render_returns_renderer_zip_and_forwards_options(scene_client):
    client, renderer = scene_client
    async with client:
        created = await client.post(
            "/v1/scenes", files={"package": ("scene.zip", _package())}
        )
        scene_id = created.json()["scene_id"]
        response = await client.post(
            f"/v1/scenes/{scene_id}/render",
            json={"views": ["top"], "width": 256, "height": 256, "format": "webp"},
        )

    assert response.status_code == 200
    assert response.content == _render_zip()
    assert response.headers["x-scene-revision"] == "1"
    assert renderer.calls[0][1] == {
        "views": ["top"],
        "width": 256,
        "height": 256,
        "format": "webp",
    }
    with zipfile.ZipFile(io.BytesIO(renderer.calls[0][0])) as archive:
        assert archive.read("scene.sdf") == SDF


def test_tools_advertises_scene_routes_when_enabled(tmp_path):
    server = AssetAcquisitionApp(
        config=OmegaConf.create(
            {
                "server": {"scenes": {"legacy_sdf_api_enabled": True}},
                "backends": {},
            }
        ),
        retrieval_engine=False,
        scene_store=SceneStore(tmp_path),
        scene_renderer=FakeRenderer(),
    )

    assert server._tools_endpoint()["routes"]["scenes"] == "/v1/scenes"


def test_scene_store_is_created_from_runtime_config(tmp_path):
    server = AssetAcquisitionApp(
        config=OmegaConf.create(
            {
                "server": {
                    "storage": {"data_root": str(tmp_path / "configured-data")},
                    "scenes": {"legacy_sdf_api_enabled": True},
                },
                "backends": {},
            }
        ),
        retrieval_engine=False,
    )

    assert "/v1/scenes" in server.app.openapi()["paths"]
    assert (tmp_path / "configured-data" / "scenes").is_dir()


@pytest.mark.asyncio
async def test_scene_render_reports_unconfigured_renderer(tmp_path):
    config = OmegaConf.create(
        {
            "server": {"scenes": {"legacy_sdf_api_enabled": True}},
            "backends": {},
        }
    )
    app = AssetAcquisitionApp(
        config=config,
        retrieval_engine=False,
        scene_store=SceneStore(tmp_path),
    ).app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/scenes", files={"package": ("scene.zip", _package())}
        )
        response = await client.post(
            f"/v1/scenes/{created.json()['scene_id']}/render", json={}
        )

    assert response.status_code == 503
    assert response.json()["error"] == "render_backend_unavailable"


@pytest.mark.asyncio
async def test_scene_render_rejects_invalid_options(scene_client):
    client, _ = scene_client
    async with client:
        created = await client.post(
            "/v1/scenes", files={"package": ("scene.zip", _package())}
        )
        scene_id = created.json()["scene_id"]
        response = await client.post(
            f"/v1/scenes/{scene_id}/render",
            json={"views": [], "width": -1, "format": "bmp"},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_scene_update_requires_xml_content_type(scene_client):
    client, _ = scene_client
    async with client:
        created = await client.post(
            "/v1/scenes", files={"package": ("scene.zip", _package())}
        )
        scene_id = created.json()["scene_id"]
        response = await client.put(
            f"/v1/scenes/{scene_id}/sdf",
            content=SDF,
            headers={"content-type": "text/plain", "x-base-revision": "1"},
        )

    assert response.status_code == 415
    assert response.json()["error"] == "unsupported_scene_media_type"
