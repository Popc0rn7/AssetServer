import asyncio
import hashlib
import json
import xml.etree.ElementTree as ET

import pytest
import trimesh

from httpx import ASGITransport, AsyncClient

from assetserver.asset_acquisition_server.server_app import AssetAcquisitionApp
from assetserver.asset_store import ContentAddressedAssetStore, canonical_source_frame
from assetserver.postprocess.collision import (
    COLLISION_PIPELINE_VERSION,
    CollisionPostprocessor,
)
from assetserver.postprocess.config import (
    PostprocessConfig,
    artifact_key,
    canonical_profile_json,
    derivation_key,
    normalized_profile,
)
from assetserver.postprocess_server.server_app import PostprocessServerApp
from assetserver.scene_compilers import blender_recipe
from assetserver.scene_ir import Room, RoomShell, SceneIR
from assetserver.simulation_assets import (
    SimulationAssetError,
    simulation_asset_payload,
)


SDF = b"""<sdf version="1.10"><model name="box"><link name="base">
<inertial><mass>1</mass></inertial>
<visual name="visual"><geometry><mesh><uri>../visual/model.glb</uri></mesh></geometry></visual>
<collision name="triangle"><geometry><mesh><uri>../visual/model.glb</uri></mesh></geometry></collision>
</link></model></sdf>"""


class FakeClient:
    def __init__(self, staging_root):
        self.staging_root = staging_root
        self.calls = 0

    async def decompose(self, *, request_id, **kwargs):
        self.calls += 1
        await asyncio.sleep(0.02)
        directory = self.staging_root / request_id
        directory.mkdir(parents=True, exist_ok=True)
        mesh = trimesh.creation.box(extents=[1, 1, 1])
        content = mesh.export(file_type="obj").encode()
        (directory / "hull_000.obj").write_bytes(content)
        return {
            "success": True,
            "pieces": [
                {
                    "path": "hull_000.obj",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "vertices": 8,
                    "faces": 12,
                }
            ],
            "processing_time_s": 0.02,
        }


class MaterializesOne:
    def __init__(self, stored):
        self.stored = stored

    def materialize(self, source, candidate_id, store):
        return self.stored


def _asset(store):
    mesh = trimesh.creation.box(extents=[1, 1, 1])
    return store.ingest(
        {
            "visual/model.glb": mesh.export(file_type="glb"),
            "simulation/model.sdf": SDF,
        },
        visual="visual/model.glb",
        simulation={"entrypoint": "simulation/model.sdf", "base_link": "base"},
        collision={"entrypoint": "visual/model.glb", "method": "triangle-mesh"},
        bounds={"min": [-0.5, -0.5, -0.5], "max": [0.5, 0.5, 0.5]},
        source={"type": "fixture", "resource_id": "box"},
        source_frame=canonical_source_frame(),
    )


def _config(tmp_path):
    return PostprocessConfig.from_mapping(
        {
            "enabled": True,
            "database": tmp_path / "postprocess.sqlite3",
            "staging_root": tmp_path / "staging",
        }
    )


def test_profile_and_derivation_keys_are_stable_and_scoped():
    left = normalized_profile({"threshold": 0.1, "seed": 4})
    right = normalized_profile({"seed": 4, "threshold": 0.1})
    assert canonical_profile_json(left) == canonical_profile_json(right)
    first = artifact_key("a" * 64, {"up_axis": "+Z"}, left, "1.0.7")
    assert first == artifact_key("a" * 64, {"up_axis": "+Z"}, right, "1.0.7")
    assert first != artifact_key("a" * 64, {"up_axis": "+Y"}, right, "1.0.7")
    assert derivation_key("b" * 64, first, "v1") != derivation_key(
        "c" * 64, first, "v1"
    )


