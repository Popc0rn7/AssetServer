import json

from assetserver.asset_store import AssetStore
from assetserver.scene_compilers import blender_recipe, compile_drake_directives
from assetserver.scene_ir import SceneIR


def test_compilers_resolve_same_asset_and_transform(tmp_path):
    assets = AssetStore(tmp_path)
    room = assets.ingest(
        {"room.glb": b"v", "room.sdf": b"s"}, visual="room.glb", simulation="room.sdf"
    )
    chair = assets.ingest(
        {"chair.glb": b"v", "chair.sdf": b"s"},
        visual="chair.glb",
        simulation="chair.sdf",
    )
    scene = SceneIR.model_validate(
        {
            "rooms": [{"id": "main", "shell": {"asset_ref": room.asset_ref}}],
            "objects": [
                {
                    "id": "chair",
                    "room_id": "main",
                    "name": "Chair",
                    "category": "furniture",
                    "asset_ref": chair.asset_ref,
                    "transform": {
                        "translation": [1, 2, 3],
                        "rotation_rpy_degrees": [0, 0, 90],
                    },
                }
            ],
        }
    )
    drake = compile_drake_directives(scene, assets).decode()
    recipe = json.loads(blender_recipe(scene, assets))
    assert "name: chair" in drake
    assert "rotation: !Rpy" in drake
    weld = drake.split("- add_weld:", 1)[1]
    assert "base_frame:" not in weld
    chair_recipe = next(item for item in recipe["instances"] if item["name"] == "chair")
    assert chair_recipe["translation"] == [1.0, 2.0, 3.0]
    assert round(chair_recipe["rotation_radians"][2], 6) == 1.570796


def test_blender_recipe_exposes_articulated_visual_parts_for_drake_fk(tmp_path):
    assets = AssetStore(tmp_path)
    room = assets.ingest(
        {"room.glb": b"v", "room.sdf": b"s"},
        visual="room.glb",
        simulation="room.sdf",
    )
    cabinet = assets.ingest(
        {
            "visual/default.glb": b"v",
            "body.gltf": b"body",
            "door.gltf": b"door",
            "model.sdf": b"sdf",
        },
        visual={
            "entrypoint": "visual/default.glb",
            "parts": [
                {"link": "body", "entrypoint": "body.gltf"},
                {"link": "door", "entrypoint": "door.gltf"},
            ],
        },
        simulation={"entrypoint": "model.sdf", "base_link": "body"},
        joints=[
            {
                "name": "door_joint",
                "type": "revolute",
                "parent_link": "body",
                "child_link": "door",
                "limits": {"lower": -1.5, "upper": 0.0},
            }
        ],
    )
    scene = SceneIR.model_validate(
        {
            "rooms": [{"id": "main", "shell": {"asset_ref": room.asset_ref}}],
            "objects": [
                {
                    "id": "cabinet",
                    "room_id": "main",
                    "name": "Cabinet",
                    "category": "furniture",
                    "asset_ref": cabinet.asset_ref,
                    "initial_joints": {"door_joint": -1.0},
                }
            ],
        }
    )

    instance = next(
        item for item in json.loads(blender_recipe(scene, assets))["instances"]
        if item["name"] == "cabinet"
    )

    assert instance["base_link"] == "body"
    assert instance["initial_joints"] == {"door_joint": -1.0}
    assert [part["link"] for part in instance["visual_parts"]] == ["body", "door"]
    assert instance["simulation"].endswith("/model.sdf")
