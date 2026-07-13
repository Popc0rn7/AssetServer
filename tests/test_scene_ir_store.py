import yaml
import pytest

from assetserver.asset_store import AssetStore
from assetserver.scene_ir_store import IRSceneConflictError, IRSceneStore


def test_ir_scene_store_revisions_and_conflicts(tmp_path):
    assets = AssetStore(tmp_path / "assets")
    asset = assets.ingest({"room.glb": b"room"}, visual="room.glb")
    document = {
        "schema_version": "scene-ir/v1",
        "rooms": [{"id": "main", "shell": {"asset_ref": asset.asset_ref}}],
    }
    store = IRSceneStore(tmp_path / "scenes", assets)
    created = store.create(yaml.safe_dump(document).encode())
    document["description"] = "changed"
    updated = store.update(created.scene_id, yaml.safe_dump(document).encode(), base_revision=1)
    assert updated.revision == 2
    assert b"changed" in store.read(created.scene_id)
    with pytest.raises(IRSceneConflictError):
        store.update(created.scene_id, yaml.safe_dump(document).encode(), base_revision=1)
