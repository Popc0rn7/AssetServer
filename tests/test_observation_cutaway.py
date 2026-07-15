import json

import pytest

from assetserver.asset_store import AssetStore
from assetserver.blender_scene_worker import observation_hidden_shell_roles
from assetserver.scene_compilers import blender_recipe
from assetserver.scene_ir import SceneIR


def test_each_standard_observation_uses_interior_cutaway():
    assert observation_hidden_shell_roles("top") == {"ceiling"}
    assert observation_hidden_shell_roles("front") == {"wall_south"}
    assert observation_hidden_shell_roles("side") == {"wall_east"}
    assert observation_hidden_shell_roles("perspective") == {
        "ceiling",
        "wall_south",
        "wall_east",
    }


def test_floor_center_world_aabb_is_stable_under_room_translation(tmp_path):
    scene = SceneIR.model_validate(
        {
            "rooms": [
                {
                    "id": "room",
                    "transform": {"translation": [10, 20, 3]},
                    "shell": {
                        "kind": "procedural",
                        "dimensions": [3.2, 2.8, 2.7],
                    },
                }
            ]
        }
    )

    recipe = json.loads(blender_recipe(scene, AssetStore(tmp_path / "assets")))

    assert recipe["observation_bounds"]["min"] == pytest.approx([8.4, 18.6, 3.0])
    assert recipe["observation_bounds"]["max"] == pytest.approx([11.6, 21.4, 5.7])
    center = [
        (low + high) / 2
        for low, high in zip(
            recipe["observation_bounds"]["min"],
            recipe["observation_bounds"]["max"],
        )
    ]
    assert center == pytest.approx([10.0, 20.0, 4.35])
