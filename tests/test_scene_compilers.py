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
