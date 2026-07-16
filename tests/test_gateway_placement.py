import yaml

from httpx import ASGITransport, AsyncClient

from assetserver.asset_acquisition_server.server_app import AssetAcquisitionApp
from assetserver.asset_store import AssetStore
from assetserver.jobs import SQLiteJobStore
from assetserver.placement.engine import propose, validate
from assetserver.scene_ir_store import IRSceneStore


def _fixture(tmp_path):
    assets = AssetStore(tmp_path / "assets")
    assets.ingest(
        {"room.glb": b"v"}, visual="room.glb", bounds={"min": [-2, -2.5, 0], "max": [2, 2.5, 2.5]}
    )
    chair = assets.ingest(
        {"chair.glb": b"v"}, visual="chair.glb", bounds={"min": [-0.25, -0.25, 0], "max": [0.25, 0.25, 1]}
    )
    scenes = IRSceneStore(tmp_path / "scenes", assets)
    scene = scenes.create(
        yaml.safe_dump(
            {
                "rooms": [{"id": "room", "shell": {"kind": "procedural", "dimensions": [4, 5, 2.5]}}],
                "objects": [{"id": "chair", "room_id": "room", "name": "chair", "category": "chair", "asset_ref": chair.asset_ref}],
            }
        ).encode()
    )
    jobs = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    app = AssetAcquisitionApp(ir_scene_store=scenes, job_store=jobs, asset_store=assets)
    app._scene_data_root = tmp_path
    return app, scene, chair


async def test_asset_response_contains_placement_profile(tmp_path):
    app, _, chair = _fixture(tmp_path)
    profile = app._public_asset(chair)["placement_profile"]
    assert profile["schema_version"] == "asset-placement/v1"
    assert profile["asset_ref"] == chair.asset_ref
    assert profile["bounds"]["obb"]["extents"] == [0.5, 0.5, 1.0]


async def test_proposal_is_sha_pinned_and_deduplicated(tmp_path):
    app, scene, _ = _fixture(tmp_path)
    request = {
        "revision": 1,
        "scene_sha256": scene.sha256,
        "intents": [{"schema_version": "placement-intent/v1", "object_id": "chair"}],
    }
    async with AsyncClient(transport=ASGITransport(app=app.app), base_url="http://test") as client:
        first = await client.post(f"/v2/scenes/{scene.scene_id}/placement-proposals", json=request)
        duplicate = await client.post(f"/v2/scenes/{scene.scene_id}/placement-proposals", json=request)
        stale = await client.post(f"/v2/scenes/{scene.scene_id}/placement-proposals", json={**request, "scene_sha256": "0" * 64})
    assert first.status_code == 202
    assert first.json()["job_type"] == "placement_proposal"
    assert duplicate.json()["job_id"] == first.json()["job_id"]
    assert duplicate.json()["deduplicated"] is True
    assert stale.status_code == 409
    assert stale.json()["error"] == "scene_revision_conflict"


async def test_unknown_constraint_and_locked_object_are_rejected(tmp_path):
    app, scene, _ = _fixture(tmp_path)
    base = {"revision": 1, "scene_sha256": scene.sha256}
    async with AsyncClient(transport=ASGITransport(app=app.app), base_url="http://test") as client:
        unsupported = await client.post(
            f"/v2/scenes/{scene.scene_id}/placement-proposals",
            json={**base, "intents": [{"schema_version": "placement-intent/v1", "object_id": "chair", "constraints": [{"id": "x", "type": "teleport"}]}]},
        )
        locked = await client.post(
            f"/v2/scenes/{scene.scene_id}/placement-repairs",
            json={**base, "issue_ids": ["penetration:-:chair:desk"], "locked_object_ids": ["missing"], "allowed_operations": ["translate"]},
        )
    assert unsupported.status_code == 422
    assert unsupported.json()["error"] == "unsupported_placement_constraint"
    assert locked.status_code == 422
    assert locked.json()["error"] == "invalid_locked_object"


def test_proposal_and_room_validation_are_deterministic(tmp_path, monkeypatch):
    app, scene, _ = _fixture(tmp_path)
    monkeypatch.setenv("ASSETSERVER_DATA_ROOT", str(tmp_path))
    request = {
        "scene_sha256": scene.sha256,
        "intents": [
            {
                "schema_version": "placement-intent/v1",
                "object_id": "chair",
                "constraints": [
                    {"id": "north", "type": "against_wall", "required": True, "wall": "north"},
                    {"id": "center", "type": "facing", "required": True, "target": {"type": "room_center", "room_id": "room"}, "angular_tolerance_degrees": 1},
                ],
            }
        ],
        "options": {"max_candidates": 5, "seed": 42},
    }
    first, _ = app._job_store.submit("placement_proposal", scene.scene_id, 1, request)
    second_request = {**request, "options": {"max_candidates": 5, "seed": 43}}
    second, _ = app._job_store.submit("placement_proposal", scene.scene_id, 1, second_request)
    first_result = propose(first)
    second_result = propose(second)
    assert first_result["candidates"]
    assert first_result["candidates"] == second_result["candidates"]

    validation, _ = app._job_store.submit(
        "validate",
        scene.scene_id,
        1,
        {
            "profile": "room-placement/v1",
            "scene_sha256": scene.sha256,
            "support_contact_tolerance": 0.001,
        },
    )
    result = validate(validation)
    assert result["valid"] is True
    assert result["issues"] == []
