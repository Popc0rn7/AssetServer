import math

import pytest

from assetserver.scene_ir import SceneIRValidationError, dump_scene_yaml, load_scene_yaml


REF = "asset://sha256/" + "a" * 64


def document(**changes):
    value = {
        "schema_version": "scene-ir/v1",
        "description": "room",
        "rooms": [{"id": "main", "shell": {"asset_ref": REF}}],
        "objects": [{
            "id": "chair", "room_id": "main", "name": "Chair",
            "category": "furniture", "asset_ref": REF,
        }],
    }
    value.update(changes)
    return value


def test_scene_ir_round_trips_as_normalized_yaml():
    import yaml
    scene = load_scene_yaml(yaml.safe_dump(document()))
    restored = load_scene_yaml(dump_scene_yaml(scene))
    assert restored == scene
    assert restored.asset_refs() == {REF}


@pytest.mark.parametrize("value", ["file:///tmp/a.glb", "../a.glb", "asset://hssd/a"])
def test_scene_ir_rejects_non_content_addressed_asset_refs(value):
    import yaml
    data = document()
    data["objects"][0]["asset_ref"] = value
    with pytest.raises(SceneIRValidationError):
        load_scene_yaml(yaml.safe_dump(data))


def test_scene_ir_rejects_unknown_room_and_non_finite_transform():
    import yaml
    data = document()
    data["objects"][0]["room_id"] = "missing"
    with pytest.raises(SceneIRValidationError, match="unknown room"):
        load_scene_yaml(yaml.safe_dump(data))
    data = document()
    data["objects"][0]["transform"] = {"translation": [0, 0, math.nan]}
    with pytest.raises(SceneIRValidationError, match="finite"):
        load_scene_yaml(yaml.safe_dump(data))