async def test_singleflight_rewrites_sdf_and_blender_remains_visual_only(tmp_path):
    store = ContentAddressedAssetStore(tmp_path / "assets")
    parent = _asset(store)
    before = (parent.root / "manifest.json").read_bytes()
    config = _config(tmp_path)
    client = FakeClient(config.staging_root)
    service = CollisionPostprocessor(
        store, config, client=client, coacd_version="1.0.7"
    )

    children = await asyncio.gather(
        *(service.ensure_collision_ready(parent.asset_ref) for _ in range(10))
    )
    assert client.calls == 1
    assert len({child.asset_ref for child in children}) == 1
    child = children[0]
    assert child.asset_ref != parent.asset_ref
    assert (parent.root / "manifest.json").read_bytes() == before
    assert child.manifest["parent"]["asset_ref"] == parent.asset_ref
    assert child.manifest["tool_versions"]["collision_pipeline"] == COLLISION_PIPELINE_VERSION
    assert child.manifest["collision"][0]["method"] == "coacd"

    sdf_path = store.entrypoint(child.asset_ref, "simulation")
    root = ET.fromstring(sdf_path.read_bytes())
    assert root.find(".//visual") is not None
    assert root.find(".//inertial") is not None
    collisions = list(root.iter("collision"))
    assert [item.get("name") for item in collisions] == ["assetserver_collision_000"]
    assert collisions[0].find(".//{drake.mit.edu}declare_convex") is not None
    assert collisions[0].findtext(".//uri") == "../collision/hull_000.obj"

    portable = simulation_asset_payload(child)
    exported_collision = portable["collision_geometries"][0]
    assert exported_collision["representation"] == "convex-mesh"
    assert exported_collision["entrypoint"] == "collision/hull_000.obj"
    assert exported_collision["path"].endswith("/files/collision/hull_000.obj")
    assert exported_collision["sha256"] == next(
        item["sha256"]
        for item in child.manifest["files"]
        if item["path"] == "collision/hull_000.obj"
    )

    scene = SceneIR(rooms=[Room(id="room", shell=RoomShell(asset_ref=child.asset_ref))])
    recipe = json.loads(blender_recipe(scene, store))
    assert "collision" not in json.dumps(recipe)
    assert recipe["instances"][0]["visual"].endswith("files/visual/model.glb")


def test_final_simulation_payload_rejects_visual_triangle_collision(tmp_path):
    store = ContentAddressedAssetStore(tmp_path / "assets")
    parent = _asset(store)
    with pytest.raises(SimulationAssetError, match="visual mesh"):
        simulation_asset_payload(parent)


async def test_worker_rejects_absolute_entrypoints(tmp_path):
    app = PostprocessServerApp(tmp_path / "assets", tmp_path / "staging")
    async with AsyncClient(
        transport=ASGITransport(app=app.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/decompositions",
            json={
                "request_id": "a" * 64,
                "source": {"asset_digest": "b" * 64, "entrypoint": "/etc/passwd"},
                "profile": {
                    "name": "rigid-object-v1",
                    "method": "coacd",
                    "parameters": {},
                },
            },
        )
    assert response.status_code == 422


async def test_gateway_materialize_returns_only_the_derived_reference(tmp_path):
    store = ContentAddressedAssetStore(tmp_path / "assets")
    parent = _asset(store)
    config = _config(tmp_path)
    service = CollisionPostprocessor(
        store,
        config,
        client=FakeClient(config.staging_root),
        coacd_version="1.0.7",
    )
    gateway = AssetAcquisitionApp(
        retrieval_engine=MaterializesOne(parent),
        asset_store=store,
        collision_postprocessor=service,
    )
    async with AsyncClient(
        transport=ASGITransport(app=gateway.app), base_url="http://test"
    ) as client:
        response = await client.post("/v2/retrieve/fixture/box/materialize")
    assert response.status_code == 201
    assert response.json()["asset_ref"] != parent.asset_ref
    derived = store.resolve(response.json()["asset_ref"])
    assert derived.manifest["parent"]["asset_ref"] == parent.asset_ref
