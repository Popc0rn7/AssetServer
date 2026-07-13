import json

import pytest

from assetserver.asset_store import (
    AssetStoreError,
    ContentAddressedAssetStore,
    canonical_source_frame,
)
from assetserver.postprocess.collision import publish_collision_asset


GLB = b"glTF" + b"\0" * 16
SDF = b"<sdf version='1.10'><model name='box'><link name='base'/></model></sdf>"


def _publish(store, **overrides):
    values = {
        "files": {"visual/model.glb": GLB, "simulation/model.sdf": SDF},
        "visual": "visual/model.glb",
        "simulation": {"entrypoint": "simulation/model.sdf", "base_link": "base"},
        "bounds": {"min": [-0.5, -0.5, 0], "max": [0.5, 0.5, 1]},
        "source": {"type": "fixture", "resource_id": "box"},
        "source_frame": canonical_source_frame(),
    }
    values.update(overrides)
    return store.ingest(**values)


def test_v2_ingest_requires_explicit_frame_and_valid_base_link(tmp_path):
    store = ContentAddressedAssetStore(tmp_path)
    with pytest.raises(AssetStoreError, match="frame declaration"):
        store.ingest({"model.glb": GLB}, visual="model.glb")
    with pytest.raises(AssetStoreError, match="base link does not exist"):
        _publish(
            store,
            simulation={"entrypoint": "simulation/model.sdf", "base_link": "missing"},
        )


def test_v2_digest_is_deterministic_and_manifest_tampering_is_detected(tmp_path):
    store = ContentAddressedAssetStore(tmp_path)
    first = _publish(store)
    second = _publish(store)
    assert first.asset_ref == second.asset_ref
    manifest_path = first.root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["bounds"]["max"][2] = 2
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(AssetStoreError, match="identity verification"):
        store.resolve(first.asset_ref)


def test_collision_derivation_keeps_parent_immutable(tmp_path):
    store = ContentAddressedAssetStore(tmp_path)
    parent = _publish(store)
    before = (parent.root / "manifest.json").read_bytes()
    child = publish_collision_asset(
        store, parent.asset_ref, {"piece.obj": b"o collision\n"}
    )
    assert child.asset_ref != parent.asset_ref
    assert child.manifest["parent"]["asset_ref"] == parent.asset_ref
    assert (parent.root / "manifest.json").read_bytes() == before
