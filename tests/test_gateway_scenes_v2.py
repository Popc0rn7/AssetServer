import yaml
from httpx import ASGITransport, AsyncClient

from assetserver.asset_acquisition_server.server_app import AssetAcquisitionApp
from assetserver.asset_store import AssetStore
from assetserver.scene_ir_store import IRSceneStore


async def test_v2_scene_yaml_create_get_update_and_conflict(tmp_path):
    assets = AssetStore(tmp_path / "assets")
    asset = assets.ingest({"room.glb": b"v"}, visual="room.glb")
    data = {"schema_version": "scene-ir/v1", "rooms": [{"id": "main", "shell": {"asset_ref": asset.asset_ref}}]}
    app = AssetAcquisitionApp(ir_scene_store=IRSceneStore(tmp_path / "scenes", assets)).app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/v2/scenes", content=yaml.safe_dump(data), headers={"content-type": "application/yaml"})
        assert created.status_code == 201
        scene_id = created.json()["scene_id"]
        fetched = await client.get(f"/v2/scenes/{scene_id}")
        assert fetched.status_code == 200
        assert fetched.headers["x-scene-revision"] == "1"
        data["description"] = "changed"
        updated = await client.put(f"/v2/scenes/{scene_id}", content=yaml.safe_dump(data), headers={"content-type": "application/yaml", "x-base-revision": "1"})
        assert updated.status_code == 201
        conflict = await client.put(f"/v2/scenes/{scene_id}", content=yaml.safe_dump(data), headers={"content-type": "application/yaml", "x-base-revision": "1"})
        assert conflict.status_code == 409


async def test_v2_rejects_missing_assets_and_wrong_media_type(tmp_path):
    app = AssetAcquisitionApp(ir_scene_store=IRSceneStore(tmp_path / "scenes", AssetStore(tmp_path / "assets"))).app
    data = {"rooms": [{"id": "main", "shell": {"asset_ref": "asset://sha256/" + "a" * 64}}]}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        wrong = await client.post("/v2/scenes", content=yaml.safe_dump(data), headers={"content-type": "text/plain"})
        assert wrong.status_code == 415
        missing = await client.post("/v2/scenes", content=yaml.safe_dump(data), headers={"content-type": "application/yaml"})
        assert missing.status_code == 422
